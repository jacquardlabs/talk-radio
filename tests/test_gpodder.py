import base64

import pytest

import gpodder
from config import Config
from db import Database, media_key_of
from dj import DJ
from fake_player import FakeSonosPlayer
from web import create_app

NOW_STR = "2026-09-01T00:00:00Z"


# ── pure mappers ──────────────────────────────────────────────────────

def test_played_action_matches_antennapod_first_sync_shape() -> None:
    row = {"duration_seconds": 1800, "audio_url": "https://cdn/x.mp3",
          "guid": "g1", "played_at": "2026-09-08T11:00:00Z"}
    a = gpodder.played_action(row, "https://feed/rss")
    assert a["started"] == a["position"] == a["total"] == 1800
    assert a["action"] == "play"
    assert a["podcast"] == "https://feed/rss"
    assert a["episode"] == "https://cdn/x.mp3"
    # gpodder's timestamp is bare UTC — no trailing Z, unlike played_at
    assert a["timestamp"] == "2026-09-08T11:00:00"


def test_played_action_none_without_duration() -> None:
    row = {"duration_seconds": None, "audio_url": "x", "guid": "g",
          "played_at": "2026-09-08T11:00:00Z"}
    assert gpodder.played_action(row, "feed") is None
    row["duration_seconds"] = 0
    assert gpodder.played_action(row, "feed") is None


def test_resume_action_reports_mid_position() -> None:
    row = {"duration_seconds": 1800, "resume_seconds": 300,
          "audio_url": "x", "guid": "g", "resume_at": "2026-09-08T11:00:00Z"}
    a = gpodder.resume_action(row, "feed")
    assert a["position"] == 300 and a["started"] == 0 and a["total"] == 1800


def test_resume_action_none_without_duration() -> None:
    row = {"duration_seconds": None, "resume_seconds": 300, "audio_url": "x",
          "guid": "g", "resume_at": "z"}
    assert gpodder.resume_action(row, "feed") is None


def test_now_playing_action_none_without_live_duration() -> None:
    assert gpodder.now_playing_action({"position": 10, "duration": None},
                                      "feed", "x", "g") is None


def test_now_playing_action_reports_current_position() -> None:
    a = gpodder.now_playing_action({"position": 42, "duration": 1800},
                                   "feed", "https://cdn/x.mp3", "g1")
    assert a["position"] == 42 and a["total"] == 1800


def test_media_key_strips_tracking_params_and_normalizes_case() -> None:
    a = media_key_of("HTTPS://CDN.example.com:443/path/ep.mp3?token=abc&exp=99")
    b = media_key_of("https://cdn.example.com:443/path/ep.mp3?token=zzz")
    assert a == b
    assert media_key_of("https://cdn/ep.mp3#frag") == media_key_of("https://cdn/ep.mp3")


def test_fmt_ts_strips_z_suffix() -> None:
    assert gpodder._fmt_ts("2026-09-08T11:00:00Z") == "2026-09-08T11:00:00"


# ── HTTP surface ──────────────────────────────────────────────────────

def make_feed(db: Database, name: str, n: int, duration: int | None = None) -> int:
    fid = db.add_feed(f"https://{name}/rss", name, None, False)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{i}", f"{name} {i}", f"https://cdn/{name}/{i}.mp3",
                          NOW_STR, duration_seconds=duration)
    return fid


def current_episode(db: Database, player: FakeSonosPlayer):
    uri = player.queue[player.index]["uri"]
    return next(e for e in db.episodes_with_status("queued") if e["play_uri"] == uri)


def next_queued_episode(db: Database, player: FakeSonosPlayer):
    uri = player.queue[player.index + 1]["uri"]
    return next(e for e in db.episodes_with_status("queued") if e["play_uri"] == uri)


def basic_header(user: str = "radio", pw: str = "secret") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def gcfg(tmp_path) -> Config:
    return Config.from_env({"DATA_DIR": str(tmp_path), "GPODDER_USER": "radio",
                            "GPODDER_PASSWORD": "secret"})


@pytest.fixture
def gclient(db: Database, gcfg: Config):
    player = FakeSonosPlayer()
    dj = DJ(db, gcfg, lambda: player)
    app = create_app(db, dj, gcfg)
    app.config["TESTING"] = True
    return app.test_client(), db, dj, player


def test_blueprint_absent_without_credentials(db: Database, cfg: Config) -> None:
    """cfg (conftest's plain fixture) sets no gpodder credentials — a stock
    install must be byte-for-byte what it is today."""
    player = FakeSonosPlayer()
    dj = DJ(db, cfg, lambda: player)
    app = create_app(db, dj, cfg)
    app.config["TESTING"] = True
    resp = app.test_client().get("/api/2/episodes/radio.json")
    assert resp.status_code == 404


def test_requires_auth(gclient) -> None:
    c, *_ = gclient
    assert c.get("/api/2/episodes/radio.json").status_code == 401


def test_basic_auth_accepted(gclient) -> None:
    c, *_ = gclient
    resp = c.get("/api/2/episodes/radio.json", headers=basic_header())
    assert resp.status_code == 200


def test_wrong_password_rejected(gclient) -> None:
    c, *_ = gclient
    resp = c.get("/api/2/episodes/radio.json", headers=basic_header(pw="nope"))
    assert resp.status_code == 401


def test_cookie_accepted_after_login(gclient) -> None:
    c, *_ = gclient
    login = c.post("/api/2/auth/radio/login.json", headers=basic_header())
    assert login.status_code == 200
    assert "sessionid=" in login.headers.get("Set-Cookie", "")
    # test client persists the cookie jar across requests on the same client
    resp = c.get("/api/2/episodes/radio.json")
    assert resp.status_code == 200


