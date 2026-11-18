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
          "guid": "g1", "reported_at": "2026-09-08T11:00:00Z"}
    a = gpodder.played_action(row, "https://feed/rss")
    assert a["started"] == a["position"] == a["total"] == gpodder.PLAYED_POSITION
    assert a["action"] == "play"
    assert a["podcast"] == "https://feed/rss"
    assert a["episode"] == "https://cdn/x.mp3"
    # gpodder's timestamp is bare UTC — no trailing Z, unlike played_at
    assert a["timestamp"] == "2026-09-08T11:00:00"


def test_played_action_overshoots_the_phones_own_copy() -> None:
    """AntennaPod marks a remote play finished only when position reaches
    its own file's length less 30s, and ignores total. The phone's copy can
    run minutes longer than ours (other inserted ads), so reporting our
    duration left it "paused near the end" and in the queue."""
    row = {"duration_seconds": 1800, "audio_url": "x", "guid": "g",
          "reported_at": "2026-09-08T11:00:00Z"}
    position = gpodder.played_action(row, "feed")["position"]
    assert position >= 24 * 3600            # past any episode's real length
    assert position * 1000 < 2 ** 31        # AntennaPod's int milliseconds


def test_played_action_needs_no_duration() -> None:
    row = {"duration_seconds": None, "audio_url": "x", "guid": "g",
          "reported_at": "2026-09-08T11:00:00Z"}
    a = gpodder.played_action(row, "feed")
    # AntennaPod drops the fields unless started>=0, position>0, total>0
    assert a["started"] >= 0 and a["position"] > 0 and a["total"] > 0


def test_resume_action_reports_mid_position() -> None:
    row = {"duration_seconds": 1800, "resume_seconds": 300,
          "audio_url": "x", "guid": "g", "resume_at": "2026-09-08T11:00:00Z"}
    a = gpodder.resume_action(row, "feed")
    assert a["position"] == 300 and a["started"] == 0 and a["total"] == 1800


def test_resume_action_keeps_the_place_without_a_duration() -> None:
    """AntennaPod never reads total for a resume, so an unknown duration
    must not cost the phone the browser's or the speakers' place."""
    row = {"duration_seconds": None, "resume_seconds": 300, "audio_url": "x",
          "guid": "g", "resume_at": "2026-09-08T11:00:00Z"}
    a = gpodder.resume_action(row, "feed")
    assert a["position"] == 300 and a["started"] == 0 and a["total"] > 0


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


def test_played_episode_with_no_duration_is_reported(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 1, duration=None)
    ep = db.episodes_with_status("new")[0]
    db.mark_played(ep["id"], "2026-09-01T00:00:00Z")
    resp = c.get("/api/2/episodes/radio.json?since=0", headers=basic_header())
    [a] = resp.get_json()["actions"]
    assert a["guid"] == ep["guid"] and a["position"] == gpodder.PLAYED_POSITION


# ── listener decisions reach the phone as played ──────────────────────
#
# A phone that synced after an episode came out asks with a `since` later
# than its publish date — the publish date a Never play records as played_at.

def _synced_after_publish() -> int:
    since = gpodder._now() - 60
    assert gpodder._epoch(NOW_STR) < since  # the premise every test below needs
    return since


def _actions(c, since: int) -> list[dict]:
    resp = c.get(f"/api/2/episodes/radio.json?since={since}", headers=basic_header())
    return resp.get_json()["actions"]


def _review(c, ids: list[int], decision: str) -> None:
    assert c.post("/episodes/review", json={"ids": ids, "decision": decision}).get_json()["ok"]


def _played(actions: list[dict]) -> list[str]:
    return [a["guid"] for a in actions if a["position"] == gpodder.PLAYED_POSITION]


def _recent(a: dict, since: int) -> bool:
    return gpodder._epoch(a["timestamp"] + "Z") >= since


@pytest.mark.parametrize("decision", ["never", "back_catalog"])
def test_review_decision_reaches_a_phone_synced_after_publish(gclient, decision) -> None:
    """Dated when decided, not when published: otherwise the row never
    passes `since`, and its timestamp loses to the phone's own actions."""
    c, db, *_ = gclient
    make_feed(db, "showa", 2)
    ep, _ = db.episodes_with_status("new")
    since = _synced_after_publish()
    _review(c, [ep["id"]], decision)
    actions = _actions(c, since)
    assert _played(actions) == [ep["guid"]]
    assert _recent(actions[0], since)


