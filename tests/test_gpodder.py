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


def rf(feed_id: int) -> str:
    """A station's rotation-feed URL as the test client's requests see it."""
    return f"http://localhost/gpodder/feeds/{feed_id}.xml"


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
    a = db.add_feed("https://a/rss", "A", None, False)
    db.add_feed("https://b/rss", "B news", None, True)
    off = db.add_feed("https://c/rss", "C", None, False)
    db.toggle_feed(off)
    resp = c.get("/api/2/subscriptions/radio/phone1.json", headers=basic_header())
    body = resp.get_json()
    assert body["add"] == [rf(a)]


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


def test_init_backfills_media_key_on_an_existing_database(tmp_path) -> None:
    """Every other test starts from a fresh schema, which never runs the
    migration path; this one starts from a database that predates it."""
    import sqlite3
    path = str(tmp_path / "old.db")
    Database(path).init()
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO feeds (url, title, added_at) VALUES ('https://f/rss', 'F', 'x')")
        c.execute("INSERT INTO episodes (feed_id, guid, audio_url, published_at)"
                  " VALUES (1, 'g', 'https://CDN/a.mp3?t=1', 'x')")
        c.execute("DROP INDEX idx_episodes_media_key")
        c.execute("ALTER TABLE episodes DROP COLUMN resume_at")
        c.execute("ALTER TABLE episodes DROP COLUMN media_key")
    Database(path).init()
    row = Database(path).find_episode_by_media_key("https://cdn/a.mp3")
    assert row is not None and row["guid"] == "g"


def test_init_heals_a_half_applied_migration(tmp_path) -> None:
    """The column exists but its backfill never ran — what a crash between
    the ALTER and the UPDATE leaves behind."""
    import sqlite3
    path = str(tmp_path / "half.db")
    Database(path).init()
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO feeds (url, title, added_at) VALUES ('https://f/rss', 'F', 'x')")
        c.execute("INSERT INTO episodes (feed_id, guid, audio_url, published_at, media_key)"
                  " VALUES (1, 'g', 'https://cdn/a.mp3', 'x', NULL)")
    Database(path).init()
    assert Database(path).find_episode_by_media_key("https://cdn/a.mp3") is not None


# ── subscription ledger (#52) ─────────────────────────────────────────

def test_ledger_seeds_from_added_at_on_first_sync() -> None:
    led = gpodder.reconcile_ledger({"a": 100, "b": 200}, {}, now=5000)
    assert led == {"a": (True, 100), "b": (True, 200)}


def test_ledger_marks_departures_and_returns_now() -> None:
    led = {"a": (True, 100), "b": (True, 200)}
    led = gpodder.reconcile_ledger({"a": 100}, led, now=5000)
    assert led["b"] == (False, 5000) and led["a"] == (True, 100)
    led = gpodder.reconcile_ledger({"a": 100, "b": 200}, led, now=6000)
    assert led["b"] == (True, 6000)


def test_ledger_unknown_url_after_seed_is_new_now() -> None:
    led = gpodder.reconcile_ledger({"a": 100, "c": 50}, {"a": (True, 100)}, now=5000)
    assert led["c"] == (True, 5000)   # not its old added_at: e.g. re-enabled


def test_ledger_departure_time_is_kept_while_still_out() -> None:
    led = gpodder.reconcile_ledger({}, {"a": (False, 4000)}, now=5000)
    assert led["a"] == (False, 4000)


def test_changes_since_zero_never_removes() -> None:
    led = {"a": (True, 100), "b": (False, 200)}
    assert gpodder.subscription_changes(led, 0) == (["a"], [])
    assert gpodder.subscription_changes(led, 150) == ([], ["b"])


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 2_000_000_000}
    monkeypatch.setattr(gpodder, "_now", lambda: t["now"])
    return t


def subs(c, since: int) -> dict:
    return c.get(f"/api/2/subscriptions/radio/phone.json?since={since}",
                 headers=basic_header()).get_json()


def test_sync_does_not_resend_existing_stations(gclient, clock) -> None:
    """Re-sending them is what re-subscribed a station the phone dropped."""
    c, db, *_ = gclient
    a = db.add_feed("https://a/rss", "A", None, False)
    first = subs(c, 0)
    assert first["add"] == [rf(a)]
    clock["now"] += 60
    assert subs(c, first["timestamp"]) == {"add": [], "remove": [],
                                           "timestamp": clock["now"]}


def test_disable_delete_and_news_each_remove_and_reenable_adds(gclient, clock) -> None:
    c, db, *_ = gclient
    off = db.add_feed("https://off/rss", "Off", None, False)
    gone = db.add_feed("https://gone/rss", "Gone", None, False)
    news = db.add_feed("https://news/rss", "News", None, False)
    ts = subs(c, 0)["timestamp"]
    clock["now"] += 60
    db.toggle_feed(off)
    db.delete_feed(gone)
    db.set_feed_news(news, True)
    r = subs(c, ts)
    assert r["add"] == [] and r["remove"] == sorted([rf(gone), rf(news), rf(off)])
    clock["now"] += 60
    db.toggle_feed(off)
    r = subs(c, r["timestamp"])
    assert r["add"] == [rf(off)]
    # Stamped in the second the last response named, so sent once more by
    # design (`>=`): removing a feed AntennaPod no longer has is a no-op.
    assert rf(off) not in r["remove"]
    clock["now"] += 60
    r = subs(c, r["timestamp"])          # the at-most-once boundary repeat
    clock["now"] += 60
    assert subs(c, r["timestamp"]) == {"add": [], "remove": [],
                                       "timestamp": clock["now"]}


