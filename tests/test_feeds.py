import sqlite3
from datetime import datetime, timedelta, timezone

import feedparser

from config import Config
from db import Database
from feeds import (FeedFetch, add_feed_from_parsed, detect_playback_mode,
                   ingest_entries, news_cutoff_iso, refresh_all, refresh_news)
from rss_fixtures import rss

NOW = datetime.now(timezone.utc)


def _items(n: int) -> list[tuple[str, datetime]]:
    """n items, item 1 oldest."""
    return [(f"Part {i}", NOW - timedelta(days=n - i)) for i in range(1, n + 1)]


def _statuses(db: Database, feed_id: int) -> dict[str, str]:
    return {e["guid"]: e["status"] for e in db.episodes_for_feed(feed_id)}


def test_scope_all_keeps_everything_new(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", _items(4)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    assert set(_statuses(db, fid).values()) == {"new"}
    assert db.oldest_new_for_feed(fid)["guid"] == "guid-part-1"


def test_scope_new_only_archives_everything(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", _items(3)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="new_only")
    assert set(_statuses(db, fid).values()) == {"archived"}


def test_scope_latest_keeps_newest_one(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", _items(3)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="latest")
    st = _statuses(db, fid)
    assert st["guid-part-3"] == "new"
    assert st["guid-part-1"] == st["guid-part-2"] == "archived"


def test_scope_last_n_keeps_newest_n(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", _items(5)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="last_n", last_n=2)
    st = _statuses(db, fid)
    assert st["guid-part-5"] == st["guid-part-4"] == "new"
    assert st["guid-part-3"] == st["guid-part-2"] == st["guid-part-1"] == "archived"


def test_ingest_takes_one_transaction_per_feed(db: Database, monkeypatch) -> None:
    """A refresh re-offers every episode a feed has ever published, and the
    library holds 23.5k of them. One connection-and-commit per episode is
    what made a full refresh minutes long — 20.6s for a single 2851-episode
    feed on the deployed host, against 0.04s for the batch."""
    parsed = feedparser.parse(rss("Show", _items(40)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")

    opened = []
    real_conn = db._conn
    monkeypatch.setattr(db, "_conn", lambda: (opened.append(1), real_conn())[1])

    assert ingest_entries(db, fid, parsed) == 0  # every one already known
    assert len(opened) == 1, f"{len(opened)} connections for one feed's 40 episodes"


def test_refresh_is_idempotent_and_new_drops_are_playable(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", _items(2)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="new_only")
    assert ingest_entries(db, fid, parsed) == 0  # re-ingest inserts nothing
    later = feedparser.parse(rss("Show", _items(3)))  # Part 3 just dropped
    assert ingest_entries(db, fid, later) == 1
    assert _statuses(db, fid)["guid-part-3"] == "new"  # playable despite new_only


class FakeResponse:
    """Just the surface fetch_feed reads."""

    def __init__(self, status_code: int = 200, body: bytes = b"",
                 headers: dict | None = None) -> None:
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


def test_fetch_asks_the_server_to_skip_a_body_it_already_has(monkeypatch) -> None:
    """Stations publish weekly and are re-read every REFRESH_MINUTES, so
    nearly every fetch has nothing to report. A 304 costs one round trip and
    no parse — and the parse is the expensive half, up to 2851 entries."""
    import feeds as feeds_mod
    sent: dict = {}

    def fake_get(url, headers=None, timeout=None, **kw):
        sent.update(headers or {})
        return FakeResponse(304, headers={"ETag": 'W/"v2"'})

    monkeypatch.setattr(feeds_mod.requests, "get", fake_get)
    fetched = feeds_mod.fetch_feed("https://x/rss", "UA", 'W/"v1"',
                                   "Wed, 29 Jul 2026 12:00:00 GMT")
    assert sent["If-None-Match"] == 'W/"v1"'
    assert sent["If-Modified-Since"] == "Wed, 29 Jul 2026 12:00:00 GMT"
    assert fetched.parsed is None       # nothing to ingest, nothing parsed
    assert fetched.etag == 'W/"v2"'     # the server's newer tag, taken


def test_a_304_that_echoes_nothing_keeps_the_validators_we_sent(monkeypatch) -> None:
    """A 304 need not repeat the validators. Omitting them is not revoking
    them — dropping ours here would make the feed fetch in full forever."""
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod.requests, "get",
                        lambda *a, **kw: FakeResponse(304))
    fetched = feeds_mod.fetch_feed("https://x/rss", "UA", 'W/"v1"', "Wed, 29 Jul 2026")
    assert (fetched.etag, fetched.last_modified) == ('W/"v1"', "Wed, 29 Jul 2026")


def test_refresh_records_validators_and_sends_them_back(
        db: Database, cfg: Config, monkeypatch) -> None:
    """The validators have to be stored on every answer, including one that
    carried no new episodes — a feed whose validators are never written can
    never start answering 304."""
    parsed = feedparser.parse(rss("Show", _items(2)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    asked: list[tuple] = []

    def fake_fetch(url, ua, etag=None, last_modified=None):
        asked.append((etag, last_modified))
        if etag:                       # second pass: server says unchanged
            return FeedFetch(None, etag, last_modified)
        return FeedFetch(parsed, 'W/"v1"', "Wed, 29 Jul 2026 12:00:00 GMT")

    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", fake_fetch)

    refresh_all(db, cfg)               # first pass: no validators to send
    assert asked[0] == (None, None)
    assert db.get_feed(fid)["etag"] == 'W/"v1"'

    refresh_all(db, cfg)               # second: asks conditionally, gets 304
    assert asked[1] == ('W/"v1"', "Wed, 29 Jul 2026 12:00:00 GMT")
    assert db.get_feed(fid)["etag"] == 'W/"v1"'
    assert len(db.episodes_for_feed(fid)) == 2   # the 304 changed nothing


def test_a_feed_that_fails_to_ingest_does_not_stop_the_rest(
        db: Database, cfg: Config, monkeypatch) -> None:
    """A station whose host is down is a station with no new episodes, and
    the same goes for one whose ingest blows up — neither is a reason for
    the other 56 to go unrefreshed."""
    good = feedparser.parse(rss("Good", _items(1)))
    add_feed_from_parsed(db, good, "https://good/rss", False, include="all")
    add_feed_from_parsed(db, feedparser.parse(rss("Bad", _items(1))),
                         "https://bad/rss", False, include="all")
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed",
                        lambda url, ua, *a: FeedFetch(feedparser.parse(
                            rss("Good", _items(2))), f'W/"{url}"', None))
    real_ingest = feeds_mod.ingest_entries
    monkeypatch.setattr(feeds_mod, "ingest_entries",
                        lambda d, fid, parsed: (_ for _ in ()).throw(
                            sqlite3.OperationalError("database is locked"))
                        if fid == 2 else real_ingest(d, fid, parsed))

    refresh_all(db, cfg)  # must not raise

    assert len(db.episodes_for_feed(1)) == 2       # the healthy feed ingested
    assert db.get_feed(1)["etag"] == 'W/"https://good/rss"'


def test_a_feed_that_fails_keeps_its_old_validators(
        db: Database, cfg: Config, monkeypatch) -> None:
    """Validators name a body that was ingested. Storing them for a body
    that failed would have the feed send If-None-Match for it forever after,
    be answered 304, and never be parsed again — a station that goes quiet
    with no error to show for it."""
    parsed = feedparser.parse(rss("Show", _items(1)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    db.set_feed_validators(fid, 'W/"good"', None)
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed",
                        lambda *a, **kw: FeedFetch(parsed, 'W/"broken"', None))
    monkeypatch.setattr(feeds_mod, "ingest_entries",
                        lambda *a: (_ for _ in ()).throw(ValueError("bad feed")))

    refresh_all(db, cfg)

    assert db.get_feed(fid)["etag"] == 'W/"good"'


def test_refresh_all_prunes_stale_news(db: Database, cfg: Config, monkeypatch) -> None:
    stale = NOW - timedelta(hours=48)
    parsed = feedparser.parse(rss("News", [("Old news", stale), ("Fresh news", NOW)]))
    fid = add_feed_from_parsed(db, parsed, "https://n/rss", True, include="all")
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", lambda *a, **kw: FeedFetch(parsed))
    refresh_all(db, cfg)
    st = _statuses(db, fid)
    assert st["guid-old-news"] == "skipped" and st["guid-fresh-news"] == "new"


def test_refresh_all_survives_a_broken_feed(db: Database, cfg: Config, monkeypatch) -> None:
    good = feedparser.parse(rss("Good", _items(1)))
    add_feed_from_parsed(db, good, "https://good/rss", False, include="new_only")
    db.add_feed("https://broken/rss", "Broken", None, False)
    calls: list[str] = []

    def fake_fetch(url: str, ua: str, etag=None, last_modified=None):
        calls.append(url)
        if "broken" in url:
            raise OSError("connection refused")
        return FeedFetch(feedparser.parse(rss("Good", _items(2))))

    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", fake_fetch)
    refresh_all(db, cfg)  # must not raise
    assert len(calls) == 2


def test_refresh_news_fetches_only_news_feeds(db: Database, cfg: Config, monkeypatch) -> None:
    """What a wake pays for. Fetching is serial with a 30s timeout apiece,
    so touching the rotation shows here is what made an alarm cost minutes."""
    add_feed_from_parsed(db, feedparser.parse(rss("News", _items(1))),
                         "https://news/rss", True, include="all")
    add_feed_from_parsed(db, feedparser.parse(rss("Show", _items(1))),
                         "https://show/rss", False, include="all")
    calls: list[str] = []

    def fake_fetch(url: str, ua: str, etag=None, last_modified=None):
        calls.append(url)
        return FeedFetch(feedparser.parse(rss("News", _items(2))))

    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", fake_fetch)
    refresh_news(db, cfg)
    assert calls == ["https://news/rss"]


def test_refresh_news_still_prunes_stale_news(db: Database, cfg: Config, monkeypatch) -> None:
    stale = NOW - timedelta(hours=48)
    parsed = feedparser.parse(rss("News", [("Old news", stale), ("Fresh news", NOW)]))
    fid = add_feed_from_parsed(db, parsed, "https://n/rss", True, include="all")
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", lambda *a, **kw: FeedFetch(parsed))
    refresh_news(db, cfg)
    st = _statuses(db, fid)
    assert st["guid-old-news"] == "skipped" and st["guid-fresh-news"] == "new"


def test_refresh_news_skips_disabled_feeds(db: Database, cfg: Config, monkeypatch) -> None:
    fid = add_feed_from_parsed(db, feedparser.parse(rss("News", _items(1))),
                               "https://news/rss", True, include="all")
    db.toggle_feed(fid)  # switched off
    calls: list[str] = []
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed",
                        lambda *a, **kw: calls.append(a[0]) or FeedFetch(
                            feedparser.parse(rss("N", _items(1)))))
    refresh_news(db, cfg)
    assert calls == []


def test_fresh_and_stale_news_never_overlap(db: Database, cfg: Config) -> None:
    """The DJ stages news on one thread while the refresh thread prunes. The
    only thing keeping that safe is that the two sides of the cutoff are
    disjoint — otherwise the pruner could bin an episode fresh_news just
    handed over, in the window before mark_queued claims it."""
    fid = db.add_feed("https://n/rss", "News", None, True)
    cutoff = news_cutoff_iso(cfg)  # one cutoff, used for every call below
    # an episode sitting exactly on the line is what tells the two predicates
    # apart — anything else lands clear of both and proves nothing
    db.insert_episode(fid, "g-on-the-line", "On the line", "https://cdn/l.mp3", cutoff)
    db.insert_episode(fid, "g-fresh", "Fresh", "https://cdn/f.mp3",
                      (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.insert_episode(fid, "g-stale", "Stale", "https://cdn/s.mp3",
                      (NOW - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    handed_out = {e["id"] for e in db.fresh_news(cutoff)}
    assert len(handed_out) == 2, "the boundary episode counts as playable news"

    db.prune_stale_news(cutoff)

    statuses = {e["id"]: e["status"] for e in db.episodes_for_feed(fid)}
    assert all(statuses[eid] == "new" for eid in handed_out), \
        "prune binned an episode fresh_news had already handed over"
    assert sum(s == "skipped" for s in statuses.values()) == 1  # only the stale one


def test_news_cutoff_format(cfg: Config) -> None:
    cutoff = news_cutoff_iso(cfg)
    assert len(cutoff) == 20 and cutoff.endswith("Z")


def test_duration_parsed_from_itunes_tag(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", [("Long Ep", NOW, "1:02:03"),
                                           ("Short Ep", NOW, "28:00"),
                                           ("Plain Ep", NOW, "95"),
                                           ("No Dur", NOW)]))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    durs = {e["guid"]: e["duration_seconds"] for e in db.episodes_for_feed(fid)}
    assert durs["guid-long-ep"] == 3723
    assert durs["guid-short-ep"] == 1680
    assert durs["guid-plain-ep"] == 95
    assert durs["guid-no-dur"] is None


def test_duration_backfilled_on_refresh(db: Database) -> None:
    without = feedparser.parse(rss("Show", [("Ep One", NOW)]))
    fid = add_feed_from_parsed(db, without, "https://x/rss", False, include="all")
    assert db.episodes_for_feed(fid)[0]["duration_seconds"] is None
    with_dur = feedparser.parse(rss("Show", [("Ep One", NOW, "10:00")]))
    assert ingest_entries(db, fid, with_dur) == 0  # no new rows, just backfill
    assert db.episodes_for_feed(fid)[0]["duration_seconds"] == 600


class _FakeItunesResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_search_podcasts_normalizes_and_drops_feedless_entries(monkeypatch) -> None:
    import feeds as feeds_mod

    payload = {
        "results": [
            {"collectionName": "Mothman Museum Hour", "artistName": "Jane Doe",
             "artworkUrl100": "https://example/art.jpg", "feedUrl": "https://example/feed.xml"},
            {"collectionName": "No Feed Show", "artistName": "No One",
             "artworkUrl100": "https://example/art2.jpg"},
        ]
    }
    monkeypatch.setattr(feeds_mod.requests, "get", lambda *a, **k: _FakeItunesResponse(payload))
    results = feeds_mod.search_podcasts("mothman", "TestAgent/1.0")
    assert results == [{
        "title": "Mothman Museum Hour", "author": "Jane Doe",
        "artwork_url": "https://example/art.jpg", "feed_url": "https://example/feed.xml",
    }]


def test_search_podcasts_handles_missing_fields(monkeypatch) -> None:
    import feeds as feeds_mod

    payload = {"results": [{"feedUrl": "https://example/feed.xml"}]}
    monkeypatch.setattr(feeds_mod.requests, "get", lambda *a, **k: _FakeItunesResponse(payload))
    results = feeds_mod.search_podcasts("x", "TestAgent/1.0")
    assert results == [{"title": "", "author": "", "artwork_url": "", "feed_url": "https://example/feed.xml"}]


class _ParsedStub:
    """Minimal stand-in for a feedparser result: .feed and entries support .get."""
    def __init__(self, itunes_type: str | None = None, titles: tuple[str, ...] = ()):
        self.feed = {"itunes_type": itunes_type} if itunes_type else {}
        self.entries = [{"title": t} for t in titles]


def test_detect_declared_serial_is_in_order() -> None:
    parsed = _ParsedStub(itunes_type="serial", titles=("Anything", "At all"))
    assert detect_playback_mode(parsed) == "in_order"


def test_detect_numbered_titles_is_in_order() -> None:
    parsed = _ParsedStub(titles=("1. Alpha", "2. Beta", "3. Gamma", "Bonus chat"))
    assert detect_playback_mode(parsed) == "in_order"  # 3/4 numbered >= 50%


def test_detect_plain_titles_is_random() -> None:
    parsed = _ParsedStub(titles=("The Mothman", "Owls", "A chat with Sam", "Cheese"))
    assert detect_playback_mode(parsed) == "random"


def test_detect_empty_feed_is_in_order() -> None:
    assert detect_playback_mode(_ParsedStub()) == "in_order"


def test_add_feed_from_parsed_sets_detected_mode(db: Database) -> None:
    items = "".join(
        f"<item><title>{i}. Chapter {i}</title><guid>g{i}</guid>"
        f"<enclosure url='https://cdn/x/{i}.mp3' type='audio/mpeg'/></item>"
        for i in range(1, 5))
    parsed = feedparser.parse(
        f"<rss version='2.0'><channel><title>Serialized</title>{items}</channel></rss>")
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    assert db.get_feed(fid)["playback_mode"] == "in_order"
