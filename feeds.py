"""Feed fetching and ingest. Dates are normalized to UTC
'%Y-%m-%dT%H:%M:%SZ' at ingest so string comparison == time comparison."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import NamedTuple

import feedparser
import requests

from config import Config
from db import Database, EpisodeSeed, utcnow_iso

logger = logging.getLogger(__name__)

INCLUDE_MODES = ("new_only", "latest", "last_n", "all")

# ── descriptions ─────────────────────────────────────────────────────
# Show notes are arbitrary HTML written by strangers, and this app has no
# authentication — every endpoint moves a speaker. So the markup never
# reaches the database: it is reduced to plain text here, at the boundary,
# and everything downstream handles a string that esc() already renders
# safely. Tags are discarded rather than filtered, which is a smaller
# problem than sanitizing: there is no allowlist to get wrong.
EPISODE_DESCRIPTION_MAX = 2000
FEED_DESCRIPTION_MAX = 1000

# Text inside these never belongs to the prose.
_DROP_CONTENT_TAGS = frozenset({"script", "style"})
# Block-level ends that read as a line break once the tags are gone.
_BREAK_TAGS = frozenset({
    "br", "p", "div", "li", "tr", "blockquote", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
})


class _TextExtractor(HTMLParser):
    """HTML in, plain text out. convert_charrefs (on by default) decodes
    entities exactly once, so a feed that publishes '&amp;amp;' yields
    '&amp;' rather than '&'."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._dropping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._dropping += 1
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._dropping = max(0, self._dropping - 1)
        elif tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _truncate(text: str, limit: int) -> str:
    """Cut at the last whitespace before the limit. Some feeds put a whole
    transcript in <description>; without a cap the database would carry tens
    of MB to render four lines in a disclosure panel."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    match = re.search(r"\s\S*$", cut)
    if match and match.start() > 0:
        cut = cut[:match.start()]
    return cut.rstrip() + "…"


def html_to_text(raw: str | None, limit: int = EPISODE_DESCRIPTION_MAX) -> str | None:
    """Plain text, or None. Returns None rather than '' for a description
    that is absent, markup-only, or whitespace — insert_episodes backfills
    on `description IS NULL`, so storing '' would make that row permanently
    ineligible for a description it later publishes."""
    if not raw:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # A parse this malformed is not worth a failed ingest; the episode
        # is still perfectly playable without its notes.
        logger.debug("could not parse description as HTML", exc_info=True)
        return None
    text = parser.text()
    # Collapse runs of spaces/tabs, then runs of blank lines, then strip.
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _truncate(text, limit) or None

# ── arc detection ────────────────────────────────────────────────────
# Leading episode numbering is show-level info ("437. ", "Show 66 - ",
# "10.91- "): stripped, but never counts as an arc marker.
_LEADING_NUM_RE = re.compile(
    r"^\s*(?:(?:show|episode|ep\.?|#)\s*)?\d+(?:\.\d+)?\s*[.\-–—:]\s*",
    re.IGNORECASE,
)
_PART_MARKER_RES = (
    re.compile(r"\(?\bpart\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b\)?",
               re.IGNORECASE),
    re.compile(r"\(?\bpt\.?\s*\d+\b\)?", re.IGNORECASE),
    re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)"),
    re.compile(r"\b\d+\s+of\s+\d+\b", re.IGNORECASE),
)
# Uppercase-only, so word endings ("remix", "xi") never match.
_TRAILING_ROMAN_RE = re.compile(
    r"\s+(?:XIII|XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I)\s*$")


def _strip_part_marker(title: str) -> tuple[str, bool]:
    """(title without its part marker, whether one was there)."""
    text = _LEADING_NUM_RE.sub("", title or "")
    found = False
    for pat in _PART_MARKER_RES:
        if pat.search(text):
            found = True
            text = pat.sub(" ", text)
    if _TRAILING_ROMAN_RE.search(text):
        found = True
        text = _TRAILING_ROMAN_RE.sub("", text)
    # a marker stripped from a prefix ("Part One: X") can leave its
    # separator behind
    return text.lstrip(" \t:–—-."), found


def _tidy(text: str) -> str | None:
    return re.sub(r"\s+", " ", text).strip(" \t-–—:.,!?'\"()").lower() or None


def arc_key(title: str) -> str | None:
    """Grouping key for multi-part arcs, or None when the title carries no
    part marker (the episode stands alone). Grouping is only ever done
    between equal non-None keys within one feed's unplayed episodes.

    Deliberately loose: it truncates at the first separator so per-part
    subtitles ("The Fall of the Aztecs: The Great Escape") still group. That
    suits the rotation guard, where over-grouping only ever costs a redirect
    to some other story's part one. Use series_key() for anything that acts on
    the whole group."""
    text, found = _strip_part_marker(title)
    if not found:
        return None
    # per-part subtitles truncate at the first real separator; plain hyphens
    # are too common inside titles to count
    positions = [i for i in (text.find(s) for s in (":", "–", "—")) if i > 0]
    if positions:
        text = text[:min(positions)]
    return _tidy(text)


def series_key(title: str) -> str | None:
    """Strict grouping key: the whole title minus its part marker.

    arc_key's truncation is wrong the moment something acts on every member,
    because plenty of shows lead with a banner rather than the story —
    "SYMHC Classics: Palmer Raids Pt. 1" truncates to "symhc classics", which
    collapses sixteen unrelated stories into one arc. Keeping the full title
    separates them ("symhc classics: palmer raids" against "symhc classics:
    irish famine") while still matching real siblings, whose titles differ
    only by the marker."""
    text, found = _strip_part_marker(title)
    return _tidy(text) if found else None


SEQUENTIAL_TITLE_THRESHOLD = 0.5


def detect_playback_mode(parsed) -> str:
    """in_order when the feed declares itunes:type=serial or when at least
    half its titles carry show numbering / part markers; random otherwise.
    Classification errors land on the safe side: in_order is the
    pre-feature behavior, and the per-station toggle fixes any miss."""
    if (parsed.feed.get("itunes_type") or "").lower() == "serial":
        return "in_order"
    titles = [e.get("title", "") for e in parsed.entries]
    if not titles:
        return "in_order"
    numbered = sum(
        1 for t in titles
        if _LEADING_NUM_RE.match(t or "") or arc_key(t) is not None)
    return "in_order" if numbered / len(titles) >= SEQUENTIAL_TITLE_THRESHOLD else "random"


class FeedError(Exception):
    pass


class FeedFetch(NamedTuple):
    """A feed as the server just described it.

    parsed is None when the server answered 304 Not Modified: there is no
    body to read and nothing to ingest. The validators come back either way
    so the next request can ask the same question — a 304 may echo them or
    omit them, and omitting is not the same as revoking."""
    parsed: feedparser.FeedParserDict | None
    etag: str | None = None
    last_modified: str | None = None


def fetch_feed(url: str, user_agent: str, etag: str | None = None,
               last_modified: str | None = None) -> FeedFetch:
    """Fetch a feed, asking the server to skip the body if nothing has
    changed since the validators it gave us last time.

    Most stations publish weekly and are re-read every REFRESH_MINUTES, so
    the overwhelming majority of fetches have nothing to report. A 304 costs
    one round trip and no parse — and parsing is the expensive half: feeds
    here run to 2851 entries, and feedparser holds the GIL through them."""
    headers = {"User-Agent": user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if resp.status_code == 304:
        if not (etag or last_modified):
            raise FeedError(f"feed answered 304 to an unconditional request: {url}")
        return FeedFetch(None, resp.headers.get("ETag") or etag,
                         resp.headers.get("Last-Modified") or last_modified)
    parsed = feedparser.parse(resp.content)
    if not parsed.entries and parsed.bozo:
        raise FeedError(f"could not parse feed: {url}")
    return FeedFetch(parsed, resp.headers.get("ETag"),
                     resp.headers.get("Last-Modified"))


def search_podcasts(term: str, user_agent: str) -> list[dict]:
    """Search Apple's podcast directory by name. No API key or signup
    required. Shorter timeout than fetch_feed's 30s -- this backs an
    interactive search-as-you-type UI, not a one-time feed add."""
    resp = requests.get(
        "https://itunes.apple.com/search",
        params={"media": "podcast", "entity": "podcast", "limit": 15, "term": term},
        headers={"User-Agent": user_agent},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in data.get("results", []):
        feed_url = r.get("feedUrl")
        if not feed_url:
            continue  # some iTunes podcast entries have no feed -- never surface an unusable pick
        results.append({
            "title": r.get("collectionName") or "",
            "author": r.get("artistName") or "",
            "artwork_url": r.get("artworkUrl100") or "",
            "feed_url": feed_url,
        })
    return results


def entry_audio_url(entry) -> str | None:
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url")
        if href:
            return href
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return None


def entry_published_iso(entry) -> str:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t is None:
        return utcnow_iso()
    return datetime(*t[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_duration_seconds(entry) -> int | None:
    """itunes:duration comes as 'HH:MM:SS', 'MM:SS', or plain seconds —
    normalize to seconds at the boundary, like every other date/number."""
    raw = entry.get("itunes_duration")
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        if ":" in raw:
            parts = [int(p) for p in raw.split(":")]
            while len(parts) < 3:
                parts.insert(0, 0)
            h, m, s = parts[-3:]
            return h * 3600 + m * 60 + s
        return int(float(raw))
    except ValueError:
        return None


def entry_description(entry) -> str | None:
    """Prefer <description>/<itunes:summary> over <content:encoded>: the
    summary is the blurb a show writes for listings, while content is often
    the same text wrapped in a page's worth of markup and player embeds."""
    raw = entry.get("summary")
    if not raw:
        contents = entry.get("content") or []
        raw = contents[0].get("value") if contents else None
    return html_to_text(raw, EPISODE_DESCRIPTION_MAX)


def feed_description(parsed) -> str | None:
    raw = parsed.feed.get("subtitle") or parsed.feed.get("summary")
    return html_to_text(raw, FEED_DESCRIPTION_MAX)


def ingest_entries(db: Database, feed_id: int, parsed) -> int:
    """Hand the whole feed to the DB at once. Entries with no audio are
    dropped here — a feed's text-only posts are not episodes."""
    seeds = [EpisodeSeed(guid=entry.get("id") or audio_url,
                         title=entry.get("title", ""),
                         audio_url=audio_url,
                         published_at=entry_published_iso(entry),
                         duration_seconds=entry_duration_seconds(entry),
                         description=entry_description(entry))
             for entry in parsed.entries
             if (audio_url := entry_audio_url(entry))]
    return db.insert_episodes(feed_id, seeds)


def add_feed_from_parsed(db: Database, parsed, url: str, is_news: bool,
                         include: str = "latest", last_n: int | None = None) -> int:
    if include not in INCLUDE_MODES:
        raise ValueError(f"bad include mode: {include}")
    title = parsed.feed.get("title") or url
    image = (parsed.feed.get("image") or {}).get("href")
    feed_id = db.add_feed(url, title, image, is_news,
                          playback_mode=detect_playback_mode(parsed),
                          description=feed_description(parsed))
    ingest_entries(db, feed_id, parsed)
    episodes = db.episodes_for_feed(feed_id)  # newest first
    keep = {"new_only": 0, "latest": 1,
            "last_n": max(1, int(last_n or 1)), "all": len(episodes)}[include]
    for episode in episodes[keep:]:
        db.archive_episode(episode["id"])
    return feed_id


def add_feed(db: Database, cfg: Config, url: str, is_news: bool,
             include: str = "latest", last_n: int | None = None) -> int:
    return add_feed_from_parsed(db, fetch_feed(url, cfg.user_agent).parsed,
                                url, is_news, include, last_n)


def news_cutoff_iso(cfg: Config) -> str:
    """The one line between news worth playing and news to bin.

    Both sides of it must stay complementary — `fresh_news` takes
    published_at >= cutoff, `prune_stale_news` takes < cutoff. They now run
    on different threads (the DJ stages news while the refresh thread
    prunes), and there is a window between `fresh_news` handing an episode
    over and `mark_queued` claiming it where the episode is still 'new'.
    Disjointness is what keeps that window harmless; widen either side and
    the pruner can bin an episode the DJ is in the middle of enqueueing."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.news_max_age_hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _refresh(db: Database, cfg: Config, feeds_to_fetch) -> None:
    """Fetch each feed in turn, then prune news that has aged out. One dead
    feed never stops the rest — a station whose host is down is a station
    with no new episodes, not a failed refresh."""
    for feed in feeds_to_fetch:
        if not feed["enabled"]:
            continue
        try:
            fetched = fetch_feed(feed["url"], cfg.user_agent,
                                 feed["etag"], feed["last_modified"])
            # A 304 is a successful answer, not a failure: the server
            # compared the validators for us and there is nothing to parse.
            if fetched.parsed is not None:
                ingest_entries(db, feed["id"], fetched.parsed)
                db.backfill_feed_description(feed["id"],
                                             feed_description(fetched.parsed))
            # Last, and inside the try, so validators are only ever recorded
            # against a body that was actually ingested. A fetch or parse
            # that failed leaves the previous pair standing: the broken body
            # carries a different validator, so the next pass is answered
            # 200 and retried rather than 304'd into silence forever.
            # Recorded even when nothing new came back, though — a feed whose
            # validators are never stored can never start answering 304.
            db.set_feed_validators(feed["id"], fetched.etag, fetched.last_modified)
        except Exception:
            logger.exception("refresh failed for feed %s (%s)", feed["id"], feed["url"])
    pruned = db.prune_stale_news(news_cutoff_iso(cfg))
    if pruned:
        logger.info("pruned %d stale news episodes", pruned)


def refresh_all(db: Database, cfg: Config) -> None:
    _refresh(db, cfg, db.list_feeds())


def refresh_news(db: Database, cfg: Config) -> None:
    """Only the news feeds. Fetching is serial with a 30s timeout apiece, so
    the cost of a refresh scales with the library — minutes across a full
    one. A wake needs the morning's headlines, which is a handful of feeds;
    the rotation shows it queues behind them are read from the DB and do not
    have to be minutes-fresh, since the standing refresh already keeps them
    current."""
    _refresh(db, cfg, [f for f in db.list_feeds() if f["is_news"]])
