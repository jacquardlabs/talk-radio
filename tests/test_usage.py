"""The usage log: one row per thing a person did, none for polling."""
import sqlite3

import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer
from web import create_app, usage_key


@pytest.fixture
def c(db: Database, cfg: Config):
    app = create_app(db, DJ(db, cfg, lambda: None), cfg)
    app.config["TESTING"] = True
    return app.test_client()


def rows(db: Database) -> list[tuple]:
    with sqlite3.connect(db.path) as conn:
        return conn.execute(
            "SELECT action, page, ok FROM usage_events ORDER BY id").fetchall()


@pytest.mark.parametrize(("method", "rule", "args", "key"), [
    ("POST", "/player/<action>", {"action": "pause"}, "POST /player/pause"),
    ("POST", "/feeds/<int:feed_id>/<action>", {"feed_id": 7, "action": "toggle"},
     "POST /feeds/<int:feed_id>/toggle"),
    ("POST", "/episodes/<int:episode_id>/play_next", {"episode_id": 3},
     "POST /episodes/<int:episode_id>/play_next"),
    ("POST", "/player/<action>", {"action": r"a\1b"}, r"POST /player/a\1b"),
])
def test_usage_key_spells_out_verbs_and_keeps_ids_generic(method, rule, args, key) -> None:
    assert usage_key(method, rule, args) == key


def test_a_post_is_recorded_with_its_page(c, db) -> None:
    c.post("/categories", json={"name": "Talk"},
           headers={"Referer": "http://localhost/stations"})
    assert rows(db) == [("POST /categories", "/stations", 1)]


def test_a_failure_reported_in_the_body_is_recorded_as_failed(c, db) -> None:
    # result(error) answers 200; only the body says it failed
    assert c.post("/feeds", json={}).status_code == 200
    assert rows(db) == [("POST /feeds", None, 0)]


def test_a_404_is_recorded_as_failed(c, db) -> None:
    c.post("/categories/999/toggle")
    assert rows(db) == [("POST /categories/<int:category_id>/toggle", None, 0)]


def test_polling_and_assets_are_not_recorded(c, db) -> None:
    c.get("/api/status")
    c.get("/static/manifest.webmanifest")
    c.get("/api/usage")
    assert rows(db) == []


def test_intent_gets_and_page_views_are_recorded(c, db) -> None:
    c.get("/stations")
    c.get("/api/episodes/search?q=climate",
          headers={"Referer": "http://localhost/stations"})
    assert rows(db) == [("GET /stations", "/stations", 1),
                        ("GET /api/episodes/search", "/stations", 1)]


def test_a_foreign_referrer_records_no_page(c, db) -> None:
    c.post("/categories", json={"name": "Talk"},
           headers={"Referer": "http://elsewhere.example/stations"})
    assert rows(db) == [("POST /categories", None, 1)]


def test_a_rejected_cross_site_post_is_not_recorded(c, db) -> None:
    resp = c.post("/categories", json={"name": "Talk"},
                  headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert rows(db) == []


def test_the_beacon_records_known_ui_events_only(c, db) -> None:
    assert c.post("/api/usage", json={"event": "earlier"},
                  headers={"Referer": "http://localhost/"}).get_json()["ok"] is True
    assert c.post("/api/usage", json={"event": "nonsense"}).get_json()["ok"] is False
    assert c.post("/api/usage", json={}).get_json()["ok"] is False
    assert rows(db) == [("UI earlier", "/", 1)]


def test_the_beacon_counts_appearance_layouts_and_menus(c, db) -> None:
    """The page sends "mode" since the themes became light and dark; the
    beacon went on accepting only "theme", so no change was ever counted."""
    for event in ("mode", "load_narrow", "menu_skip"):
        assert c.post("/api/usage", json={"event": event}).get_json()["ok"] is True
    assert c.post("/api/usage", json={"event": "theme"}).get_json()["ok"] is False
    assert [r[0] for r in rows(db)] == ["UI mode", "UI load_narrow", "UI menu_skip"]


def test_a_verb_in_the_body_is_spelled_out_like_one_in_the_path(c, db) -> None:
    """Drop and review take their verb in the body, so every disposition
    used to share one key."""
    fid = db.add_feed("https://x/rss", "X", None, False)
    db.insert_episode(fid, "g1", "One", "https://cdn/1.mp3", "2026-01-01T00:00:00Z")
    eid = db.episodes_with_status("new")[0]["id"]
    c.post("/episodes/review", json={"ids": [eid], "decision": "back_catalog"})
    c.post("/episodes/review", json={"ids": [eid], "decision": "shred"})   # refused
    assert [r[0] for r in rows(db)] == [
        "POST /episodes/review/back_catalog", "POST /episodes/review"]


def test_dropping_an_up_next_row_is_counted_by_what_it_did(db, cfg) -> None:
    player = FakeSonosPlayer()
    dj = DJ(db, cfg, lambda: player)
    fid = db.add_feed("https://x/rss", "X", None, False)
    for i in range(3):
        db.insert_episode(fid, f"g{i}", f"Ep {i}", f"https://cdn/{i}.mp3", "2026-01-01T00:00:00Z")
    dj.start()
    waiting = next(e for e in db.episodes_with_status("queued")
                   if e["play_uri"] != player.queue[player.index]["uri"])
    app = create_app(db, dj, cfg)
    app.config["TESTING"] = True
    app.test_client().post(f"/episodes/{waiting['id']}/drop", json={"disposition": "archive"})
    assert rows(db) == [("POST /episodes/<int:episode_id>/drop/archive", None, 1)]


def test_the_report_groups_by_action_and_page(c, db) -> None:
    for _ in range(3):
        c.post("/player/pause", headers={"Referer": "http://localhost/"})
    c.post("/player/pause", headers={"Referer": "http://localhost/stations"})
    report = c.get("/api/usage?days=7").get_json()
    assert [(a["action"], a["page"], a["count"], a["failed"])
            for a in report["actions"]] == [
        ("POST /player/pause", "/", 3, 3),        # no speaker: every one failed
        ("POST /player/pause", "/stations", 1, 1),
    ]
    assert report["since"]


def test_the_report_tolerates_a_bad_days_argument(c) -> None:
    assert c.get("/api/usage?days=soon").status_code == 200
