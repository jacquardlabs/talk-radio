import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer
from web import create_app
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
    assert b"cat-toggles" in resp.data


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
    assert len(data["up_next"]) >= 1
    assert data["stations"][0]["counts"]["queued"] >= 1


def _add_episode(db, fid, n, status="new"):
    db.insert_episode(fid, f"g{fid}-{n}", f"Ep {n}", f"https://cdn/{fid}/{n}.mp3",
                      f"2026-01-{n:02d}T00:00:00Z", status)
    return next(e["id"] for e in db.episodes_for_feed(fid) if e["guid"] == f"g{fid}-{n}")


def test_feed_episodes_paginates_and_searches(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X Show", None, False)
    for i in range(1, 4):
        _add_episode(db, fid, i)
    resp = c.get(f"/api/feeds/{fid}/episodes?page=1")
    data = resp.get_json()
    assert data["total"] == 3 and len(data["episodes"]) == 3
    assert data["episodes"][0]["show"] == "X Show"
    filtered = c.get(f"/api/feeds/{fid}/episodes?q=Ep 2").get_json()
    assert filtered["total"] == 1 and filtered["episodes"][0]["title"] == "Ep 2"


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


def test_drop_route_validates_disposition(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    eid = _add_episode(db, fid, 1)
    data = c.post(f"/episodes/{eid}/drop", json={"disposition": "someday"}).get_json()
    assert data["ok"] is False and "disposition" in data["error"]
    assert c.post("/episodes/999/drop").status_code == 404


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


def test_board_renders_reorder_grips(no_sonos_client) -> None:
    """The grip is a real <button> carrying the episode id, so it is
    focusable and the keyboard path has something to act on."""
    body = no_sonos_client[0].get("/").data
    assert b'data-grip=' in body
    assert b'/queue/reorder' in body
    assert b'data-episode-id=' in body


def test_skip_flap_offers_defer_first(no_sonos_client) -> None:
    """The flap runs least-destructive to most, and defer is the only one of
    the three that keeps the episode."""
    body = no_sonos_client[0].get("/").data
    assert b'data-player="defer"' in body
    assert body.index(b'data-player="defer"') < body.index(b'data-player="skip_later"')


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
