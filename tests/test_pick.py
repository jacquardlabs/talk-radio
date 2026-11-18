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
