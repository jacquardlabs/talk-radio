"""SQLite persistence. Every episode status transition lives here —
no raw status UPDATEs anywhere else in the codebase."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, NamedTuple

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    rotation_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    is_news INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    playback_mode TEXT NOT NULL DEFAULT 'in_order'
        CHECK (playback_mode IN ('in_order','random')),
    -- What the server last said this feed's body was, so the next refresh
    -- can ask for it only if it changed. Opaque strings: they are echoed
    -- back verbatim as If-None-Match / If-Modified-Since and never parsed.
    etag TEXT,
    last_modified TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    audio_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','queued','played','skipped','archived')),
    play_uri TEXT,
    resume_seconds INTEGER,
    local_path TEXT,
    played_at TEXT,
    duration_seconds INTEGER,
    -- The furthest position Sonos was ever seen to reach in this episode.
    -- Evidence that audio actually happened: an episode the speaker refused
    -- to play (dead link, URI too long for it to store) is retired with this
    -- still NULL, and must not be recorded as heard.
    observed_seconds INTEGER,
    -- Playback failures are counted rather than given a status of their own:
    -- a failure means "not heard yet", so the episode goes back to 'new' and
    -- gets another chance on a later rotation. The count is what stops a
    -- permanently dead link from cycling forever.
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_failed_at TEXT,
    -- Survives "Refresh all". Set on anything queued by hand, clear on
    -- anything the rotation picked, and togglable either way from the row.
    pinned INTEGER NOT NULL DEFAULT 0,
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_episodes_feed_status ON episodes (feed_id, status);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    days TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_date TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EpisodeSeed(NamedTuple):
    """One episode as a feed describes it — everything ingest can know before
    the row exists. Status, playback state and failure counts are the DB's to
    set, so they are absent here by design."""
    guid: str
    title: str
    audio_url: str
    published_at: str
    duration_seconds: int | None = None


class Database:
    """One short-lived connection per operation; WAL lets the web and DJ
    threads share the file safely."""

    def __init__(self, path: str) -> None:
        self.path = str(path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # journal_mode is NOT set here: WAL is written into the database
        # header and outlives the connection, so init() sets it once and
        # every connection after that inherits it. foreign_keys is the
        # opposite — per-connection, off by default, so it is set every time.
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as c:
            # First statement on the connection, before executescript opens a
            # transaction — journal_mode cannot be changed inside one.
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            # migration for databases created before duration_seconds existed
            cols = {r["name"] for r in c.execute("PRAGMA table_info(episodes)")}
            if "duration_seconds" not in cols:
                c.execute("ALTER TABLE episodes ADD COLUMN duration_seconds INTEGER")
            # Playback evidence and the shortlist. All plain ADD COLUMNs on
            # purpose: SQLite cannot alter the status CHECK constraint without
            # rebuilding the table, so a failed playback is recorded beside the
            # status rather than as one more value inside it.
            for name, ddl in (
                ("observed_seconds", "observed_seconds INTEGER"),
                ("failure_count", "failure_count INTEGER NOT NULL DEFAULT 0"),
                ("last_failed_at", "last_failed_at TEXT"),
            ):
                if name not in cols:
                    c.execute(f"ALTER TABLE episodes ADD COLUMN {ddl}")
            # The shortlist star became the Up Next pin: same bit, and the
            # rows already carrying it are exactly the ones that should
            # survive a refresh, so it is renamed rather than re-collected.
            if "pinned" not in cols:
                if "starred" in cols:
                    c.execute("ALTER TABLE episodes RENAME COLUMN starred TO pinned")
                else:
                    c.execute("ALTER TABLE episodes ADD COLUMN pinned"
                              " INTEGER NOT NULL DEFAULT 0")
            # migration for databases created before categories existed
            feed_cols = {r["name"] for r in c.execute("PRAGMA table_info(feeds)")}
            if "category_id" not in feed_cols:
                c.execute(
                    "ALTER TABLE feeds ADD COLUMN category_id INTEGER"
                    " REFERENCES categories(id) ON DELETE SET NULL"
                )
            if "playback_mode" not in feed_cols:
                c.execute(
                    "ALTER TABLE feeds ADD COLUMN playback_mode TEXT NOT NULL"
                    " DEFAULT 'in_order' CHECK (playback_mode IN ('in_order','random'))"
                )
            # migration for databases created before conditional refresh.
            # NULL means "never asked" — the first refresh fetches in full
            # and fills them in.
            for name in ("etag", "last_modified"):
                if name not in feed_cols:
                    c.execute(f"ALTER TABLE feeds ADD COLUMN {name} TEXT")

    # ── feeds ─────────────────────────────────────────────────────────
    def add_feed(self, url: str, title: str, image_url: str | None, is_news: bool,
                 playback_mode: str = "in_order") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO feeds (url, title, image_url, is_news, added_at, playback_mode)"
                " VALUES (?,?,?,?,?,?)",
                (url, title, image_url, int(is_news), utcnow_iso(), playback_mode),
            )
            return int(cur.lastrowid)

    def get_feed(self, feed_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()

    def list_feeds(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM feeds ORDER BY title COLLATE NOCASE").fetchall()

    def delete_feed(self, feed_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM feeds WHERE id=?", (feed_id,))

    def toggle_feed(self, feed_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE feeds SET enabled = 1 - enabled WHERE id=?", (feed_id,))

    def toggle_feed_playback(self, feed_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE feeds SET playback_mode = CASE playback_mode"
                " WHEN 'in_order' THEN 'random' ELSE 'in_order' END WHERE id=?",
                (feed_id,),
            )

    def set_feed_news(self, feed_id: int, is_news: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE feeds SET is_news=? WHERE id=?", (int(is_news), feed_id))

    def counts_by_feed(self) -> dict[int, dict[str, int]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT feed_id, status, COUNT(*) AS n FROM episodes GROUP BY feed_id, status"
            ).fetchall()
        out: dict[int, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["feed_id"], {})[r["status"]] = r["n"]
        return out

    # ── categories ────────────────────────────────────────────────────
    def add_category(self, name: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO categories (name) VALUES (?)", (name,))
            return int(cur.lastrowid)

    def get_category(self, category_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()

    def list_categories(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT cat.*, COUNT(f.id) AS station_count FROM categories cat"
                " LEFT JOIN feeds f ON f.category_id = cat.id"
                " GROUP BY cat.id ORDER BY cat.name COLLATE NOCASE"
            ).fetchall()

    def rename_category(self, category_id: int, name: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE categories SET name=? WHERE id=?", (name, category_id))

    def toggle_category_rotation(self, category_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE categories SET rotation_enabled = 1 - rotation_enabled WHERE id=?",
                (category_id,),
            )

    def delete_category(self, category_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM categories WHERE id=?", (category_id,))

    def set_feed_category(self, feed_id: int, category_id: int | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE feeds SET category_id=? WHERE id=?", (category_id, feed_id))

    def set_feed_validators(self, feed_id: int, etag: str | None,
                            last_modified: str | None) -> None:
        """Remember what the server said this feed's body was, so the next
        refresh can ask for it conditionally. Both may be None — a server
        that offers neither is simply one we always fetch in full."""
        with self._conn() as c:
            c.execute("UPDATE feeds SET etag=?, last_modified=? WHERE id=?",
                      (etag, last_modified, feed_id))

    # ── episodes ──────────────────────────────────────────────────────
    def insert_episodes(self, feed_id: int, episodes: list[EpisodeSeed]) -> int:
        """Everything a feed offers, in one transaction. Returns how many
        were new.

        One connection for the batch rather than one per episode. A refresh
        re-offers every episode the feed has ever published — 23.5k rows
        across the library — and each connection is opened, PRAGMA'd,
        committed and closed: 20.6s for a single 2851-episode feed on the
        deployed host, 0.04s batched.

        Rows that already exist are left alone apart from a duration
        backfill, which rides the same transaction. Episodes ingested before
        durations were parsed have none, and the feed is the only place one
        can come from."""
        if not episodes:
            return 0
        with self._conn() as c:
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO episodes"
                " (feed_id, guid, title, audio_url, published_at, duration_seconds)"
                " VALUES (?,?,?,?,?,?)",
                [(feed_id, e.guid, e.title, e.audio_url, e.published_at,
                  e.duration_seconds) for e in episodes],
            )
            inserted = c.total_changes - before
            c.executemany(
                "UPDATE episodes SET duration_seconds=?"
                " WHERE feed_id=? AND guid=? AND duration_seconds IS NULL",
                [(e.duration_seconds, feed_id, e.guid) for e in episodes
                 if e.duration_seconds is not None],
            )
            return inserted

    def insert_episode(self, feed_id: int, guid: str, title: str, audio_url: str,
                       published_at: str, duration_seconds: int | None = None) -> bool:
        """One episode. True if it was new."""
        return self.insert_episodes(feed_id, [EpisodeSeed(
            guid, title, audio_url, published_at, duration_seconds)]) == 1

    def get_episode(self, episode_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()

    def episodes_for_feed(self, feed_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM episodes WHERE feed_id=? ORDER BY published_at DESC, id DESC",
                (feed_id,),
            ).fetchall()

    def episodes_with_status(self, status: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM episodes WHERE status=? ORDER BY published_at ASC, id ASC",
                (status,),
            ).fetchall()

    @staticmethod
    def _cooling(retry_after_iso: str | None, prefix: str = "") -> tuple[str, tuple]:
        """SQL excluding episodes that failed to play too recently to be worth
        retrying. An episode put back to 'new' the instant it fails is the
        oldest unplayed row on its feed, so an in_order station would re-pick
        it on the very next top-up and burn its whole retry budget inside a
        minute — which turns a CDN hiccup into a permanent retirement."""
        if retry_after_iso is None:
            return "", ()
        col = f"{prefix}last_failed_at"
        return f" AND ({col} IS NULL OR {col} <= ?)", (retry_after_iso,)

    def oldest_new_for_feed(self, feed_id: int,
                            retry_after_iso: str | None = None) -> sqlite3.Row | None:
        cool, params = self._cooling(retry_after_iso)
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM episodes WHERE feed_id=? AND status='new'"
                f"{cool} ORDER BY published_at ASC, id ASC LIMIT 1",
                (feed_id, *params),
            ).fetchone()

    def new_episodes_for_feed(self, feed_id: int,
                              retry_after_iso: str | None = None) -> list[sqlite3.Row]:
        cool, params = self._cooling(retry_after_iso)
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM episodes WHERE feed_id=? AND status='new'"
                f"{cool} ORDER BY published_at ASC, id ASC",
                (feed_id, *params),
            ).fetchall()

    def rotation_feeds_with_new(self,
                                retry_after_iso: str | None = None) -> list[sqlite3.Row]:
        cool, params = self._cooling(retry_after_iso, "e.")
        with self._conn() as c:
            return c.execute(
                "SELECT f.* FROM feeds f LEFT JOIN categories cat ON cat.id=f.category_id"
                " WHERE f.enabled=1 AND f.is_news=0"
                " AND (f.category_id IS NULL OR cat.rotation_enabled=1) AND EXISTS"
                " (SELECT 1 FROM episodes e WHERE e.feed_id=f.id AND e.status='new'"
                f"{cool}) ORDER BY f.id",
                params,
            ).fetchall()

    def fresh_news(self, cutoff_iso: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT e.* FROM episodes e JOIN feeds f ON f.id=e.feed_id"
                " WHERE f.is_news=1 AND f.enabled=1 AND e.status='new' AND e.published_at>=?"
                " ORDER BY e.published_at ASC, e.id ASC",
                (cutoff_iso,),
            ).fetchall()

    def prune_stale_news(self, cutoff_iso: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET status='skipped' WHERE status='new' AND published_at<?"
                " AND feed_id IN (SELECT id FROM feeds WHERE is_news=1)",
                (cutoff_iso,),
            )
            return cur.rowcount

    def recently_played(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT e.*, f.title AS feed_title FROM episodes e"
                " JOIN feeds f ON f.id=e.feed_id WHERE e.status='played'"
                " ORDER BY e.played_at DESC, e.id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # ── status transitions ────────────────────────────────────────────
    def mark_queued(self, episode_id: int, play_uri: str, local_path: str | None = None) -> None:
        """Staging clears observed_seconds, so every attempt is judged on the
        audio it produces rather than on some earlier one's.

        It has to happen here because this is the only way an episode enters
        the queue. The revert paths — revert_all_queued() at the top of every
        start() and in stop_off_air() — deliberately leave the column alone,
        so without this an episode listened to for five minutes yesterday
        would carry that evidence into today's staging and be booked as heard
        even if the CDN refused it."""
        with self._conn() as c:
            c.execute(
                "UPDATE episodes SET status='queued', play_uri=?, local_path=?,"
                " observed_seconds=NULL WHERE id=?",
                (play_uri, local_path, episode_id),
            )

    def mark_played(self, episode_id: int, played_at: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT local_path FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            c.execute(
                "UPDATE episodes SET status='played', played_at=?, resume_seconds=NULL,"
                " play_uri=NULL, local_path=NULL, observed_seconds=NULL WHERE id=?",
                (played_at, episode_id),
            )
            return row["local_path"] if row else None

    def record_observed(self, episode_id: int, seconds: int) -> None:
        """Bank the furthest point Sonos has been seen to reach. Monotonic —
        the speaker reports 0 for a moment while it transitions, and a track
        that has demonstrably played must not un-prove itself."""
        with self._conn() as c:
            c.execute(
                "UPDATE episodes SET observed_seconds=MAX(COALESCE(observed_seconds,0),?)"
                " WHERE id=?",
                (max(0, int(seconds)), episode_id),
            )

    def mark_failed(self, episode_id: int, failed_at: str, max_failures: int) -> bool:
        """Record that an episode was retired having produced no audio.

        Back to 'new' so a later rotation retries it — a CDN outage is not a
        reason to lose an episode out of the backlog for good. Returns True
        once the count reaches max_failures and the episode is given up on as
        'skipped', so a permanently dead link stops cycling."""
        with self._conn() as c:
            row = c.execute(
                "SELECT failure_count FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if row is None:
                return False
            count = int(row["failure_count"] or 0) + 1
            spent = count >= max_failures
            c.execute(
                "UPDATE episodes SET status=?, failure_count=?, last_failed_at=?,"
                " play_uri=NULL, local_path=NULL, resume_seconds=NULL,"
                " observed_seconds=NULL WHERE id=?",
                ("skipped" if spent else "new", count, failed_at, episode_id),
            )
            return spent

    def clear_failures(self, episode_id: int) -> None:
        """A successful listen wipes the slate: the next failure on this
        episode starts a fresh count rather than inheriting an old CDN blip."""
        with self._conn() as c:
            c.execute("UPDATE episodes SET failure_count=0 WHERE id=?", (episode_id,))

    def recent_failures(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT e.*, f.title AS feed_title FROM episodes e"
                " JOIN feeds f ON f.id=e.feed_id WHERE e.last_failed_at IS NOT NULL"
                " ORDER BY e.last_failed_at DESC, e.id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def failure_counts_by_feed(self) -> dict[int, int]:
        """How many distinct episodes on each station have failed to play —
        one dead episode is bad luck, fifty is a broken station."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT feed_id, COUNT(*) AS n FROM episodes"
                " WHERE failure_count > 0 GROUP BY feed_id"
            ).fetchall()
        return {r["feed_id"]: r["n"] for r in rows}

    # ── the durable Up Next ───────────────────────────────────────────
    # Order lives here rather than on the Sonos queue, which is only ever a
    # rolling window onto the next few: signed CDN URLs expire in hours, so a
    # queue staged deeper than that would rot before it played.
    _ORDER_KEY = "up_next_order"

    def set_pinned(self, episode_id: int, pinned: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE episodes SET pinned=? WHERE id=?",
                      (int(pinned), episode_id))

    def set_up_next(self, episode_ids: list[int]) -> None:
        self.kv_set(self._ORDER_KEY, json.dumps([int(i) for i in episode_ids]))

    def up_next_order(self) -> list[int]:
        raw = self.kv_get(self._ORDER_KEY)
        if not raw:
            return []
        try:
            return [int(i) for i in json.loads(raw)]
        except (TypeError, ValueError):
            logger.warning("up_next order unreadable; starting from empty")
            return []

    def remove_from_up_next(self, episode_id: int) -> None:
        """Take an episode out of the saved order.

        Called explicitly by everything that deliberately removes one, because
        status cannot be read backwards: an episode dropped, deferred or
        failed all land on 'new', and so does one merely waiting its turn.
        Inferring it from status puts dropped episodes straight back."""
        order = self.up_next_order()
        if episode_id in order:
            self.set_up_next([i for i in order if i != episode_id])

    def episodes_by_ids(self, episode_ids: list[int]) -> list[sqlite3.Row]:
        """Rows for these ids, in the order asked for. Missing ids are
        dropped — a feed can be deleted out from under the queue."""
        if not episode_ids:
            return []
        placeholders = ",".join("?" * len(episode_ids))
        with self._conn() as c:
            found = {r["id"]: r for r in c.execute(
                f"SELECT * FROM episodes WHERE id IN ({placeholders})", episode_ids)}
        return [found[i] for i in episode_ids if i in found]

    def revert_queued_except(self, keep_ids: list[int]) -> int:
        """Put back any episode still flagged 'queued' that the saved order
        no longer lists. Without this a row orphaned by a lost or truncated
        order would sit 'queued' forever: never played, and never picked
        again, because the rotation only ever draws from 'new'."""
        with self._conn() as c:
            if keep_ids:
                placeholders = ",".join("?" * len(keep_ids))
                cur = c.execute(
                    "UPDATE episodes SET status='new', play_uri=NULL"
                    f" WHERE status='queued' AND id NOT IN ({placeholders})",
                    keep_ids)
            else:
                cur = c.execute(
                    "UPDATE episodes SET status='new', play_uri=NULL"
                    " WHERE status='queued'")
            return cur.rowcount

    def unpinned_in(self, episode_ids: list[int]) -> list[int]:
        """Which of these are not pinned — what "Refresh all" re-rolls."""
        if not episode_ids:
            return []
        placeholders = ",".join("?" * len(episode_ids))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id FROM episodes WHERE id IN ({placeholders}) AND pinned=0",
                episode_ids).fetchall()
        return [r["id"] for r in rows]

    def mark_skipped(self, episode_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE episodes SET status='skipped', play_uri=NULL, local_path=NULL WHERE id=?",
                (episode_id,),
            )

    def revert_to_new(self, episode_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE episodes SET status='new', play_uri=NULL WHERE id=?", (episode_id,)
            )

    def revert_all_queued(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET status='new', play_uri=NULL WHERE status='queued'"
            )
            return cur.rowcount

    def archive_episode(self, episode_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE episodes SET status='archived' WHERE id=?", (episode_id,))

    def unarchive_feed(self, feed_id: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET status='new' WHERE feed_id=? AND status='archived'",
                (feed_id,),
            )
            return cur.rowcount

    def count_episodes_for_feed(self, feed_id: int, q: str | None = None) -> int:
        with self._conn() as c:
            if q:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM episodes WHERE feed_id=? AND title LIKE ?",
                    (feed_id, f"%{q}%"),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM episodes WHERE feed_id=?", (feed_id,)
                ).fetchone()
            return row["n"]

    def episodes_for_feed_page(self, feed_id: int, page: int, page_size: int,
                               q: str | None = None) -> list[sqlite3.Row]:
        offset = max(0, page - 1) * page_size
        with self._conn() as c:
            if q:
                return c.execute(
                    "SELECT * FROM episodes WHERE feed_id=? AND title LIKE ?"
                    " ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
                    (feed_id, f"%{q}%", page_size, offset),
                ).fetchall()
            return c.execute(
                "SELECT * FROM episodes WHERE feed_id=?"
                " ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
                (feed_id, page_size, offset),
            ).fetchall()

    def count_search_episodes(self, q: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM episodes e JOIN feeds f ON f.id=e.feed_id"
                " WHERE e.title LIKE ? OR f.title LIKE ?",
                (f"%{q}%", f"%{q}%"),
            ).fetchone()
            return row["n"]

    def search_episodes(self, q: str, page: int, page_size: int) -> list[sqlite3.Row]:
        offset = max(0, page - 1) * page_size
        with self._conn() as c:
            return c.execute(
                "SELECT e.*, f.title AS feed_title FROM episodes e"
                " JOIN feeds f ON f.id=e.feed_id"
                " WHERE e.title LIKE ? OR f.title LIKE ?"
                " ORDER BY e.published_at DESC, e.id DESC LIMIT ? OFFSET ?",
                (f"%{q}%", f"%{q}%", page_size, offset),
            ).fetchall()

    def release_episode(self, episode_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET status='new' WHERE id=? AND status='archived'",
                (episode_id,),
            )
            return cur.rowcount == 1

    def release_episodes(self, episode_ids: list[int]) -> int:
        if not episode_ids:
            return 0
        with self._conn() as c:
            placeholders = ",".join("?" * len(episode_ids))
            cur = c.execute(
                f"UPDATE episodes SET status='new' WHERE status='archived'"
                f" AND id IN ({placeholders})",
                episode_ids,
            )
            return cur.rowcount

    def set_resume(self, episode_id: int, seconds: int | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE episodes SET resume_seconds=? WHERE id=?", (seconds, episode_id))

    # ── kv ────────────────────────────────────────────────────────────
    def kv_get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def kv_del(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM kv WHERE key=?", (key,))

    # ── schedules ─────────────────────────────────────────────────────
    def add_schedule(self, time_str: str, days: list[int]) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO schedules (time, days) VALUES (?,?)",
                (time_str, ",".join(str(d) for d in sorted(set(days)))),
            )
            return int(cur.lastrowid)

    def list_schedules(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM schedules ORDER BY time, id").fetchall()

    def toggle_schedule(self, schedule_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE schedules SET enabled = 1 - enabled WHERE id=?", (schedule_id,))

    def delete_schedule(self, schedule_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    def set_last_fired(self, schedule_id: int, date_iso: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE schedules SET last_fired_date=? WHERE id=?", (date_iso, schedule_id))
