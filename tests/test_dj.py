import logging
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer

NOW = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def player() -> FakeSonosPlayer:
    return FakeSonosPlayer()


@pytest.fixture
def dj(db: Database, cfg: Config, player: FakeSonosPlayer) -> DJ:
    return DJ(db, cfg, lambda: player)


def make_feed(db: Database, name: str, n: int, is_news: bool = False,
              hours_old: int = 1, duration: int | None = None) -> int:
    """n episodes, episode 1 oldest; newest is hours_old hours old."""
    fid = db.add_feed(f"https://{name}/rss", name, None, is_news)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{name}-{i}", f"{name} {i}",
                          f"https://cdn/{name}/{i}.mp3",
                          iso(NOW - timedelta(hours=hours_old + n - i)),
                          duration_seconds=duration)
    return fid


def uris_of(player: FakeSonosPlayer) -> list[str]:
    return player.queue_uris()


def current_episode_row(db: Database, player: FakeSonosPlayer):
    """The DB row behind whatever the player has cued up right now."""
    uri = player.queue[player.index]["uri"]
    return next(e for e in db.episodes_with_status("queued")
                if e["play_uri"] == uri)


class TestStart:
    def test_no_speaker_returns_error(self, db: Database, cfg: Config) -> None:
        d = DJ(db, cfg, lambda: None)
        assert "speaker" in d.start().lower()

    def test_nothing_to_play_returns_error(self, dj: DJ) -> None:
        assert "station" in dj.start().lower()

    def test_a_speaker_that_rejects_every_track_is_not_called_on_air(
            self, db, cfg, player, dj) -> None:
        """Resolving is only half of staging. However many URLs come back, if
        the speaker refuses every one of them nothing is playing — so start()
        reports it rather than setting dj_state and leaving a silent house
        listed as on air."""
        make_feed(db, "showa", 5)

        def refuse(*args, **kwargs):
            raise OSError("speaker rejected the track")

        player.add_to_queue = refuse
        assert "station" in dj.start().lower()
        assert db.kv_get("dj_state") != "playing"
        assert player.queue == []

    def test_an_episode_listed_twice_is_staged_once(self, db, cfg, player, dj) -> None:
        """A news episode put back to 'new' by a failed playback is offered
        by fresh_news and still named in the saved order. Staging both lists
        blind would queue it twice and play it twice."""
        make_feed(db, "news", 1, is_news=True)
        make_feed(db, "showa", 3)
        headline = db.episodes_for_feed(1)[0]
        db.set_up_next([headline["id"]])

        assert dj.start() is None

        assert uris_of(player).count(headline["audio_url"]) == 1

    def test_news_first_oldest_first_then_rotation(self, db, cfg, player, dj) -> None:
        make_feed(db, "news", 2, is_news=True)
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        assert dj.start() is None
        q = uris_of(player)
        assert q[0] == "https://cdn/news/1.mp3" and q[1] == "https://cdn/news/2.mp3"
        # news (2) + top-up to QUEUE_AHEAD(3)+1 total minimum
        assert len(q) >= cfg.queue_ahead + 1
        assert player.state == "PLAYING" and player.index == 0
        assert db.kv_get("dj_state") == "playing"
        assert len(db.episodes_with_status("queued")) == len(q)

    def test_stale_news_not_queued(self, db, cfg, player, dj) -> None:
        make_feed(db, "news", 1, is_news=True, hours_old=48)
        make_feed(db, "showa", 3)
        dj.start()
        assert "https://cdn/news/1.mp3" not in uris_of(player)

    def test_failed_episode_is_skipped_and_topped_up_next_tick(
            self, db, cfg, player, dj, monkeypatch) -> None:
        """A dead link is skipped and never enqueued. It is no longer replaced
        inside the same pass — the batch is claimed up front, so the queue
        lands one short and the following tick makes up the shortfall."""
        make_feed(db, "showa", 20)
        import audio

        def flaky(url: str, user_agent: str) -> str:
            if url == "https://cdn/showa/1.mp3":
                raise OSError("dead CDN")
            return url

        monkeypatch.setattr(audio, "resolve_audio_url", flaky)
        assert dj.start() is None
        assert "https://cdn/showa/1.mp3" not in uris_of(player)
        bad = [e for e in db.episodes_for_feed(1) if e["guid"] == "g-showa-1"][0]
        assert bad["status"] == "skipped"
        assert len(uris_of(player)) == cfg.queue_ahead  # one short of full

        dj.tick()
        assert len(uris_of(player)) == cfg.queue_ahead + 1