@pytest.mark.parametrize("archive", ["unrelease_route", "skip_archive", "drop_archive"])
def test_archive_decision_is_sent_as_played(gclient, archive) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 4)
    since = _synced_after_publish()
    if archive == "unrelease_route":
        ep = db.episodes_with_status("new")[0]
        assert c.post(f"/episodes/{ep['id']}/unrelease").get_json()["ok"]
    else:
        dj.start()
        if archive == "skip_archive":
            ep = current_episode(db, player)
            assert dj.skip_archive() is None
        else:
            ep = next_queued_episode(db, player)
            assert dj.drop_from_queue(ep["id"], "archive") is None
    assert db.get_episode(ep["id"])["status"] == "archived"
    actions = [a for a in _actions(c, since) if a["guid"] == ep["guid"]]
    assert _played(actions) == [ep["guid"]] and _recent(actions[0], since)


def test_a_recut_or_skipped_title_reaches_the_phone(gclient) -> None:
    """Lowering a station's depth and adding a skipped title take episodes
    out of the rotation feed the phone already has, so they go as played."""
    c, db, *_ = gclient
    fid = make_feed(db, "showa", 3)
    since = _synced_after_publish()
    assert c.post(f"/feeds/{fid}/backfill", json={"include": "latest"}).get_json()["ok"]
    assert db.set_feed_skip_titles(fid, ["showa 3"]) == 1
    archived = {e["guid"] for e in db.episodes_with_status("archived")}
    assert len(archived) == 3
    assert set(_played(_actions(c, since))) == archived


def test_adding_a_station_sends_nothing_for_its_back_catalog(gclient) -> None:
    """The cut made when a station is added never reached the phone: its
    rotation feed was born without those episodes."""
    c, db, *_ = gclient
    import feedparser
    from feeds import add_feed_from_parsed
    items = "".join(
        f"<item><title>Ep {i}</title><guid>a{i}</guid><pubDate>Mon, 0{i} Jun 2026 00:00:00 GMT</pubDate>"
        f"<enclosure url='https://cdn/a/{i}.mp3' type='audio/mpeg'/></item>" for i in range(1, 6))
    parsed = feedparser.parse(f"<rss version='2.0'><channel><title>A</title>{items}</channel></rss>")
    since = _synced_after_publish()
    add_feed_from_parsed(db, parsed, "https://a/rss", False, include="latest")
    assert len(db.episodes_with_status("archived")) == 4
    assert _actions(c, since) == []


def test_a_prune_of_the_back_catalog_is_not_sent(gclient) -> None:
    c, db, *_ = gclient
    fid = make_feed(db, "showa", 3)
    db.set_rotation_depth(fid, 1)
    cut = [e["id"] for e in db.episodes_with_status("archived")]
    since = _synced_after_publish()
    _review(c, cut, "never")  # Never play from the back catalog: a prune
    assert _actions(c, since) == []


def test_a_decision_taken_back_is_not_sent(gclient) -> None:
    c, db, *_ = gclient
    make_feed(db, "showa", 3)
    a, b, d = (e["id"] for e in db.episodes_with_status("new"))
    _review(c, [a, b], "back_catalog")
    _review(c, [d], "never")
    assert c.post(f"/episodes/{a}/release").get_json()["ok"]
    _review(c, [b, d], "keep")
    assert _actions(c, 0) == []


def test_a_bulk_decision_past_the_cap_reaches_the_phone_in_full(
        gclient, monkeypatch) -> None:
    """Select all on Review dates every row the same second. Cut at the cap
    and answered with timestamp=now, the rest never passed `since`; a cut
    inside the second, resumed from it, would resend the same rows forever."""
    c, db, *_ = gclient
    monkeypatch.setattr(gpodder, "PLAYED_PER_SYNC", 3)
    make_feed(db, "showa", 6)
    ids = [e["id"] for e in db.episodes_with_status("new")]
    _review(c, ids[:5], "back_catalog")
    with db._conn() as conn:   # one second for the batch, a later one for the last
        conn.execute("UPDATE episodes SET decided_at='2026-11-17T10:00:00Z'"
                     " WHERE decided_at IS NOT NULL")
    _review(c, ids[5:], "never")
    with db._conn() as conn:
        conn.execute("UPDATE episodes SET decided_at='2026-11-17T10:00:05Z' WHERE id=?",
                     (ids[5],))
    body = c.get("/api/2/episodes/radio.json?since=0", headers=basic_header()).get_json()
    first = {a["guid"] for a in body["actions"]}
    assert len(first) == 5                            # the whole second, past the cap
    assert body["timestamp"] == gpodder._epoch("2026-11-17T10:00:01Z")
    rest = {a["guid"] for a in _actions(c, body["timestamp"])}
    assert first | rest == {db.get_episode(i)["guid"] for i in ids}


