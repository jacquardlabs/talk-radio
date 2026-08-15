import threading
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from dj import DJ, next_start, parse_days, schedule_due
from fake_player import FakeSonosPlayer

# Mon 2026-07-06 08:03 local — a Monday
MON_0803 = datetime(2026, 7, 6, 8, 3)


def make_schedule(db: Database, time_str: str = "08:00",
                  days: list[int] = [0, 1, 2, 3, 4]):
    db.add_schedule(time_str, days)
    return db.list_schedules()[-1]


def test_parse_days() -> None:
    assert parse_days("0,2,6") == {0, 2, 6}


def test_due_within_grace(db: Database) -> None:
    s = make_schedule(db)
    assert schedule_due(s, MON_0803, grace_minutes=10) is True


def test_not_due_before_time(db: Database) -> None:
    s = make_schedule(db)
    assert schedule_due(s, MON_0803.replace(hour=7), 10) is False


def test_not_due_past_grace(db: Database) -> None:
    s = make_schedule(db)
    # server down all morning: 15:00 must not blast podcasts
    assert schedule_due(s, MON_0803.replace(hour=15, minute=0), 10) is False


def test_not_due_wrong_day(db: Database) -> None:
    s = make_schedule(db, days=[5, 6])  # weekend alarm, Monday now
    assert schedule_due(s, MON_0803, 10) is False


def test_not_due_when_disabled_or_already_fired(db: Database) -> None:
    s = make_schedule(db)
    db.set_last_fired(s["id"], MON_0803.date().isoformat())
    assert schedule_due(db.list_schedules()[0], MON_0803, 10) is False
    s2 = make_schedule(db, time_str="08:01")
    db.toggle_schedule(s2["id"])
    assert schedule_due(db.list_schedules()[-1], MON_0803, 10) is False


def test_next_start_same_day_and_week_wrap(db: Database) -> None:
    make_schedule(db, "09:00", [0])          # later today (Mon)
    assert next_start(db.list_schedules(), MON_0803).hour == 9
    db.delete_schedule(db.list_schedules()[0]["id"])
    make_schedule(db, "07:00", [0])          # already passed -> next Monday
    nxt = next_start(db.list_schedules(), MON_0803)
    assert nxt.weekday() == 0 and (nxt.date() - MON_0803.date()).days == 7


def test_next_start_none_without_schedules(db: Database) -> None:
    assert next_start(db.list_schedules(), MON_0803) is None


class RecordingPlayer(FakeSonosPlayer):
    """Logs the calls the wake path makes, so their order can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.group_error: Exception | None = None

    def group_all(self) -> None:
        self.calls.append("group_all")
        if self.group_error is not None:
            raise self.group_error
        super().group_all()

    def clear_queue(self) -> None:
        self.calls.append("clear_queue")
        super().clear_queue()

    def play_from_queue(self, index0: int) -> None:
        self.calls.append("play_from_queue")
        super().play_from_queue(index0)


class RegroupBlindsPlayer(RecordingPlayer):
    """Grouping leaves Sonos mid-transition, reporting no current track —
    partymode moves coordination and the speaker needs a beat to re-settle.
    Anything that has to read what's playing must do so before the regroup."""

    def group_all(self) -> None:
        super().group_all()
        self.blind = True

    def current(self):
        return None if getattr(self, "blind", False) else super().current()


class RegroupSilencesPlayer(FakeSonosPlayer):
    """Grouping moved coordination onto a speaker holding a different queue,
    so the music we were playing stops — the case a wake has to notice and
    play through, rather than mark the alarm fired on silence."""

    def group_all(self) -> None:
        super().group_all()
        self.state = "STOPPED"
        self.queue = []
        self.index = 0


@pytest.fixture
def wake_env(db: Database, cfg: Config, monkeypatch, request):
    player = getattr(request, "param", FakeSonosPlayer)()
    dj = DJ(db, cfg, lambda: player)
    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_all", lambda db_, cfg_: None)
    monkeypatch.setattr(dj_mod.feeds, "refresh_news", lambda db_, cfg_: None)
    fid = db.add_feed("https://showa/rss", "showa", None, False)
    for i in range(1, 4):
        db.insert_episode(fid, f"g{i}", f"ep{i}", f"https://cdn/showa/{i}.mp3",
                          f"2026-01-0{i}T00:00:00Z")
    return db, dj, player


def test_fire_starts_when_stopped(wake_env) -> None:
    db, dj, player = wake_env
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.state == "PLAYING"
    assert db.kv_get("dj_state") == "playing"
    assert db.list_schedules()[0]["last_fired_date"] == MON_0803.date().isoformat()


def test_fire_only_once_per_day(wake_env) -> None:
    db, dj, player = wake_env
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    player.stop()
    db.kv_set("dj_state", "stopped")
    dj.check_schedules(now=MON_0803 + timedelta(minutes=2))
    assert player.state == "STOPPED"  # did not refire