def test_subscriptions_lists_enabled_non_news_feeds(gclient) -> None:
    c, db, *_ = gclient
    db.add_feed("https://a/rss", "A", None, False)
    db.add_feed("https://b/rss", "B news", None, True)
    off = db.add_feed("https://c/rss", "C", None, False)
    db.toggle_feed(off)
    resp = c.get("/api/2/subscriptions/radio/phone1.json", headers=basic_header())
    body = resp.get_json()
    assert body["add"] == ["https://a/rss"]


def test_since_filters_out_earlier_plays(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 1, duration=1800)
    ep = db.episodes_with_status("new")[0]
    db.mark_played(ep["id"], "2026-09-01T00:00:00Z")
    resp = c.get("/api/2/episodes/radio.json?since=1893456000",  # far future
                 headers=basic_header())
    assert resp.get_json()["actions"] == []
    resp2 = c.get("/api/2/episodes/radio.json?since=0", headers=basic_header())
    assert len(resp2.get_json()["actions"]) == 1


def test_played_episode_with_no_duration_is_skipped_outbound(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 1, duration=None)
    ep = db.episodes_with_status("new")[0]
    db.mark_played(ep["id"], "2026-09-01T00:00:00Z")
    resp = c.get("/api/2/episodes/radio.json?since=0", headers=basic_header())
    assert resp.get_json()["actions"] == []


def test_post_play_action_sets_resume(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 1)
    ep = db.episodes_with_status("new")[0]
    body = [{"podcast": "https://showa/rss", "episode": ep["audio_url"],
             "guid": ep["guid"], "action": "play",
             "timestamp": "2026-09-08T11:00:00",
             "started": 0, "position": 300, "total": 1800}]
    resp = c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    assert resp.status_code == 200
    row = db.get_episode(ep["id"])
    assert row["resume_seconds"] == 300
    assert row["resume_at"] is not None
    assert row["status"] == "new"  # unaffected — no rotation impact


def test_post_play_action_near_end_marks_played(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 1)
    ep = db.episodes_with_status("new")[0]
    body = [{"podcast": "https://showa/rss", "episode": ep["audio_url"],
             "guid": ep["guid"], "action": "play",
             "timestamp": "2026-09-08T11:00:00",
             "started": 0, "position": 1790, "total": 1800}]
    c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    row = db.get_episode(ep["id"])
    assert row["status"] == "played"
    assert row["played_at"] is not None
    assert row["pinned"] == 0


def test_post_play_action_on_queued_episode_drops_it_from_sonos_and_up_next(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 3)
    dj.start()
    target = next_queued_episode(db, player)
    play_uri = target["play_uri"]
    body = [{"podcast": "https://showa/rss", "episode": target["audio_url"],
             "guid": target["guid"], "action": "play",
             "timestamp": "2026-09-08T11:00:00",
             "started": 0, "position": 1790, "total": 1800}]
    c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    after = db.get_episode(target["id"])
    assert after["status"] == "played"
    assert all(t["uri"] != play_uri for t in player.queue)
    assert target["id"] not in db.up_next_order()


def test_post_play_action_ignores_the_episode_on_air(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 2)
    dj.start()
    player.listen(600)
    dj.tick()  # banks resume_episode_id for the on-air track
    on_air = current_episode(db, player)
    body = [{"podcast": "https://showa/rss", "episode": on_air["audio_url"],
             "guid": on_air["guid"], "action": "play",
             "timestamp": "2026-09-08T11:00:00",
             "started": 0, "position": 1790, "total": 1800}]
    c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    after = db.get_episode(on_air["id"])
    assert after["status"] == "queued"  # untouched — Skip/Play now only
    assert player.seeks == []


def test_post_new_and_delete_actions_are_ignored(gclient) -> None:
    """AntennaPod itself never acts on a received new/download/delete —
    EpisodeActionFilter breaks on all three. We hold ourselves to the same
    rule rather than inventing meaning for what its own engine treats as
    inert (a phone deleting a stale download must not mark it played)."""
    c, db, dj, player = gclient
    make_feed(db, "showa", 1)
    ep = db.episodes_with_status("new")[0]
    for action in ("new", "delete", "download"):
        body = [{"podcast": "https://showa/rss", "episode": ep["audio_url"],
                 "guid": ep["guid"], "action": action}]
        c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    row = db.get_episode(ep["id"])
    assert row["status"] == "new" and row["resume_seconds"] is None


def test_sync_traffic_is_not_counted_as_usage(gclient) -> None:
    """AntennaPod syncs on its own schedule; usage_events counts people."""
    c, db, *_ = gclient
    c.post("/api/2/auth/radio/login.json", headers=basic_header())
    c.post("/api/2/episodes/radio.json", json=[], headers=basic_header())
    with db._conn() as x:
        assert x.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_session_tokens_are_bounded(gclient) -> None:
    c, *_ = gclient
    first = c.post("/api/2/auth/radio/login.json", headers=basic_header())
    token = first.headers["Set-Cookie"].split("sessionid=")[1].split(";")[0]
    for _ in range(16):
        c.post("/api/2/auth/radio/login.json", headers=basic_header())
    c.set_cookie("sessionid", token)
    assert c.get("/api/2/episodes/radio.json").status_code == 401


def test_logout_revokes_the_cookie(gclient) -> None:
    c, *_ = gclient
    c.post("/api/2/auth/radio/login.json", headers=basic_header())
    c.post("/api/2/auth/radio/logout.json")
    assert c.get("/api/2/episodes/radio.json").status_code == 401