def test_a_listen_after_a_decision_reports_when_it_was_heard(gclient) -> None:
    c, db, *_ = gclient
    make_feed(db, "showa", 1)
    ep = db.episodes_with_status("new")[0]
    _review(c, [ep["id"]], "never")
    _review(c, [ep["id"]], "keep")
    db.mark_played(ep["id"], "2026-09-02T10:00:00Z")
    [a] = _actions(c, 0)
    assert a["timestamp"] == "2026-09-02T10:00:00"


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
    # Off air, what the phone was playing leads Up Next for the next device.
    assert db.up_next_order()[0] == ep["id"]
    assert row["status"] == "queued" and row["pinned"] == 1


def _play(ep, position: int, ts: str) -> dict:
    return {"podcast": "https://showa/rss", "episode": ep["audio_url"],
            "guid": ep["guid"], "action": "play", "timestamp": ts,
            "started": 0, "position": position, "total": 7200}


def test_the_phones_latest_listen_leads_up_next_whatever_the_upload_order(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 2)
    older, newer = db.episodes_with_status("new")
    body = [_play(newer, 200, "2026-09-08T12:00:00"), _play(older, 100, "2026-09-08T11:00:00")]
    c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    assert db.up_next_order()[:2] == [newer["id"], older["id"]]


def test_a_phone_listen_on_air_keeps_only_the_place(gclient) -> None:
    """On air the speakers' list stands: the phone's episode is not pushed
    into what Sonos plays next."""
    c, db, dj, player = gclient
    make_feed(db, "showa", 15)
    make_feed(db, "showb", 15)
    dj.start()
    before = db.up_next_order()
    ep = next(e for e in db.episodes_with_status("new"))
    c.post("/api/2/episodes/radio.json", json=[_play(ep, 300, "2026-09-08T11:00:00")],
           headers=basic_header())
    assert db.get_episode(ep["id"])["resume_seconds"] == 300
    assert ep["id"] not in db.up_next_order()[:1]
    assert db.up_next_order() == before


def test_phone_then_browser_picks_up_where_the_phone_left_off(gclient) -> None:
    """The handoff: pause in AntennaPod mid-episode, open the web player —
    it offers that episode first, at the phone's place."""
    c, db, dj, player = gclient
    make_feed(db, "showa", 15)
    dj.start()
    dj.stop_off_air()
    elsewhere = db.episodes_with_status("new")[0]
    # after the speakers' Off air banked their own place, as a real pause is
    c.post("/api/2/episodes/radio.json", json=[_play(elsewhere, 754, "2999-01-01T00:00:00")],
           headers=basic_header())
    head = dj.status()["up_next"][0]
    assert head["episode_id"] == elsewhere["id"] and head["resume"] == 754


def _off_air_with_two_new(db, dj):
    make_feed(db, "showa", 15)
    dj.start()
    dj.stop_off_air()
    return db.episodes_with_status("new")[:2]


def test_a_late_phone_upload_does_not_overwrite_a_newer_browser_place(gclient) -> None:
    """The car case: the phone syncs an hour later than it listened. The
    browser played on since; the browser's place stands."""
    c, db, dj, player = gclient
    p, _ = _off_air_with_two_new(db, dj)
    dj.report_from_device(p["id"], 3000, "progress")
    c.post("/api/2/episodes/radio.json", json=[_play(p, 2400, "2000-01-01T00:00:00")],
           headers=basic_header())
    assert db.get_episode(p["id"])["resume_seconds"] == 3000


def test_a_late_phone_listen_does_not_take_the_head_from_a_newer_one(gclient) -> None:
    c, db, dj, player = gclient
    p, q = _off_air_with_two_new(db, dj)
    dj.report_from_device(p["id"], 3000, "progress")
    c.post("/api/2/episodes/radio.json", json=[_play(q, 600, "2000-01-01T00:00:00")],
           headers=basic_header())
    assert db.up_next_order()[0] == p["id"]
    assert db.get_episode(q["id"])["resume_seconds"] == 600   # its own place is still kept


