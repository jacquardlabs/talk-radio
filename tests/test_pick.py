import random

import dj as dj_mod
from db import Database
from dj import pick_next


def _feed_with_eps(db: Database, name: str, n: int, is_news: bool = False) -> int:
    fid = db.add_feed(f"https://{name}/rss", name, None, is_news)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{name}-{i}", f"{name} {i}",
                          f"https://cdn/{name}/{i}.mp3", f"2026-01-{i:02d}T00:00:00Z")
    return fid


def test_returns_none_with_no_rotation_feeds(db: Database) -> None:
    _feed_with_eps(db, "news-only", 2, is_news=True)
    assert pick_next(db) is None


def test_returns_oldest_new_episode(db: Database) -> None:
    _feed_with_eps(db, "show", 3)
    ep = pick_next(db)
    assert ep["guid"] == "g-show-1"


def test_avoids_last_feed_when_alternative_exists(db: Database) -> None:
    a = _feed_with_eps(db, "aaa", 2)
    b = _feed_with_eps(db, "bbb", 2)
    db.kv_set("last_feed_id", str(a))
    for _ in range(10):  # random, so hammer it: must never pick a
        db.kv_set("last_feed_id", str(a))
        assert pick_next(db)["feed_id"] == b


def test_repeats_when_no_alternative(db: Database) -> None:
    a = _feed_with_eps(db, "aaa", 2)
    db.kv_set("last_feed_id", str(a))
    assert pick_next(db)["feed_id"] == a


def test_remembers_last_feed(db: Database) -> None:
    a = _feed_with_eps(db, "aaa", 1)
    pick_next(db)
    assert db.kv_get("last_feed_id") == str(a)


def test_series_parts_come_in_order_across_picks(db: Database) -> None:
    a = _feed_with_eps(db, "series", 3)
    order = []
    for _ in range(3):
        ep = pick_next(db)
        order.append(ep["guid"])
        db.mark_queued(ep["id"], ep["audio_url"])  # consume it
    assert order == ["g-series-1", "g-series-2", "g-series-3"]


def test_pick_next_never_picks_rotation_disabled_category(db: Database) -> None:
    off_cat = db.add_category("Comedy")
    db.toggle_category_rotation(off_cat)
    off_feed = _feed_with_eps(db, "off-show", 3)
    db.set_feed_category(off_feed, off_cat)
    on_feed = _feed_with_eps(db, "on-show", 1)
    for _ in range(10):  # random rotation, so hammer it
        ep = pick_next(db)
        assert ep["feed_id"] == on_feed
        db.mark_queued(ep["id"], ep["audio_url"])
        db.mark_played(ep["id"], "2026-01-01T00:00:00Z")
        db.insert_episode(on_feed, ep["guid"] + "-again", ep["title"],
                          ep["audio_url"], "2026-01-02T00:00:00Z")


def test_random_mode_can_pick_nonchronologically(db: Database, monkeypatch) -> None:
    fid = _feed_with_eps(db, "epi", 6)
    db.toggle_feed_playback(fid)
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])
    assert pick_next(db)["guid"] == "g-epi-6"  # newest, not oldest


def test_in_order_mode_ignores_randomness(db: Database, monkeypatch) -> None:
    fid = _feed_with_eps(db, "serial", 5)  # default in_order
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])
    assert pick_next(db)["guid"] == "g-serial-1"  # still oldest


def _saga_feed(db: Database) -> int:
    fid = db.add_feed("https://saga/rss", "saga", None, False)
    titles = ["Intro", "The Long War (Part 1)", "The Long War (Part 2)",
              "The Long War (Part 3)"]
    for i, t in enumerate(titles, 1):
        db.insert_episode(fid, f"g-saga-{i}", t, f"https://cdn/saga/{i}.mp3",
                          f"2026-01-{i:02d}T00:00:00Z")
    return fid


def test_arc_guard_redirects_midarc_draw(db: Database, monkeypatch) -> None:
    fid = _saga_feed(db)
    db.toggle_feed_playback(fid)
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])  # draw Part 3
    assert pick_next(db)["title"] == "The Long War (Part 1)"


def test_arc_guard_resumes_partial_arc(db: Database, monkeypatch) -> None:
    fid = _saga_feed(db)
    db.toggle_feed_playback(fid)
    part1 = next(e for e in db.episodes_for_feed(fid)
                 if e["title"] == "The Long War (Part 1)")
    db.mark_queued(part1["id"], "u")
    db.mark_played(part1["id"], "2026-02-01T00:00:00Z")
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])  # draw Part 3
    assert pick_next(db)["title"] == "The Long War (Part 2)"  # not a replay of 1


def test_random_mode_seeded_draws_vary(db: Database) -> None:
    fid = _feed_with_eps(db, "epi", 30)
    db.toggle_feed_playback(fid)
    random.seed(42)
    picks = []
    for _ in range(10):
        ep = pick_next(db)
        picks.append(ep["guid"])
        db.mark_queued(ep["id"], ep["audio_url"])  # consume
    assert picks != [f"g-epi-{i}" for i in range(1, 11)]  # not chronological
    assert len(set(picks)) == 10  # consumed picks never repeat
