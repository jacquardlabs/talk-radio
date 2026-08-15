import pytest
import sqlite3

from db import Database, utcnow_iso


def _ep(db: Database, feed_id: int, n: int, status: str = "new") -> int:
    db.insert_episode(feed_id, f"guid-{feed_id}-{n}", f"Ep {n}",
                      f"https://cdn.example.com/f{feed_id}/e{n}.mp3",
                      f"2026-01-{n:02d}T00:00:00Z", status)
    eps = db.episodes_for_feed(feed_id)
    return next(e["id"] for e in eps if e["guid"] == f"guid-{feed_id}-{n}")


def test_init_is_idempotent(db: Database) -> None:
    db.init()  # second run must not raise


def test_feed_crud(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, is_news=False)
    feed = db.get_feed(fid)
    assert feed["title"] == "Show" and feed["enabled"] == 1 and feed["is_news"] == 0
    db.toggle_feed(fid)
    assert db.get_feed(fid)["enabled"] == 0
    db.set_feed_news(fid, True)
    assert db.get_feed(fid)["is_news"] == 1
    assert [f["id"] for f in db.list_feeds()] == [fid]


def test_delete_feed_cascades_episodes(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    _ep(db, fid, 1)
    db.delete_feed(fid)
    assert db.episodes_with_status("new") == []


def test_insert_episode_dedupes_on_guid(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    assert db.insert_episode(fid, "g1", "A", "https://a/1.mp3", "2026-01-01T00:00:00Z") is True
    assert db.insert_episode(fid, "g1", "A again", "https://a/1.mp3", "2026-01-01T00:00:00Z") is False


def test_oldest_new_for_feed_orders_by_published(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    _ep(db, fid, 3)
    _ep(db, fid, 1)
    _ep(db, fid, 2)
    assert db.oldest_new_for_feed(fid)["guid"] == f"guid-{fid}-1"


def test_status_lifecycle(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    eid = _ep(db, fid, 1)
    db.mark_queued(eid, "https://resolved/1.mp3", "/media/ep1.mp3")
    ep = db.get_episode(eid)
    assert ep["status"] == "queued" and ep["play_uri"] == "https://resolved/1.mp3"
    db.set_resume(eid, 300)
    local = db.mark_played(eid, utcnow_iso())
    ep = db.get_episode(eid)
    assert local == "/media/ep1.mp3"
    assert ep["status"] == "played" and ep["resume_seconds"] is None
    assert ep["play_uri"] is None and ep["local_path"] is None and ep["played_at"]


def test_revert_to_new_keeps_resume(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    eid = _ep(db, fid, 1)
    db.mark_queued(eid, "https://resolved/1.mp3")
    db.set_resume(eid, 120)
    db.revert_to_new(eid)
    ep = db.get_episode(eid)
    assert ep["status"] == "new" and ep["resume_seconds"] == 120 and ep["play_uri"] is None


def test_revert_all_queued(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    a, b = _ep(db, fid, 1), _ep(db, fid, 2)
    db.mark_queued(a, "u1")
    db.mark_queued(b, "u2")
    assert db.revert_all_queued() == 2
    assert db.episodes_with_status("queued") == []


def test_archive_and_unarchive(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    eid = _ep(db, fid, 1)
    db.archive_episode(eid)
    assert db.get_episode(eid)["status"] == "archived"
    assert db.unarchive_feed(fid) == 1
    assert db.get_episode(eid)["status"] == "new"


def test_rotation_feeds_excludes_news_disabled_and_empty(db: Database) -> None:
    plain = db.add_feed("https://a/rss", "A", None, False)
    news = db.add_feed("https://b/rss", "B", None, True)
    off = db.add_feed("https://c/rss", "C", None, False)
    empty = db.add_feed("https://d/rss", "D", None, False)
    _ep(db, plain, 1)
    _ep(db, news, 1)
    _ep(db, off, 1)
    db.toggle_feed(off)
    assert [f["id"] for f in db.rotation_feeds_with_new()] == [plain]
    assert empty not in [f["id"] for f in db.rotation_feeds_with_new()]


def test_fresh_news_ordering_and_prune(db: Database) -> None:
    news = db.add_feed("https://b/rss", "News", None, True)
    _ep(db, news, 2)
    _ep(db, news, 1)
    fresh = db.fresh_news("2026-01-01T00:00:00Z")
    assert [e["guid"] for e in fresh] == [f"guid-{news}-1", f"guid-{news}-2"]
    assert db.prune_stale_news("2026-01-02T00:00:00Z") == 1
    assert db.get_episode(fresh[0]["id"])["status"] == "skipped"


def test_prune_ignores_non_news(db: Database) -> None:
    plain = db.add_feed("https://a/rss", "A", None, False)
    _ep(db, plain, 1)
    assert db.prune_stale_news("2027-01-01T00:00:00Z") == 0


def test_counts_by_feed(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "A", None, False)
    _ep(db, fid, 1)
    eid = _ep(db, fid, 2)
    db.mark_queued(eid, "u")
    counts = db.counts_by_feed()
    assert counts[fid] == {"new": 1, "queued": 1}


def test_recently_played_includes_feed_title(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "My Show", None, False)
    eid = _ep(db, fid, 1)
    db.mark_queued(eid, "u")
    db.mark_played(eid, utcnow_iso())
    rows = db.recently_played()
    assert rows[0]["feed_title"] == "My Show"


def test_kv(db: Database) -> None:
    assert db.kv_get("k") is None
    db.kv_set("k", "v1")
    db.kv_set("k", "v2")
    assert db.kv_get("k") == "v2"
    db.kv_del("k")
    assert db.kv_get("k") is None


def test_schedules(db: Database) -> None:
    sid = db.add_schedule("08:00", [0, 1, 2, 3, 4])
    s = db.list_schedules()[0]
    assert s["id"] == sid and s["time"] == "08:00" and s["days"] == "0,1,2,3,4"
    assert s["enabled"] == 1 and s["last_fired_date"] is None
    db.set_last_fired(sid, "2026-07-05")
    db.toggle_schedule(sid)
    s = db.list_schedules()[0]
    assert s["last_fired_date"] == "2026-07-05" and s["enabled"] == 0
    db.delete_schedule(sid)
    assert db.list_schedules() == []


def test_episodes_for_feed_page_paginates_and_filters(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    for i in range(1, 8):
        _ep(db, fid, i)
    assert db.count_episodes_for_feed(fid) == 7
    page1 = db.episodes_for_feed_page(fid, 1, 5)
    page2 = db.episodes_for_feed_page(fid, 2, 5)
    assert len(page1) == 5 and len(page2) == 2
    assert page1[0]["guid"] == f"guid-{fid}-7"  # newest first
    assert db.count_episodes_for_feed(fid, q="Ep 3") == 1
    assert db.episodes_for_feed_page(fid, 1, 5, q="Ep 3")[0]["guid"] == f"guid-{fid}-3"


def test_search_episodes_matches_title_or_feed_title(db: Database) -> None:
    a = db.add_feed("https://a/rss", "Mothman Museum Hour", None, False)
    b = db.add_feed("https://b/rss", "Other Show", None, False)
    _ep(db, a, 1)  # title "Ep 1"
    db.insert_episode(b, "g-b-1", "The Mothman Prophecies", "https://cdn/b/1.mp3",
                      "2026-01-01T00:00:00Z")
    assert db.count_search_episodes("mothman") == 2
    results = db.search_episodes("mothman", 1, 25)
    titles = {r["feed_title"] for r in results}
    assert titles == {"Mothman Museum Hour", "Other Show"}


def test_search_episodes_paginates(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "Cryptid Hour", None, False)
    for i in range(1, 4):
        _ep(db, fid, i)
    assert db.count_search_episodes("Ep") == 3
    assert len(db.search_episodes("Ep", 1, 2)) == 2
    assert len(db.search_episodes("Ep", 2, 2)) == 1


def test_release_episode_only_affects_archived(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    new_id = _ep(db, fid, 1)
    archived_id = _ep(db, fid, 2)
    db.archive_episode(archived_id)
    assert db.release_episode(new_id) is False
    assert db.get_episode(new_id)["status"] == "new"
    assert db.release_episode(archived_id) is True
    assert db.get_episode(archived_id)["status"] == "new"


def test_release_episodes_bulk_only_releases_archived(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    ids = [_ep(db, fid, i) for i in range(1, 4)]
    db.archive_episode(ids[0])
    db.archive_episode(ids[1])
    # ids[2] stays "new" -- bulk release must skip it, not error
    assert db.release_episodes(ids) == 2
    assert db.get_episode(ids[0])["status"] == "new"
    assert db.get_episode(ids[1])["status"] == "new"
    assert db.get_episode(ids[2])["status"] == "new"  # unchanged, was never archived


def test_release_episodes_bulk_empty_list(db: Database) -> None:
    assert db.release_episodes([]) == 0


def test_category_crud(db: Database) -> None:
    cid = db.add_category("History")
    cat = db.get_category(cid)
    assert cat["name"] == "History" and cat["rotation_enabled"] == 1
    db.rename_category(cid, "Ancient History")
    assert db.get_category(cid)["name"] == "Ancient History"
    db.toggle_category_rotation(cid)
    assert db.get_category(cid)["rotation_enabled"] == 0
    db.toggle_category_rotation(cid)
    assert db.get_category(cid)["rotation_enabled"] == 1


def test_add_category_duplicate_name_raises(db: Database) -> None:
    db.add_category("History")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_category("History")


def test_list_categories_includes_station_count(db: Database) -> None:
    cid = db.add_category("History")
    db.add_category("Comedy")
    fid1 = db.add_feed("https://a/rss", "Show A", None, False)
    fid2 = db.add_feed("https://b/rss", "Show B", None, False)
    db.set_feed_category(fid1, cid)
    db.set_feed_category(fid2, cid)
    counts = {c["name"]: c["station_count"] for c in db.list_categories()}
    assert counts["History"] == 2 and counts["Comedy"] == 0


def test_delete_category_unassigns_feeds_without_deleting_them(db: Database) -> None:
    cid = db.add_category("History")
    fid = db.add_feed("https://a/rss", "Show A", None, False)
    db.set_feed_category(fid, cid)
    db.delete_category(cid)
    feed = db.get_feed(fid)
    assert feed is not None and feed["category_id"] is None


def test_set_feed_category_invalid_id_raises(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "Show A", None, False)
    with pytest.raises(sqlite3.IntegrityError):
        db.set_feed_category(fid, 999)


def test_rotation_feeds_excludes_rotation_disabled_category(db: Database) -> None:
    on_cat = db.add_category("History")
    off_cat = db.add_category("Comedy")
    db.toggle_category_rotation(off_cat)  # now disabled
    on_feed = db.add_feed("https://a/rss", "On Show", None, False)
    off_feed = db.add_feed("https://b/rss", "Off Show", None, False)
    uncategorized_feed = db.add_feed("https://c/rss", "Uncat Show", None, False)
    db.set_feed_category(on_feed, on_cat)
    db.set_feed_category(off_feed, off_cat)
    _ep(db, on_feed, 1)
    _ep(db, off_feed, 1)
    _ep(db, uncategorized_feed, 1)
    ids = {f["id"] for f in db.rotation_feeds_with_new()}
    assert on_feed in ids
    assert uncategorized_feed in ids
    assert off_feed not in ids


def test_playback_mode_default_and_toggle(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    assert db.get_feed(fid)["playback_mode"] == "in_order"
    db.toggle_feed_playback(fid)
    assert db.get_feed(fid)["playback_mode"] == "random"
    db.toggle_feed_playback(fid)
    assert db.get_feed(fid)["playback_mode"] == "in_order"


def test_add_feed_with_playback_mode(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False, playback_mode="random")
    assert db.get_feed(fid)["playback_mode"] == "random"


def test_playback_mode_check_constraint(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.add_feed("https://ex.com/rss", "Show", None, False, playback_mode="chaos")


def test_new_episodes_for_feed_ordering_and_filter(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    _ep(db, fid, 3)
    _ep(db, fid, 1)
    played = _ep(db, fid, 2)
    db.mark_queued(played, "u")
    db.mark_played(played, utcnow_iso())
    rows = db.new_episodes_for_feed(fid)
    assert [r["guid"] for r in rows] == [f"guid-{fid}-1", f"guid-{fid}-3"]