def add_news_feed(db: Database, when=None) -> str:
    """A news feed with one episode, as if the refresh just pulled it.
    Returns its audio url — which doubles as its play_uri, since URL
    resolution is the identity in tests."""
    fid = db.add_feed("https://news/rss", "News", None, True)
    published = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.insert_episode(fid, "n-1", "Headlines", "https://cdn/news/1.mp3", published)
    return "https://cdn/news/1.mp3"


def playing_episode(db: Database, player: FakeSonosPlayer):
    uri = player.queue[player.index]["uri"]
    return next(e for e in db.episodes_with_status("queued") if e["play_uri"] == uri)


class TestWarmUp:
    """A few minutes before an alarm, the URLs it will need are resolved and
    thrown away — the caches the resolution leaves behind are the point."""

    def resolves(self, dj, monkeypatch) -> list:
        seen: list = []
        import dj as dj_mod
        monkeypatch.setattr(dj_mod.audio, "resolve_audio_url",
                            lambda url, ua: seen.append(url) or url)
        return seen

    def test_warms_the_urls_the_alarm_will_stage(self, wake_env, monkeypatch) -> None:
        db, dj, player = wake_env
        news_uri = add_news_feed(db)
        db.set_up_next([e["id"] for e in db.episodes_with_status("new")])
        make_schedule(db)
        seen = self.resolves(dj, monkeypatch)

        dj.warm_next_start(now=MON_0803.replace(hour=7, minute=57))

        assert news_uri in seen                       # the headlines lead
        assert len(seen) == len(db.up_next_order())   # and the list behind them

    def test_warms_once_not_every_tick_in_the_window(self, wake_env, monkeypatch) -> None:
        """check_schedules and its neighbours run every TICK_SECONDS. A flag
        set after the work would let each tick in the window start its own."""
        db, dj, player = wake_env
        db.set_up_next([e["id"] for e in db.episodes_with_status("new")])
        make_schedule(db)
        seen = self.resolves(dj, monkeypatch)

        for minute in (56, 57, 58, 59):
            dj.warm_next_start(now=MON_0803.replace(hour=7, minute=minute))

        assert len(seen) == len(db.up_next_order())

    def test_does_not_warm_outside_the_window(self, wake_env, monkeypatch) -> None:
        db, dj, player = wake_env
        db.set_up_next([e["id"] for e in db.episodes_with_status("new")])
        make_schedule(db)
        seen = self.resolves(dj, monkeypatch)

        dj.warm_next_start(now=MON_0803.replace(hour=6, minute=30))  # too early
        dj.warm_next_start(now=MON_0803)                             # already fired past
        assert seen == []

    def test_a_url_that_fails_to_warm_is_not_marked(self, wake_env, monkeypatch) -> None:
        """The alarm's own staging decides what is dead. A warm-up that
        skipped episodes on a transient failure would empty the very queue
        it exists to speed up."""
        db, dj, player = wake_env
        db.set_up_next([e["id"] for e in db.episodes_with_status("new")])
        make_schedule(db)
        import dj as dj_mod

        def refuse(url, ua):
            raise OSError("resolver down")

        monkeypatch.setattr(dj_mod.audio, "resolve_audio_url", refuse)

        dj.warm_next_start(now=MON_0803.replace(hour=7, minute=57))

        assert db.episodes_with_status("skipped") == []
        assert len(db.episodes_with_status("new")) == 3

    def test_warming_needs_no_speaker(self, wake_env, monkeypatch) -> None:
        """Resolution is HTTP, not Sonos. An alarm whose speaker is still
        asleep at 07:57 must warm anyway — that is the morning it helps."""
        db, dj, player = wake_env
        dj.get_player = lambda: None
        db.set_up_next([e["id"] for e in db.episodes_with_status("new")])
        make_schedule(db)
        seen = self.resolves(dj, monkeypatch)

        dj.warm_next_start(now=MON_0803.replace(hour=7, minute=57))

        assert len(seen) == len(db.up_next_order())


def test_fire_leaves_an_episode_in_progress_alone(wake_env) -> None:
    """An alarm that finds the radio already on has nothing to turn on.
    Rebuilding from there tears the queue down around a listener who is
    listening, and sends the track they're part-way through back to its top —
    which is what an 07:45 start and an 08:00 alarm did to a news episode."""
    db, dj, player = wake_env
    dj.start()                      # already on air, part-way into a show
    player.listen(700)
    playing = playing_episode(db, player)
    queue_before = player.queue_uris()
    add_news_feed(db)               # the headlines land overnight

    make_schedule(db)
    dj.check_schedules(now=MON_0803)

    assert player.queue_uris() == queue_before      # queue untouched
    assert player.position == 700                   # not sent back to the top
    assert player.state == "PLAYING" and player.index == 0
    assert playing_episode(db, player)["id"] == playing["id"]
    # resolved, not deferred: no later tick in the grace window may refire it
    assert db.list_schedules()[0]["last_fired_date"] == MON_0803.date().isoformat()


