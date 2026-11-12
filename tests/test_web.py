import re

import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer
from web import UI_EVENTS, create_app
import feeds as feeds_mod


@pytest.fixture
def no_sonos_client(db: Database, cfg: Config):
    dj = DJ(db, cfg, lambda: None)
    app = create_app(db, dj, cfg)
    app.config["TESTING"] = True
    return app.test_client(), db


@pytest.fixture
def client(db: Database, cfg: Config):
    player = FakeSonosPlayer()
    dj = DJ(db, cfg, lambda: player)
    app = create_app(db, dj, cfg)
    app.config["TESTING"] = True
    return app.test_client(), db, player


def test_dashboard_loads_without_sonos(no_sonos_client) -> None:
    c, _ = no_sonos_client
    resp = c.get("/")
    assert resp.status_code == 200
    assert b"Sonos Talk Radio" in resp.data
    assert b'id="dial"' in resp.data


def test_pwa_manifest_served(no_sonos_client) -> None:
    c, _ = no_sonos_client
    resp = c.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/manifest+json")
    assert b"Talk Radio" in resp.data


def test_stations_page_loads_without_sonos(no_sonos_client) -> None:
    c, _ = no_sonos_client
    resp = c.get("/stations")
    assert resp.status_code == 200
    assert b"station-groups" in resp.data
    # whether the DJ picks from a category is switched on its shelf
    assert b'class="rotsw" role="switch"' in resp.data


def test_status_degrades_without_sonos(no_sonos_client) -> None:
    c, _ = no_sonos_client
    data = c.get("/api/status").get_json()
    assert data["speaker"] is None and data["now_playing"] is None
    assert data["dj_state"] == "stopped"
    for key in ("up_next", "stations", "schedules", "recently_played"):
        assert isinstance(data[key], list)


def test_player_action_without_speaker_reports_error(no_sonos_client) -> None:
    c, _ = no_sonos_client
    data = c.post("/player/play").get_json()
    assert data["ok"] is False and "speaker" in data["error"].lower()


