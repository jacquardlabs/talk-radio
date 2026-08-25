from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from dj import DJ, current_index
from fake_player import FakeSonosPlayer
from sonos_ctl import TrackInfo


class RacingPlayer(FakeSonosPlayer):
    """Simulates Sonos advancing to the next track between the two
    _match_queue reads inside play_episode(mode='now'). After add_to_queue
    inserts the target episode at cur_idx+1, the N-th call to current()
    reports Sonos at cur_idx+2 (the track that was originally 'next', now
    shifted by the insert) — exactly what real Sonos does when it finishes
    one track and starts the next at that exact moment."""

    def __init__(self) -> None:
        super().__init__()
        self._current_call_count = 0
        self.race_on_call: int | None = None  # advance on this call number

    def arm_race(self, on_call: int) -> None:
        """Arm the race counting from here, so the call number refers to the
        method under test rather than to every current() read since the player
        was built — setup does its own, and the count must not depend on how
        many."""
        self._current_call_count = 0
        self.race_on_call = on_call

    def current(self) -> TrackInfo | None:
        self._current_call_count += 1
        if (self.race_on_call is not None
                and self._current_call_count == self.race_on_call):
            # Simulate Sonos having advanced to what was the next track before
            # our insert; that track is now at self.index + 2 after X was
            # inserted at self.index + 1.
            target = self.index + 2
            if target < len(self.queue):
                item = self.queue[target]
                return TrackInfo(uri=item["uri"], queue_index=target,
                                 position=0, duration=self.duration,
                                 title=item["title"])
        return super().current()


class TransitioningPlayer(FakeSonosPlayer):
    """Sonos in the moment right after a transport command — the window the
    dashboard lands in, because post() polls /api/status the instant a skip
    or play-now returns. The queue has already changed but AVTransport
    hasn't caught up: `blind` makes current() report no track at all (empty
    track URI, which SonosPlayer.current() turns into None), `stale_index`
    makes it report a playlist position that no longer matches the queue."""

    def __init__(self) -> None:
        super().__init__()
        self.blind = False
        self.stale_index: int | None = None

    def current(self) -> TrackInfo | None:
        if self.blind:
            return None
        cur = super().current()
        if cur is not None and self.stale_index is not None:
            return replace(cur, queue_index=self.stale_index)
        return cur


class LaggingRemovalPlayer(FakeSonosPlayer):
    """The other stale-read ordering: the removal has been acked but hasn't
    propagated, so the queue browse still lists the track we just dropped and
    AVTransport still names it as current. TransitioningPlayer can't produce
    this — its removals land instantly — yet it's the ordering that makes a
    URI match resolve to a slot we've already reconciled."""

    def __init__(self) -> None:
        super().__init__()
        self.lag = False
        self._stale: list[dict[str, str]] | None = None
        self._stale_index = 0

    def settle(self) -> None:
        """Sonos catches up — what the next poll, 5 seconds later, sees."""
        self._stale = None

    def remove_from_queue(self, index0: int) -> None:
        if self.lag and self._stale is None:
            self._stale = list(self.queue)
            self._stale_index = index0
        super().remove_from_queue(index0)

    def queue_uris(self) -> list[str]:
        if self._stale is None:
            return super().queue_uris()
        return [item["uri"] for item in self._stale]

    def queue_length(self) -> int:
        return len(self.queue_uris())  # as SonosPlayer derives it

    def current(self) -> TrackInfo | None:
        if self._stale is None:
            return super().current()
        item = self._stale[self._stale_index]
        return TrackInfo(uri=item["uri"], queue_index=self._stale_index,
                         position=0, duration=self.duration, title=item["title"])


NOW = datetime.now(timezone.utc)


@pytest.fixture
def player() -> FakeSonosPlayer:
    return FakeSonosPlayer()


@pytest.fixture
def dj(db: Database, cfg: Config, player: FakeSonosPlayer) -> DJ:
    return DJ(db, cfg, lambda: player)