def test_a_wake_that_leaves_playback_alone_still_puts_news_next(wake_env) -> None:
    """Leaving playback be is only defensible because the news still gets in:
    tick()'s standing insertion rule slots it directly behind the current
    track, so the morning's headlines lead the moment this episode ends."""
    db, dj, player = wake_env
    dj.start()
    player.listen(700)
    news_uri = add_news_feed(db)
    make_schedule(db)

    dj.check_schedules(now=MON_0803)
    dj.tick()

    assert player.queue[player.index]["uri"] != news_uri       # still on the show
    assert player.queue[player.index + 1]["uri"] == news_uri   # news is up next


@pytest.mark.parametrize("wake_env", [RegroupSilencesPlayer], indirect=True)
def test_fire_banks_the_position_before_tearing_the_queue_down(wake_env) -> None:
    """A regroup that silences the music forces the rebuild the wake had
    declined — and by then the speaker can report nothing, so the position
    must already be banked or the episode comes back from 0:00 having lost
    the listener's place. tick() only writes resume_seconds once per pass, so
    whatever it last wrote can be a whole tick stale."""
    db, dj, player = wake_env
    dj.start()
    interrupted = playing_episode(db, player)
    db.set_resume(interrupted["id"], 5)   # a stale value from an earlier tick
    player.position = 640                 # where the listener actually is
    add_news_feed(db)
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert db.get_episode(interrupted["id"])["resume_seconds"] == 640


def test_fire_takes_over_a_hijacked_queue(wake_env) -> None:
    """dj_state outlives a hijack by a tick: someone put Spotify on and the
    stand-down hasn't run yet. There is no playback of ours to leave alone,
    so the alarm rebuilds and takes the speaker back."""
    db, dj, player = wake_env
    dj.start()
    player.hijack(["x-sonos-spotify:track1"])
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.queue_uris() != ["x-sonos-spotify:track1"]
    assert player.state == "PLAYING"


def test_fire_restarts_when_paused_mid_episode(wake_env) -> None:
    db, dj, player = wake_env
    dj.start()
    player.position = 200
    dj.pause()
    interrupted = db.kv_get("resume_episode_id")
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.state == "PLAYING" and player.index == 0
    # interrupted episode is staged (news would come first if any existed)
    staged = [e["id"] for e in db.episodes_with_status("queued")]
    assert int(interrupted) in staged


def test_fire_defers_and_retries_when_speaker_not_yet_discovered(wake_env) -> None:
    """Reboot-recovery scenario: the speaker isn't discoverable at the first
    post-reboot tick. The once-per-day token must NOT be burned so a later
    tick within the grace window can retry — otherwise the schedule silently
    never fires that day."""
    db, dj, player = wake_env
    holder = {"player": None}
    dj.get_player = lambda: holder["player"]
    make_schedule(db)

    dj.check_schedules(now=MON_0803)
    assert player.state != "PLAYING"
    assert db.list_schedules()[0]["last_fired_date"] is None  # not marked fired

    holder["player"] = player  # speaker discovered a couple minutes later
    dj.check_schedules(now=MON_0803 + timedelta(minutes=2))
    assert player.state == "PLAYING"
    assert db.list_schedules()[0]["last_fired_date"] == MON_0803.date().isoformat()


def test_fire_gathers_every_speaker(wake_env) -> None:
    db, dj, player = wake_env
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.grouped is True
    assert player.state == "PLAYING"


def test_fire_gathers_speakers_even_when_already_playing(wake_env) -> None:
    """A wake wakes the house whether or not the selected room is already on
    air — the other rooms still have to join."""
    db, dj, player = wake_env
    dj.start()
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.grouped is True
    assert player.state == "PLAYING"


@pytest.mark.parametrize("wake_env", [RecordingPlayer], indirect=True)
def test_fire_gathers_speakers_before_building_the_queue(wake_env) -> None:
    """Order matters: the queue lives on the group coordinator and grouping
    is what decides which speaker that is. Build first and the queue is
    stranded on whoever was coordinating beforehand."""
    db, dj, player = wake_env
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.calls.index("group_all") < player.calls.index("clear_queue")
    assert player.calls.index("group_all") < player.calls.index("play_from_queue")


@pytest.mark.parametrize("wake_env", [RecordingPlayer], indirect=True)
def test_fire_wakes_the_selected_speaker_when_grouping_fails(wake_env) -> None:
    """A speaker that's off or unreachable must not silence the alarm."""
    db, dj, player = wake_env
    player.group_error = OSError("Bedroom unreachable")
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.state == "PLAYING"
    assert db.list_schedules()[0]["last_fired_date"] == MON_0803.date().isoformat()