def test_a_newer_phone_listen_takes_the_place_and_the_head(gclient) -> None:
    c, db, dj, player = gclient
    p, q = _off_air_with_two_new(db, dj)
    dj.report_from_device(p["id"], 3000, "progress")
    c.post("/api/2/episodes/radio.json",
           json=[_play(p, 3300, "2999-01-01T00:00:00"), _play(q, 600, "2999-01-01T00:00:01")],
           headers=basic_header())
    assert db.get_episode(p["id"])["resume_seconds"] == 3300
    assert db.up_next_order()[:2] == [q["id"], p["id"]]


def test_browser_then_phone_gets_the_browsers_place(gclient) -> None:
    c, db, dj, player = gclient
    make_feed(db, "showa", 2)
    dj.start()
    dj.stop_off_air()
    head = db.up_next_order()[0]
    dj.report_from_device(head, 612, "progress")
    actions = c.get("/api/2/episodes/radio.json?since=0", headers=basic_header()).get_json()["actions"]
    mine = [a for a in actions if a["episode"] == db.get_episode(head)["audio_url"]]
    # only the browser's place: not also the stopped speaker's stale one
    assert [a["position"] for a in mine] == [612]


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


@pytest.mark.parametrize("on_air", [False, True], ids=["off-air", "on-air"])
def test_a_trip_of_finished_episodes_leaves_up_next_as_heard(gclient, on_air) -> None:
    """Travelling: several Up Next episodes heard on the phone over a few
    hours, uploaded in one sync. Each leaves Up Next and counts as heard
    (Recently played), whether or not the speakers are on air — except the
    one the speakers are playing, which only Skip or Play now take off."""
    c, db, dj, player = gclient
    make_feed(db, "showa", 8)
    make_feed(db, "showb", 8)
    dj.start()
    if not on_air:
        dj.stop_off_air()
    order = db.up_next_order()
    heard = order[1:4]
    body = [{"podcast": "x", "episode": db.get_episode(i)["audio_url"],
             "guid": db.get_episode(i)["guid"], "action": "play",
             "timestamp": f"2026-09-08T1{n}:00:00",
             "started": 0, "position": 1790, "total": 1800}
            for n, i in enumerate(heard)]
    c.post("/api/2/episodes/radio.json", json=body, headers=basic_header())
    for i in heard:
        row = db.get_episode(i)
        assert (row["status"], row["review"]) == ("played", None), "heard, not dismissed"
        assert i not in db.up_next_order()
    listed = [e["episode_id"] for e in dj.status()["up_next"]]
    assert not set(heard) & set(listed)
    recent = {e["title"] for e in dj.status()["recently_played"]}
    assert {db.get_episode(i)["title"] for i in heard} <= recent


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


def _paused_on_air(db, dj, player):
    make_feed(db, "showa", 4, duration=3600)
    dj.start()
    player.listen(600)
    dj.tick()                 # banks resume_episode_id for the on-air track
    assert dj.pause() is None
    return current_episode(db, player)


def test_a_phone_jump_moves_the_paused_speakers(gclient) -> None:
    """Speakers paused on an episode, you carry on with it on the phone and
    skip ahead: the phone's place wins, and the paused speaker moves there so
    Play on the speakers, the web deck and the next phone sync all agree."""
    c, db, dj, player = gclient
    on_air = _paused_on_air(db, dj, player)
    c.post("/api/2/episodes/radio.json",
           json=[_play(on_air, 2400, "2999-01-01T00:00:00")], headers=basic_header())
    assert player.seeks[-1:] == [2400]
    assert dj.status()["now_playing"]["position"] == 2400
    assert db.get_episode(on_air["id"])["status"] == "queued"   # still on the speakers


def test_an_older_phone_place_does_not_move_the_paused_speakers(gclient) -> None:
    c, db, dj, player = gclient
    on_air = _paused_on_air(db, dj, player)
    c.post("/api/2/episodes/radio.json",
           json=[_play(on_air, 2400, "2000-01-01T00:00:00")], headers=basic_header())
    assert player.seeks == []


def test_paused_speakers_report_their_place_as_of_the_pause(gclient) -> None:
    """A paused speaker's place is not news: stamped now on every sync, it
    beat a later jump made on the phone. Paused, the phone gets the place the
    pause banked, dated when it was banked."""
    c, db, dj, player = gclient
    on_air = _paused_on_air(db, dj, player)
    actions = [a for a in c.get("/api/2/episodes/radio.json?since=0", headers=basic_header())
               .get_json()["actions"] if a["guid"] == on_air["guid"]]
    banked = db.get_episode(on_air["id"])["resume_at"].rstrip("Z")
    assert [a["timestamp"] for a in actions] == [banked]


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
