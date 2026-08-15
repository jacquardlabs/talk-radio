"""The DJ engine: show rotation, news-first, queue reconciliation."""
from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable, Literal

import audio
import feeds
from config import Config
from db import Database, utcnow_iso

logger = logging.getLogger(__name__)


def retry_cutoff_iso(minutes: int) -> str:
    """Episodes that failed to play more recently than this are left alone."""
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_next(db: Database, retry_after_iso: str | None = None) -> sqlite3.Row | None:
    """Random enabled non-news feed with unplayed episodes, avoiding
    last_feed_id when an alternative exists. Within the show: the oldest
    new episode for in_order feeds; a random unplayed episode for random
    feeds, arc-guarded so a multi-part story never starts mid-arc.

    retry_after_iso holds back episodes that just failed to play, so a dead
    link on an in_order feed doesn't get re-picked on the next top-up and
    spend its whole retry budget in under a minute."""
    candidates = db.rotation_feeds_with_new(retry_after_iso)
    if not candidates:
        return None
    last = db.kv_get("last_feed_id")
    others = [f for f in candidates if str(f["id"]) != last]
    feed = random.choice(others or candidates)
    db.kv_set("last_feed_id", str(feed["id"]))
    if feed["playback_mode"] == "random":
        return _random_pick(db, feed["id"], retry_after_iso)
    return db.oldest_new_for_feed(feed["id"], retry_after_iso)


def _random_pick(db: Database, feed_id: int,
                 retry_after_iso: str | None = None) -> sqlite3.Row | None:
    """Draw a random unplayed episode; if its title carries an arc marker,
    play the arc's oldest unplayed member instead — never Part 3 before an
    unplayed Part 1. The episode list is oldest-first, so the first key
    match IS the arc's oldest unplayed member (the draw itself when nothing
    earlier matches). Also makes started arcs likelier to finish: any arc
    member drawn redirects to the next unplayed part."""
    episodes = db.new_episodes_for_feed(feed_id, retry_after_iso)
    if not episodes:
        return None
    draw = random.choice(episodes)
    key = feeds.arc_key(draw["title"])
    if key is None:
        return draw
    return next(ep for ep in episodes if feeds.arc_key(ep["title"]) == key)


def current_index(queue: list[str], matches: list, cur) -> int:
    """Where the needle sits in THIS queue snapshot.

    Sonos's own playlist position is not trustworthy in the moment right
    after a transport command, and that is exactly the moment we read it:
    the dashboard polls /api/status the instant a skip or play-now returns.
    Mid-transition the speaker reports no track at all, and removing the
    outgoing track renumbers every slot behind it, so the position we get
    back can address a different queue than the one we just read.

    Falling back to index 0 there is the worst available guess. Finished
    tracks stay on the Sonos queue — tick() only strips their `queued`
    status — so the head of the queue is a dead slot, and landing on it
    blanks the deck and leaves the episode that just started playing
    listed under Up Next.

    A URI survives renumbering, so match on that first — but only onto a
    slot still holding a `queued` episode. The two stale reads can also
    arrive the other way round, with the removal not yet propagated: then
    the URI still resolves, onto the outgoing track we just reconciled to
    played. A hit on a dead slot is by definition a stale reference, never
    the needle.

    With no live URI to go on, take the first slot still holding a `queued`
    episode: the needle has passed everything before it and tick()
    reconciles what it passes, so the first unreconciled slot is at worst
    one track behind — the safe direction, since a needle that runs ahead
    makes tick() mark unheard episodes played."""
    if cur is not None and cur.uri:
        hits = [i for i, uri in enumerate(queue)
                if audio.uris_match(uri, cur.uri) and matches[i] is not None]
        if hits:  # an episode can legitimately sit on the queue twice —
            # break the tie on Sonos's own claim rather than always taking the first
            return min(hits, key=lambda i: abs(i - cur.queue_index))
    unreconciled = next((i for i, m in enumerate(matches) if m is not None), None)
    if unreconciled is not None:
        return unreconciled
    reported = cur.queue_index if cur is not None else 0
    return min(max(reported, 0), max(len(queue) - 1, 0))


def ids_of(matches) -> set[int]:
    """Episode ids currently on the speaker, from a _match_queue result."""
    return {m["id"] for m in matches if m is not None}


def parse_days(days: str) -> set[int]:
    return {int(d) for d in days.split(",") if d != ""}