def make_feed(db: Database, name: str, n: int, is_news: bool = False) -> int:
    fid = db.add_feed(f"https://{name}/rss", name, None, is_news)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{name}-{i}", f"{name} {i}",
                          f"https://cdn/{name}/{i}.mp3",
                          (NOW - timedelta(hours=n - i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return fid


def current_episode(db: Database, player: FakeSonosPlayer):
    uri = player.queue[player.index]["uri"]
    return next(e for e in db.episodes_with_status("queued") if e["play_uri"] == uri)


def test_play_starts_when_stopped(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    assert dj.play() is None
    assert player.state == "PLAYING" and db.kv_get("dj_state") == "playing"


def test_pause_then_play_resumes_without_restart(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    queue_before = player.queue_uris()
    player.position = 500
    assert dj.pause() is None
    assert player.state == "PAUSED_PLAYBACK"
    ep = current_episode(db, player)
    assert db.get_episode(ep["id"])["resume_seconds"] == 500
    assert dj.play() is None
    assert player.state == "PLAYING"
    assert player.queue_uris() == queue_before  # resumed, not re-started


def test_play_while_transitioning_does_not_rebuild_the_session(db, cfg, player, dj) -> None:
    """Regression: a Play arriving while Sonos is still spinning up a track —
    a client that polled before the skip re-rendered — must not fall through
    to start() and clear the queue out from under it."""
    make_feed(db, "showa", 5)
    dj.start()
    queue_before = player.queue_uris()
    player.state = "TRANSITIONING"
    assert dj.play() is None
    assert player.queue_uris() == queue_before
    assert player.state == "PLAYING"


def test_seek_abs_clamps_to_duration(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 2)
    dj.start()
    player.duration = 100
    dj.seek_abs(500)
    assert player.seeks[-1] == 99
    dj.seek_abs(-3)
    assert player.seeks[-1] == 0


def test_seek_rel_back_and_forward(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 2)
    dj.start()
    player.position = 60
    player.duration = 1800
    dj.seek_rel(-15)
    assert player.seeks[-1] == 45
    player.position = 45
    dj.seek_rel(30)
    assert player.seeks[-1] == 75


def test_seek_without_speaker_errors(db, cfg) -> None:
    d = DJ(db, cfg, lambda: None)
    assert d.seek_abs(0) is not None


def test_skip_later_returns_episode_to_rotation(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    make_feed(db, "showb", 5)
    dj.start()
    ep = current_episode(db, player)
    skipped_uri = player.queue[player.index]["uri"]
    assert dj.skip_later() is None
    after = db.get_episode(ep["id"])
    assert after["status"] == "new" and after["resume_seconds"] is None
    assert skipped_uri not in player.queue_uris()  # removed from Sonos queue
    assert player.state == "PLAYING"


def test_skip_done_marks_played(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    make_feed(db, "showb", 5)
    dj.start()
    ep = current_episode(db, player)
    assert dj.skip_done() is None
    assert db.get_episode(ep["id"])["status"] == "played"
    assert player.state == "PLAYING"


def test_skip_on_last_queued_tops_up_first(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 1)
    dj.start()  # queue holds exactly 1 track
    make_feed(db, "showb", 3)  # new material arrives
    assert dj.skip_done() is None
    assert player.state == "PLAYING"
    assert player.queue_length() >= 1


def test_defer_lands_after_the_news_block(db, cfg, player, dj) -> None:
    """The morning case: podcast on, news landed. Defer puts the podcast back
    behind the whole news block, not back in the backlog."""
    make_feed(db, "showa", 5)
    dj.start()
    make_feed(db, "news", 2, is_news=True)
    dj.tick()  # news rides in at cur_idx + 1
    deferred_uri = player.queue[player.index]["uri"]
    assert dj.defer_current() is None
    q = player.queue_uris()
    assert q[0] == "https://cdn/news/1.mp3"
    assert q[1] == "https://cdn/news/2.mp3"
    assert q[2] == deferred_uri
    assert player.state == "PLAYING"


def test_defer_with_no_news_lands_after_one_item(db, cfg, player, dj) -> None:
    """The floor. A literal "after the news block" with no news would put it
    back at position 0 — next again — and defer would replay it on the spot."""
    make_feed(db, "showa", 5)
    dj.start()
    deferred_uri = player.queue[player.index]["uri"]
    next_uri = player.queue[player.index + 1]["uri"]
    assert dj.defer_current() is None
    q = player.queue_uris()
    assert q[0] == next_uri
    assert q[1] == deferred_uri


def test_defer_banks_resume_from_the_snapshot(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    ep = current_episode(db, player)
    player.position = 742
    assert dj.defer_current() is None
    assert db.get_episode(ep["id"])["resume_seconds"] == 742


def test_defer_keeps_the_episode_queued_and_in_up_next(db, cfg, player, dj) -> None:
    """Where defer and skip_later part company: same prologue, and a tail that
    keeps every piece of state the other one tears down."""
    make_feed(db, "showa", 5)
    dj.start()
    dj.tick()  # bank the order so there is something to still be in
    ep = current_episode(db, player)
    assert ep["id"] in db.up_next_order()
    assert dj.defer_current() is None
    after = db.get_episode(ep["id"])
    assert after["status"] == "queued"
    assert ep["id"] in db.up_next_order()


def test_defer_pins_the_row(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    ep = current_episode(db, player)
    assert db.get_episode(ep["id"])["pinned"] == 0
    assert dj.defer_current() is None
    assert db.get_episode(ep["id"])["pinned"] == 1


def test_defer_reuses_the_staged_uri(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    ep = current_episode(db, player)
    assert dj.defer_current() is None
    after = db.get_episode(ep["id"])
    assert after["play_uri"] == ep["play_uri"]  # no re-resolve, no new token
    assert ep["play_uri"] in player.queue_uris()


def test_defer_with_nothing_else_stops_and_keeps_resume(db, cfg, player, dj) -> None:
    """Pool dry: the station stops rather than replaying it, and the place is
    kept — start() restores from the saved order, news first."""
    make_feed(db, "showa", 1)
    dj.start()  # queue holds exactly 1 track
    ep = current_episode(db, player)
    player.position = 55
    assert dj.defer_current() is None
    assert player.state == "STOPPED"
    after = db.get_episode(ep["id"])
    assert after["status"] == "queued" and after["resume_seconds"] == 55


def test_defer_without_speaker_errors(db, cfg) -> None:
    d = DJ(db, cfg, lambda: None)
    assert d.defer_current() is not None


def test_stop_off_air_keeps_up_next_and_saves_resume(db, cfg, player, dj) -> None:
    """Going off air pauses the station; it does not throw away the queue.
    Up Next is banked, led by whatever was playing, so going back on picks up
    exactly where it stopped."""
    make_feed(db, "showa", 5)
    dj.start()
    ep = current_episode(db, player)
    player.position = 321
    dj.stop_off_air()
    assert player.state == "STOPPED"
    assert db.kv_get("dj_state") == "stopped"
    after = db.get_episode(ep["id"])
    assert after["status"] == "queued" and after["resume_seconds"] == 321
    assert db.up_next_order()[0] == ep["id"]
    assert db.kv_get("resume_episode_id") == str(ep["id"])


def next_queued_episode(db: Database, player: FakeSonosPlayer):
    uri = player.queue[player.index + 1]["uri"]
    return next(e for e in db.episodes_with_status("queued") if e["play_uri"] == uri)


def test_drop_from_queue_recycles_and_removes(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    target = next_queued_episode(db, player)
    before_len = player.queue_length()
    assert dj.drop_from_queue(target["id"]) is None
    after = db.get_episode(target["id"])
    assert after["status"] == "new" and after["play_uri"] is None
    assert all(t["uri"] != target["play_uri"] for t in player.queue)
    assert player.queue_length() >= before_len - 1  # top-up refilled behind it


def test_drop_from_queue_done_marks_played(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    target = next_queued_episode(db, player)
    assert dj.drop_from_queue(target["id"], "done") is None
    after = db.get_episode(target["id"])
    assert after["status"] == "played" and after["played_at"] is not None
    assert all(t["uri"] != target["play_uri"] for t in player.queue)
    assert target["id"] not in db.up_next_order()


def test_drop_from_queue_refuses_current_track(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    cur = current_episode(db, player)
    assert dj.drop_from_queue(cur["id"]) == "That episode isn't in Up Next"
    assert db.get_episode(cur["id"])["status"] == "queued"


def test_drop_from_queue_refuses_unqueued_episode(db, cfg, player, dj) -> None:
    fid = make_feed(db, "showa", 20)  # deeper than QUEUE_AHEAD, so some stay new
    dj.start()
    unqueued = db.oldest_new_for_feed(fid)
    assert dj.drop_from_queue(unqueued["id"]) == "That episode isn't in Up Next"


def test_group_all(db, cfg, player, dj) -> None:
    assert dj.group_all() is None
    assert player.grouped is True


def test_play_episode_next_queues_right_after_current(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)  # excluded from rotation -- proves force-play works on any status
    dj.start()
    cur_idx = player.index
    assert dj.play_episode(archived_id, "next") is None
    assert player.queue[cur_idx + 1]["uri"] == "https://cdn/showb/1.mp3"
    assert player.index == cur_idx  # current track undisturbed
    assert db.get_episode(archived_id)["status"] == "queued"


def test_play_episode_now_interrupts_and_recycles_current(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)
    dj.start()
    interrupted = current_episode(db, player)
    assert dj.play_episode(archived_id, "now") is None
    assert player.queue[player.index]["uri"] == "https://cdn/showb/1.mp3"
    after = db.get_episode(interrupted["id"])
    assert after["status"] == "new" and after["resume_seconds"] is None  # same as skip_later
    assert db.get_episode(archived_id)["status"] == "queued"


def test_play_episode_can_replay_already_played_episode(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 2)
    dj.start()
    first = current_episode(db, player)
    dj.skip_done()
    assert db.get_episode(first["id"])["status"] == "played"
    assert dj.play_episode(first["id"], "now") is None
    assert player.queue[player.index]["uri"] == first["audio_url"]
    assert db.get_episode(first["id"])["status"] == "queued"


def test_play_episode_turns_on_air_when_stopped(db, cfg, player, dj) -> None:
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    assert db.kv_get("dj_state") != "playing"
    assert dj.play_episode(target["id"], "now") is None
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING"
    assert player.queue[player.index]["uri"] == "https://cdn/showa/1.mp3"


def test_play_episode_off_air_with_no_other_content(db, cfg, player, dj) -> None:
    """Regression: if start() finds nothing to auto-queue (no news, no
    resume, no other 'new' rotation content), play_episode must still come
    on air using only the explicitly chosen episode -- not bubble up
    start()'s "nothing to play" error."""
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    db.archive_episode(target["id"])  # nothing left for start()'s own rotation
    assert dj.play_episode(target["id"], "now") is None
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING"
    assert player.queue[player.index]["uri"] == "https://cdn/showa/1.mp3"


def test_play_episode_next_off_air_with_no_other_content(db, cfg, player, dj) -> None:
    """Same scenario as test_play_episode_off_air_with_no_other_content but
    for mode="next": there's no "current" track to queue after, so it must
    still actually start playback, not just mark dj_state "playing" while
    leaving the transport silently stopped."""
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    db.archive_episode(target["id"])  # nothing left for start()'s own rotation
    assert dj.play_episode(target["id"], "next") is None
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING"
    assert player.queue[player.index]["uri"] == "https://cdn/showa/1.mp3"


def test_play_episode_now_on_already_current_restarts_in_place(db, cfg, player, dj) -> None:
    """Regression: forcing "now" on the episode that's already current must
    not re-enqueue+skip itself (which would corrupt its DB status back to
    "new" while it's still the uri actually playing on the speaker) -- it
    should just restart from 0:00."""
    make_feed(db, "showa", 1)
    dj.start()
    current = current_episode(db, player)
    player.position = 500
    assert dj.play_episode(current["id"], "now") is None
    assert player.seeks[-1] == 0
    assert db.get_episode(current["id"])["status"] == "queued"
    assert len(player.queue) == 1  # no duplicate inserted


def test_play_episode_updates_last_feed_id_for_rotation(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)
    dj.start()
    dj.play_episode(archived_id, "next")
    assert db.kv_get("last_feed_id") == str(other)


def test_play_episode_unknown_id_errors(db, cfg, player, dj) -> None:
    assert dj.play_episode(999, "next") == "no such episode"


def test_play_episode_no_speaker_errors(db, cfg) -> None:
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    d = DJ(db, cfg, lambda: None)
    assert "speaker" in d.play_episode(target["id"], "next").lower()


def test_set_volume(db, cfg, player, dj) -> None:
    assert dj.set_volume(75) is None
    assert player.volume == 75


def test_set_volume_without_speaker(db, cfg) -> None:
    d = DJ(db, cfg, lambda: None)
    assert d.set_volume(50) is not None


def test_play_episode_now_does_not_skip_next_track_on_sonos_advance(db, cfg) -> None:
    """Regression: play_episode(mode='now') must not remove the episode that
    was queued as 'next' (B) when Sonos advances to B between the two
    _match_queue reads. The old code called _skip(), which re-read
    _match_queue and could see B at its shifted position (cur_idx+2 after X
    was inserted at cur_idx+1) as 'current', then removed it. The fix
    inlines the advance-and-remove logic using the cur_idx already captured
    in play_episode's own _match_queue, eliminating the race window."""
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)

    # Use a RacingPlayer whose second current() call (inside _skip's
    # _match_queue in the old code) reports Sonos at cur_idx+2 — the
    # position B shifted to after X was inserted at cur_idx+1.
    racing = RacingPlayer()
    dj = DJ(db, cfg, lambda: racing)
    dj.start()

    b_uri = racing.queue[racing.index + 1]["uri"]  # B is initially at index+1
    b_id = next(e["id"] for e in db.episodes_with_status("queued")
                if e["play_uri"] == b_uri)

    # Arm the race: 1st current() is in play_episode's _match_queue (A);
    # 2nd would be in _skip's _match_queue (simulated Sonos advance to B).
    racing.arm_race(2)

    assert dj.play_episode(archived_id, "now") is None

    # The archived episode should now be playing
    assert racing.queue[racing.index]["uri"] == "https://cdn/showb/1.mp3"
    # B must still be queued right after the current track — not removed
    assert racing.queue[racing.index + 1]["uri"] == b_uri
    assert db.get_episode(b_id)["status"] == "queued"


# ── the needle vs. Sonos's own playlist position ──────────────────────

def episode_id_for(db: Database, uri: str) -> int:
    return next(e["id"] for e in db.episodes_with_status("queued")
                if e["play_uri"] == uri)


def mid_session(db: Database, cfg: Config, player=None):
    """A session that has already played one track through: Sonos advanced
    to index 1 and tick() reconciled index 0 to played. Finished tracks are
    never removed from the Sonos queue, so slot 0 is now an unmatched slot —
    which is what makes a needle that falls back to index 0 land on nothing."""
    make_feed(db, "showa", 5)
    make_feed(db, "showb", 5)
    player = player or TransitioningPlayer()
    dj = DJ(db, cfg, lambda: player)
    dj.start()
    player.listen(600)   # track 0 actually plays...
    dj.tick()            # ...and this tick is what banks the evidence
    player.advance_to(1)
    dj.tick()
    queued = {e["play_uri"] for e in db.episodes_with_status("queued")}
    assert player.queue_uris()[0] not in queued  # slot 0 is a finished track
    return player, dj


def test_status_after_skip_when_sonos_reports_no_position(db, cfg) -> None:
    """Regression: the dashboard polls /api/status the instant skip returns,
    catching Sonos mid-transition with no playlist position to report.
    Pinning the needle to index 0 there landed on the finished track that
    is still sitting at the head of the queue — the deck went blank and the
    episode that had just started playing stayed listed under Up Next."""
    player, dj = mid_session(db, cfg)
    assert dj.skip_done() is None
    started = episode_id_for(db, player.queue[player.index]["uri"])

    player.blind = True
    s = dj.status()

    assert s["now_playing"] is not None
    assert s["now_playing"]["episode_id"] == started
    assert started not in [e["episode_id"] for e in s["up_next"]]


def test_status_after_play_now_when_sonos_reports_stale_position(db, cfg) -> None:
    """Same window, the other way Sonos gets it wrong: it still reports the
    playlist position from before play_now removed the interrupted track,
    so the reported index no longer addresses the queue we just read."""
    player, dj = mid_session(db, cfg)
    fid = make_feed(db, "showc", 1)
    target = db.oldest_new_for_feed(fid)["id"]
    db.archive_episode(target)
    assert dj.play_episode(target, "now") is None

    player.stale_index = 0
    s = dj.status()

    assert s["now_playing"] is not None
    assert s["now_playing"]["episode_id"] == target
    assert target not in [e["episode_id"] for e in s["up_next"]]


def test_status_after_skip_when_the_queue_read_still_lags(db, cfg) -> None:
    """Regression: with the removal not yet propagated, the URI Sonos reports
    still resolves to a slot on the queue — but one we already reconciled to
    played. That hit is a stale outgoing reference, not the needle; taking it
    blanks the deck and leaves the started track under Up Next, the very
    symptom this is meant to fix."""
    player = LaggingRemovalPlayer()
    player, dj = mid_session(db, cfg, player)
    started_uri = player.queue[player.index + 1]["uri"]  # what skip will start

    player.lag = True
    assert dj.skip_done() is None
    started = episode_id_for(db, started_uri)

    s = dj.status()
    assert s["now_playing"] is not None
    assert s["now_playing"]["episode_id"] == started
    assert started not in [e["episode_id"] for e in s["up_next"]]

    player.settle()  # and it still reads right once Sonos catches up
    s = dj.status()
    assert s["now_playing"]["episode_id"] == started
    assert started not in [e["episode_id"] for e in s["up_next"]]


def test_status_off_air_stays_blank_when_sonos_reports_no_position(db, cfg) -> None:
    """The mid-transition fallback must not fire off air: a stopped speaker
    also reports no track, and "queued" rows outlive a crash-restart, so
    there the silence means idle — not a track about to start."""
    player, dj = mid_session(db, cfg)
    db.kv_set("dj_state", "stopped")  # crashed off air, queue rows left behind
    player.blind = True
    assert dj.status()["now_playing"] is None


def test_tick_with_no_reported_position_finishes_nothing(db, cfg) -> None:
    """The same fallback runs inside tick(), where a needle that drifts is
    worse than a blank deck: too high and tick() marks unheard episodes
    played. With no position to trust it must stay put."""
    player, dj = mid_session(db, cfg)
    playing = episode_id_for(db, player.queue[player.index]["uri"])
    player.blind = True
    dj.tick()
    assert db.get_episode(playing)["status"] == "queued"


def track(uri: str, queue_index: int) -> TrackInfo:
    return TrackInfo(uri=uri, queue_index=queue_index, position=0,
                     duration=0, title="")


def test_current_index_trusts_the_uri_over_the_reported_position() -> None:
    queue = ["a.mp3", "b.mp3", "c.mp3"]
    matches = [None, "ep-b", "ep-c"]
    assert current_index(queue, matches, track("c.mp3", 0)) == 2


def test_current_index_ignores_a_uri_hit_on_a_reconciled_slot() -> None:
    """b was just skipped: marked played, removed, but both still showing in
    Sonos's lagging reads. A hit on a dead slot is a stale outgoing
    reference, so the needle is the next live slot."""
    queue = ["a.mp3", "b.mp3", "c.mp3"]
    matches = [None, None, "ep-c"]
    assert current_index(queue, matches, track("b.mp3", 1)) == 2


def test_current_index_falls_back_to_first_unreconciled_slot() -> None:
    queue = ["a.mp3", "b.mp3", "c.mp3"]
    matches = [None, "ep-b", "ep-c"]
    assert current_index(queue, matches, None) == 1


def test_current_index_breaks_uri_ties_on_the_reported_position() -> None:
    queue = ["a.mp3", "b.mp3", "a.mp3"]
    matches = ["ep-a", "ep-b", "ep-a"]
    assert current_index(queue, matches, track("a.mp3", 2)) == 2
    assert current_index(queue, matches, track("a.mp3", 0)) == 0


def test_current_index_on_a_queue_that_is_no_longer_ours() -> None:
    queue = ["spotify-1", "spotify-2"]
    assert current_index(queue, [None, None], track("spotify-2", 1)) == 1
    assert current_index(queue, [None, None], None) == 0
    assert current_index([], [], None) == 0


# ── play now / play next on a row that's already queued ───────────────

def test_play_now_moves_an_up_next_episode_instead_of_copying_it(db, cfg, player, dj) -> None:
    """Regression: Play now is almost always pressed on a row in Up Next.
    Staging a second copy left the first one sitting there, so the episode
    read as playing AND upcoming — both queue slots resolve to the same
    episode row, so status() named it twice."""
    make_feed(db, "showa", 6)
    dj.start()
    target = next_queued_episode(db, player)
    uri = target["play_uri"]

    assert dj.play_episode(target["id"], "now") is None

    assert player.queue_uris().count(uri) == 1      # moved, not copied
    assert player.queue[player.index]["uri"] == uri  # and it's what's playing
    s = dj.status()
    assert s["now_playing"]["episode_id"] == target["id"]
    assert target["id"] not in [e["episode_id"] for e in s["up_next"]]


def test_play_next_moves_an_up_next_episode_instead_of_copying_it(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 6)
    dj.start()
    target = next(e for e in db.episodes_with_status("queued")
                  if e["play_uri"] == player.queue[player.index + 2]["uri"])

    assert dj.play_episode(target["id"], "next") is None

    assert player.queue_uris().count(target["play_uri"]) == 1
    assert player.queue[player.index + 1]["uri"] == target["play_uri"]


def test_play_now_clears_a_stray_copy_left_behind_the_needle(db, cfg, player, dj) -> None:
    """A queue that already carries a duplicate — from before this was
    fixed, say — must come out of Play now clean, including a copy sitting
    behind the current track, where tick() would otherwise mark the episode
    played out from under the copy that is actually playing."""
    make_feed(db, "showa", 6)
    dj.start()
    target = next_queued_episode(db, player)
    player.add_to_queue(target["play_uri"], target["title"], "showa", 0)  # stray copy
    player.index += 1
    assert player.queue_uris().count(target["play_uri"]) == 2

    assert dj.play_episode(target["id"], "now") is None

    assert player.queue_uris().count(target["play_uri"]) == 1
    s = dj.status()
    assert s["now_playing"]["episode_id"] == target["id"]
    assert target["id"] not in [e["episode_id"] for e in s["up_next"]]

    dj.tick()
    assert db.get_episode(target["id"])["status"] == "queued"


def test_play_now_purges_every_stale_copy_of_a_played_episode(
        db, cfg, player, dj, monkeypatch) -> None:
    """The live failure, reproduced. P v NP sat in the Sonos queue eight times
    over — one copy per failed Play now — all behind the needle.

    The precondition the old tests missed: the episode is 'played', so its
    play_uri is NULL and it is absent from episodes_with_status('queued').
    That makes every leftover copy invisible to a `matches`-based cleanup, so
    each attempt added one more. Once any copy sits before the needle, tick()
    finishes the episode the instant it runs — it vanishes from Up Next
    without ever playing.

    play_episode must purge by raw queue URI (all copies normalize alike) and
    leave exactly one, at the needle."""
    import audio

    make_feed(db, "showa", 8)
    dj.start()

    first = current_episode(db, player)
    base_uri = first["play_uri"]

    # It was heard once, so the row is 'played' and play_uri is cleared —
    # exactly what mark_played does.
    db.mark_played(first["id"], "2000-01-01T00:00:00Z")

    # Each earlier attempt resolved a fresh expiry token off the same path.
    stale = [f"{base_uri}?Expires={n}&Signature=sig{n}" for n in range(3)]
    for uri in stale:
        player.add_to_queue(uri, first["title"], "showa", 1)
    player.advance_to(5)  # needle well past every copy

    behind = [i for i, u in enumerate(player.queue_uris())
              if audio.uris_match(u, base_uri) and i < player.index]
    assert len(behind) >= 4, "expected several stale copies behind the needle"

    counter = iter(range(100, 200))
    monkeypatch.setattr(audio, "resolve_audio_url",
                        lambda url, ua: f"{url}?Expires={next(counter)}")

    assert dj.play_episode(first["id"], "now") is None

    # Exactly one copy survives, and it is what's playing.
    survivors = [i for i, u in enumerate(player.queue_uris())
                 if audio.uris_match(u, base_uri)]
    assert len(survivors) == 1, f"expected 1 copy, found {len(survivors)}"
    assert survivors[0] == player.index

    # And tick() must leave it alone rather than finishing it on the spot.
    dj.tick()
    assert db.get_episode(first["id"])["status"] == "queued"


def test_play_now_doesnt_restart_in_place_on_a_guessed_needle(
        db, cfg, player, dj, monkeypatch) -> None:
    """The seek_abs(0) shortcut may only fire when the needle was pinned by a
    real URI hit. current_index() falls back to 'first slot still holding a
    queued episode' when it can't match, and taking that guess as proof the
    episode is already playing is what restarted an expired copy — silence,
    then Sonos wandering on to the next track."""
    import audio

    make_feed(db, "showa", 5)
    dj.start()
    target = current_episode(db, player)

    # Sonos reports a URI that is in no queue slot, so current_index() has to
    # guess — it returns the first queued slot, which is this episode's.
    player.current_uri_override = "https://cdn/elsewhere/unknown.mp3"
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, ua: url + "?fresh=1")

    seeks: list[int] = []
    monkeypatch.setattr(dj, "seek_abs", lambda s: seeks.append(s))

    assert dj.play_episode(target["id"], "now") is None

    assert seeks == [], "must not restart in place on a guessed needle"
    assert player.queue[player.index]["uri"].endswith("?fresh=1"), (
        "a freshly staged copy must be playing"
    )


# ── reordering Up Next ────────────────────────────────────────────────

def up_next_ids(dj: DJ) -> list[int]:
    return [e["episode_id"] for e in dj.status()["up_next"]]


def test_move_in_queue_moves_a_row_down(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    before = up_next_ids(dj)
    assert dj.move_in_queue(before[0], 2) is None
    after = up_next_ids(dj)
    assert after[:3] == [before[1], before[2], before[0]]
    assert sorted(after) == sorted(before)  # nothing gained or lost


def test_move_in_queue_moves_a_row_up(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    before = up_next_ids(dj)
    assert dj.move_in_queue(before[3], 0) is None
    after = up_next_ids(dj)
    assert after[:4] == [before[3], before[0], before[1], before[2]]


def test_move_in_queue_to_last(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    before = up_next_ids(dj)
    assert dj.move_in_queue(before[0], len(before) - 1) is None
    assert up_next_ids(dj)[-1] == before[0]


def test_move_in_queue_onto_itself_is_a_noop(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    before = up_next_ids(dj)
    assert dj.move_in_queue(before[1], 1) is None
    assert up_next_ids(dj) == before


def test_move_in_queue_clamps_out_of_range(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    before = up_next_ids(dj)
    assert dj.move_in_queue(before[0], 999) is None
    assert up_next_ids(dj)[-1] == before[0]
    assert dj.move_in_queue(before[0], -5) is None
    assert up_next_ids(dj)[0] == before[0]


def test_move_in_queue_refuses_the_current_track(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    cur = current_episode(db, player)
    assert dj.move_in_queue(cur["id"], 2) == "That episode isn't in Up Next"


def test_move_in_queue_refuses_an_unqueued_episode(db, cfg, player, dj) -> None:
    fid = make_feed(db, "showa", 20)
    dj.start()
    unqueued = db.oldest_new_for_feed(fid)
    assert dj.move_in_queue(unqueued["id"], 0) == "That episode isn't in Up Next"


def test_move_in_queue_reuses_the_staged_uri(db, cfg, player, dj, monkeypatch) -> None:
    """A move is remove-and-re-add on the URI already staged — it must not
    re-resolve, which would be a network round trip per drag."""
    make_feed(db, "showa", 20)
    dj.start()
    target_id = up_next_ids(dj)[0]
    before = db.get_episode(target_id)["play_uri"]

    import audio
    monkeypatch.setattr(audio, "resolve_audio_url",
                        lambda url, ua: pytest.fail("re-staged on a move"))
    assert dj.move_in_queue(target_id, 3) is None
    assert db.get_episode(target_id)["play_uri"] == before


def test_manual_order_survives_a_tick_top_up(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    make_feed(db, "showb", 20)
    dj.start()
    before = up_next_ids(dj)
    dj.move_in_queue(before[0], 4)
    arranged = up_next_ids(dj)
    dj.tick()
    after = up_next_ids(dj)
    assert after[:len(arranged)] == arranged  # top-ups append below


def test_news_still_rides_ahead_of_a_manual_order(db, cfg, player, dj) -> None:
    """News insertion is deliberately not subordinate to a hand-made
    arrangement — the board says News rides first and it still does."""
    make_feed(db, "showa", 20)
    dj.start()
    dj.move_in_queue(up_next_ids(dj)[0], 3)
    arranged = up_next_ids(dj)

    news_fid = db.add_feed("https://news/rss", "News", None, True)
    db.insert_episode(news_fid, "n-1", "Headlines", "https://cdn/news/1.mp3",
                      NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    dj.tick()

    after = up_next_ids(dj)
    news_id = next(e["id"] for e in db.episodes_for_feed(news_fid))
    assert after[0] == news_id
    assert after[1:len(arranged) + 1] == arranged  # order below it intact


# ── adding from the backlog ───────────────────────────────────────────

def test_play_last_appends_to_the_end_of_up_next(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    other = make_feed(db, "showb", 1)
    target = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(target)  # any status, same as the other force-play modes
    dj.start()
    before = up_next_ids(dj)

    assert dj.play_episode(target, "last") is None

    after = up_next_ids(dj)
    assert after[:len(before)] == before   # nothing above it disturbed
    assert after[-1] == target
    assert db.get_episode(target)["status"] == "queued"


def test_play_last_moves_rather_than_copies_an_already_queued_episode(
        db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    dj.start()
    target = up_next_ids(dj)[0]
    uri = db.get_episode(target)["play_uri"]

    assert dj.play_episode(target, "last") is None

    assert player.queue_uris().count(uri) == 1
    assert up_next_ids(dj)[-1] == target


def test_play_last_comes_on_air_when_stopped(db, cfg, player, dj) -> None:
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)["id"]
    db.archive_episode(target)
    assert dj.play_episode(target, "last") is None
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING"
    assert player.queue[player.index]["uri"] == "https://cdn/showa/1.mp3"


def test_play_last_updates_last_feed_id(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 20)
    other = make_feed(db, "showb", 1)
    target = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(target)
    dj.start()
    dj.play_episode(target, "last")
    assert db.kv_get("last_feed_id") == str(other)


# ── sleep timer ───────────────────────────────────────────────────────


def test_sleep_timer_requires_being_on_air(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    assert "Not on air" in dj.set_sleep_timer("fade", 30)
    assert db.kv_get("sleep_mode") is None


def test_sleep_fade_steps_volume_down_and_goes_off_air(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    player.volume = 40
    assert dj.set_sleep_timer("fade", 1) is None      # 60s, four 15s ticks
    player.state = "PLAYING"
    dj.tick()
    assert player.volume == 30                        # 45/60 of 40
    dj.tick()
    assert player.volume == 20
    dj.tick()
    assert player.volume == 10
    dj.tick()                                         # deadline: off air
    assert db.kv_get("dj_state") == "stopped"
    assert player.state == "STOPPED"
    # the volume the fade borrowed is handed back, so the morning alarm is
    # not what discovers it was left at 10
    assert player.volume == 40
    assert db.kv_get("sleep_mode") is None


def test_sleep_fade_holds_while_paused(db, cfg, player, dj) -> None:
    """A paused station is already quiet. Burning the countdown down through
    a pause would put it off air the moment someone pressed Play."""
    make_feed(db, "showa", 3)
    dj.start()
    player.volume = 40
    dj.set_sleep_timer("fade", 1)
    dj.pause()
    for _ in range(6):
        dj.tick()
    assert db.kv_get("dj_state") == "playing"
    assert db.kv_get("sleep_remaining") == "60"
    assert player.volume == 40


def test_sleep_episode_goes_off_air_at_the_end_of_the_track(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    player.duration = 1800
    player.position = 100
    assert dj.set_sleep_timer("episode") is None
    player.state = "PLAYING"
    dj.tick()
    assert db.kv_get("dj_state") == "playing"          # nowhere near the end
    player.position = 1790                             # 10s left, inside a tick
    dj.tick()
    assert db.kv_get("dj_state") == "stopped"
    assert db.kv_get("sleep_mode") is None


def test_sleep_episode_refused_when_nothing_knows_the_length(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    player.duration = 0                                # chunked CDN, no length
    assert "no length" in dj.set_sleep_timer("episode")
    assert db.kv_get("sleep_mode") is None


def test_sleep_episode_uses_the_feed_duration_when_sonos_reports_none(
        db, cfg, player, dj) -> None:
    fid = db.add_feed("https://showa/rss", "showa", None, False)
    db.insert_episode(fid, "g-1", "showa 1", "https://cdn/showa/1.mp3",
                      NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), 600)
    dj.start()
    player.duration = 0
    player.position = 60
    assert dj.set_sleep_timer("episode") is None
    player.state = "PLAYING"
    dj.tick()
    assert db.kv_get("dj_state") == "playing"
    player.position = 595
    dj.tick()
    assert db.kv_get("dj_state") == "stopped"


def test_going_on_air_clears_a_timer_from_the_last_session(db, cfg, player, dj) -> None:
    """Regression guard for the wake alarm: a countdown left armed overnight
    would take the morning off air minutes after it started."""
    make_feed(db, "showa", 3)
    dj.start()
    player.volume = 40
    dj.set_sleep_timer("fade", 30)
    dj.tick()
    dj.stop_off_air()
    assert db.kv_get("sleep_mode") is None
    dj.start()
    assert db.kv_get("sleep_mode") is None
    assert player.volume == 40


def test_cancel_sleep_timer_restores_the_volume(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    player.volume = 40
    dj.set_sleep_timer("fade", 1)
    dj.tick()
    assert player.volume == 30
    assert dj.cancel_sleep_timer() is None
    assert player.volume == 40
    assert db.kv_get("sleep_mode") is None


def test_rearming_replaces_and_does_not_compound_the_fade(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    player.volume = 40
    dj.set_sleep_timer("fade", 1)
    dj.tick()
    assert player.volume == 30
    dj.set_sleep_timer("fade", 2)
    # the second fade ramps from the original 40, not from the faded 30
    assert player.volume == 40
    assert db.kv_get("sleep_volume") == "40"
    assert db.kv_get("sleep_remaining") == "120"


def test_sleep_timer_rejects_bad_input(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    assert "minutes must be" in dj.set_sleep_timer("fade", 0)
    assert "minutes must be" in dj.set_sleep_timer("fade", 999)
    assert "minutes must be" in dj.set_sleep_timer("fade", None)
    assert "unknown sleep mode" in dj.set_sleep_timer("nap")
    assert db.kv_get("sleep_mode") is None


def test_status_reports_the_armed_timer(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 3)
    dj.start()
    assert dj.status()["sleep"] is None
    dj.set_sleep_timer("fade", 30)
    assert dj.status()["sleep"] == {"mode": "fade", "remaining": 1800, "total": 1800}
    dj.cancel_sleep_timer()
    player.duration = 1800
    player.position = 300
    dj.set_sleep_timer("episode")
    sleep = dj.status()["sleep"]
    assert sleep["mode"] == "episode" and sleep["remaining"] == 1500