def test_station_added_after_last_sync_is_sent(gclient, clock) -> None:
    c, db, *_ = gclient
    db.add_feed("https://a/rss", "A", None, False)
    ts = subs(c, 0)["timestamp"]
    clock["now"] += 60
    b = db.add_feed("https://b/rss", "B", None, False)
    assert subs(c, ts)["add"] == [rf(b)]


def test_second_device_catches_up_on_a_removal(gclient, clock) -> None:
    """The ledger is shared; a device that last synced earlier still sees it."""
    c, db, *_ = gclient
    fid = db.add_feed("https://a/rss", "A", None, False)
    early = subs(c, 0)["timestamp"]
    clock["now"] += 60
    late = subs(c, early)["timestamp"]
    clock["now"] += 60
    db.toggle_feed(fid)
    subs(c, late)                       # the first device notices the removal
    clock["now"] += 60
    assert subs(c, early)["remove"] == [rf(fid)]


# ── rotation feeds: the phone sees only what the station would play ──

def test_rotation_feed_holds_only_new_and_queued(gclient) -> None:
    import xml.etree.ElementTree as ET
    c, db, *_ = gclient
    fid = make_feed(db, "pel", 5, duration=1800)
    eps = db.episodes_for_feed(fid)
    db.archive_episode(eps[0]["id"])
    db.mark_played(eps[1]["id"], NOW_STR)
    db.mark_skipped(eps[2]["id"])
    db.mark_queued(eps[3]["id"], "x-sonos://staged")
    resp = c.get(f"/gpodder/feeds/{fid}.xml")        # no credentials: by design
    assert resp.status_code == 200 and resp.mimetype == "application/rss+xml"
    root = ET.fromstring(resp.data)
    items = root.findall("./channel/item")
    assert sorted(i.findtext("guid") for i in items) == sorted([eps[3]["guid"], eps[4]["guid"]])
    item = next(i for i in items if i.findtext("guid") == eps[4]["guid"])
    assert item.find("enclosure").get("url") == eps[4]["audio_url"]   # the show's CDN
    assert item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration") == "1800"
    assert root.findtext("./channel/title") == "pel"


def test_rotation_feed_unknown_station_is_404(gclient) -> None:
    c, *_ = gclient
    assert c.get("/gpodder/feeds/999.xml").status_code == 404


def test_rotation_feed_escapes_markup_in_titles(gclient) -> None:
    import xml.etree.ElementTree as ET
    c, db, *_ = gclient
    fid = db.add_feed("https://x/rss", "Tom & Jerry <Live>", None, False)
    db.insert_episode(fid, "g", "Q&A: <b>bold</b>", "https://cdn/x/1.mp3", NOW_STR)
    root = ET.fromstring(c.get(f"/gpodder/feeds/{fid}.xml").data)
    assert root.findtext("./channel/title") == "Tom & Jerry <Live>"
    assert root.findtext("./channel/item/title") == "Q&A: <b>bold</b>"


def test_feed_urls_follow_the_forwarded_scheme(gclient) -> None:
    """Behind `tailscale serve` the app sees plain HTTP; the phone must be
    handed https URLs or its feed fetches go to a port nothing listens on."""
    c, db, *_ = gclient
    fid = db.add_feed("https://a/rss", "A", None, False)
    body = c.get("/api/2/subscriptions/radio/phone.json?since=0",
                 headers={**basic_header(), "X-Forwarded-Proto": "https",
                          "Host": "mothership.example.ts.net"}).get_json()
    assert body["add"] == [f"https://mothership.example.ts.net/gpodder/feeds/{fid}.xml"]


def test_existing_ledger_moves_phone_from_show_feeds_to_rotation_feeds(gclient, clock) -> None:
    """A ledger written before rotation feeds holds the shows' own URLs.
    The next sync must drop those from the phone and add the station feeds."""
    import json
    c, db, *_ = gclient
    fid = db.add_feed("https://show/rss", "Show", None, False)
    db.kv_set("gpodder_subscriptions",
              json.dumps({"https://show/rss": [True, clock["now"] - 3600]}))
    r = subs(c, clock["now"] - 60)
    assert r["add"] == [rf(fid)]
    assert r["remove"] == ["https://show/rss"]


def test_inbound_action_resolves_through_the_rotation_feed_url(gclient) -> None:
    c, db, *_ = gclient
    fid = make_feed(db, "pel", 1)
    ep = db.episodes_for_feed(fid)[0]
    body = [{"podcast": rf(fid), "episode": ep["audio_url"], "guid": ep["guid"],
             "action": "play", "timestamp": "2026-10-29T11:00:00",
             "started": 0, "position": 300, "total": 1800}]
    c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    assert db.get_episode(ep["id"])["resume_seconds"] == 300


def test_outbound_actions_name_the_rotation_feed(gclient) -> None:
    c, db, *_ = gclient
    fid = make_feed(db, "pel", 1, duration=1800)
    ep = db.episodes_for_feed(fid)[0]
    db.mark_played(ep["id"], "2026-10-29T11:00:00Z")
    actions = c.get("/api/2/episodes/radio.json?since=0", headers=basic_header()).get_json()["actions"]
    assert [a["podcast"] for a in actions] == [rf(fid)]
    assert actions[0]["episode"] == ep["audio_url"] and actions[0]["guid"] == ep["guid"]
