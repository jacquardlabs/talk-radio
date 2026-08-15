import threading

from config import Config
from db import Database
from main import dj_loop, refresh_loop


class ExplodingDJ:
    """Every call raises — the loop must swallow and keep going."""
    calls = 0

    def warm_next_start(self) -> None:
        type(self).calls += 1   # counted on the loop's first call, so the
        raise RuntimeError("boom")  # count is iterations, not survivors

    def check_schedules(self) -> None:
        raise RuntimeError("boom")

    def tick(self) -> None:
        raise RuntimeError("boom")


class CountingDJ:
    def __init__(self) -> None:
        self.calls = 0

    def warm_next_start(self) -> None:
        pass

    def check_schedules(self) -> None:
        self.calls += 1

    def tick(self) -> None:
        pass


def spin_until(predicate, limit: int = 300) -> None:
    for _ in range(limit):
        if predicate():
            return
        threading.Event().wait(0.01)


def test_dj_loop_never_fetches_feeds(monkeypatch) -> None:
    """The reconcile loop must not block on the network. Feed fetching is
    serial and minutes long on a large library; an alarm coming due during
    one has to fire on its own tick, not once the fetching finishes."""
    cfg = Config.from_env({"TICK_SECONDS": "0"})
    fetched: list[str] = []
    import main as main_mod
    monkeypatch.setattr(main_mod.feeds, "refresh_all",
                        lambda db_, cfg_: fetched.append("refresh_all"))
    stop = threading.Event()
    dj = CountingDJ()
    t = threading.Thread(target=dj_loop, args=(cfg, dj, stop), daemon=True)
    t.start()
    spin_until(lambda: dj.calls >= 5)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert dj.calls >= 5      # kept ticking
    assert fetched == []      # and never fetched


def test_loop_survives_exceptions_and_stops(monkeypatch) -> None:
    cfg = Config.from_env({"TICK_SECONDS": "0"})
    stop = threading.Event()
    dj = ExplodingDJ()
    t = threading.Thread(target=dj_loop, args=(cfg, dj, stop), daemon=True)
    t.start()
    spin_until(lambda: dj.calls >= 2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert dj.calls >= 2  # kept looping despite exceptions


def test_refresh_loop_fetches_immediately_then_waits(db: Database, monkeypatch) -> None:
    """Refresh first, sleep after — a restart picks up whatever published
    while the server was down, rather than waiting a full interval."""
    cfg = Config.from_env({"REFRESH_MINUTES": "9999"})
    calls: list[int] = []
    import main as main_mod
    monkeypatch.setattr(main_mod.feeds, "refresh_all", lambda db_, cfg_: calls.append(1))
    stop = threading.Event()
    t = threading.Thread(target=refresh_loop, args=(db, cfg, stop), daemon=True)
    t.start()
    spin_until(lambda: calls)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()      # the long wait is interruptible
    assert calls == [1]          # one pass, then it slept on the interval


def test_refresh_loop_survives_a_failing_refresh(db: Database, monkeypatch) -> None:
    cfg = Config.from_env({"REFRESH_MINUTES": "0"})
    calls: list[int] = []

    def boom(db_, cfg_):
        calls.append(1)
        raise OSError("network down")

    import main as main_mod
    monkeypatch.setattr(main_mod.feeds, "refresh_all", boom)
    stop = threading.Event()
    t = threading.Thread(target=refresh_loop, args=(db, cfg, stop), daemon=True)
    t.start()
    spin_until(lambda: len(calls) >= 2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert len(calls) >= 2  # kept retrying despite the failure