@pytest.mark.parametrize("wake_env", [RegroupSilencesPlayer], indirect=True)
def test_fire_plays_through_a_regroup_that_stopped_playback(wake_env) -> None:
    """Regrouping can hand coordination to a speaker holding a different
    queue, which stops the music. The alarm must end up playing regardless —
    never marked fired on silence."""
    db, dj, player = wake_env
    dj.start()
    assert player.state == "PLAYING"
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.grouped is True
    assert player.state == "PLAYING"
    assert player.queue_length() > 0


def test_wake_refreshes_news_only_not_the_whole_library(wake_env, monkeypatch) -> None:
    """Fetching is serial with a 30s timeout apiece, so a full-library
    refresh costs minutes. A wake needs the morning's headlines; the shows it
    queues behind them come from the DB and the standing refresh keeps those
    current."""
    db, dj, player = wake_env
    called = []
    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_news", lambda d, c: called.append("news"))
    monkeypatch.setattr(dj_mod.feeds, "refresh_all", lambda d, c: called.append("all"))
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert called == ["news"]


def test_wake_refreshes_without_holding_the_lock(wake_env, monkeypatch) -> None:
    """The refresh is network I/O, and the DJ lock serializes tick() plus
    every transport control the dashboard can send. Holding it across the
    fetch is what froze the UI at alarm time."""
    db, dj, player = wake_env
    seen = {}

    def probe(db_, cfg_):
        # from another thread: the lock is re-entrant, so the firing thread
        # could re-acquire it and learn nothing
        def check():
            got = dj._lock.acquire(blocking=False)
            seen["free"] = got
            if got:
                dj._lock.release()
        t = threading.Thread(target=check)
        t.start()
        t.join()

    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_news", probe)
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert seen["free"] is True


def test_no_schedule_due_means_no_fetch(wake_env, monkeypatch) -> None:
    """check_schedules runs every tick. It must not fetch anything on the
    ticks — the overwhelming majority — where no alarm is due."""
    db, dj, player = wake_env
    called = []
    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_news", lambda d, c: called.append("news"))
    make_schedule(db, time_str="23:00")  # not due at 08:03
    dj.check_schedules(now=MON_0803)
    assert called == []


def test_two_alarms_due_together_fetch_once(wake_env, monkeypatch) -> None:
    db, dj, player = wake_env
    called = []
    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_news", lambda d, c: called.append("news"))
    make_schedule(db, time_str="08:00")
    make_schedule(db, time_str="08:01")
    dj.check_schedules(now=MON_0803)
    assert called == ["news"]
    assert all(s["last_fired_date"] == MON_0803.date().isoformat()
               for s in db.list_schedules())


def test_fire_marks_fired_on_terminal_error(db: Database, cfg: Config, monkeypatch) -> None:
    """A terminal failure (nothing to play — no station added) must still mark
    the schedule fired: retrying within the grace window wouldn't help, and an
    un-fired schedule would just keep failing on every subsequent tick."""
    player = FakeSonosPlayer()
    dj = DJ(db, cfg, lambda: player)
    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_all", lambda db_, cfg_: None)
    monkeypatch.setattr(dj_mod.feeds, "refresh_news", lambda db_, cfg_: None)
    make_schedule(db)

    dj.check_schedules(now=MON_0803)
    assert player.state != "PLAYING"
    assert db.list_schedules()[0]["last_fired_date"] == MON_0803.date().isoformat()


def test_fire_starts_when_the_speaker_is_playing_something_of_ours_off_air(
        wake_env) -> None:
    """Playing our tracks is not the same as being on air: after a stop, the
    queue still holds them and someone can hit play in the Sonos app. The
    URIs are stale by then and nothing is topping the queue up, so the alarm
    rebuilds rather than adopting a session it isn't running."""
    db, dj, player = wake_env
    dj.start()
    dj.stop_off_air()
    player.play()                   # played straight from the Sonos app
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING" and player.index == 0


@pytest.mark.parametrize("wake_env", [RegroupBlindsPlayer], indirect=True)
def test_a_regroup_that_blinds_the_speaker_is_not_read_as_silence(wake_env) -> None:
    """Regrouping leaves Sonos reporting no current track for a beat. Asking
    "what's playing" there answers nothing — and a wake that took that for
    silence would rebuild the queue out from under a listener who is in fact
    listening. Transport state is what survives the regroup, so that is what
    the decision to leave playback alone is re-checked against."""
    db, dj, player = wake_env
    dj.start()
    player.listen(700)
    queue_before = player.queue_uris()
    add_news_feed(db)
    make_schedule(db)

    dj.check_schedules(now=MON_0803)

    assert player.grouped is True
    assert player.queue_uris() == queue_before
    assert player.position == 700