def schedule_due(schedule, now: datetime, grace_minutes: int) -> bool:
    """Fire when the time has passed today, it hasn't fired today, and we're
    within the grace window — a reboot at 8:03 still catches the 8:00 start,
    but a server down all morning doesn't blast podcasts at 3 pm."""
    if not schedule["enabled"] or now.weekday() not in parse_days(schedule["days"]):
        return False
    if schedule["last_fired_date"] == now.date().isoformat():
        return False
    hh, mm = (int(p) for p in schedule["time"].split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return target <= now <= target + timedelta(minutes=grace_minutes)


def next_start(schedules, now: datetime) -> datetime | None:
    upcoming = []
    for s in schedules:
        if not s["enabled"]:
            continue
        hh, mm = (int(p) for p in s["time"].split(":"))
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            if day.weekday() not in parse_days(s["days"]):
                continue
            candidate = datetime.combine(day, dtime(hh, mm), tzinfo=now.tzinfo)
            if candidate > now:
                upcoming.append(candidate)
                break
    return min(upcoming) if upcoming else None


class DJ:
    """Owns the Sonos queue while on air. The web thread and the DJ loop
    both call in, so every public method takes the lock."""

    def __init__(self, db: Database, cfg: Config,
                 get_player: Callable[[], "SonosPlayer | None"]) -> None:
        self.db = db
        self.cfg = cfg
        self.get_player = get_player
        self._lock = threading.RLock()
        # Which episode id tick() is currently tracking playback position for.
        # Distinguishes "first tick this episode is current" (apply the
        # one-shot resume seek) from "still current" (keep tracking position)
        # — without this the two behaviors alias onto the same DB column and
        # oscillate every other tick (see tick()).
        self._resume_tracking_episode_id: str | None = None
        # The upcoming start whose URLs have already been warmed, so the
        # ticks through the rest of the warm window don't re-resolve them.
        self._warmed_for: datetime | None = None
        # Set whenever we tell Sonos to start a new track; cleared by tick()
        # once transport actually reaches PLAYING. Lets tick() warn when the
        # speaker itself is slow to start audio (CDN/ad-insertion buffering
        # on Sonos's own fetch) — invisible to our request timing, since our
        # side of the command (resolve URL, enqueue, play_from_queue) returns
        # long before Sonos has actually buffered anything.
        self._pending_start_at: float | None = None

    # ── staging ───────────────────────────────────────────────────────
    def _stage_uri(self, episode, player) -> tuple[str, str | None]:
        """Resolve (stream mode) or download (download mode) one episode.
        Returns (playable uri, local_path or None)."""
        if self.cfg.download_mode:
            filename = audio.download_episode(
                episode["audio_url"], self.cfg.media_dir, episode["id"],
                self.cfg.user_agent)
            base = self.cfg.base_url or audio.detect_base_url(player.ip, self.cfg.port)
            return audio.media_url(base, filename), os.path.join(self.cfg.media_dir, filename)
        uri = audio.resolve_audio_url(episode["audio_url"], self.cfg.user_agent)
        if len(uri) > audio.SONOS_URI_LIMIT:
            # Too long for Sonos to hold; hand it our own /stream/ URL and let
            # the app fetch the CDN. Decided here, where the URL enters the
            # system, so nothing downstream has to know which hosts are long.
            base = self.cfg.base_url or audio.detect_base_url(player.ip, self.cfg.port)
            return audio.stream_url(base, episode["id"]), None
        return uri, None

    def _enqueue(self, player, episode, index0: int | None = None,
                 staged: tuple[str, str | None] | None = None) -> bool:
        """Stage one episode on the Sonos queue. On any failure the episode
        is marked skipped — a dead CDN link must never stall the station.

        `staged` passes in an already-resolved (uri, local_path) instead of
        resolving again. play_episode needs the URI *before* it enqueues, to
        purge the episode's stale queue copies; resolving a second time would
        hand back a different expiry token and defeat that purge."""
        try:
            uri, local_path = (staged if staged is not None
                               else self._stage_uri(episode, player))
            feed = self.db.get_feed(episode["feed_id"])
            player.add_to_queue(uri, episode["title"],
                                feed["title"] if feed else "", index0)
        except Exception:
            logger.exception("failed to stage episode %s", episode["id"])
            self.db.mark_skipped(episode["id"])
            return False
        self.db.mark_queued(episode["id"], uri, local_path)
        return True

    _STAGE_WORKERS = 8

    def _claim_batch(self, count: int, already: set[int] | None = None) -> list:
        """Reserve up to `count` episodes so the whole batch can be staged at
        once. Each pick is claimed the instant it's made — `mark_queued` with
        no play_uri — because `pick_next` only ever considers `new`, and a
        feed's oldest unplayed episode doesn't change until something marks
        it. Without the claim, picking ten before enqueueing any would hand
        back the same episode ten times.

        The saved Up Next is drawn down first and only then the rotation. The
        speaker holds a rolling window of the next few, so the rest of a long
        list lives here and comes forward as the needle advances — which is
        also what keeps its signed URLs from expiring before they play."""
        already = already or set()
        claimed: list = []
        for episode in self.db.episodes_by_ids(self.db.up_next_order()):
            if len(claimed) >= count:
                break
            if episode["id"] in already or episode["status"] not in ("new", "queued"):
                continue
            self.db.mark_queued(episode["id"], None)
            claimed.append(episode)
            already.add(episode["id"])
        cutoff = retry_cutoff_iso(self._RETRY_COOLDOWN_MINUTES)
        while len(claimed) < count:
            episode = pick_next(self.db, cutoff)
            if episode is None:
                break
            self.db.mark_queued(episode["id"], None)
            claimed.append(episode)
        return claimed

    def _stage_batch(self, episodes: list, player) -> tuple[list, list]:
        """Resolve (or download) every episode at once instead of one after
        another — staging is a network round trip apiece, and serially it is
        the entire cost of a deep queue. Returns (staged, failed), staged as
        (episode, uri, local_path) in pick order."""
        if not episodes:
            return [], []
        staged, failed = [], []
        with ThreadPoolExecutor(max_workers=min(self._STAGE_WORKERS, len(episodes))) as pool:
            futures = [pool.submit(self._stage_uri, ep, player) for ep in episodes]
            for episode, future in zip(episodes, futures):
                try:
                    uri, local_path = future.result()
                except Exception:
                    logger.exception("failed to stage episode %s", episode["id"])
                    failed.append(episode)
                else:
                    staged.append((episode, uri, local_path))
        return staged, failed

    def _top_up(self, player, current_index: int,
                staged_ids: set[int] | None = None) -> None:
        """Refill the queue to queue_ahead: claim a batch, stage it
        concurrently, then enqueue in pick order.

        staged_ids names what is already on the speaker. The saved Up Next
        still lists those until the needle passes them, so without it the
        top-up would pull the very episodes it can already see forward a
        second time. Callers all hold a fresh `matches`, so it is passed in
        rather than re-read — one Sonos round trip a tick is worth avoiding."""
        needed = self.cfg.queue_ahead - (player.queue_length() - current_index - 1)
        if needed <= 0:
            return
        episodes = self._claim_batch(needed, set(staged_ids or ()))
        if not episodes:
            return
        staged, failed = self._stage_batch(episodes, player)
        if not staged:
            # Nothing at all resolved. That reads as the network being down
            # (WAN out while Sonos control on the LAN still answers), not as a
            # batch of dead links — so put them back rather than burning a
            # whole batch out of the catalog on one bad tick. There is no
            # un-skip, so a wrong call here starves the station permanently.
            for episode in failed:
                self.db.revert_to_new(episode["id"])
            logger.warning("top_up: all %d staging attempts failed — leaving them "
                           "in rotation and stopping for this tick", len(failed))
            return
        for episode in failed:  # a dead link among working ones really is dead
            self.db.mark_skipped(episode["id"])
        for episode, uri, local_path in staged:
            feed = self.db.get_feed(episode["feed_id"])
            try:
                player.add_to_queue(uri, episode["title"],
                                    feed["title"] if feed else "", None)
            except Exception:
                logger.exception("failed to enqueue episode %s", episode["id"])
                self.db.mark_skipped(episode["id"])
                continue
            self.db.mark_queued(episode["id"], uri, local_path)

    # ── queue reconciliation ──────────────────────────────────────────
    def _match_queue(self, player):
        """(queue uris, per-slot matched queued episode or None, current
        index, current track info). Queries player.current() exactly once
        and hands that single snapshot back to the caller — querying it
        again separately would risk a second, later live read racing a
        track change and pairing this snapshot's episode identity with a
        different track's position/duration."""
        queue = player.queue_uris()
        queued = self.db.episodes_with_status("queued")

        # Match right-to-left so each episode maps to its rightmost queue
        # slot. Stale copies of a replayed episode share the same normalized
        # URI (same path, different expiry tokens) and would otherwise match
        # at positions before the needle, causing tick() to _finish() an
        # episode that just started playing.
        matched_ids: set[int] = set()
        matches: list = [None] * len(queue)
        for i in range(len(queue) - 1, -1, -1):
            for ep in queued:
                if (ep["play_uri"] and ep["id"] not in matched_ids
                        and audio.uris_match(queue[i], ep["play_uri"])):
                    matches[i] = ep
                    matched_ids.add(ep["id"])
                    break

        cur = player.current()
        return queue, matches, current_index(queue, matches, cur), cur

    # An episode is given up on as permanently broken after this many
    # consecutive failures. Three rides out a CDN outage without letting a
    # genuinely dead link reappear in the rotation forever — but only because
    # the cooldown below spaces the attempts across hours rather than ticks.
    _MAX_PLAYBACK_FAILURES = 3
    _RETRY_COOLDOWN_MINUTES = 90

    def _finish(self, episode) -> None:
        """Record an episode as heard. Unconditional — callers that aren't
        certain go through _retire()."""
        local_path = self.db.mark_played(episode["id"], utcnow_iso())
        self.db.clear_failures(episode["id"])
        # Heard, so the pin has nothing left to protect. Only here, never in
        # mark_played(): the reconcile paths can retire an episode that never
        # made a sound, and clearing the pin on one of those would expose a
        # row you chose to the next refresh.
        if episode["pinned"]:
            self.db.set_pinned(episode["id"], False)
        if self.db.kv_get("resume_episode_id") == str(episode["id"]):
            self.db.kv_del("resume_episode_id")
        if local_path:
            audio.delete_local(local_path)

    # Dead rows behind the needle are swept once there are this many. A
    # threshold rather than every tick because each removal is its own UPnP
    # round trip, and they are inert until they pile up.
    _MAX_DEAD_ROWS = 10

    def _prune_dead_rows(self, player, matches, cur_idx: int) -> int:
        """Drop finished tracks off the Sonos queue.

        Sonos keeps every track it has played until the queue is cleared, so
        tick() only ever strips their 'queued' status and the slots stay. Over
        a long session they accumulate without bound, and they are exactly
        what current_index has to step over when it cannot match the live URI.

        Behind the needle only. A dead slot ahead of it is either about to be
        played or a stale copy, and removing rows the speaker is about to
        reach is the operation that caused the P v NP incident — not worth it
        for slots that no longer accumulate now that play_episode purges by
        URI. Returns how many were dropped."""
        # An empty match means the read was bad, not that the queue is dead.
        # Without this a momentary miss would sweep the whole history — and,
        # with no queued episodes at all, every slot looks dead.
        if not any(m is not None for m in matches):
            return 0
        dead = [i for i in range(min(cur_idx, len(matches))) if matches[i] is None]
        if len(dead) < self._MAX_DEAD_ROWS:
            return 0
        for idx in reversed(dead):  # high to low, so each index stays valid
            player.remove_from_queue(idx)
        logger.info("pruned %d finished tracks from the queue", len(dead))
        return len(dead)

    def _bank_order(self, matches, cur_idx: int) -> None:
        """Write down Up Next as it stands, from the current track onward.

        Anything behind the needle has been heard and is not part of the list;
        the current track leads it, which is what lets a restored session
        resume exactly where it stopped without the queue carrying a separate
        note about where that was.

        The speaker only ever holds a window, so what it shows is the head of
        the list, not the whole of it. Everything still waiting its turn keeps
        its place behind — writing down only the staged part would quietly
        truncate a long queue to ten every time this ran."""
        staged = [m["id"] for m in matches[cur_idx:] if m is not None]
        seen = set(staged)
        waiting = [ep["id"] for ep in self.db.episodes_by_ids(self.db.up_next_order())
                   if ep["id"] not in seen and ep["status"] in ("new", "queued")]
        self.db.set_up_next(staged + waiting)

    def _retire(self, episode) -> None:
        """Retire a track the needle has moved past — but only call it heard
        if there is evidence it made a sound.

        The needle moving past an episode is not proof it played. When Sonos
        cannot fetch a track it advances immediately, and the old reconcile
        loop booked that as a completed listen: the episode left the backlog
        having produced silence, and out of a five-figure library it would
        never be seen again. tick() banks the speaker's reported position
        while a track is current, so an episode retired with none recorded is
        one Sonos never played."""
        if episode["observed_seconds"]:
            self._finish(episode)
            return
        spent = self.db.mark_failed(episode["id"], utcnow_iso(),
                                    self._MAX_PLAYBACK_FAILURES)
        # Out of the list, or the next top-up re-stages it straight away: the
        # list is drawn ahead of pick_next, which is the only thing that
        # applies the retry cooldown. All three strikes would go in a minute.
        self.db.remove_from_up_next(episode["id"])
        if self.db.kv_get("resume_episode_id") == str(episode["id"]):
            self.db.kv_del("resume_episode_id")
        logger.warning(
            "playback produced no audio for %r (%s) — %s",
            episode["title"], episode["id"],
            "giving up, marked skipped" if spent else "back in rotation to retry")

    def _save_resume(self, player) -> int | None:
        """Bank where the listener is, so a later start() can put them back.
        Returns the episode banked, or None when there was nothing to bank —
        which callers log, because a silent None means a lost place."""
        try:
            _, matches, cur_idx, cur = self._match_queue(player)
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            if episode is not None and cur is not None:
                self.db.set_resume(episode["id"], cur.position)
                self.db.kv_set("resume_episode_id", str(episode["id"]))
                return int(episode["id"])
        except Exception:
            logger.exception("could not save resume position")
            self._invalidate_player()
        return None

    _LIVE_TRANSPORT = ("PLAYING", "TRANSITIONING")

    def _transport_live(self, player) -> bool:
        """Is the speaker on its way to making sound? TRANSITIONING counts:
        Sonos sits there while a track loads, and it is headed for PLAYING."""
        try:
            return player.transport_state() in self._LIVE_TRANSPORT
        except Exception:
            logger.exception("could not read transport state")
            self._invalidate_player()
            return False

    def _invalidate_player(self) -> None:
        """Tell the (possibly cached) player provider to forget its cached
        player after a call to it just failed, so the next lookup re-runs
        discovery instead of returning the same dead player forever. Not
        every get_player callable (e.g. a bare lambda in tests) exposes
        invalidate(), so this is a no-op guard, not a hard dependency."""
        invalidate = getattr(self.get_player, "invalidate", None)
        if invalidate:
            invalidate()

    # ── on air ────────────────────────────────────────────────────────
    _SLOW_START_WARN_SECONDS = 20

    def _mark_transport_cmd(self) -> None:
        self._pending_start_at = time.monotonic()

    def _set_paused(self, paused: bool) -> None:
        """Remember whether the silence is deliberate.

        A paused session stays dj_state='playing' on purpose — Up Next
        survives, and tick() has to keep banking position, inserting news and
        topping up. So dj_state cannot tell tick()'s end-of-queue recovery
        that the listener asked for quiet, and a speaker that comes back from
        a reboot reporting STOPPED reads exactly like one that ran off the
        end of its queue. It played the house awake at 03:49 (2026-08-05).

        In kv rather than on self: the process outlives most sessions but not
        all of them, and a restart that forgot a pause would leave tick()
        free to make the same noise on the next STOPPED it saw."""
        if paused:
            self.db.kv_set("paused", "1")
        else:
            self.db.kv_del("paused")

    def _is_paused(self) -> bool:
        return self.db.kv_get("paused") == "1"

    def _queue_plan(self) -> tuple[list, list]:
        """(fresh news, the saved Up Next behind it) — what going on air
        right now would stage, in order.

        Only the window's worth goes to the speaker; _top_up draws the rest
        of the list down as the needle advances. Room is counted off the news
        list rather than the live queue because at the moment start() asks,
        nothing is on the queue yet — a news item that fails to stage just
        leaves its slot to _top_up, which draws from this same saved order
        first. Shared with the warm-up so it resolves the URLs the alarm is
        actually going to ask for.

        The two lists never overlap. An episode is normally in one or the
        other — the saved order holds staged episodes, and fresh_news only
        offers unstaged ones — but a news episode put back to 'new' by a
        failed playback can be in both, and staging it twice would play it
        twice."""
        news = self.db.fresh_news(feeds.news_cutoff_iso(self.cfg))
        room = max(0, self.cfg.queue_ahead - len(news))
        leading = {ep["id"] for ep in news}
        restored = [ep for ep in self.db.episodes_by_ids(self.db.up_next_order())
                    if ep["status"] in ("new", "queued")
                    and ep["id"] not in leading][:room]
        return news, restored

    def start(self) -> str | None:
        """On air. Returns an error message, or None on success."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            # Restore, not rebuild. Up Next survives going off air, so going
            # back on picks up the same list — including the episode that was
            # playing, which sits at its head and gets its resume seek from
            # tick() as usual. The Sonos queue itself still has to be thrown
            # away and re-staged: it may have had Spotify played through it,
            # and its signed URLs expire within hours.
            saved = self.db.up_next_order()
            orphaned = self.db.revert_queued_except(saved)
            player.clear_queue()
            news, restored = self._queue_plan()
            # Resolve every URL at once — the pool _top_up has always used.
            # Staging is a network round trip apiece, a redirect chain at a
            # 20s timeout, and taken one at a time it is the entire cost of
            # going on air: ten episodes spent 26 seconds of dead air at an
            # 08:00 alarm before the first sound.
            resolved, failed = self._stage_batch(news + restored, player)
            for episode in failed:  # dead link: _enqueue's own verdict, batched
                self.db.mark_skipped(episode["id"])
            news_ids = {ep["id"] for ep in news}
            enqueued = [ep for ep, uri, local_path in resolved
                        if self._enqueue(player, ep, staged=(uri, local_path))]
            staged = sum(1 for ep in enqueued if ep["id"] not in news_ids)
            # The queue this builds is the whole product — news first, then
            # the list the listener left behind, then rotation. When any of
            # that goes missing there is nothing else to reconstruct it from,
            # so say what was laid down.
            logger.info("start: %d news, %d/%d restored of %d saved, %d orphans "
                        "released, queue=%d", len(news), staged, len(restored),
                        len(saved), orphaned, player.queue_length())
            self._top_up(player, 0,
                         {ep["id"] for ep in news} | {ep["id"] for ep in restored})
            if player.queue_length() == 0:
                return "Nothing to play — add a station first"
            self._mark_transport_cmd()
            player.play_from_queue(0)
            self.db.kv_set("dj_state", "playing")
            self._set_paused(False)
            # Bank what was just laid down rather than waiting for the first
            # tick: the list the restore was read from is now out of date by
            # everything the top-up added, and a crash before that tick would
            # otherwise lose the difference.
            try:
                _q, matches, cur_idx, _cur = self._match_queue(player)
                self._bank_order(matches, cur_idx)
            except Exception:
                logger.exception("start: could not bank Up Next")
            # a fresh on-air session must treat whatever is current as
            # "just became current" so a pending resume_seconds (e.g. from
            # an overnight stop_off_air on this same long-lived instance)
            # gets its one-shot seek instead of being silently overwritten
            # by tick()'s position-tracking branch.
            self._resume_tracking_episode_id = None
            return None

    # ── the reconcile loop ────────────────────────────────────────────
    def tick(self) -> None:
        with self._lock:
            if self.db.kv_get("dj_state") != "playing":
                return
            player = self.get_player()
            if player is None:
                return
            try:
                queue, matches, cur_idx, cur = self._match_queue(player)
                transport = player.transport_state()
            except Exception:
                logger.exception("tick: cannot read player state")
                self._invalidate_player()
                return
            if self._pending_start_at is not None and transport == "PLAYING":
                elapsed = time.monotonic() - self._pending_start_at
                self._pending_start_at = None
                if elapsed > self._SLOW_START_WARN_SECONDS:
                    episode = matches[cur_idx] if cur_idx < len(matches) else None
                    title = episode["title"] if episode is not None else "unknown episode"
                    logger.warning(
                        "slow playback start: %.0fs from transport command to "
                        "PLAYING for %r — Sonos was buffering/transitioning "
                        "that whole time, not our own request handling",
                        elapsed, title)
            queued = self.db.episodes_with_status("queued")
            # hijack detection: none of our staged items remain on the queue
            if queued and not any(m is not None for m in matches):
                logger.info("queue no longer ours — standing down")
                self.db.revert_all_queued()
                self.db.kv_set("dj_state", "stopped")
                return
            # Bank the speaker's position for the current track BEFORE
            # retiring anything. A track can be current and already past the
            # needle in the same tick when Sonos advances mid-read, and the
            # evidence has to exist by the time _retire() asks for it.
            if cur is not None and cur.position:
                here = matches[cur_idx] if cur_idx < len(matches) else None
                if here is not None:
                    self.db.record_observed(here["id"], cur.position)
            # everything before the current position has been listened past —
            # if it actually played; _retire() is what tells those apart
            for i in range(min(cur_idx, len(matches))):
                if matches[i] is not None:
                    self._retire(matches[i])
            # current episode: one-shot resume seek, then position tracking.
            # is_new_current distinguishes "this episode just became current"
            # (apply the pending seek once) from "still current" (keep
            # tracking position) — without it both branches fire in
            # alternating ticks and the episode never progresses.
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            if episode is not None and cur is not None:
                is_new_current = self._resume_tracking_episode_id != str(episode["id"])
                if is_new_current and episode["resume_seconds"] is not None:
                    player.seek_seconds(max(0, int(episode["resume_seconds"]) - 10))
                    self.db.set_resume(episode["id"], None)
                elif not is_new_current and transport == "PLAYING":
                    self.db.set_resume(episode["id"], cur.position)
                self._resume_tracking_episode_id = str(episode["id"])
                self.db.kv_set("resume_episode_id", str(episode["id"]))
            # Bank the running order, current track first. This is what makes
            # Up Next durable: the Sonos queue is thrown away at the next
            # start(), so if the order is not written down here there is
            # nothing to restore it from. Written every tick because a
            # reorder, a drop or a play-now can land between any two of them.
            self._bank_order(matches, cur_idx)
            # news insertion: fresh news exists and the next track isn't news
            news = self.db.fresh_news(feeds.news_cutoff_iso(self.cfg))
            if news:
                nxt = matches[cur_idx + 1] if cur_idx + 1 < len(matches) else None
                next_is_news = False
                if nxt is not None:
                    feed = self.db.get_feed(nxt["feed_id"])
                    next_is_news = bool(feed and feed["is_news"])
                if not next_is_news:
                    insert_at = cur_idx + 1
                    for ep in news:
                        if self._enqueue(player, ep, insert_at):
                            insert_at += 1
            # keep QUEUE_AHEAD tracks ahead of the needle
            self._top_up(player, cur_idx, ids_of(matches))
            # survive hitting the end of the queue between ticks — but never
            # against a pause. STOPPED is not only what Sonos reports at the
            # end of a queue; it is also what a speaker reports for the first
            # ticks after it reboots, and a firmware update at 03:47 is how
            # this branch turned a session paused the previous morning back on
            # in the middle of the night.
            if (transport == "STOPPED" and not self._is_paused()
                    and player.queue_length() - cur_idx - 1 > 0):
                self._mark_transport_cmd()
                player.play_from_queue(cur_idx)
                # play_from_queue restarts the track at its top, and the
                # position tracking above would then bank that as the
                # listener's place — overwriting the real one within a tick.
                # Forgetting what was current hands the next tick the one-shot
                # resume seek instead, so a recovery mid-episode picks the
                # listener up where they stopped.
                self._resume_tracking_episode_id = None
            # Last, because it renumbers every slot behind the needle and
            # nothing after it may use cur_idx or matches again.
            self._prune_dead_rows(player, matches, cur_idx)

    # ── transport controls ────────────────────────────────────────────
    _NO_SPEAKER = "No Sonos speaker available — pick one first"

    def play(self) -> str | None:
        """The dashboard's single play/pause toggle calls play or pause
        based on transport state; play = start or resume."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            # TRANSITIONING counts as already on air: a Play arriving in the
            # split second after a skip — from a client that polled before we
            # re-rendered — must not fall through to start(), which clears the
            # queue and rebuilds the session out from under the track Sonos is
            # in the middle of starting.
            if (self.db.kv_get("dj_state") == "playing"
                    and player.transport_state() in ("PAUSED_PLAYBACK", "TRANSITIONING")):
                player.play()
                self._set_paused(False)
                return None
            return self.start()

    def pause(self) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            self._save_resume(player)
            player.pause()
            self._set_paused(True)
            return None

    def stop_off_air(self) -> None:
        """Off air. Up Next is left standing — going off air is a pause on the
        station, not a decision to throw away the queue. Banked here as well
        as on the tick so a Stop right after a reorder keeps it."""
        with self._lock:
            player = self.get_player()
            if player is not None:
                self._save_resume(player)
                try:
                    _q, matches, cur_idx, _cur = self._match_queue(player)
                    self._bank_order(matches, cur_idx)
                except Exception:
                    logger.exception("stop: could not bank Up Next")
                try:
                    player.stop()
                except Exception:
                    logger.exception("stop failed")
                    self._invalidate_player()
            self.db.kv_set("dj_state", "stopped")
            # Off air is its own answer to "should tick() play?" — dj_state
            # alone stops it. Cleared so a pause left standing from before
            # can't outlive the session that made it.
            self._set_paused(False)

    # ── seeking ───────────────────────────────────────────────────────
    def seek_abs(self, seconds: int) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            cur = player.current()
            if cur is None:
                return "Nothing is playing"
            upper = cur.duration - 1 if cur.duration > 0 else int(seconds)
            player.seek_seconds(max(0, min(int(seconds), max(0, upper))))
            return None

    def seek_rel(self, delta: int) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            cur = player.current()
            if cur is None:
                return "Nothing is playing"
            return self.seek_abs(cur.position + delta)

    # ── the three dispositions ────────────────────────────────────────
    def _skip(self, disposition: Literal["later", "done", "defer"]) -> str | None:
        """Take the current episode off the air. All three dispositions share
        this prologue — fill first when this is the last track so there is
        something to advance to, advance or stop, then drop the outgoing slot —
        and differ only in what becomes of the episode afterwards."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            queue, matches, cur_idx, cur = self._match_queue(player)
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            if episode is None:
                return "Current track isn't a station episode"
            if player.queue_length() - cur_idx - 1 == 0:
                self._top_up(player, cur_idx, ids_of(matches))  # last one: fill first
            if player.queue_length() - cur_idx - 1 > 0:
                self._mark_transport_cmd()
                player.play_from_queue(cur_idx + 1)  # advance
                self._set_paused(False)
            else:
                player.stop()
            player.remove_from_queue(cur_idx)  # then drop the skipped item
            if disposition == "done":
                self._finish(episode)
            elif disposition == "later":
                self.db.revert_to_new(episode["id"])
                self.db.remove_from_up_next(episode["id"])  # let go, not waiting
                self.db.set_resume(episode["id"], None)  # replays fresh another day
                if self.db.kv_get("resume_episode_id") == str(episode["id"]):
                    self.db.kv_del("resume_episode_id")
            else:
                self._hold_in_up_next(player, episode, cur, matches, cur_idx)
            return None

    def skip_later(self) -> str | None:
        return self._skip("later")

    def skip_done(self) -> str | None:
        return self._skip("done")

    def defer_current(self) -> str | None:
        """Hold the playing episode in Up Next rather than sending it back to
        rotation: the news goes on now, and this picks up where it stopped."""
        return self._skip("defer")

    def _news_run(self, matches, cur_idx: int) -> int:
        """How many news episodes sit in an unbroken run directly after the
        needle. Read off the feed, the way tick()'s own insertion reads it, not
        off the episode row.

        The run stops at the first slot that is not a matched news episode — an
        unmatched slot ends it rather than being stepped over, because a slot
        we cannot identify is not one we can claim to have counted past."""
        n = 0
        for i in range(cur_idx + 1, len(matches)):
            episode = matches[i]
            if episode is None:
                break
            feed = self.db.get_feed(episode["feed_id"])
            if not (feed and feed["is_news"]):
                break
            n += 1
        return n

    def _hold_in_up_next(self, player, episode, cur, matches,
                         cur_idx: int) -> None:
        """Put the outgoing episode back into Up Next behind the news, holding
        its place. The inverse of the "later" tail: status, saved order and
        position all stay exactly as they were, and the row is pinned for the
        same reason a hand-queued one is — an episode you were listening to a
        second ago is one you chose.

        The offset floors at one item. Taken literally, "after the news block"
        with no news ahead lands the episode at the head of Up Next — next
        again — so advancing would replay it on the spot and the whole control
        would amount to nothing.

        The stored `play_uri` is re-added as-is rather than re-resolved: the row
        is already staged and correct, and a second resolve would hand back a
        different expiry token."""
        if cur is not None:
            self.db.set_resume(episode["id"], cur.position)
        dest = cur_idx + max(1, self._news_run(matches, cur_idx))
        feed = self.db.get_feed(episode["feed_id"])
        # `dest` needs no adjustment for the removal that just happened: taking
        # the outgoing row out shifts the block down one, so inserting at the
        # original index lands just after it — the same arithmetic move_in_queue
        # documents for moving a row down. Past the end, append.
        player.add_to_queue(episode["play_uri"], episode["title"],
                            feed["title"] if feed else "",
                            dest if dest < player.queue_length() else None)
        self.db.set_pinned(episode["id"], True)

    def drop_from_queue(self, episode_id: int) -> str | None:
        """Remove an upcoming episode from Up Next and recycle it to the
        new pool — the DJ will pick it again another day. The current track
        isn't droppable (that's what Skip is for)."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            _, matches, cur_idx, _cur = self._match_queue(player)
            for idx in range(cur_idx + 1, len(matches)):
                ep = matches[idx]
                if ep is not None and ep["id"] == episode_id:
                    player.remove_from_queue(idx)
                    # refill BEFORE reverting: the target is still 'queued'
                    # here, so the top-up can't immediately re-pick it
                    self._top_up(player, cur_idx, ids_of(matches))
                    self.db.revert_to_new(ep["id"])
                    self.db.remove_from_up_next(ep["id"])
                    return None
            return "That episode isn't in Up Next"

    def move_in_queue(self, episode_id: int, to_position: int) -> str | None:
        """Move an episode to `to_position` within Up Next.

        Positions are expressed in Up Next terms, not raw Sonos queue
        indices, because the two drift apart: tick() strips a finished
        episode's `queued` status but never removes the track, so the head
        of the Sonos queue fills with slots the listener can't see.

        SoCo has no reorder for the playback queue — reorder_sonos_playlist
        works on saved playlists, not Q:0 — so a move is a remove and a
        re-add at the target. It re-adds the URI already staged rather than
        re-resolving, which keeps a drag free of network round trips."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            _, matches, cur_idx, _cur = self._match_queue(player)
            up_next = [i for i in range(cur_idx + 1, len(matches))
                       if matches[i] is not None]
            from_idx = next((i for i in up_next
                             if matches[i]["id"] == episode_id), None)
            if from_idx is None:
                return "That episode isn't in Up Next"
            to_idx = up_next[max(0, min(to_position, len(up_next) - 1))]
            if to_idx == from_idx:
                return None
            episode = matches[from_idx]
            feed = self.db.get_feed(episode["feed_id"])
            player.remove_from_queue(from_idx)
            # to_idx works unadjusted in both directions. Moving down, taking
            # the row out first shifts the target up one, so inserting at the
            # original index lands just after it; moving up, nothing below
            # from_idx shifts, so the row lands exactly where asked.
            player.add_to_queue(episode["play_uri"], episode["title"],
                                feed["title"] if feed else "", to_idx)
            return None

    def group_all(self) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            try:
                player.group_all()
                return None
            except Exception:
                logger.exception("partymode failed")
                self._invalidate_player()
                return "Grouping failed"

    def play_episode(self, episode_id: int,
                     mode: Literal["next", "now", "last"]) -> str | None:
        """Force-play a specific episode, any status. mode="next" queues it
        right after the current track; mode="now" interrupts immediately;
        mode="last" appends it to the end of Up Next.
        For the on-air bootstrap we reuse start(); for the actual interrupt
        we inline the skip logic rather than delegating to _skip(), because
        _skip() re-reads _match_queue and can race with Sonos naturally
        advancing to the next track between our _enqueue() call and _skip()'s
        read — causing _skip() to see that shifted track as "current" and
        accidentally remove it from the queue."""
        with self._lock:
            episode = self.db.get_episode(episode_id)
            if episode is None:
                return "no such episode"
            if self.db.kv_get("dj_state") != "playing":
                error = self.start()
                if error == self._NO_SPEAKER:
                    return error
                # error may be "Nothing to play" if start()'s own rotation/
                # news/resume bootstrap found nothing -- irrelevant here,
                # we have an explicit episode to play regardless.
                self.db.kv_set("dj_state", "playing")
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            queue, matches, cur_idx, cur = self._match_queue(player)
            current = matches[cur_idx] if cur_idx < len(matches) else None
            # current_index() falls back to a positional guess when it can't
            # pin the needle by URI. Restarting in place on a guess is how an
            # expired copy got seek(0) while the fresh one was never staged,
            # so only take the shortcut when a real URI hit found the needle.
            needle_found = bool(cur is not None and cur.uri
                                and cur_idx < len(queue)
                                and audio.uris_match(queue[cur_idx], cur.uri))
            if (mode == "now" and needle_found
                    and current is not None and current["id"] == episode_id):
                return self.seek_abs(0)  # already playing it -- just restart
            # Resolve before enqueueing: the purge below needs the URI, and
            # _enqueue would otherwise resolve a second time and hand back a
            # different expiry token.
            try:
                staged = self._stage_uri(episode, player)
            except Exception:
                logger.exception("failed to stage episode %s", episode_id)
                self.db.mark_skipped(episode_id)
                return "Could not queue that episode"
            # Take the episode off the queue wherever it already sits before
            # staging it again. Play now is almost always pressed on a row in
            # Up Next, and staging a second copy leaves the first one there —
            # the episode then reads as playing AND upcoming, because both
            # slots resolve to the same episode row. Move it, don't copy it.
            #
            # Match on the raw queue URI, not through `matches`: once an
            # episode is marked played its play_uri is cleared, so its leftover
            # queue rows are invisible to the match and every replay adds one
            # more. Enough of them pile up behind the needle that tick() finds
            # one before cur_idx and finishes the episode on the spot. Every
            # copy normalizes to the same URI, so one comparison catches them
            # all. Walk high to low so each index stays valid as we remove, and
            # follow the needle down if a copy sat behind it. The needle's own
            # slot is left to the transport code below, which retires it.
            for idx in range(len(queue) - 1, -1, -1):
                if idx == cur_idx or not audio.uris_match(queue[idx], staged[0]):
                    continue
                player.remove_from_queue(idx)
                if idx < cur_idx:
                    cur_idx -= 1
            if current is None:
                insert_at = 0  # nothing playing: it becomes the whole session
            elif mode == "last":
                insert_at = None  # append — the end of Up Next
            else:
                insert_at = cur_idx + 1
            if not self._enqueue(player, episode, insert_at, staged):
                return "Could not queue that episode"
            # Queued by hand, so it arrives pinned: losing an episode you
            # chose because you did not also think to pin it is the same
            # silent loss the playback check exists to prevent. Unpin it from
            # the row if you would rather a refresh re-rolled it.
            self.db.set_pinned(episode_id, True)
            self.db.kv_set("last_feed_id", str(episode["feed_id"]))
            if current is None:
                self._mark_transport_cmd()
                player.play_from_queue(insert_at)
                self._set_paused(False)
            elif mode == "now":
                # _enqueue just placed the episode at cur_idx+1, so there is
                # always a track to advance to — no top-up needed here.
                # Use cur_idx from the snapshot above; re-reading via _skip()
                # would open a race window where Sonos can advance between
                # the two reads and cause the wrong episode to be removed.
                self._mark_transport_cmd()
                player.play_from_queue(cur_idx + 1)
                self._set_paused(False)
                player.remove_from_queue(cur_idx)
                # The outgoing track can BE this episode when the needle sat on
                # a stale copy of it — recycling it then would revert the row we
                # just queued and drop it straight back out of Up Next.
                if current["id"] != episode_id:
                    self.db.revert_to_new(current["id"])
                    self.db.remove_from_up_next(current["id"])
                    self.db.set_resume(current["id"], None)
                    if self.db.kv_get("resume_episode_id") == str(current["id"]):
                        self.db.kv_del("resume_episode_id")
            return None

    def pin_episode(self, episode_id: int, pinned: bool) -> str | None:
        """Mark an Up Next row to survive "Refresh all", or stop it doing so.

        A property of the row, not a list of its own: pinning changes nothing
        about where an episode sits or when it plays, only whether a refresh
        is allowed to re-roll it."""
        if self.db.get_episode(episode_id) is None:
            return "no such episode"
        self.db.set_pinned(episode_id, bool(pinned))
        return None

    def refresh_queue(self) -> str | None:
        """Re-roll the DJ's picks. Pinned rows and the track on air stay put;
        everything else goes back in the pool and the queue fills again from
        rotation.

        The escape hatch a durable queue needs — without it a list you have
        stopped wanting is one you have to dismantle by hand."""
        with self._lock:
            player = self.get_player()
            saved = self.db.up_next_order()
            on_air = None
            cur_idx = 0
            matches: list = []
            if player is not None and self.db.kv_get("dj_state") == "playing":
                try:
                    _q, matches, cur_idx, _cur = self._match_queue(player)
                    here = matches[cur_idx] if cur_idx < len(matches) else None
                    on_air = here["id"] if here is not None else None
                except Exception:
                    logger.exception("refresh: cannot read the queue")
                    self._invalidate_player()
                    return "Speaker unreachable"
            drop = {i for i in self.db.unpinned_in(saved) if i != on_air}
            if not drop:
                return None
            self.db.set_up_next([i for i in saved if i not in drop])
            for episode_id in drop:
                self.db.revert_to_new(episode_id)
            if player is None or self.db.kv_get("dj_state") != "playing":
                return None
            try:
                # Take the dropped rows off the speaker, high to low so each
                # index stays valid, then let the top-up refill from rotation.
                for idx in range(len(matches) - 1, cur_idx, -1):
                    ep = matches[idx]
                    if ep is not None and ep["id"] in drop:
                        player.remove_from_queue(idx)
                self._top_up(player, cur_idx, ids_of(matches))
            except Exception:
                logger.exception("refresh: could not restage the queue")
                self._invalidate_player()
                return "Speaker unreachable"
            logger.info("refresh: re-rolled %d of %d", len(drop), len(saved))
            return None

    def arc_parts(self, episode) -> list:
        """Every still-unheard part of the multi-part story this episode
        belongs to, oldest first. Empty when the title carries no part marker.

        Unheard means 'new' or 'queued' — a part already sitting in Up Next
        still belongs to the story, and queueing the series just moves it into
        line. Includes the given episode whatever its status, since asking for
        the series off a part you have already heard should still queue the
        rest, but no other played part, so replaying part 3 doesn't drag parts
        1 and 2 back out of the archive.

        Grouped by series_key, not the rotation's looser arc_key: acting on a
        whole group cannot afford arc_key's truncation, which collapses every
        "SYMHC Classics: …" episode into one sixteen-part arc."""
        key = feeds.series_key(episode["title"])
        if key is None:
            return []
        parts = [ep for ep in self.db.episodes_for_feed(episode["feed_id"])
                 if feeds.series_key(ep["title"]) == key
                 and (ep["status"] in ("new", "queued")
                      or ep["id"] == episode["id"])]
        return sorted(parts, key=lambda ep: (ep["published_at"], ep["id"]))

    def queue_arc(self, episode_id: int) -> str | None:
        """Append a whole multi-part story to Up Next, in order.

        The rotation's arc guard offered as a deliberate action. Resolving is
        done off the lock and all at once: every part costs a network round
        trip with a 20-second timeout, and the lock serialises tick() along
        with every transport control the dashboard can send, so five parts
        against a dead CDN would freeze the board for a minute and a half.
        The wake alarm moved its refresh off the lock for the same reason."""
        with self._lock:
            episode = self.db.get_episode(episode_id)
            if episode is None:
                return "no such episode"
            parts = self.arc_parts(episode)
            if not parts:
                return "That episode isn't part of a series"
            if self.db.kv_get("dj_state") != "playing":
                error = self.start()
                if error == self._NO_SPEAKER:
                    return error
                self.db.kv_set("dj_state", "playing")
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            try:
                _, matches, cur_idx, _cur = self._match_queue(player)
            except Exception:
                logger.exception("queue_arc: cannot read the queue")
                self._invalidate_player()
                return "Speaker unreachable"
            # A part on air right now is already where it should be. Appending
            # it would leave two queue rows for one episode — the ambiguity
            # that makes a queue impossible to reconcile — because "last" has
            # no transport step to retire the needle's own slot the way "now"
            # does.
            on_air = matches[cur_idx] if cur_idx < len(matches) else None
            pending = [p for p in parts
                       if on_air is None or p["id"] != on_air["id"]]

        staged, failed = self._stage_batch(pending, player)
        for ep in failed:
            self.db.mark_skipped(ep["id"])

        with self._lock:
            try:
                queue, _matches, cur_idx, _cur = self._match_queue(player)
                # Drop any copy already sitting in Up Next before appending, so
                # queueing a series a listener half-queued moves those parts
                # into line rather than doubling them.
                for idx in range(len(queue) - 1, -1, -1):
                    if idx != cur_idx and any(audio.uris_match(queue[idx], uri)
                                              for _ep, uri, _lp in staged):
                        player.remove_from_queue(idx)
                for ep, uri, local_path in staged:
                    self._enqueue(player, ep, None, (uri, local_path))
            except Exception:
                logger.exception("queue_arc: could not stage the series")
                self._invalidate_player()
                return "Could not queue that series"
        logger.info("queued %d of a %d-part series from episode %s",
                    len(staged), len(parts), episode_id)
        return None

    def set_volume(self, value: int) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            try:
                player.set_volume(value)
                return None
            except Exception:
                logger.exception("set volume failed")
                self._invalidate_player()
                return "Speaker unreachable"

    # ── wake schedules ────────────────────────────────────────────────
    def warm_next_start(self, now: datetime | None = None) -> None:
        """A few minutes before an alarm is due, resolve the URLs it will
        need — so the listener isn't waiting on redirect chains at 08:00.

        Nothing is stored and nothing is staged. The whole value is the DNS
        and redirect-chain caches the resolution leaves behind: the same ten
        episodes measured 25.2s to resolve cold on the deployed library and
        5.5s warm. It writes nothing, takes no lock and never marks an
        episode, so the worst a bad warm can cost is the round trips it made.

        Resolution only, never a download: under DOWNLOAD_MODE staging pulls
        whole episodes, and doing that here would double the transfer for an
        alarm that is about to do it properly."""
        now = now or datetime.now().astimezone()
        upcoming = next_start(self.db.list_schedules(), now)
        if upcoming is None or self._warmed_for == upcoming:
            return
        if not timedelta() < upcoming - now <= timedelta(minutes=self.cfg.warm_minutes):
            return
        # Claimed before the work, not after: warming takes seconds and the
        # loop ticks throughout, so a flag set at the end would let every
        # tick in the window start a warm of its own.
        self._warmed_for = upcoming
        feeds.refresh_news(self.db, self.cfg)
        news, restored = self._queue_plan()
        episodes = news + restored
        if not episodes:
            return
        logger.info("warming %d URLs for the %s start", len(episodes),
                    upcoming.strftime("%a %H:%M"))
        started = time.monotonic()
        with ThreadPoolExecutor(
                max_workers=min(self._STAGE_WORKERS, len(episodes))) as pool:
            list(pool.map(self._warm_one, episodes))
        logger.info("warmed %d URLs in %.1fs", len(episodes),
                    time.monotonic() - started)

    def _warm_one(self, episode) -> None:
        """One URL resolved and thrown away. A failure here is a non-event:
        the episode is not marked, because the alarm's own staging is what
        decides whether a link is dead, and a warm-up that skipped episodes
        on a transient error would empty the queue it exists to speed up."""
        try:
            audio.resolve_audio_url(episode["audio_url"], self.cfg.user_agent)
        except Exception as exc:
            logger.info("warm-up: %s did not resolve (%s)",
                        episode["id"], type(exc).__name__)

    def check_schedules(self, now: datetime | None = None) -> None:
        now = now or datetime.now().astimezone()  # LOCAL time — TZ matters in Docker
        due = [s for s in self.db.list_schedules()
               if schedule_due(s, now, self.cfg.grace_minutes)]
        if not due:
            return
        # Fetch the headlines BEFORE taking the lock, and fetch only the news
        # feeds. This is network I/O, and the lock serializes tick() and every
        # transport control the dashboard can send — holding it across a full
        # library refresh froze the UI for minutes at alarm time. Refreshing
        # here rather than per-schedule also means two alarms due in the same
        # window fetch once between them, not once each.
        feeds.refresh_news(self.db, self.cfg)
        with self._lock:
            for schedule in due:
                logger.info("wake schedule %s (%s) firing", schedule["id"], schedule["time"])
                # Only burn the once-per-day token once we know the fire attempt
                # was resolved (succeeded, was a no-op, or hit a terminal error).
                # A transient failure — e.g. the speaker isn't discoverable yet
                # right after a reboot — must NOT mark the schedule fired, so the
                # remaining grace-window ticks get a chance to retry.
                if self._fire_wake():
                    self.db.set_last_fired(schedule["id"], now.date().isoformat())

    # ── status for the dashboard poll ─────────────────────────────────
    def status(self) -> dict:
        now = datetime.now().astimezone()
        counts = self.db.counts_by_feed()
        failures = self.db.failure_counts_by_feed()
        schedules = self.db.list_schedules()
        data: dict = {
            "dj_state": self.db.kv_get("dj_state") or "stopped",
            "transport": None,
            "speaker": None,
            "now_playing": None,
            "up_next": [],
            "stations": [
                {
                    "id": f["id"], "title": f["title"], "url": f["url"],
                    "image_url": f["image_url"],
                    "description": f["description"],
                    "is_news": bool(f["is_news"]), "enabled": bool(f["enabled"]),
                    "category_id": f["category_id"],
                    "playback_mode": f["playback_mode"],
                    "counts": {s: counts.get(f["id"], {}).get(s, 0)
                               for s in ("new", "queued", "played", "skipped", "archived")},
                    "failures": failures.get(f["id"], 0),
                }
                for f in self.db.list_feeds()
            ],
            "schedules": [
                {"id": s["id"], "time": s["time"],
                 "days": sorted(parse_days(s["days"])), "enabled": bool(s["enabled"])}
                for s in schedules
            ],
            "categories": [
                {"id": cat["id"], "name": cat["name"],
                 "rotation_enabled": bool(cat["rotation_enabled"]),
                 "station_count": cat["station_count"]}
                for cat in self.db.list_categories()
            ],
            "next_start": None,
            "recently_played": [
                {"title": e["title"], "show": e["feed_title"], "played_at": e["played_at"]}
                for e in self.db.recently_played()
            ],
            # Episodes the speaker accepted and then made no sound on. Surfaced
            # because the failure is otherwise invisible: playback moves on, and
            # without this the only symptom is an episode you queued not being
            # the one you hear.
            "recent_failures": [
                {"title": e["title"], "show": e["feed_title"],
                 "failed_at": e["last_failed_at"], "attempts": e["failure_count"],
                 "given_up": e["status"] == "skipped"}
                for e in self.db.recent_failures()
            ],
            "download_mode": self.cfg.download_mode,
            "volume": None,
        }
        upcoming = next_start(schedules, now)
        data["next_start"] = upcoming.strftime("%a %H:%M") if upcoming else None

        def entry(ep) -> dict:
            feed = self.db.get_feed(ep["feed_id"])
            return {"episode_id": ep["id"], "title": ep["title"],
                    "show": feed["title"] if feed else "",
                    "is_news": bool(feed and feed["is_news"]),
                    "image_url": feed["image_url"] if feed else None,
                    "published_at": ep["published_at"],
                    "pinned": bool(ep["pinned"]),
                    "duration": ep["duration_seconds"],
                    "description": ep["description"]}

        # Off air Up Next still exists — it just isn't on the speaker. Show it
        # from the saved order so the queue can be read and rearranged before
        # going on, rather than looking empty until something is playing.
        if data["dj_state"] != "playing":
            data["up_next"] = [entry(ep) for ep
                               in self.db.episodes_by_ids(self.db.up_next_order())
                               if ep["status"] in ("new", "queued")]
        try:
            player = self.get_player()
        except Exception:
            player = None
        if player is None:
            return data
        try:
            data["speaker"] = {"name": player.name, "ip": player.ip}
            data["transport"] = player.transport_state()
            data["volume"] = player.get_volume()
            queue, matches, cur_idx, cur = self._match_queue(player)
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            # cur is None while Sonos is mid-transition — right after a skip
            # or play-now, which is exactly when the dashboard polls. The
            # needle still sits on a real episode there, so name it rather
            # than blanking the deck. Only while on air, though: a stopped
            # speaker also reports no track, and "queued" rows can outlive a
            # crash, so off air that silence means idle, not starting.
            if episode is not None and (cur is not None
                                        or data["dj_state"] == "playing"):
                # entry() seeds duration from the feed's itunes:duration; the
                # live Sonos length overrides it only when Sonos actually has
                # one. Some streams (Buzzsprout redirects, chunked CDNs) report
                # duration 0, and dropping the DB fallback there pins the
                # progress bar at 0% and disables tap-to-seek.
                now_playing = {**entry(episode),
                               "position": cur.position if cur is not None else 0}
                if cur is not None and cur.duration:
                    now_playing["duration"] = cur.duration
                data["now_playing"] = now_playing
            # On air the speaker is the truth; off air it still holds the last
            # session's tracks, which would otherwise overwrite the saved list
            # set above with a stale window missing its head.
            if data["dj_state"] == "playing":
                data["up_next"] = [entry(ep) for ep in matches[cur_idx + 1:]
                                   if ep is not None]
        except Exception:
            logger.exception("status: player unreachable")
            self._invalidate_player()
        return data

    def _fire_wake(self) -> bool:
        """Attempt the wake fire. Returns True if the schedule should be marked
        fired for today (success, or a terminal error that a retry wouldn't
        fix), False if the failure is transient and a later tick within the
        grace window should retry."""
        player = self.get_player()
        if player is not None:
            # Bank where the listener is FIRST, off an untouched speaker.
            # A rebuild re-queues that episode directly behind the news and
            # tick() seeks it back to here, so an alarm that does rebuild
            # costs them nothing but the wait — but only if we actually read
            # a current track. Grouping is precisely what stops us reading
            # one: partymode moves coordination and leaves Sonos
            # mid-transition, and _save_resume banks nothing when it can't
            # see a current track. So this goes before the regroup, not after.
            interrupted = self._save_resume(player)
            # That same read answers the question the whole fire turns on: is
            # the station already on? An episode came back only if the track
            # the speaker is on is one of ours, which is what rules out a
            # hijack — Spotify playing over our queue is a takeover the alarm
            # still has to win, and dj_state alone outlives one by a tick.
            on_air = (interrupted is not None
                      and self.db.kv_get("dj_state") == "playing"
                      and self._transport_live(player))
            logger.info("wake: %s episode %s",
                        "already on air on" if on_air else "interrupting",
                        interrupted)
            # Gather the house into one group either way — the other rooms
            # have to join whether or not we go on to rebuild. Ahead of any
            # queue work, because the queue lives on the group coordinator and
            # grouping is what decides which speaker that is. Build first and
            # the queue is stranded on whoever was coordinating beforehand.
            error = self.group_all()
            if error:
                # a speaker that's off or unreachable is a reason to wake the
                # rooms that can be reached, never a reason to stay silent
                logger.warning("wake: %s — waking the selected speaker alone", error)
            if on_air:
                # Already listening means the alarm's job — turn the radio on —
                # is done. Rebuilding from here buys nothing and costs the
                # listener their place: the queue is torn down and re-staged
                # around them, and the track they are part-way through goes
                # back to its top until the next tick seeks it home.
                # Re-read the transport rather than the current track: a
                # regroup can hand coordination to a speaker holding another
                # queue and stop the music, which is the one case that still
                # has to rebuild, while a speaker merely mid-transition reports
                # no track and must not be mistaken for silence.
                if self._transport_live(player):
                    # The morning's news still leads as soon as this episode
                    # ends — tick()'s standing insertion rule puts it next.
                    logger.info("wake: leaving playback where it is")
                    return True
                logger.warning("wake: the regroup stopped playback — rebuilding")
        # Nothing playing: a full restart, from silence or from a paused
        # episode. The queue is rebuilt around the news rather than having it
        # inserted, because insertion can only slot news in *behind* the
        # current track — second, not first — and an alarm that turns the
        # radio on has no reason to lead with anything else.
        error = self.start()  # news first, then the interrupted episode, then rotation
        if error == self._NO_SPEAKER:
            logger.warning("wake start failed (no speaker yet): %s", error)
            return False  # transient — retry on a later tick within the grace window
        if error:
            logger.warning("wake start failed: %s", error)
        return True
