"""Show notes: HTML reduced to plain text at the boundary, and the backfill
that reaches episodes which already exist."""
from datetime import datetime, timedelta, timezone

import feedparser

from db import Database, EpisodeSeed
from feeds import (EPISODE_DESCRIPTION_MAX, add_feed_from_parsed,
                   entry_description, feed_description, html_to_text,
                   ingest_entries)
from rss_fixtures import rss

NOW = datetime.now(timezone.utc)


def _seed(guid: str, description: str | None = None) -> EpisodeSeed:
    return EpisodeSeed(guid=guid, title=guid, audio_url=f"https://cdn/{guid}.mp3",
                       published_at="2026-01-01T00:00:00Z", description=description)


# ── html_to_text ─────────────────────────────────────────────────────

def test_tags_are_stripped_to_prose() -> None:
    assert html_to_text("<p>Hello <em>there</em>.</p>") == "Hello there."


def test_entities_decoded_exactly_once() -> None:
    # A feed publishing a literal '&amp;' must yield '&', and one publishing
    # '&amp;amp;' must yield '&amp;' — decoding twice would collapse both.
    assert html_to_text("Tom &amp; Jerry") == "Tom & Jerry"
    assert html_to_text("Tom &amp;amp; Jerry") == "Tom &amp; Jerry"


def test_script_and_style_contents_dropped() -> None:
    raw = "<p>Real</p><script>alert('x')</script><style>p{color:red}</style>"
    assert html_to_text(raw) == "Real"


def test_script_content_never_survives_as_text() -> None:
    # The whole reason stripping happens at ingest rather than at render.
    assert "alert" not in (html_to_text("<script>alert(1)</script>hi") or "")


def test_breaks_become_newlines_and_blank_runs_collapse() -> None:
    assert html_to_text("a<br>b") == "a\nb"
    assert html_to_text("<p>a</p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_markup_only_and_whitespace_become_none() -> None:
    # Not "" — insert_episodes backfills on `description IS NULL`, so an
    # empty string would make the row permanently ineligible.
    assert html_to_text("<p></p>") is None
    assert html_to_text("   \n  ") is None
    assert html_to_text("") is None
    assert html_to_text(None) is None


def test_truncates_at_word_boundary() -> None:
    text = html_to_text("word " * 100, limit=20)
    assert text.endswith("…")
    assert len(text) <= 21
    assert "wor…" not in text  # cut between words, not mid-word


def test_under_limit_is_not_truncated() -> None:
    assert html_to_text("short", limit=20) == "short"


def test_malformed_markup_does_not_raise() -> None:
    assert html_to_text("<p>unclosed <b>bold") == "unclosed bold"


# ── field selection ──────────────────────────────────────────────────

def test_summary_preferred_over_content() -> None:
    entry = {"summary": "<p>The blurb</p>",
             "content": [{"value": "<p>The whole page</p>"}]}
    assert entry_description(entry) == "The blurb"


def test_content_used_when_summary_absent() -> None:
    assert entry_description({"content": [{"value": "<p>From content</p>"}]}) \
        == "From content"


def test_no_description_fields_yields_none() -> None:
    assert entry_description({"title": "x"}) is None


def test_feed_description_falls_back_to_summary() -> None:
    parsed = feedparser.parse(rss("Show", [], feed_description="<p>About the show</p>"))
    assert feed_description(parsed) == "About the show"


# ── ingest ───────────────────────────────────────────────────────────

def test_description_stored_on_ingest(db: Database) -> None:
    parsed = feedparser.parse(
        rss("Show", [("Ep One", NOW, None, "<p>What happens</p>")]))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    assert db.episodes_for_feed(fid)[0]["description"] == "What happens"


def test_show_description_stored_on_add(db: Database) -> None:
    parsed = feedparser.parse(
        rss("Show", [("Ep One", NOW)], feed_description="A show about things"))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False)
    assert db.get_feed(fid)["description"] == "A show about things"


def test_episode_without_description_is_null(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", [("Ep One", NOW)]))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")
    assert db.episodes_for_feed(fid)[0]["description"] is None


# ── backfill ─────────────────────────────────────────────────────────
# The load-bearing path: a re-fetch re-offers episodes that already exist,
# and INSERT OR IGNORE skips them. Without the backfill UPDATE the forced
# refresh would download the whole library and change nothing.

def test_backfill_fills_a_null_description(db: Database) -> None:
    fid = db.add_feed("https://x/rss", "Show", None, False)
    assert db.insert_episodes(fid, [_seed("a")]) == 1
    assert db.episodes_for_feed(fid)[0]["description"] is None

    assert db.insert_episodes(fid, [_seed("a", "arrived later")]) == 0
    assert db.episodes_for_feed(fid)[0]["description"] == "arrived later"