class TestTick:
    def test_noop_when_stopped(self, db, dj, player) -> None:
        dj.tick()  # dj_state is unset -> treated as stopped; must not raise
        assert player.state == "STOPPED"

    def test_marks_passed_episodes_played(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        dj.start()
        for i in range(2):  # each of the first two tracks actually plays
            player.advance_to(i)
            player.listen(600)
            dj.tick()
        player.advance_to(2)
        dj.tick()
        played = db.recently_played()
        assert len(played) == 2
        assert len(db.episodes_with_status("queued")) >= 1

    def test_hijack_stands_down(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        dj.start()
        player.hijack(["x-sonos-spotify:track1", "x-sonos-spotify:track2"])
        dj.tick()
        assert db.kv_get("dj_state") == "stopped"
        assert db.episodes_with_status("queued") == []
        assert len(db.episodes_with_status("new")) == 5

    def test_news_inserted_after_current(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        dj.start()
        player.advance_to(1)
        nid = make_feed(db, "news", 2, is_news=True)
        dj.tick()
        q = uris_of(player)
        assert q[2] == "https://cdn/news/1.mp3" and q[3] == "https://cdn/news/2.mp3"

    def test_news_not_reinserted_when_next_is_news(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        dj.start()
        make_feed(db, "news", 1, is_news=True)
        dj.tick()
        before = uris_of(player)
        dj.tick()  # news already next -> no duplicate insert
        assert uris_of(player) == before

    def test_top_up_keeps_queue_ahead(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 10)
        make_feed(db, "showb", 10)
        dj.start()
        player.advance_to(2)
        dj.tick()
        assert player.queue_length() - player.index - 1 >= cfg.queue_ahead

    def test_top_up_caps_consecutive_failures_during_outage(self, db, cfg, player,
                                                             dj, monkeypatch) -> None:
        """Simulates the WAN being down while Sonos control (LAN) still
        works: every audio resolve fails. _top_up must stop after a handful
        of consecutive failures rather than marking the entire catalog
        skipped in one tick — there's no "un-skip" path, so draining the
        whole catalog would permanently starve the station."""
        make_feed(db, "showa", 10)
        make_feed(db, "showb", 10)
        import audio

        def always_fail(url: str, user_agent: str) -> str:
            raise OSError("network down")

        monkeypatch.setattr(audio, "resolve_audio_url", always_fail)
        dj.start()
        skipped = db.episodes_with_status("skipped")
        new = db.episodes_with_status("new")
        assert len(skipped) <= 3
        assert len(new) > 0

    def test_resumes_when_stopped_mid_queue(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 10)
        make_feed(db, "showb", 10)
        dj.start()
        player.advance_to(1)
        player.state = "STOPPED"
        dj.tick()
        assert player.state == "PLAYING" and player.index == 1

    def test_a_paused_session_is_not_restarted_by_a_stopped_speaker(
            self, db, cfg, player, dj) -> None:
        """A pause leaves dj_state='playing', so tick() keeps reconciling —
        and STOPPED is what a speaker reports for the first ticks after it
        reboots, not only at the end of a queue. Firmware updates land in the
        small hours, which is when this played the house awake."""
        make_feed(db, "showa", 10)
        dj.start()
        player.listen(600)
        dj.tick()
        dj.pause()
        player.state = "STOPPED"  # the speaker rebooted under us
        player.position = 0
        dj.tick()
        assert player.state == "STOPPED"

    def test_the_pause_outlives_the_process(self, db, cfg, player, dj) -> None:
        """The pause is banked in kv rather than on the instance: a container
        restart that forgot it would leave the next STOPPED free to play."""
        make_feed(db, "showa", 10)
        dj.start()
        player.listen(600)
        dj.tick()
        dj.pause()
        player.state = "STOPPED"
        player.position = 0
        DJ(db, cfg, lambda: player).tick()  # fresh instance, same DB
        assert player.state == "STOPPED"

    def test_play_lifts_the_pause(self, db, cfg, player, dj) -> None:
        """Gating the recovery on a pause must not disable it for good — once
        the listener is back on air a STOPPED speaker is a queue to kick."""
        make_feed(db, "showa", 10)
        dj.start()
        player.listen(600)
        dj.tick()
        dj.pause()
        dj.play()
        player.advance_to(1)
        player.state = "STOPPED"
        dj.tick()
        assert player.state == "PLAYING" and player.index == 1

    def test_recovering_a_stopped_speaker_keeps_the_listeners_place(
            self, db, cfg, player, dj) -> None:
        """play_from_queue restarts the track at its top. Without handing the
        next tick the one-shot resume seek, position tracking banks that zero
        over the real place and the episode replays whole — 98 minutes of it,
        the night this was found."""
        make_feed(db, "showa", 10)
        dj.start()
        dj.tick()
        ep = current_episode_row(db, player)
        player.listen(900)
        dj.tick()
        assert db.get_episode(ep["id"])["resume_seconds"] == 900
        player.state = "STOPPED"  # rebooted mid-episode, still on air
        player.position = 0
        dj.tick()
        assert player.state == "PLAYING"
        dj.tick()
        assert player.seeks[-1] == 890  # resume minus ~10s of context

    def test_resume_seek_happens_once_then_tracks_position(self, db, cfg, player,
                                                           dj) -> None:
        fid = make_feed(db, "showa", 2)
        make_feed(db, "showb", 2)
        dj.start()
        current = db.episodes_with_status("queued")[0]
        cur_uri = player.queue[player.index]["uri"]
        ep = next(e for e in db.episodes_with_status("queued")
                  if e["play_uri"] == cur_uri)
        db.set_resume(ep["id"], 300)
        dj.tick()
        assert player.seeks[-1] == 290  # resume minus ~10s of context
        assert db.get_episode(ep["id"])["resume_seconds"] is None
        player.position = 42
        dj.tick()
        assert db.get_episode(ep["id"])["resume_seconds"] == 42
        assert db.kv_get("resume_episode_id") == str(ep["id"])
        # regression: a third tick on the SAME still-current episode must
        # not re-seek — the one-shot resume seek must not oscillate with
        # position tracking every other tick (it used to rewind ~10s
        # every other tick and the episode would never finish playing).
        player.position = 55
        dj.tick()
        assert player.seeks == [290]  # no additional seek happened
        assert db.get_episode(ep["id"])["resume_seconds"] == 55

    def test_resume_survives_stop_and_restart_same_instance(self, db, cfg,
                                                             player, dj) -> None:
        """Regression: a wake schedule firing start() on the same long-lived
        DJ instance after an overnight stop_off_air() must still fire the
        one-shot resume seek for the re-enqueued interrupted episode. Before
        the fix, self._resume_tracking_episode_id was never reset in
        start(), so tick() thought this episode was "still current" from
        the night before and silently discarded the saved resume position
        instead of seeking to it."""
        make_feed(db, "showa", 3)
        make_feed(db, "showb", 3)
        dj.start()
        cur_uri = player.queue[player.index]["uri"]
        ep = next(e for e in db.episodes_with_status("queued")
                  if e["play_uri"] == cur_uri)
        dj.tick()  # establishes resume-tracking for this episode
        player.position = 321
        dj.stop_off_air()  # saves resume_seconds=321; reverts episode to "new"
        assert db.get_episode(ep["id"])["resume_seconds"] == 321
        dj.start()  # same DJ instance -- a wake schedule firing overnight
        refreshed = db.get_episode(ep["id"])
        assert refreshed["status"] == "queued"
        assert player.queue[player.index]["uri"] == refreshed["play_uri"]
        dj.tick()
        assert player.seeks[-1] == 311  # 321 - 10s of context
        assert db.get_episode(ep["id"])["resume_seconds"] is None

    def test_tick_does_not_oscillate_over_many_ticks(self, db, cfg, player,
                                                      dj) -> None:
        """Simulates real playback: many ticks in a row on the same episode
        with position steadily advancing. Nothing was ever pending resume,
        so no seek should ever fire, and resume_seconds should simply track
        the latest position each tick — guards the whole reconcile loop
        against the tick()-oscillation regression."""
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        dj.start()
        cur_uri = player.queue[player.index]["uri"]
        ep = next(e for e in db.episodes_with_status("queued")
                  if e["play_uri"] == cur_uri)
        # the very first tick on a freshly-started episode establishes which
        # episode is "current" for tracking purposes but doesn't record a
        # position yet (there is no pending resume to seek to either) —
        # from the second tick onward every tick tracks the latest position.
        for i in range(1, 8):
            player.position = i * 10
            dj.tick()
            assert player.seeks == []
            expected = None if i == 1 else i * 10
            assert db.get_episode(ep["id"])["resume_seconds"] == expected

    def test_slow_transport_start_logs_warning(self, db, cfg, player, dj,
                                                monkeypatch, caplog) -> None:
        """Sonos can ack our play command instantly while still taking a
        long time to actually buffer audio from a slow CDN (ad-insertion
        services in particular) -- invisible to our own request timing.
        tick() must flag it once transport finally reaches PLAYING."""
        import time as time_module
        make_feed(db, "showa", 3)
        clock = [1000.0]
        monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
        assert dj.start() is None  # records the transport command at t=1000
        clock[0] = 1030.0  # speaker took 30s to actually start playing
        with caplog.at_level(logging.WARNING):
            dj.tick()
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("slow playback start" in w for w in warnings)
        assert any("30" in w for w in warnings)

    def test_fast_transport_start_no_warning(self, db, cfg, player, dj,
                                              monkeypatch, caplog) -> None:
        import time as time_module
        make_feed(db, "showa", 3)
        clock = [1000.0]
        monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
        assert dj.start() is None
        clock[0] = 1005.0  # well under the slow-start threshold
        with caplog.at_level(logging.WARNING):
            dj.tick()
        assert not any("slow playback start" in r.message for r in caplog.records)


class TestPlayerCurrentSnapshot:
    """_match_queue() already calls player.current() once to resolve
    cur_idx. Callers that then call player.current() again to read
    position/duration take two separate live snapshots of the Sonos
    device — if the track advances between them (a real race right at a
    track boundary), the episode identity from the first call gets paired
    with the position/duration from the second, mismatched, call. Every
    caller must reuse a single snapshot instead."""

    def _count_current_calls(self, monkeypatch, player: FakeSonosPlayer) -> list[int]:
        calls = [0]
        original = player.current

        def counting_current():
            calls[0] += 1
            return original()

        monkeypatch.setattr(player, "current", counting_current)
        return calls

    def test_status_queries_current_exactly_once(self, db, cfg, player, dj,
                                                  monkeypatch) -> None:
        make_feed(db, "showa", 3)
        make_feed(db, "showb", 3)
        dj.start()
        calls = self._count_current_calls(monkeypatch, player)
        dj.status()
        assert calls[0] == 1

    def test_tick_queries_current_exactly_once(self, db, cfg, player, dj,
                                                monkeypatch) -> None:
        make_feed(db, "showa", 3)
        make_feed(db, "showb", 3)
        dj.start()
        calls = self._count_current_calls(monkeypatch, player)
        dj.tick()
        assert calls[0] == 1

    def test_save_resume_queries_current_exactly_once(self, db, cfg, player,
                                                       dj, monkeypatch) -> None:
        make_feed(db, "showa", 3)
        make_feed(db, "showb", 3)
        dj.start()
        calls = self._count_current_calls(monkeypatch, player)
        dj.pause()  # pause() -> _save_resume() -> _match_queue()
        assert calls[0] == 1

    def test_status_position_matches_the_matched_episode(self, db, cfg, player,
                                                          dj, monkeypatch) -> None:
        """If player.current() were queried twice, a track change between
        the two calls would let status() report one episode's identity
        with a different episode's (e.g. freshly-started, position 0)
        timing. A single snapshot makes that structurally impossible."""
        from sonos_ctl import TrackInfo
        make_feed(db, "showa", 3)
        make_feed(db, "showb", 3)
        dj.start()
        player.position = 777
        original_index = player.index
        original_uri = player.queue[original_index]["uri"]

        calls = [0]

        def racy_current():
            calls[0] += 1
            if calls[0] == 1:
                item = player.queue[original_index]
                return TrackInfo(uri=item["uri"], queue_index=original_index,
                                 position=777, duration=player.duration,
                                 title=item["title"])
            # any second call simulates the device having already moved
            # on to the next track by the time it's queried again
            next_index = original_index + 1
            item = player.queue[next_index]
            return TrackInfo(uri=item["uri"], queue_index=next_index,
                             position=0, duration=player.duration,
                             title=item["title"])

        monkeypatch.setattr(player, "current", racy_current)

        data = dj.status()
        np = data["now_playing"]
        assert calls[0] == 1  # only _match_queue()'s internal call happened
        assert np["position"] == 777  # the single snapshot taken, not a later one
        current_episode = next(e for e in db.episodes_with_status("queued")
                               if e["play_uri"] == original_uri)
        assert np["episode_id"] == current_episode["id"]


class TestNowPlayingDuration:
    """Sonos can't measure the length of some streams (Buzzsprout redirects,
    chunked CDNs) and reports duration 0. The feed's itunes:duration is
    already in the DB, so status() must fall back to it — otherwise the
    progress bar pins at 0%, tap-to-seek disables itself, and 'min left'
    is nonsense for those stations."""

    def _current_episode(self, db, player):
        cur_uri = player.queue[player.index]["uri"]
        return next(e for e in db.episodes_with_status("queued")
                    if e["play_uri"] == cur_uri)

    def test_falls_back_to_db_duration_when_sonos_reports_zero(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 2, duration=4840)
        make_feed(db, "showb", 2, duration=4840)
        dj.start()
        player.position = 1363
        player.duration = 0  # Sonos couldn't measure this stream
        np = dj.status()["now_playing"]
        assert np["duration"] == 4840  # kept the feed's known length
        assert np["position"] == 1363

    def test_prefers_sonos_duration_when_present(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 2, duration=4840)
        make_feed(db, "showb", 2, duration=4840)
        dj.start()
        player.duration = 4900  # Sonos knows the actual file length
        np = dj.status()["now_playing"]
        assert np["duration"] == 4900  # live value is authoritative when present

    def test_duration_unknown_from_both_sources_is_none(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 2)  # no duration_seconds set
        make_feed(db, "showb", 2)
        dj.start()
        player.duration = 0
        np = dj.status()["now_playing"]
        # neither the feed nor Sonos knows: duration is None, matching the
        # up_next convention. The frontend treats null and 0 identically.
        assert np["duration"] is None


class TestTopUpBatching:
    """_top_up claims a batch, stages it concurrently, then enqueues. The
    claim is what makes batching safe: pick_next only reads status='new',
    so without it the same episode comes back every time."""

    def test_claim_never_picks_the_same_episode_twice(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        claimed = dj._claim_batch(5)
        ids = [e["id"] for e in claimed]
        assert len(ids) == 5
        assert len(set(ids)) == 5

    def test_claim_stops_when_the_catalog_runs_out(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 2)
        assert len(dj._claim_batch(10)) == 2

    def test_a_claimed_episode_is_invisible_until_staged(self, db, cfg, player, dj) -> None:
        """Claimed means 'queued' with no play_uri. match() already skips
        rows with a falsy play_uri, so the half-staged state can't be
        mistaken for a real queue slot."""
        make_feed(db, "showa", 3)
        claimed = dj._claim_batch(1)[0]
        row = db.get_episode(claimed["id"])
        assert row["status"] == "queued" and row["play_uri"] is None

        player.add_to_queue(claimed["audio_url"], claimed["title"], "showa")
        _, matches, _, _ = dj._match_queue(player)
        assert matches == [None]

    def test_staging_runs_concurrently(self, db, cfg, player, dj, monkeypatch) -> None:
        """Asserted by observing overlap, not by wall-clock timing — a
        timing assertion would be flaky under load."""
        import threading
        import time

        state = {"live": 0, "peak": 0}
        guard = threading.Lock()

        def slow_stage(episode, player_):
            with guard:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(0.05)
            with guard:
                state["live"] -= 1
            return episode["audio_url"], None

        monkeypatch.setattr(dj, "_stage_uri", slow_stage)
        make_feed(db, "showa", 6)
        dj.start()
        assert state["peak"] > 1, "episodes were staged one at a time"

    def test_going_on_air_stages_the_saved_queue_concurrently(
            self, db, cfg, player, dj, monkeypatch) -> None:
        """The saved Up Next is staged by start() itself, never reaching
        _top_up — so it needs its own proof. Serially it was the whole cost
        of an alarm: ten round trips of dead air before the first sound.

        The catalog is exactly the saved list, so _top_up has nothing left to
        claim and cannot be the thing that overlaps."""
        import threading
        import time

        state = {"live": 0, "peak": 0}
        guard = threading.Lock()

        def slow_stage(episode, player_):
            with guard:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(0.05)
            with guard:
                state["live"] -= 1
            return episode["audio_url"], None

        fid = make_feed(db, "showa", 6)
        db.set_up_next([ep["id"] for ep in db.episodes_for_feed(fid)])
        monkeypatch.setattr(dj, "_stage_uri", slow_stage)

        dj.start()

        assert len(player.queue) == 6, "the saved queue was not restored"
        assert state["peak"] > 1, "the saved queue was staged one at a time"

    def test_partial_staging_failure_skips_only_the_dead_links(
            self, db, cfg, player, dj, monkeypatch) -> None:
        make_feed(db, "showa", 6)
        import audio

        def one_dead_link(url: str, user_agent: str) -> str:
            if url.endswith("/2.mp3"):
                raise OSError("dead link")
            return url

        monkeypatch.setattr(audio, "resolve_audio_url", one_dead_link)
        dj.start()
        assert len(db.episodes_with_status("skipped")) == 1
        assert len(db.episodes_with_status("queued")) == 5

    def test_total_staging_failure_returns_every_episode_to_rotation(
            self, db, cfg, player, dj, monkeypatch) -> None:
        """A batch where nothing at all resolves reads as the network being
        down, not as a batch of dead links. Burning ten episodes out of the
        catalog on one bad tick is unrecoverable — there is no un-skip."""
        make_feed(db, "showa", 10)
        make_feed(db, "showb", 10)
        import audio

        def always_fail(url: str, user_agent: str) -> str:
            raise OSError("network down")

        monkeypatch.setattr(audio, "resolve_audio_url", always_fail)
        dj.start()
        assert db.episodes_with_status("skipped") == []
        assert len(db.episodes_with_status("new")) == 20


# ── oversized CDN URLs ────────────────────────────────────────────────

def test_staging_proxies_a_url_too_long_for_sonos(db, cfg, player, monkeypatch) -> None:
    """Sonos keeps only the first 1024 bytes of a queue item's URI and reports
    no error, so a longer URL enqueues truncated and 403s on fetch — the
    episode never plays and Sonos moves to the next track. BBC's signed
    CloudFront links run past 2000 characters. Staging must hand the speaker
    our own /stream/ URL instead."""
    import audio

    dj = DJ(db, cfg, lambda: player)
    make_feed(db, "bbc", 1)
    episode = db.new_episodes_for_feed(db.list_feeds()[0]["id"])[0]

    long_url = "https://cdn.example/ep.mp3?sig=" + "z" * 2000
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, ua: long_url)
    monkeypatch.setattr(audio, "detect_base_url", lambda ip, port: "http://10.0.0.5:8080")

    uri, local_path = dj._stage_uri(episode, player)

    assert uri == f"http://10.0.0.5:8080/stream/{episode['id']}.mp3"
    assert len(uri) <= audio.SONOS_URI_LIMIT
    assert local_path is None  # proxied, not downloaded — nothing to clean up


def test_staging_leaves_a_normal_url_alone(db, cfg, player, monkeypatch) -> None:
    """The proxy is for URLs Sonos cannot hold. Everything else keeps talking
    to the CDN directly, so a server hiccup can't take down playback that has
    no need of us."""
    import audio

    dj = DJ(db, cfg, lambda: player)
    make_feed(db, "showa", 1)
    episode = db.new_episodes_for_feed(db.list_feeds()[0]["id"])[0]

    direct = "https://cdn.example/showa/1.mp3?token=abc"
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, ua: direct)

    uri, local_path = dj._stage_uri(episode, player)

    assert uri == direct
    assert local_path is None


def test_staging_at_exactly_the_limit_is_not_proxied(db, cfg, player, monkeypatch) -> None:
    """1024 bytes is what Sonos stores, so a URL of exactly that length
    survives intact — the boundary is 'longer than', not 'as long as'."""
    import audio

    dj = DJ(db, cfg, lambda: player)
    make_feed(db, "showa", 1)
    episode = db.new_episodes_for_feed(db.list_feeds()[0]["id"])[0]

    base = "https://cdn.example/x.mp3?s="
    exact = base + "y" * (audio.SONOS_URI_LIMIT - len(base))
    assert len(exact) == audio.SONOS_URI_LIMIT
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, ua: exact)

    uri, _ = dj._stage_uri(episode, player)
    assert uri == exact


def test_status_carries_publication_dates(db, cfg, player, dj) -> None:
    """The board shows when an episode aired, on the deck and on every Up Next
    row — an archive spanning a decade is hard to read without it."""
    make_feed(db, "showa", 4)
    dj.start()

    data = dj.status()

    published = {e["id"]: e["published_at"] for e in db.episodes_with_status("queued")}
    np = data["now_playing"]
    assert np["published_at"] == published[np["episode_id"]]
    assert data["up_next"]
    for row in data["up_next"]:
        assert row["published_at"] == published[row["episode_id"]]


# ── playback verification ─────────────────────────────────────────────

class TestPlaybackVerification:
    """The needle moving past a track is not proof it played. Sonos advances
    immediately when it cannot fetch one, and booking that as a completed
    listen loses the episode out of a five-figure backlog for good."""

    def test_episode_that_never_played_goes_back_to_new_not_played(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        dj.start()
        stranded = current_episode_row(db, player)

        # Sonos refused the track and jumped on without reporting a position.
        player.advance_to(1)
        dj.tick()

        row = db.get_episode(stranded["id"])
        assert row["status"] == "new"          # retryable, not consumed
        assert row["failure_count"] == 1
        assert row["last_failed_at"]
        assert db.recently_played() == []

    def test_episode_that_played_is_marked_played(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        dj.start()
        heard = current_episode_row(db, player)

        player.listen(600)
        dj.tick()               # banks the observed position
        player.advance_to(1)
        dj.tick()               # retires it against that evidence

        row = db.get_episode(heard["id"])
        assert row["status"] == "played"
        assert row["failure_count"] == 0
        assert [e["id"] for e in db.recently_played()] == [heard["id"]]

    def test_repeated_failures_give_up_and_stop_cycling(
            self, db, cfg, player, dj) -> None:
        """A permanently dead link must not reappear in the rotation forever."""
        make_feed(db, "showa", 5)
        dj.start()
        doomed = current_episode_row(db, player)
        db.mark_failed(doomed["id"], "2026-01-01T00:00:00Z",
                       DJ._MAX_PLAYBACK_FAILURES)
        db.mark_failed(doomed["id"], "2026-01-01T00:00:01Z",
                       DJ._MAX_PLAYBACK_FAILURES)
        assert db.get_episode(doomed["id"])["status"] == "new"  # still retrying

        spent = db.mark_failed(doomed["id"], "2026-01-01T00:00:02Z",
                               DJ._MAX_PLAYBACK_FAILURES)

        assert spent is True
        assert db.get_episode(doomed["id"])["status"] == "skipped"

    def test_a_manual_skip_is_trusted_without_evidence(
            self, db, cfg, player, dj) -> None:
        """Pressing Done means done. Only the reconcile loop needs proof —
        an explicit instruction is its own evidence."""
        make_feed(db, "showa", 5)
        dj.start()
        target = current_episode_row(db, player)

        assert dj.skip_done() is None

        row = db.get_episode(target["id"])
        assert row["status"] == "played"
        assert row["failure_count"] == 0

    def test_observed_position_never_goes_backwards(self, db, cfg, player, dj) -> None:
        """Sonos reports 0 for a moment while it transitions; a track that has
        demonstrably played must not un-prove itself."""
        make_feed(db, "showa", 3)
        dj.start()
        episode = current_episode_row(db, player)

        db.record_observed(episode["id"], 900)
        db.record_observed(episode["id"], 0)

        assert db.get_episode(episode["id"])["observed_seconds"] == 900

    def test_a_successful_listen_clears_an_earlier_failure(
            self, db, cfg, player, dj) -> None:
        """A CDN blip must not count against an episode that later plays —
        otherwise three unlucky nights retire it permanently."""
        fid = make_feed(db, "showa", 3)
        first = db.new_episodes_for_feed(fid)[0]
        db.mark_failed(first["id"], "2026-01-01T00:00:00Z", 3)
        assert db.get_episode(first["id"])["failure_count"] == 1

        dj.start()
        heard = current_episode_row(db, player)
        assert heard["id"] == first["id"]  # oldest-first, so it's back up
        player.listen(600)
        dj.tick()
        player.advance_to(1)
        dj.tick()

        row = db.get_episode(first["id"])
        assert row["status"] == "played"
        assert row["failure_count"] == 0


# ── queue a whole series ──────────────────────────────────────────────

def make_arc_feed(db: Database) -> int:
    """A feed carrying one three-part story plus two standalone episodes."""
    fid = db.add_feed("https://arc/rss", "Arc Show", None, False)
    titles = ["The Bronze Age Collapse (Part One)", "A Standalone Episode",
              "The Bronze Age Collapse (Part Two)", "Another Standalone",
              "The Bronze Age Collapse (Part Three)"]
    for i, title in enumerate(titles, start=1):
        db.insert_episode(fid, f"g-arc-{i}", title, f"https://cdn/arc/{i}.mp3",
                          iso(NOW - timedelta(hours=20 - i)))
    return fid


class TestQueueArc:
    """arc_key already stops the rotation starting a story at part three.
    This is the same grouping offered as a deliberate action."""

    def test_queues_every_part_in_published_order(self, db, cfg, player, dj) -> None:
        make_arc_feed(db)
        make_feed(db, "showa", 5)
        dj.start()
        part_two = next(e for e in db.episodes_for_feed(1)
                        if "Part Two" in e["title"])

        assert dj.queue_arc(part_two["id"]) is None

        # Every part present exactly once, in published order, however the
        # rotation had scattered them beforehand. Asserted this way rather than
        # on the tail because which track start() left on air is random, and a
        # part already on air is deliberately left where it is.
        staged = uris_of(player)
        for n in (1, 3, 5):
            assert staged.count(f"https://cdn/arc/{n}.mp3") == 1, "moved, not copied"
        positions = [staged.index(f"https://cdn/arc/{n}.mp3") for n in (1, 3, 5)]
        assert positions == sorted(positions), "parts must stay in published order"

    def test_a_part_already_on_air_is_not_queued_again(
            self, db, cfg, player, dj) -> None:
        """"last" has no transport step to retire the needle's own slot, so
        appending the playing part would leave two queue rows for one episode."""
        make_arc_feed(db)
        dj.start()
        player.advance_to(player.queue_uris().index("https://cdn/arc/1.mp3"))
        part_one = next(e for e in db.episodes_for_feed(1)
                        if "Part One" in e["title"])

        assert dj.queue_arc(part_one["id"]) is None

        assert player.queue_uris().count("https://cdn/arc/1.mp3") == 1
        assert player.queue[player.index]["uri"] == "https://cdn/arc/1.mp3"

    def test_refuses_an_episode_with_no_part_marker(self, db, cfg, player, dj) -> None:
        make_arc_feed(db)
        dj.start()
        standalone = next(e for e in db.episodes_for_feed(1)
                          if e["title"] == "A Standalone Episode")

        assert "series" in dj.queue_arc(standalone["id"]).lower()

    def test_already_heard_parts_are_not_dragged_back(self, db, cfg, player, dj) -> None:
        """Asking for the series off part two, having heard part one, queues
        what is left — not the whole story again."""
        make_arc_feed(db)
        rows = {e["title"]: e for e in db.episodes_for_feed(1)}
        db.mark_played(rows["The Bronze Age Collapse (Part One)"]["id"],
                       "2026-01-01T00:00:00Z")
        make_feed(db, "showa", 5)
        dj.start()

        part_two = rows["The Bronze Age Collapse (Part Two)"]
        parts = dj.arc_parts(db.get_episode(part_two["id"]))

        assert [p["title"] for p in parts] == [
            "The Bronze Age Collapse (Part Two)",
            "The Bronze Age Collapse (Part Three)"]

    def test_the_chosen_episode_is_included_even_if_already_played(
            self, db, cfg, player, dj) -> None:
        make_arc_feed(db)
        rows = {e["title"]: e for e in db.episodes_for_feed(1)}
        heard = rows["The Bronze Age Collapse (Part One)"]
        db.mark_played(heard["id"], "2026-01-01T00:00:00Z")

        parts = dj.arc_parts(db.get_episode(heard["id"]))

        assert [p["title"] for p in parts] == [
            "The Bronze Age Collapse (Part One)",
            "The Bronze Age Collapse (Part Two)",
            "The Bronze Age Collapse (Part Three)"]

    def test_yesterdays_evidence_cannot_vouch_for_todays_staging(
            self, db, cfg, player, dj) -> None:
        """The daily cycle that would otherwise resurrect the original bug.

        Stopping keeps Up Next but throws away the Sonos queue, so the next
        start() re-stages the same episode. Evidence must not ride across that
        gap: five minutes heard yesterday would otherwise vouch for a silent
        play today and book an episode the CDN refused as heard."""
        fid = make_feed(db, "showa", 4)
        dj.start()
        episode = current_episode_row(db, player)

        player.listen(300)          # five minutes of it, yesterday
        dj.tick()
        assert db.get_episode(episode["id"])["observed_seconds"] == 300

        dj.stop_off_air()           # Up Next stands; it is still the head of it
        assert db.get_episode(episode["id"])["status"] == "queued"
        assert db.up_next_order()[0] == episode["id"]

        dj.start()                  # re-staged this morning
        assert db.get_episode(episode["id"])["observed_seconds"] is None

        # ...and today it makes no sound at all.
        restaged = player.queue_uris().index(
            db.get_episode(episode["id"])["play_uri"])
        player.advance_to(restaged + 1)
        dj.tick()

        row = db.get_episode(episode["id"])
        assert row["status"] != "played", "stale evidence vouched for a silent play"
        assert row["failure_count"] == 1

    def test_resolves_without_holding_the_lock(self, db, cfg, player, dj,
                                               monkeypatch) -> None:
        """Every part costs a network round trip with a 20-second timeout, and
        the lock serialises tick() and every transport control the dashboard
        can send. Holding it across five of them would freeze the board.

        The probe asks a neutral thread whether the lock is free, never the
        thread doing the resolving: _lock is an RLock, so the caller can
        always re-enter it and would report the lock free while holding it."""
        import audio
        import threading

        make_arc_feed(db)
        dj.start()
        blocked = []

        def probe(url, user_agent):
            seen = {}

            def check():
                got = dj._lock.acquire(blocking=False)
                seen["blocked"] = not got
                if got:
                    dj._lock.release()

            t = threading.Thread(target=check)
            t.start()
            t.join()
            blocked.append(seen["blocked"])
            return url

        monkeypatch.setattr(audio, "resolve_audio_url", probe)
        part_one = next(e for e in db.episodes_for_feed(1)
                        if "Part One" in e["title"])

        assert dj.queue_arc(part_one["id"]) is None

        assert blocked, "expected the parts to be resolved"
        assert not any(blocked), "the lock was held across a network resolve"


# ── the durable Up Next ───────────────────────────────────────────────

class TestDurableUpNext:
    """Up Next survives going off air. The Sonos queue does not — it may have
    had Spotify played through it, and its signed URLs expire within hours —
    so the order is written down and the speaker is re-staged from it."""

    def _up_next(self, db, player):
        by_uri = {e["play_uri"]: e["id"] for e in db.episodes_with_status("queued")}
        return [by_uri[u] for u in player.queue_uris()[player.index:] if u in by_uri]

    def test_the_running_order_is_banked_on_every_tick(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 20)
        dj.start()
        dj.tick()

        assert db.up_next_order() == self._up_next(db, player)

    def test_stop_keeps_up_next_and_going_back_on_restores_it(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 20)
        make_feed(db, "showb", 20)
        dj.start()
        dj.tick()
        before = db.up_next_order()

        dj.stop_off_air()
        assert db.up_next_order() == before      # the list stands
        assert player.state == "STOPPED"

        dj.start()

        # Same episodes, same order, at the head of the rebuilt queue.
        assert self._up_next(db, player)[:len(before)] == before

    def test_the_episode_that_was_playing_leads_the_restored_queue(
            self, db, cfg, player, dj) -> None:
        """What used to need a resume_episode_id hand-off falls out of the
        order: the current track is simply the head of the saved list."""
        make_feed(db, "showa", 20)
        dj.start()
        player.listen(300)
        dj.tick()
        player.advance_to(2)
        dj.tick()
        playing = current_episode_row(db, player)

        dj.stop_off_air()
        dj.start()

        assert current_episode_row(db, player)["id"] == playing["id"]

    def test_only_a_window_is_staged_however_long_the_list(
            self, db, cfg, player, dj) -> None:
        """Signed CDN URLs expire in hours, so staging a long queue deep into
        Sonos would rot it. The speaker holds queue_ahead; the rest waits."""
        fid = make_feed(db, "showa", 60)
        wanted = [ep["id"] for ep in db.new_episodes_for_feed(fid)[:40]]
        db.set_up_next(wanted)

        dj.start()

        assert player.queue_length() <= cfg.queue_ahead + 1  # a window, not the list
        assert db.up_next_order() == wanted                  # which is left whole

    def test_the_list_is_drawn_down_before_rotation(self, db, cfg, player, dj) -> None:
        fid = make_feed(db, "showa", 40)
        make_feed(db, "showb", 40)
        dj.start()
        dj.tick()
        saved = db.up_next_order()

        player.advance_to(3)
        dj.tick()

        # What was already written down stays ahead of anything newly rolled.
        assert self._up_next(db, player)[:len(saved) - 3] == saved[3:]

    def test_an_orphaned_row_is_released_rather_than_stranded(
            self, db, cfg, player, dj) -> None:
        """A row left 'queued' but missing from the order would sit forever:
        never played, and never re-picked, since rotation only draws 'new'."""
        fid = make_feed(db, "showa", 20)
        dj.start()
        dj.tick()
        stranded = db.up_next_order()[-1]
        db.set_up_next([i for i in db.up_next_order() if i != stranded])
        assert db.get_episode(stranded)["status"] == "queued"

        dj.start()
        dj.tick()          # the order is banked on the tick, not on start

        row = db.get_episode(stranded)
        assert row["status"] == "new" or stranded in db.up_next_order(), (
            "released back to the pool, or picked up again — never stranded")


class TestPinAndRefresh:
    """Refresh re-rolls what the DJ chose. The pin is how a row opts out."""

    def test_hand_added_episodes_arrive_pinned(self, db, cfg, player, dj) -> None:
        """Losing an episode you deliberately queued because you did not also
        think to pin it is the silent loss this whole pass exists to stop."""
        fid = make_feed(db, "showa", 20)
        make_feed(db, "showb", 20)
        dj.start()
        wanted = db.new_episodes_for_feed(fid)[0]

        assert dj.play_episode(wanted["id"], "last") is None

        assert db.get_episode(wanted["id"])["pinned"]

    def test_rotation_picks_are_not_pinned(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 20)
        dj.start()
        assert not any(db.get_episode(i)["pinned"] for i in db.up_next_order())

    def test_refresh_rerolls_unpinned_and_keeps_pinned(
            self, db, cfg, player, dj) -> None:
        fid = make_feed(db, "showa", 40)
        make_feed(db, "showb", 40)
        dj.start()
        dj.tick()
        kept = db.up_next_order()[2]
        db.set_pinned(kept, True)
        rerolled = [i for i in db.up_next_order()[1:] if i != kept]

        assert dj.refresh_queue() is None

        order = db.up_next_order()
        assert kept in order, "a pinned row must survive the refresh"
        assert not set(rerolled) & set(order), "unpinned rows must be re-rolled"
        # Released back to the pool. The rotation refill runs in the same
        # call, so one may legitimately have been drawn again already — what
        # matters is that none kept its old place in the list.
        assert all(db.get_episode(i)["status"] in ("new", "queued")
                   for i in rerolled)

    def test_refresh_leaves_the_track_on_air_alone(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 40)
        make_feed(db, "showb", 40)
        dj.start()
        dj.tick()
        playing = current_episode_row(db, player)

        assert dj.refresh_queue() is None

        assert current_episode_row(db, player)["id"] == playing["id"]
        assert db.up_next_order()[0] == playing["id"]

    def test_refresh_refills_the_window(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 40)
        make_feed(db, "showb", 40)
        dj.start()
        dj.tick()

        assert dj.refresh_queue() is None

        assert player.queue_length() - player.index - 1 >= cfg.queue_ahead

    def test_refresh_off_air_still_rerolls_the_saved_list(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 40)
        dj.start()
        dj.tick()
        kept = db.up_next_order()[1]
        db.set_pinned(kept, True)
        dj.stop_off_air()

        assert dj.refresh_queue() is None

        assert db.up_next_order() == [kept] or kept in db.up_next_order()

    def test_a_listened_episode_gives_up_its_pin(self, db, cfg, player, dj) -> None:
        fid = make_feed(db, "showa", 20)
        dj.start()
        episode = current_episode_row(db, player)
        db.set_pinned(episode["id"], True)

        player.listen(600)
        dj.tick()
        player.advance_to(1)
        dj.tick()

        assert db.get_episode(episode["id"])["status"] == "played"
        assert not db.get_episode(episode["id"])["pinned"]

    def test_an_episode_that_never_played_keeps_its_pin(
            self, db, cfg, player, dj) -> None:
        """Clearing the pin on a silent retire would expose a row you chose to
        the very next refresh."""
        fid = make_feed(db, "showa", 20)
        dj.start()
        episode = current_episode_row(db, player)
        db.set_pinned(episode["id"], True)

        player.advance_to(1)     # no audio ever reported
        dj.tick()

        assert db.get_episode(episode["id"])["status"] != "played"
        assert db.get_episode(episode["id"])["pinned"]


class TestRemovalLeavesTheList:
    """Removing an episode has to remove it from the saved order too.

    Status alone cannot say why a row is 'new': "waiting its turn" and "just
    deliberately taken out" look identical, so anything that infers one from
    the other puts dropped episodes straight back."""

    def test_drop_actually_drops(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 40)
        dj.start()
        dj.tick()
        target = db.up_next_order()[1]

        assert dj.drop_from_queue(target) is None
        dj.tick()

        assert target not in db.up_next_order(), "dropped episode came back"

    def test_skip_later_actually_defers(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 40)
        dj.start()
        dj.tick()
        target = db.up_next_order()[0]

        assert dj.skip_later() is None
        dj.tick()

        assert target not in db.up_next_order(), "skipped episode came back"

    def test_a_failed_episode_leaves_the_list_so_the_cooldown_applies(
            self, db, cfg, player, dj) -> None:
        """Left in the order, a failure is re-staged by the very next top-up —
        which draws the list before pick_next and so never sees the 90-minute
        retry cooldown. All three strikes would burn inside a minute."""
        make_feed(db, "showa", 40)
        dj.start()
        dj.tick()
        doomed = db.up_next_order()[0]

        player.advance_to(1)      # no audio ever reported
        dj.tick()

        assert db.get_episode(doomed)["failure_count"] == 1
        assert doomed not in db.up_next_order()
        dj.tick()
        assert db.get_episode(doomed)["failure_count"] == 1, "retried immediately"

    def test_an_interrupted_track_leaves_the_list(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 40)
        make_feed(db, "showb", 40)
        dj.start()
        dj.tick()
        outgoing = db.up_next_order()[0]
        elsewhere = db.new_episodes_for_feed(2)[0]

        assert dj.play_episode(elsewhere["id"], "now") is None
        dj.tick()

        assert outgoing not in db.up_next_order()

    def test_up_next_is_readable_off_air(self, db, cfg, player, dj) -> None:
        """The list exists whether or not it is on the speaker, so the board
        can show and rearrange it before going on air rather than looking
        empty until something plays."""
        make_feed(db, "showa", 40)
        dj.start()
        dj.tick()
        saved = db.up_next_order()

        dj.stop_off_air()

        rows = dj.status()["up_next"]
        assert [e["episode_id"] for e in rows] == saved
        assert all("pinned" in e and "published_at" in e for e in rows)


class TestDeadRowPruning:
    """Sonos keeps every track it has played until the queue is cleared, so a
    long session accumulates slots matching no queued episode. They are inert
    but unbounded, and they are what current_index steps over when it cannot
    match the live URI."""

    def _dead_behind(self, db, player):
        live = {e["play_uri"] for e in db.episodes_with_status("queued")}
        return [u for u in player.queue_uris()[:player.index] if u not in live]

    def test_finished_tracks_are_swept_once_they_pile_up(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 60)
        dj.start()
        # Listen through enough tracks to cross the threshold.
        for i in range(DJ._MAX_DEAD_ROWS + 1):
            player.advance_to(i)
            player.listen(600)
            dj.tick()
        playing = current_episode_row(db, player)
        assert len(self._dead_behind(db, player)) >= DJ._MAX_DEAD_ROWS

        player.advance_to(player.index + 1)
        dj.tick()

        assert len(self._dead_behind(db, player)) < DJ._MAX_DEAD_ROWS
        # and the sweep did not disturb what is playing
        assert current_episode_row(db, player)["id"] != playing["id"]

    def test_a_few_dead_rows_are_left_alone(self, db, cfg, player, dj) -> None:
        """Each removal is its own round trip; they are not worth it until
        they pile up."""
        make_feed(db, "showa", 40)
        dj.start()
        player.listen(600)
        dj.tick()
        player.advance_to(1)
        dj.tick()
        before = player.queue_uris()

        dj.tick()

        assert player.queue_uris() == before

    def test_the_needle_and_everything_ahead_are_untouched(
            self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 60)
        dj.start()
        for i in range(DJ._MAX_DEAD_ROWS + 2):
            player.advance_to(i)
            player.listen(600)
            dj.tick()
        ahead = player.queue_uris()[player.index:]

        dj.tick()

        assert player.queue_uris()[player.index:] == ahead

    def test_an_empty_match_never_sweeps_the_queue(self, db, cfg, player, dj) -> None:
        """A read that matched nothing means the read was bad, not that every
        track is finished — sweeping on it would wipe the session."""
        make_feed(db, "showa", 40)
        dj.start()
        player.advance_to(20)
        matches = [None] * player.queue_length()
        before = player.queue_uris()

        assert dj._prune_dead_rows(player, matches, 20) == 0
        assert player.queue_uris() == before

    def test_pruning_leaves_the_saved_order_alone(self, db, cfg, player, dj) -> None:
        """Dead rows carry no episode, so sweeping them must not change what
        is queued."""
        make_feed(db, "showa", 60)
        dj.start()
        for i in range(DJ._MAX_DEAD_ROWS + 1):
            player.advance_to(i)
            player.listen(600)
            dj.tick()
        before = db.up_next_order()

        player.advance_to(player.index + 1)
        dj.tick()

        after = db.up_next_order()
        assert after[0] == before[1], "the needle moved on by one"
        # Nothing still waiting was lost. The top-up legitimately adds to the
        # tail in the same tick, so this is a subset check the other way.
        assert set(before[1:]) <= set(after), "pruning dropped a queued episode"