def test_unknown_player_action_is_404(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.post("/player/warp_speed").status_code == 404


def test_player_defer_is_a_known_action(no_sonos_client) -> None:
    """A missing map entry would 404 here; reaching call_player instead is what
    proves it is wired, and that a dead speaker degrades rather than 500s."""
    c, _ = no_sonos_client
    resp = c.post("/player/defer")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False and "speaker" in data["error"].lower()


def test_player_action_degrades_gracefully_on_exception(client, monkeypatch) -> None:
    """A cached player that starts raising (speaker powered off, network
    blip) must never bubble a 500 up through Flask — transport routes
    degrade the same way dj.status() already does."""
    c, db, player = client

    def boom() -> None:
        raise RuntimeError("speaker powered off")

    monkeypatch.setattr(player, "pause", boom)
    resp = c.post("/player/pause")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"]


def test_player_seek_degrades_gracefully_on_exception(client, monkeypatch) -> None:
    c, db, player = client

    def boom() -> None:
        raise RuntimeError("speaker powered off")

    monkeypatch.setattr(player, "current", boom)
    resp = c.post("/player/seek", json={"seconds": 30})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"]


def test_seek_requires_seconds(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.post("/player/seek", json={}).get_json()["ok"] is False


def test_add_feed_requires_url(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.post("/feeds", json={}).get_json()["ok"] is False


def test_add_feed_explains_a_show_page(no_sonos_client) -> None:
    """The Pocket Casts page answered 403, and the listener was shown the
    HTTP error rather than told to search by name."""
    c, _ = no_sonos_client
    data = c.post("/feeds", json={
        "url": "https://pocketcasts.com/podcast/pretty-heady-stuff/96798930"}).get_json()
    assert data["ok"] is False
    assert "Search for the show by name" in data["error"]


def test_add_feed_explains_a_page_that_is_not_a_feed(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client

    def html_page(url, user_agent, *a, **k):
        raise feeds_mod.NotAFeed(f"could not parse feed: {url}")

    monkeypatch.setattr(feeds_mod, "fetch_feed", html_page)
    data = c.post("/feeds", json={"url": "https://example.com/about"}).get_json()
    assert data["error"].startswith("That address isn't a podcast feed")


def test_feed_actions(client, monkeypatch) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    assert c.post(f"/feeds/{fid}/news").get_json()["ok"] is True
    assert db.get_feed(fid)["is_news"] == 1
    assert c.post(f"/feeds/{fid}/toggle").get_json()["ok"] is True
    assert db.get_feed(fid)["enabled"] == 0
    db.insert_episode(fid, "g1", "e", "https://a/1.mp3", "2026-01-01T00:00:00Z")
    db.archive_episode(db.episodes_for_feed(fid)[0]["id"])
    assert c.post(f"/feeds/{fid}/unarchive").get_json()["ok"] is True
    assert db.episodes_with_status("archived") == []
    assert c.post(f"/feeds/{fid}/delete").get_json()["ok"] is True
    assert c.post(f"/feeds/{fid}/delete").status_code == 404


def test_schedule_routes_and_next_start(client) -> None:
    c, db, _ = client
    assert c.post("/schedules", json={"time": "08:00", "days": [0, 1, 2, 3, 4]}
                  ).get_json()["ok"] is True
    data = c.get("/api/status").get_json()
    assert data["schedules"][0]["time"] == "08:00"
    assert data["next_start"] is not None
    sid = data["schedules"][0]["id"]
    assert c.post(f"/schedules/{sid}/toggle").get_json()["ok"] is True
    assert c.post(f"/schedules/{sid}/delete").get_json()["ok"] is True
    assert c.get("/api/status").get_json()["schedules"] == []


def test_schedule_validation(client) -> None:
    c, _, _ = client
    assert c.post("/schedules", json={"time": "8pm", "days": [0]}).get_json()["ok"] is False
    assert c.post("/schedules", json={"time": "08:00", "days": []}).get_json()["ok"] is False


def test_speaker_selection(client) -> None:
    c, db, _ = client
    assert c.post("/api/speaker", json={"ip": "10.0.0.7"}).get_json()["ok"] is True
    assert db.kv_get("speaker_ip") == "10.0.0.7"


def test_status_now_playing_with_fake(client) -> None:
    c, db, player = client
    fid = db.add_feed("https://showa/rss", "showa", None, False)
    for i in range(1, 5):
        db.insert_episode(fid, f"g{i}", f"ep{i}", f"https://cdn/a/{i}.mp3",
                          f"2026-01-0{i}T00:00:00Z")
    assert c.post("/player/play").get_json()["ok"] is True
    data = c.get("/api/status").get_json()
    assert data["dj_state"] == "playing" and data["transport"] == "PLAYING"
    assert data["now_playing"]["show"] == "showa"
    # the board links each show to its station sheet
    assert data["now_playing"]["feed_id"] == data["stations"][0]["id"]
    assert all("feed_id" in e for e in data["up_next"])
    assert len(data["up_next"]) >= 1
    assert data["stations"][0]["counts"]["queued"] >= 1


def _add_episode(db, fid, n, status="new", duration=None):
    # insert_episode has no status parameter — passing status positionally
    # used to land in duration_seconds and leave every row 'new', which made
    # the release tests below pass without ever exercising a real release.
    db.insert_episode(fid, f"g{fid}-{n}", f"Ep {n}", f"https://cdn/{fid}/{n}.mp3",
                      f"2026-01-{n:02d}T00:00:00Z", duration)
    eid = next(e["id"] for e in db.episodes_for_feed(fid) if e["guid"] == f"g{fid}-{n}")
    if status == "archived":
        db.archive_episode(eid)
    return eid


def test_feed_episodes_paginates_and_searches(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    for i in range(1, 4):
        _add_episode(db, fid, i)
    _add_episode(db, fid, 4, duration=1860)
    resp = c.get(f"/api/feeds/{fid}/episodes?page=1")
    data = resp.get_json()
    assert data["total"] == 4 and len(data["episodes"]) == 4
    assert data["episodes"][0]["show"] == "X Show"
    assert data["episodes"][0]["duration"] == 1860
    assert data["episodes"][1]["duration"] is None
    filtered = c.get(f"/api/feeds/{fid}/episodes?q=Ep 2").get_json()
    assert filtered["total"] == 1 and filtered["episodes"][0]["title"] == "Ep 2"


def test_feed_episodes_list_every_status_newest_first(client) -> None:
    """One list, whatever the status: a news show's heard episodes are its
    recent ones, and grouping them under an older back catalog hid them."""
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    _add_episode(db, fid, 1, status="archived")
    db.mark_played(_add_episode(db, fid, 3), "2026-01-09T00:00:00Z")
    _add_episode(db, fid, 2)
    rows = c.get(f"/api/feeds/{fid}/episodes").get_json()["episodes"]
    assert [(e["title"], e["status"]) for e in rows] == [
        ("Ep 3", "played"), ("Ep 2", "new"), ("Ep 1", "archived")]



def test_feed_episodes_by_shelf(client) -> None:
    """A station sheet splits Unheard, Heard and Archived, so a heard episode
    to replay isn't buried among hundreds archived. Missed is unheard: it
    failed to play. The total pages the same rows."""
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    for i in range(1, 6):
        _add_episode(db, fid, i, status="archived")
    released = _add_episode(db, fid, 6, status="archived")
    db.release_episodes([released])
    db.mark_played(_add_episode(db, fid, 7), "2026-01-09T00:00:00Z")
    db.mark_skipped(_add_episode(db, fid, 8))

    def shelf(name: str, q: str = "") -> dict:
        return c.get(f"/api/feeds/{fid}/episodes?shelf={name}&q={q}").get_json()

    unheard = shelf("unheard")
    assert unheard["total"] == 2
    assert [(e["title"], e["status"]) for e in unheard["episodes"]] == [
        ("Ep 8", "skipped"), ("Ep 6", "new")]
    assert [e["title"] for e in shelf("heard")["episodes"]] == ["Ep 7"]
    assert shelf("archived")["total"] == 5
    assert shelf("archived", "Ep 3")["total"] == 1 and shelf("unheard", "Ep 3")["total"] == 0
    assert c.get(f"/api/feeds/{fid}/episodes").get_json()["total"] == 8
    assert c.get(f"/api/feeds/{fid}/episodes?shelf=bogus").status_code == 400


def test_feed_episodes_unknown_feed_404s(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.get("/api/feeds/999/episodes").status_code == 404


def test_episode_search_across_feeds(client) -> None:
    c, db, _ = client
    a = db.add_feed("https://a/rss", "Mothman Museum Hour", None, False)
    b = db.add_feed("https://b/rss", "Other Show", None, False)
    _add_episode(db, a, 1)
    _add_episode(db, b, 1)
    data = c.get("/api/episodes/search?q=mothman").get_json()
    assert data["total"] == 1
    assert data["episodes"][0]["show"] == "Mothman Museum Hour"
    empty = c.get("/api/episodes/search?q=").get_json()
    assert empty["episodes"] == [] and empty["total"] == 0


def test_release_episode_route(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1, status="archived")
    assert c.post(f"/episodes/{eid}/release").get_json()["ok"] is True
    assert db.get_episode(eid)["status"] == "new"
    assert c.post("/episodes/999/release").status_code == 404


def test_unrelease_episode_route(client) -> None:
    """Only an unplayed episode goes back: one in Up Next or already heard
    keeps its status rather than vanishing into the back catalog."""
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1)
    assert c.post(f"/episodes/{eid}/unrelease").get_json()["ok"] is True
    assert db.get_episode(eid)["status"] == "archived"
    heard = _add_episode(db, fid, 2)
    db.mark_played(heard, "2026-01-09T00:00:00Z")
    data = c.post(f"/episodes/{heard}/unrelease").get_json()
    assert data["ok"] is False and "unplayed" in data["error"]
    assert db.get_episode(heard)["status"] == "played"
    assert c.post("/episodes/999/unrelease").status_code == 404


def test_done_episode_route(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1)
    assert c.post(f"/episodes/{eid}/done").get_json()["ok"] is True
    assert db.get_episode(eid)["status"] == "played"
    assert c.post(f"/episodes/{eid}/done").get_json()["ok"] is False  # already heard
    assert c.post("/episodes/999/done").status_code == 404


def test_drop_route_validates_disposition(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1)
    data = c.post(f"/episodes/{eid}/drop", json={"disposition": "someday"}).get_json()
    assert data["ok"] is False and "disposition" in data["error"]
    assert c.post("/episodes/999/drop").status_code == 404
    # archive is a disposition; this one isn't on the speaker, so it's refused there
    data = c.post(f"/episodes/{eid}/drop", json={"disposition": "archive"}).get_json()
    assert data["error"] == "That episode isn't in Up Next"


def test_skip_archive_route(client) -> None:
    c, _, _ = client
    data = c.post("/player/skip_archive").get_json()
    assert data["ok"] is False and "episode" in data["error"]  # nothing on air


def test_release_episodes_bulk_route(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    a = _add_episode(db, fid, 1, status="archived")
    b = _add_episode(db, fid, 2, status="archived")
    resp = c.post("/episodes/release", json={"ids": [a, b]})
    assert resp.get_json()["ok"] is True
    assert db.get_episode(a)["status"] == "new"
    assert db.get_episode(b)["status"] == "new"


def test_play_next_and_play_now_routes(client) -> None:
    c, db, player = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1, status="archived")
    resp = c.post(f"/episodes/{eid}/play_next")
    assert resp.get_json()["ok"] is True
    assert db.get_episode(eid)["status"] == "queued"
    assert c.post("/episodes/999/play_now").status_code == 404


def test_play_now_takes_a_position_to_resume_from(client) -> None:
    c, db, player = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    on_air, eid = _add_episode(db, fid, 1), _add_episode(db, fid, 2)
    c.post(f"/episodes/{on_air}/play_now")
    assert c.post(f"/episodes/{eid}/play_now", json={"resume": 600}).get_json()["ok"] is True
    assert db.get_episode(eid)["resume_seconds"] == 600   # tick() seeks there
    bad = c.post(f"/episodes/{eid}/play_now", json={"resume": "soon"}).get_json()
    assert bad["ok"] is False and "resume" in bad["error"]


def test_play_now_degrades_gracefully_on_exception(client, monkeypatch) -> None:
    c, db, player = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1)

    def boom(*a, **k):
        raise RuntimeError("speaker powered off")

    monkeypatch.setattr(player, "add_to_queue", boom)
    resp = c.post(f"/episodes/{eid}/play_now")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


def test_volume_endpoint(client) -> None:
    c, db, player = client
    assert c.post("/player/volume", json={"volume": 40}).get_json()["ok"] is True
    assert player.volume == 40
    assert c.get("/api/status").get_json()["volume"] == 40


def test_volume_clamped_and_validated(client) -> None:
    c, db, player = client
    assert c.post("/player/volume", json={"volume": 150}).get_json()["ok"] is True
    assert player.volume == 100
    assert c.post("/player/volume", json={}).get_json()["ok"] is False
    assert c.post("/player/volume", json={"volume": "loud"}).get_json()["ok"] is False


def test_volume_without_speaker_reports_error(no_sonos_client) -> None:
    c, _ = no_sonos_client
    data = c.post("/player/volume", json={"volume": 30}).get_json()
    assert data["ok"] is False and "speaker" in data["error"].lower()
    assert c.get("/api/status").get_json()["volume"] is None


def test_podcast_search_blank_query_short_circuits(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client
    called = []
    monkeypatch.setattr(feeds_mod, "search_podcasts", lambda *a, **k: called.append(1) or [])
    data = c.get("/api/podcasts/search?q=").get_json()
    assert data == {"results": []}
    assert called == []


def test_podcast_search_returns_results(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client
    fake_results = [{"title": "Mothman Museum Hour", "author": "Jane Doe",
                     "artwork_url": "https://x/art.jpg", "feed_url": "https://x/feed.xml"}]
    monkeypatch.setattr(feeds_mod, "search_podcasts", lambda term, ua: fake_results)
    data = c.get("/api/podcasts/search?q=mothman").get_json()
    assert data == {"results": fake_results}


def test_podcast_search_degrades_gracefully_on_error(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client

    def boom(term, ua):
        raise RuntimeError("itunes unreachable")

    monkeypatch.setattr(feeds_mod, "search_podcasts", boom)
    resp = c.get("/api/podcasts/search?q=mothman")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == [] and "error" in data


def test_add_category_and_list_in_status(client) -> None:
    c, db, _ = client
    assert c.post("/categories", json={"name": "History"}).get_json()["ok"] is True
    data = c.get("/api/status").get_json()
    assert len(data["categories"]) == 1
    assert data["categories"][0]["name"] == "History"
    assert data["categories"][0]["rotation_enabled"] is True
    assert data["categories"][0]["station_count"] == 0


def test_add_category_requires_name(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.post("/categories", json={}).get_json()["ok"] is False


def test_add_category_duplicate_name_errors(client) -> None:
    c, db, _ = client
    c.post("/categories", json={"name": "History"})
    resp = c.post("/categories", json={"name": "History"})
    assert resp.get_json()["ok"] is False


def test_category_toggle_rename_delete(client) -> None:
    c, db, _ = client
    c.post("/categories", json={"name": "History"})
    cid = db.list_categories()[0]["id"]
    assert c.post(f"/categories/{cid}/toggle").get_json()["ok"] is True
    assert db.get_category(cid)["rotation_enabled"] == 0
    assert c.post(f"/categories/{cid}/rename", json={"name": "Ancient History"}).get_json()["ok"] is True
    assert db.get_category(cid)["name"] == "Ancient History"
    assert c.post(f"/categories/{cid}/delete").get_json()["ok"] is True
    assert db.get_category(cid) is None


def test_category_action_unknown_id_404s(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.post("/categories/999/toggle").status_code == 404


def test_category_rename_requires_name(client) -> None:
    c, db, _ = client
    c.post("/categories", json={"name": "History"})
    cid = db.list_categories()[0]["id"]
    resp = c.post(f"/categories/{cid}/rename", json={})
    assert resp.get_json()["ok"] is False


def test_set_feed_category(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    c.post("/categories", json={"name": "History"})
    cid = db.list_categories()[0]["id"]
    assert c.post(f"/feeds/{fid}/category", json={"category_id": cid}).get_json()["ok"] is True
    assert db.get_feed(fid)["category_id"] == cid
    assert c.post(f"/feeds/{fid}/category", json={"category_id": None}).get_json()["ok"] is True
    assert db.get_feed(fid)["category_id"] is None
    data = c.get("/api/status").get_json()
    station = next(s for s in data["stations"] if s["id"] == fid)
    assert station["category_id"] is None


def test_set_feed_category_unknown_feed_404s(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert c.post("/feeds/999/category", json={"category_id": None}).status_code == 404


def test_set_feed_category_invalid_category_errors(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    resp = c.post(f"/feeds/{fid}/category", json={"category_id": 999})
    assert resp.get_json()["ok"] is False


def test_feed_playback_toggle_route(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    assert c.post(f"/feeds/{fid}/playback").get_json()["ok"] is True
    assert db.get_feed(fid)["playback_mode"] == "random"
    data = c.get("/api/status").get_json()
    station = next(s for s in data["stations"] if s["id"] == fid)
    assert station["playback_mode"] == "random"
    assert c.post(f"/feeds/{fid}/playback").get_json()["ok"] is True
    assert db.get_feed(fid)["playback_mode"] == "in_order"


def test_status_playback_mode_defaults_in_order(client) -> None:
    c, db, _ = client
    db.add_feed("https://x/rss", "X", None, False)
    data = c.get("/api/status").get_json()
    assert data["stations"][0]["playback_mode"] == "in_order"


def test_reorder_route_validates_its_arguments(client) -> None:
    c, db, player = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1, status="archived")
    for body in ({}, {"episode_id": eid}, {"episode_id": "x", "to_position": 0},
                 {"episode_id": eid, "to_position": "up"}):
        resp = c.post("/queue/reorder", json=body)
        assert resp.get_json()["ok"] is False


def test_reorder_route_404s_on_unknown_episode(client) -> None:
    c, db, player = client
    resp = c.post("/queue/reorder", json={"episode_id": 999, "to_position": 0})
    assert resp.status_code == 404


def test_reorder_route_degrades_when_the_speaker_is_unreachable(client, monkeypatch) -> None:
    c, db, player = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1, status="archived")

    def boom(*args, **kwargs):
        raise OSError("speaker gone")

    monkeypatch.setattr(player, "queue_uris", boom)
    resp = c.post("/queue/reorder", json={"episode_id": eid, "to_position": 0})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


def test_play_last_route(client) -> None:
    c, db, player = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1, status="archived")
    assert c.post(f"/episodes/{eid}/play_last").get_json()["ok"] is True
    assert db.get_episode(eid)["status"] == "queued"
    assert c.post("/episodes/999/play_last").status_code == 404


def test_stations_page_offers_add_to_up_next(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert b"play_last" in c.get("/stations").data


def test_stations_page_names_the_archive_one_way(no_sonos_client) -> None:
    """"archived" was also "in the vault", "Release", and "Back catalog",
    which read as episodes waiting to play. Each verb names where the
    episode ends up: Archive, Add to rotation, Mark heard."""
    c, _ = no_sonos_client
    page = c.get("/stations").data
    for verb in (b"Archive <i>", b"Add to rotation <i>", b"Mark heard <i>", b'"Archived"'):
        assert verb in page
    for old in (b"vault", b"Release selected", b"Add back catalog", b">Held<",
                b"Drop for good", b"Add to Up Next", b"Never play", b"Back catalog",
                b"back catalog", b"Send to back catalog"):
        assert old not in page


def test_board_renders_reorder_grips(no_sonos_client) -> None:
    """The grip is a real <button> carrying the episode id, so it is
    focusable and the keyboard path has something to act on."""
    body = no_sonos_client[0].get("/").data
    assert b'data-grip=' in body
    assert b'/queue/reorder' in body
    assert b'data-episode-id=' in body


def test_board_uses_the_one_verb_set(no_sonos_client) -> None:
    """Skip and an Up next row offer the same ways out: back to rotation,
    archived, or marked heard. Done and Drop for good were two names for one
    act, and Never play read as archiving."""
    body = no_sonos_client[0].get("/").data
    assert body.count(b"Back to rotation <i>") == 2 and body.count(b"Archive <i>") == 2
    # marking heard is an item in a row's menu, and what Skip itself does
    assert body.count(b"Mark heard <i>") == 1
    assert b"Skip this episode and mark it heard" in body
    assert b"Keep in Up next" in body
    for old in (b"Drop for good", b"Drop for later", b">Done <i>", b">Later <i>", b">Defer <i>",
                b"Never play", b'"Pin"'):
        assert old not in body


def test_skip_is_one_tap_and_its_caret_keeps_the_episode(no_sonos_client) -> None:
    """60 of 61 skips in the usage log chose Mark heard, the last and red
    item of a menu. Skip does that itself now, with Undo on its confirmation;
    the caret holds the ways to skip and keep the episode, least final first."""
    body = no_sonos_client[0].get("/").data
    assert b'id="btn-skip" data-player="skip_done"' in body
    flap = body[body.index(b'id="skip-flap"'):body.index(b'id="vol"')]
    order = [flap.index(f'data-player="{a}"'.encode())
             for a in ("defer", "skip_later", "skip_archive")]
    assert order == sorted(order)
    assert b"skip_done" not in flap
    assert b'flash("Skipped and marked heard", true, { label: "Undo"' in body
    # Undo takes the mark back and plays it from where it stopped
    assert b'decision: "keep"' in body and b"play_now`, { resume: at }" in body


def test_controls_go_dead_when_nothing_is_on_air(no_sonos_client) -> None:
    """Skip and the seek buttons act on the episode playing. Off air stops
    the speaker whatever the DJ believes, so it stays live until the speaker
    is silent too."""
    body = no_sonos_client[0].get("/").data
    assert b"""["#btn-skip", "#btn-skip-more", '[data-player="back_15"]', '[data-player="fwd_30"]']""" in body
    assert b'const silent = s.dj_state !== "playing" && (!s.transport || s.transport === "STOPPED");' in body
    assert b'$("btn-power").disabled = $("btn-sleep").disabled = silent;' in body


def test_rooms_and_earlier_say_why_and_when(no_sonos_client) -> None:
    """The room holding the queue was greyed out with its reason only in an
    aria-label, and Earlier printed bare times across several days."""
    body = no_sonos_client[0].get("/").data
    assert b"""r.coordinator && !isStation ? '<span class="spk-note">Holds the queue</span>'""" in body
    assert body.count(b"playedWhen(e.played_at)") == 2
    assert b"hm(new Date(e.played_at))" not in body


def test_every_flap_has_a_usage_event_for_opening(no_sonos_client) -> None:
    """The flap toggle counts an open as menu_<id>; an id the beacon doesn't
    know would be refused without a sound."""
    c, _ = no_sonos_client
    pages = b"".join(c.get(path).data for path in ("/", "/stations", "/settings"))
    flaps = set(re.findall(rb'data-flap="([a-z]+)-flap"', pages))
    assert flaps and {f"menu_{f.decode()}" for f in flaps} <= UI_EVENTS


def test_a_message_sits_above_whatever_is_docked(no_sonos_client) -> None:
    """On a phone the message landed on the docked transport and hid it."""
    c, _ = no_sonos_client
    assert b'class="transport" data-dock' in c.get("/").data
    assert b'id="mini-transport" data-dock' in c.get("/stations").data
    assert b'id="review-bulk" data-dock' in c.get("/settings").data
    assert b'querySelectorAll("[data-dock]")' in c.get("/").data


def test_phone_menus_name_what_they_act_on(no_sonos_client) -> None:
    c, _ = no_sonos_client
    assert b'<span class="flap-title">${esc(e.title)}</span>' in c.get("/").data
    assert b'<span class="flap-title">${esc(e.title)}</span>' in c.get("/stations").data


def test_the_episode_on_air_says_so_on_stations(no_sonos_client) -> None:
    """It is "queued" like the rest of Up next, so its row read Up next and
    offered Play next behind itself."""
    body = no_sonos_client[0].get("/stations").data
    assert b"status.now_playing.episode_id === e.id" in body
    assert b"""onAir ? '<span class="pri">On air</span>'""" in body
    assert b'(onAir ? "" : `<button type="button" class="tab small" data-episode="${e.id}/play_next"' in body


def test_add_station_with_nothing_chosen_says_what_to_do(no_sonos_client) -> None:
    """The URL field was required while hidden, so the browser blocked the
    submit and had nowhere to show why: the button did nothing."""
    body = no_sonos_client[0].get("/stations").data
    assert b'aria-label="Feed URL" style="display:none">' in body
    assert b'$("feed-url").required = mode === "url";' in body
    assert b'flash("Search for a podcast and choose it, or paste a link")' in body



def test_alarm_toggle_is_a_switch(no_sonos_client) -> None:
    """A bare "On" read as "turn on": one tap right after adding an alarm
    switched it off. The state now reads by the knob's position."""
    body = no_sonos_client[0].get("/").data
    assert b'class="sw${a.enabled ? " on" : ""}" role="switch"' in body
    assert b'? "On" : "Off"' not in body


def test_alarm_delete_arms_first(no_sonos_client) -> None:
    """Delete goes through armed() like every other destructive button, and
    the schedule list is signature-guarded so a poll can't disarm it."""
    body = no_sonos_client[0].get("/").data
    assert b'btn.dataset.sched.endsWith("/delete") && !armed(btn)' in body
    assert b"schedSig !== lastSchedSig" in body

# ── /stream proxy ─────────────────────────────────────────────────────

class FakeUpstream:
    """Stands in for the CDN response requests.get would hand back."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self) -> None:
        self.closed = True


def _episode(db: Database) -> int:
    fid = db.add_feed("https://bbc/rss", "In Our Time", None, False)
    db.insert_episode(fid, "g-1", "P v NP", "https://open.bbc/redir/p037611x.mp3",
                      "2015-11-05T00:00:00Z")
    return db.new_episodes_for_feed(fid)[0]["id"]


def test_stream_proxies_upstream_bytes(client, monkeypatch) -> None:
    import audio

    c, db, _ = client
    episode_id = _episode(db)
    seen = {}

    def fake_open(url, ua, range_header):
        seen["url"] = url
        seen["range"] = range_header
        return FakeUpstream(b"ID3xxxaudio"), {"Content-Type": "audio/mpeg",
                                              "Accept-Ranges": "bytes"}

    monkeypatch.setattr(audio, "open_upstream", fake_open)

    resp = c.get(f"/stream/{episode_id}.mp3")

    assert resp.status_code == 200
    assert resp.data == b"ID3xxxaudio"
    assert resp.headers["Content-Type"] == "audio/mpeg"
    # It resolves from the episode's own source URL, freshly, at play time —
    # a signed link staged an hour ago may already have expired.
    assert seen["url"] == "https://open.bbc/redir/p037611x.mp3"


def test_stream_forwards_range_and_partial_status(client, monkeypatch) -> None:
    """Seeking is why this matters: without Range passing both ways Sonos
    treats the stream as unseekable."""
    import audio

    c, db, _ = client
    episode_id = _episode(db)
    seen = {}

    def fake_open(url, ua, range_header):
        seen["range"] = range_header
        return FakeUpstream(b"partial", status=206), {
            "Content-Type": "audio/mpeg", "Content-Range": "bytes 5-11/12"}

    monkeypatch.setattr(audio, "open_upstream", fake_open)

    resp = c.get(f"/stream/{episode_id}.mp3", headers={"Range": "bytes=5-"})

    assert seen["range"] == "bytes=5-"
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 5-11/12"


def test_stream_unknown_episode_404s(client) -> None:
    c, _, _ = client
    assert c.get("/stream/999999.mp3").status_code == 404


def test_stream_upstream_failure_is_502_not_500(client, monkeypatch) -> None:
    import audio

    c, db, _ = client
    episode_id = _episode(db)

    def boom(url, ua, range_header):
        raise RuntimeError("CDN down")

    monkeypatch.setattr(audio, "open_upstream", boom)

    resp = c.get(f"/stream/{episode_id}.mp3")
    assert resp.status_code == 502
    assert resp.get_json()["ok"] is False


# ── shortlist and series routes ───────────────────────────────────────

def _feed_with(db: Database, titles: list[str]) -> int:
    fid = db.add_feed("https://arc/rss", "Arc Show", None, False)
    for i, t in enumerate(titles, start=1):
        db.insert_episode(fid, f"g-{i}", t, f"https://cdn/arc/{i}.mp3",
                          f"2020-01-0{i}T00:00:00Z")
    return fid


def test_pin_route_toggles(client) -> None:
    c, db, _ = client
    fid = _feed_with(db, ["Solo Episode"])
    ep = db.new_episodes_for_feed(fid)[0]

    assert c.post(f"/episodes/{ep['id']}/pin").get_json()["ok"] is True
    assert db.get_episode(ep["id"])["pinned"] == 1
    assert c.post(f"/episodes/{ep['id']}/pin").get_json()["ok"] is True
    assert db.get_episode(ep["id"])["pinned"] == 0


def test_pin_route_accepts_an_explicit_value(client) -> None:
    """Two tabs open on the same row must not fight: sending the state you
    want is idempotent where a blind toggle isn't."""
    c, db, _ = client
    fid = _feed_with(db, ["Solo Episode"])
    ep = db.new_episodes_for_feed(fid)[0]

    c.post(f"/episodes/{ep['id']}/pin", json={"pinned": True})
    c.post(f"/episodes/{ep['id']}/pin", json={"pinned": True})

    assert db.get_episode(ep["id"])["pinned"] == 1


def test_pin_route_404s_on_unknown_episode(client) -> None:
    c, _, _ = client
    assert c.post("/episodes/999999/pin").status_code == 404


def test_episode_json_exposes_pin_and_series(client) -> None:
    """The client shows "Queue series" only where there is one, so the arc key
    has to travel with the row rather than being reimplemented in JavaScript."""
    c, db, _ = client
    fid = _feed_with(db, ["The Siege (Part One)", "Solo Episode"])

    rows = c.get(f"/api/feeds/{fid}/episodes").get_json()["episodes"]
    by_title = {r["title"]: r for r in rows}

    assert by_title["The Siege (Part One)"]["arc"]
    assert by_title["Solo Episode"]["arc"] is None
    assert by_title["Solo Episode"]["pinned"] is False
    assert by_title["Solo Episode"]["failures"] == 0


def test_arc_preview_route_lists_the_parts(client) -> None:
    c, db, _ = client
    fid = _feed_with(db, ["The Siege (Part One)", "Solo Episode",
                          "The Siege (Part Two)"])
    part_one = next(e for e in db.episodes_for_feed(fid)
                    if e["title"] == "The Siege (Part One)")

    parts = c.get(f"/api/episodes/{part_one['id']}/arc").get_json()["parts"]

    assert [p["title"] for p in parts] == ["The Siege (Part One)",
                                           "The Siege (Part Two)"]


def test_queue_arc_route_queues_the_series(client) -> None:
    c, db, player = client
    fid = _feed_with(db, ["The Siege (Part One)", "The Siege (Part Two)"])
    part_one = next(e for e in db.episodes_for_feed(fid)
                    if e["title"] == "The Siege (Part One)")

    assert c.post(f"/episodes/{part_one['id']}/queue_arc").get_json()["ok"] is True

    assert player.queue_uris()[-2:] == ["https://cdn/arc/1.mp3",
                                        "https://cdn/arc/2.mp3"]


def test_queue_arc_route_reports_a_standalone_episode(client) -> None:
    c, db, _ = client
    fid = _feed_with(db, ["Solo Episode"])
    solo = db.new_episodes_for_feed(fid)[0]

    body = c.post(f"/episodes/{solo['id']}/queue_arc").get_json()

    assert body["ok"] is False
    assert "series" in body["error"].lower()


def test_status_reports_failures(client) -> None:
    c, db, _ = client
    fid = _feed_with(db, ["Solo Episode", "Another One"])
    dead, wanted = db.new_episodes_for_feed(fid)
    db.mark_failed(dead["id"], "2026-01-01T00:00:00Z", 3)

    body = c.get("/api/status").get_json()

    assert [f["title"] for f in body["recent_failures"]] == ["Solo Episode"]
    assert body["recent_failures"][0]["attempts"] == 1
    assert body["recent_failures"][0]["given_up"] is False
    assert body["stations"][0]["failures"] == 1


def test_refresh_route_rerolls_the_queue(client) -> None:
    c, db, player = client
    fid = _feed_with(db, [f"Episode {n}" for n in range(1, 9)])
    c.post("/player/play")
    before = db.up_next_order()
    assert before, "expected a queue to refresh"

    assert c.post("/queue/refresh").get_json()["ok"] is True

    # The track on air is never re-rolled; the unpinned rows behind it are.
    assert db.up_next_order()[0] == before[0]


def test_sleep_routes_arm_and_cancel(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    _add_episode(db, fid, 1, duration=1800)
    c.post("/player/play")
    assert c.post("/player/sleep", json={"mode": "fade", "minutes": 30}
                  ).get_json()["ok"] is True
    assert db.kv_get("sleep_mode") == "fade"
    assert c.get("/api/status").get_json()["sleep"]["remaining"] == 1800
    assert c.post("/player/sleep_cancel").get_json()["ok"] is True
    assert db.kv_get("sleep_mode") is None


def test_sleep_route_rejects_bad_minutes(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    _add_episode(db, fid, 1, duration=1800)
    c.post("/player/play")
    data = c.post("/player/sleep", json={"mode": "fade", "minutes": "soon"}).get_json()
    assert data["ok"] is False and "minutes" in data["error"]
    assert db.kv_get("sleep_mode") is None


def _statuses(db, fid) -> list[tuple[str, str]]:
    return [(e["title"], e["status"]) for e in db.episodes_for_feed(fid)]


def test_backfill_caps_rotation_and_spares_what_was_queued_or_heard(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    ids = {n: _add_episode(db, fid, n) for n in range(1, 6)}       # Ep 5 newest
    db.mark_queued(ids[4], "uri")                                   # in Up Next
    db.mark_played(ids[1], "2026-01-09T00:00:00Z")
    assert c.post(f"/feeds/{fid}/backfill",
                  json={"include": "last_n", "count": 2}).get_json()["ok"] is True
    assert _statuses(db, fid) == [
        ("Ep 5", "new"), ("Ep 4", "queued"),            # the newest two
        ("Ep 3", "archived"), ("Ep 2", "archived"),     # older, unplayed
        ("Ep 1", "played"),                             # history stays
    ]


def test_backfill_all_restores_and_new_only_empties(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    for n in range(1, 4):
        _add_episode(db, fid, n, status="archived")
    c.post(f"/feeds/{fid}/backfill", json={"include": "all"})
    assert {s for _, s in _statuses(db, fid)} == {"new"}
    c.post(f"/feeds/{fid}/backfill", json={"include": "new_only"})
    assert {s for _, s in _statuses(db, fid)} == {"archived"}


def test_backfill_validates(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    assert c.post(f"/feeds/{fid}/backfill", json={"include": "some"}).get_json()["ok"] is False
    assert c.post("/feeds/999/backfill", json={"include": "all"}).status_code == 404


# ── settings: review ────────────────────────────────────────────────
def _review_seed(db) -> tuple[int, list[int]]:
    fid = db.add_feed("https://x/rss", "X", None, False)
    return fid, [_add_episode(db, fid, n) for n in (1, 2, 3)]


def test_settings_page_loads_and_is_in_the_nav(no_sonos_client) -> None:
    c, _ = no_sonos_client
    resp = c.get("/settings")
    assert resp.status_code == 200 and b"Review episodes" in resp.data
    assert b'href="/settings" aria-current="page"' in resp.data
    assert b'href="/settings"' in c.get("/").data


def test_review_lists_counts_and_ids(client) -> None:
    c, db, _ = client
    fid, ids = _review_seed(db)
    db.archive_episode(ids[0])
    body = c.get("/api/review?view=recent").get_json()
    assert body["counts"] == {"rotation": 2, "recent": 2, "back_catalog": 1, "never": 0}
    assert body["total"] == 2
    assert [e["show"] for e in body["episodes"]] == ["X", "X"]
    assert c.get(f"/api/review/ids?view=back_catalog&feed={fid}").get_json()["ids"] == [ids[0]]
    assert c.get("/api/review/ids?view=back_catalog&feed=999").get_json()["ids"] == []
    assert c.get("/api/review?view=bogus").status_code == 400
    assert c.get("/api/review/ids").status_code == 400


def test_review_route_reports_what_moved(client) -> None:
    c, db, _ = client
    _, ids = _review_seed(db)
    db.mark_queued(ids[2], "uri")
    body = c.post("/episodes/review", json={"ids": ids, "decision": "back_catalog"}).get_json()
    # queued but not on the speaker's queue: it can't be taken off, so it's held
    assert body == {"ok": True, "error": None, "moved": 2, "held": 1}
    assert db.get_episode(ids[0])["status"] == "archived"
    assert db.get_episode(ids[2])["status"] == "queued"


def test_review_route_validates(client) -> None:
    c, db, _ = client
    assert c.post("/episodes/review", json={"ids": [1], "decision": "x"}).get_json()["ok"] is False
    assert c.post("/episodes/review", json={"ids": ["a"], "decision": "keep"}).get_json()["ok"] is False
    assert c.post("/episodes/review", json={"decision": "keep"}).get_json()["moved"] == 0


def test_review_shows_rank_and_flag_the_mostly_skipped(client) -> None:
    c, db, _ = client
    skipped = db.add_feed("https://x/a", "Passed over", None, False)
    heard = db.add_feed("https://x/b", "Loved", None, False)
    few = db.add_feed("https://x/c", "Barely tried", None, False)
    passed = [_add_episode(db, skipped, n) for n in range(1, 7)]
    db.mark_played(passed[0], "2026-11-01T00:00:00Z")
    c.post("/episodes/review", json={"ids": passed[1:], "decision": "never"})
    for n in range(1, 7):
        db.mark_played(_add_episode(db, heard, n), "2026-11-01T00:00:00Z")
    c.post("/episodes/review", json={"ids": [_add_episode(db, few, 1)], "decision": "back_catalog"})

    shows = c.get("/api/review/shows").get_json()["shows"]

    assert [(s["show"], s["skip_percent"], s["mostly_skipped"]) for s in shows] == [
        ("Barely tried", 100, False), ("Passed over", 83, True), ("Loved", 0, False)]


def test_settings_tabs_say_what_will_play(no_sonos_client) -> None:
    """"Back catalog 11,372" read as episodes still to come. The tabs lead
    with what the DJ can pick, and name the rest by what happened to them."""
    page = no_sonos_client[0].get("/settings").data
    tabs = [page.index(f'data-view="{v}"'.encode())
            for v in ("rotation", "recent", "back_catalog", "never", "shows")]
    assert tabs == sorted(tabs)
    for label in (b"In rotation", b"Archived", b"Marked heard"):
        assert label in page
    assert b"Back catalog" not in page and b"Never play" not in page


def test_settings_visits_count_as_usage(client) -> None:
    c, db, _ = client
    c.get("/settings")
    c.get("/api/review?view=recent")
    actions = {r["action"] for r in db.usage_summary("2000-01-01T00:00:00Z")}
    assert {"GET /settings", "GET /api/review"} <= actions


def test_skip_titles_route_moves_waiting_episodes_and_reports_in_status(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    db.insert_episode(fid, "s", "Selects: Salem", "https://x/s.mp3", "2026-01-01T00:00:00Z")
    body = c.post(f"/feeds/{fid}/skip_titles", json={"titles": ["Selects"]}).get_json()
    assert body == {"ok": True, "error": None, "moved": 1}
    station = next(f for f in c.get("/api/status").get_json()["stations"] if f["id"] == fid)
    assert station["skip_titles"] == ["Selects"]
    assert station["counts"]["archived"] == 1


def test_skip_titles_route_validates(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    assert c.post(f"/feeds/{fid}/skip_titles", json={"titles": "Selects"}).get_json()["ok"] is False
    assert c.post(f"/feeds/{fid}/skip_titles", json={"titles": [1]}).get_json()["ok"] is False
    assert c.post("/feeds/999/skip_titles", json={"titles": []}).status_code == 404