def test_backfill_does_not_overwrite_existing(db: Database) -> None:
    fid = db.add_feed("https://x/rss", "Show", None, False)
    db.insert_episodes(fid, [_seed("a", "original")])
    db.insert_episodes(fid, [_seed("a", "rewritten")])
    assert db.episodes_for_feed(fid)[0]["description"] == "original"


def test_backfill_feed_description_fills_then_holds(db: Database) -> None:
    fid = db.add_feed("https://x/rss", "Show", None, False)
    assert db.get_feed(fid)["description"] is None
    db.backfill_feed_description(fid, "first")
    db.backfill_feed_description(fid, "second")
    assert db.get_feed(fid)["description"] == "first"


def test_backfill_feed_description_ignores_none(db: Database) -> None:
    fid = db.add_feed("https://x/rss", "Show", None, False, description="kept")
    db.backfill_feed_description(fid, None)
    assert db.get_feed(fid)["description"] == "kept"


# ── migration ────────────────────────────────────────────────────────

def _strip_description_columns(db: Database) -> None:
    """Rewind a database to before descriptions existed, and give it the
    validators a live feed would be carrying.

    DROP COLUMN rewrites the stored CREATE TABLE text, so a column that is
    both last and comment-preceded leaves a trailing `--` that swallows the
    closing paren ("incomplete input"). SCHEMA is ordered to avoid that.
    """
    with db._conn() as c:
        c.execute("ALTER TABLE episodes DROP COLUMN description")
        c.execute("ALTER TABLE feeds DROP COLUMN description")
        c.execute("UPDATE feeds SET etag='W/\"abc\"', last_modified='Mon, 01 Jan 2026'")


def test_migration_adds_columns_to_an_old_database(db: Database) -> None:
    db.add_feed("https://x/rss", "Show", None, False)
    _strip_description_columns(db)

    db.init()

    with db._conn() as c:
        assert "description" in {r["name"] for r in c.execute("PRAGMA table_info(episodes)")}
        assert "description" in {r["name"] for r in c.execute("PRAGMA table_info(feeds)")}


def test_migration_clears_validators_so_the_backfill_can_happen(db: Database) -> None:
    # Conditional refresh answers 304 for an unchanged feed and never parses
    # the body, so without this every existing feed would stay descriptionless
    # forever no matter how many refreshes ran.
    fid = db.add_feed("https://x/rss", "Show", None, False)
    _strip_description_columns(db)

    db.init()

    feed = db.get_feed(fid)
    assert feed["etag"] is None
    assert feed["last_modified"] is None


def test_migration_does_not_clear_validators_on_every_init(db: Database) -> None:
    # The clear rides inside the column guard. If it ran unconditionally,
    # every restart would re-download the entire library.
    fid = db.add_feed("https://x/rss", "Show", None, False)
    db.set_feed_validators(fid, 'W/"abc"', "Mon, 01 Jan 2026")

    db.init()

    assert db.get_feed(fid)["etag"] == 'W/"abc"'


def test_default_truncation_limit_is_the_episode_cap() -> None:
    long = "x" * (EPISODE_DESCRIPTION_MAX + 500)
    assert len(html_to_text(long)) <= EPISODE_DESCRIPTION_MAX + 1


# ── payloads ─────────────────────────────────────────────────────────

def test_description_reaches_the_status_payload(db: Database, cfg) -> None:
    from dj import DJ
    from fake_player import FakeSonosPlayer
    from web import create_app

    parsed = feedparser.parse(
        rss("Show", [("Ep One", NOW, None, "<p>What happens</p>")],
            feed_description="About the show"))
    add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")

    app = create_app(db, DJ(db, cfg, lambda: FakeSonosPlayer()), cfg)
    app.config["TESTING"] = True
    status = app.test_client().get("/api/status").get_json()

    assert status["stations"][0]["description"] == "About the show"


def test_description_reaches_the_episode_list(db: Database, cfg) -> None:
    from dj import DJ
    from fake_player import FakeSonosPlayer
    from web import create_app

    parsed = feedparser.parse(
        rss("Show", [("Ep One", NOW, None, "<p>What happens</p>")]))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="all")

    app = create_app(db, DJ(db, cfg, lambda: FakeSonosPlayer()), cfg)
    app.config["TESTING"] = True
    body = app.test_client().get(f"/api/feeds/{fid}/episodes").get_json()

    assert body["episodes"][0]["description"] == "What happens"
