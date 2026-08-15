# Sonos Talk Radio Implementation Plan

**Goal:** A self-hosted Flask + background-thread app that manages podcast RSS feeds and continuously programs Sonos speakers like a personal talk-radio station (random show rotation, multi-part episodes in order, news first).

**Architecture:** Single Python 3.12 process: a Flask web thread plus one DJ loop thread, sharing a WAL-mode SQLite file with a connection per operation. `sonos_ctl.SonosPlayer` wraps the SoCo group coordinator behind a small interface; `dj.DJ` takes any player-shaped object, so all queue-reconciliation logic is tested against an in-memory `FakeSonosPlayer`.

**Tech Stack:** Python 3.12, Flask, SoCo, feedparser, requests, SQLite (stdlib), pytest (dev only), uv for dev tooling.

**Authoritative requirements:** `docs/original-build-prompt.md` and `docs/design/specs/2026-07-05-sonos-talk-radio-design.md`. Where this plan is ambiguous, the prompt wins.

## Global Constraints

- Python 3.12; runtime deps limited to exactly: `flask`, `soco`, `feedparser`, `requests`. `pytest` is dev-only (`requirements-dev.txt`), never in `requirements.txt`.
- Single process, two threads. SQLite WAL mode; open a connection per operation; no connection sharing across threads.
- Episode statuses: exactly `new`, `queued`, `played`, `skipped`, `archived`. Only `new` episodes are pickable. All status transitions go through named `db.Database` methods — no raw status `UPDATE`s outside `db.py`.
- All timestamps stored as UTC strings `%Y-%m-%dT%H:%M:%SZ` so lexical comparison == time comparison. Normalize at ingest (boundary), never at call sites.
- SoCo index rules live only in `sonos_ctl.SonosPlayer`: `playlist_position` is a 1-based string; `play_from_queue()` and `remove_from_queue()` are 0-based; `add_to_queue`'s position is 1-based where 0 = append. Everything outside `sonos_ctl.py` thinks in 0-based ints.
- Always control `device.group.coordinator`, never the discovered device directly.
- Env defaults: `DATA_DIR=./data`, `DB_PATH=$DATA_DIR/radio.db`, `MEDIA_DIR=$DATA_DIR/media`, `HOST=0.0.0.0`, `PORT=8080`, `TICK_SECONDS=15`, `REFRESH_MINUTES=30`, `QUEUE_AHEAD=3`, `NEWS_MAX_AGE_HOURS=24`, `DOWNLOAD_MODE=0`, `GRACE_MINUTES=10`, `USER_AGENT=SonosTalkRadio/1.0`; `SONOS_SPEAKER`, `SONOS_IP`, `BASE_URL`, `TZ` optional.
- The app must boot and the dashboard must load with no Sonos reachable. Any player call in web/status paths degrades gracefully (returns an error string or `None`), never a 500.
- Tests must run with no Sonos and no network. `tests/conftest.py` has an autouse fixture stubbing `audio.resolve_audio_url`; feed tests parse fixture RSS strings directly.
- Test commands use `.venv/bin/pytest` (created in Task 1 via `uv venv`).
- Commit after every task (steps include the exact commands). Work directly on `main` — this is a fresh single-owner repo.

---

### Task 1: Project scaffolding + config.py

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `pytest.ini`, `config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `Config` frozen dataclass with fields `data_dir: str`, `db_path: str`, `media_dir: str`, `host: str`, `port: int`, `sonos_speaker: str | None`, `sonos_ip: str | None`, `tick_seconds: int`, `refresh_minutes: int`, `queue_ahead: int`, `news_max_age_hours: int`, `download_mode: bool`, `base_url: str | None`, `user_agent: str`, `grace_minutes: int`; classmethod `Config.from_env(env: Mapping[str, str] | None = None) -> Config` (reads `os.environ` when `env` is None).

- [ ] **Step 1: Create tooling files**

`requirements.txt`:
```
flask
soco
feedparser
requests
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
data/
.viva/
```

`pytest.ini`:
```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 2: Create the venv and install**

Run: `uv venv && uv pip install -r requirements-dev.txt`
Expected: venv at `.venv/`, all five packages install without error.

- [ ] **Step 3: Write the failing test**

`tests/test_config.py`:
```python
from config import Config

def test_defaults() -> None:
    cfg = Config.from_env({})
    assert cfg.data_dir == "./data"
    assert cfg.db_path == "./data/radio.db"
    assert cfg.media_dir == "./data/media"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8080
    assert cfg.sonos_speaker is None
    assert cfg.sonos_ip is None
    assert cfg.tick_seconds == 15
    assert cfg.refresh_minutes == 30
    assert cfg.queue_ahead == 3
    assert cfg.news_max_age_hours == 24
    assert cfg.download_mode is False
    assert cfg.base_url is None
    assert cfg.user_agent == "SonosTalkRadio/1.0"
    assert cfg.grace_minutes == 10

def test_overrides_and_derived_paths() -> None:
    cfg = Config.from_env({"DATA_DIR": "/x", "PORT": "9090", "DOWNLOAD_MODE": "1"})
    assert cfg.db_path == "/x/radio.db"
    assert cfg.media_dir == "/x/media"
    assert cfg.port == 9090
    assert cfg.download_mode is True

def test_explicit_db_path_beats_derived() -> None:
    cfg = Config.from_env({"DATA_DIR": "/x", "DB_PATH": "/elsewhere/r.db"})
    assert cfg.db_path == "/elsewhere/r.db"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 5: Write config.py**

```python
"""All configuration enters here, from env vars, with defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class Config:
    data_dir: str
    db_path: str
    media_dir: str
    host: str
    port: int
    sonos_speaker: str | None
    sonos_ip: str | None
    tick_seconds: int
    refresh_minutes: int
    queue_ahead: int
    news_max_age_hours: int
    download_mode: bool
    base_url: str | None
    user_agent: str
    grace_minutes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        e: Mapping[str, str] = os.environ if env is None else env
        data_dir = e.get("DATA_DIR", "./data")
        return cls(
            data_dir=data_dir,
            db_path=e.get("DB_PATH", os.path.join(data_dir, "radio.db")),
            media_dir=e.get("MEDIA_DIR", os.path.join(data_dir, "media")),
            host=e.get("HOST", "0.0.0.0"),
            port=int(e.get("PORT", "8080")),
            sonos_speaker=e.get("SONOS_SPEAKER") or None,
            sonos_ip=e.get("SONOS_IP") or None,
            tick_seconds=int(e.get("TICK_SECONDS", "15")),
            refresh_minutes=int(e.get("REFRESH_MINUTES", "30")),
            queue_ahead=int(e.get("QUEUE_AHEAD", "3")),
            news_max_age_hours=int(e.get("NEWS_MAX_AGE_HOURS", "24")),
            download_mode=e.get("DOWNLOAD_MODE", "0") == "1",
            base_url=e.get("BASE_URL") or None,
            user_agent=e.get("USER_AGENT", "SonosTalkRadio/1.0"),
            grace_minutes=int(e.get("GRACE_MINUTES", "10")),
        )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: `3 passed`

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-dev.txt .gitignore pytest.ini config.py tests/test_config.py
git commit -m "feat: project scaffolding and env-driven Config"
```

---

### Task 2: db.py — schema + Database

**Files:**
- Create: `db.py`
- Test: `tests/test_db.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `utcnow_iso() -> str`; class `Database(path: str)` with `.init()` and methods (all open their own connection):
  - Feeds: `add_feed(url: str, title: str, image_url: str | None, is_news: bool) -> int`, `get_feed(feed_id: int) -> sqlite3.Row | None`, `list_feeds() -> list[sqlite3.Row]`, `delete_feed(feed_id: int)`, `toggle_feed(feed_id: int)`, `set_feed_news(feed_id: int, is_news: bool)`, `counts_by_feed() -> dict[int, dict[str, int]]`.
  - Episodes: `insert_episode(feed_id: int, guid: str, title: str, audio_url: str, published_at: str, status: str = "new") -> bool`, `get_episode(episode_id: int) -> Row | None`, `episodes_for_feed(feed_id: int) -> list[Row]` (newest first), `episodes_with_status(status: str) -> list[Row]`, `oldest_new_for_feed(feed_id: int) -> Row | None`, `rotation_feeds_with_new() -> list[Row]`, `fresh_news(cutoff_iso: str) -> list[Row]` (oldest first), `prune_stale_news(cutoff_iso: str) -> int`, `recently_played(limit: int = 10) -> list[Row]` (rows include `feed_title`).
  - Transitions: `mark_queued(episode_id: int, play_uri: str, local_path: str | None = None)`, `mark_played(episode_id: int, played_at: str) -> str | None` (returns prior `local_path`, clears it and `resume_seconds`/`play_uri`), `mark_skipped(episode_id: int)`, `revert_to_new(episode_id: int)` (keeps `resume_seconds`, clears `play_uri`), `revert_all_queued() -> int`, `archive_episode(episode_id: int)`, `unarchive_feed(feed_id: int) -> int`, `set_resume(episode_id: int, seconds: int | None)`.
  - KV: `kv_get(key: str) -> str | None`, `kv_set(key: str, value: str)`, `kv_del(key: str)`.
  - Schedules: `add_schedule(time_str: str, days: list[int]) -> int` (days are ints Mon=0..Sun=6, stored comma-joined), `list_schedules() -> list[Row]`, `toggle_schedule(schedule_id: int)`, `delete_schedule(schedule_id: int)`, `set_last_fired(schedule_id: int, date_iso: str)`.

- [ ] **Step 1: Write conftest and failing tests**

`tests/conftest.py`:
```python
import pytest

from config import Config
from db import Database

@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    d.init()
    return d

@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.from_env({"DATA_DIR": str(tmp_path)})

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the network: URL resolution becomes identity.

    audio.py doesn't exist until Task 4 — skip patching until it does.
    """
    try:
        import audio
    except ModuleNotFoundError:
        yield
        return
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, user_agent: url)
    yield
```

`tests/test_db.py`:
```python
from db import Database, utcnow_iso

def _ep(db: Database, feed_id: int, n: int, status: str = "new") -> int:
    db.insert_episode(feed_id, f"guid-{feed_id}-{n}", f"Ep {n}",
                      f"https://cdn.example.com/f{feed_id}/e{n}.mp3",
                      f"2026-01-{n:02d}T00:00:00Z", status)
    eps = db.episodes_for_feed(feed_id)
    return next(e["id"] for e in eps if e["guid"] == f"guid-{feed_id}-{n}")

def test_init_is_idempotent(db: Database) -> None:
    db.init()  # second run must not raise

def test_feed_crud(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, is_news=False)
    feed = db.get_feed(fid)
    assert feed["title"] == "Show" and feed["enabled"] == 1 and feed["is_news"] == 0
    db.toggle_feed(fid)
    assert db.get_feed(fid)["enabled"] == 0
    db.set_feed_news(fid, True)
    assert db.get_feed(fid)["is_news"] == 1
    assert [f["id"] for f in db.list_feeds()] == [fid]

def test_delete_feed_cascades_episodes(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    _ep(db, fid, 1)
    db.delete_feed(fid)
    assert db.episodes_with_status("new") == []

def test_insert_episode_dedupes_on_guid(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    assert db.insert_episode(fid, "g1", "A", "https://a/1.mp3", "2026-01-01T00:00:00Z") is True
    assert db.insert_episode(fid, "g1", "A again", "https://a/1.mp3", "2026-01-01T00:00:00Z") is False

def test_oldest_new_for_feed_orders_by_published(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    _ep(db, fid, 3)
    _ep(db, fid, 1)
    _ep(db, fid, 2)
    assert db.oldest_new_for_feed(fid)["guid"] == f"guid-{fid}-1"

def test_status_lifecycle(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    eid = _ep(db, fid, 1)
    db.mark_queued(eid, "https://resolved/1.mp3", "/media/ep1.mp3")
    ep = db.get_episode(eid)
    assert ep["status"] == "queued" and ep["play_uri"] == "https://resolved/1.mp3"
    db.set_resume(eid, 300)
    local = db.mark_played(eid, utcnow_iso())
    ep = db.get_episode(eid)
    assert local == "/media/ep1.mp3"
    assert ep["status"] == "played" and ep["resume_seconds"] is None
    assert ep["play_uri"] is None and ep["local_path"] is None and ep["played_at"]

def test_revert_to_new_keeps_resume(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    eid = _ep(db, fid, 1)
    db.mark_queued(eid, "https://resolved/1.mp3")
    db.set_resume(eid, 120)
    db.revert_to_new(eid)
    ep = db.get_episode(eid)
    assert ep["status"] == "new" and ep["resume_seconds"] == 120 and ep["play_uri"] is None

def test_revert_all_queued(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    a, b = _ep(db, fid, 1), _ep(db, fid, 2)
    db.mark_queued(a, "u1")
    db.mark_queued(b, "u2")
    assert db.revert_all_queued() == 2
    assert db.episodes_with_status("queued") == []

def test_archive_and_unarchive(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    eid = _ep(db, fid, 1)
    db.archive_episode(eid)
    assert db.get_episode(eid)["status"] == "archived"
    assert db.unarchive_feed(fid) == 1
    assert db.get_episode(eid)["status"] == "new"

def test_rotation_feeds_excludes_news_disabled_and_empty(db: Database) -> None:
    plain = db.add_feed("https://a/rss", "A", None, False)
    news = db.add_feed("https://b/rss", "B", None, True)
    off = db.add_feed("https://c/rss", "C", None, False)
    empty = db.add_feed("https://d/rss", "D", None, False)
    _ep(db, plain, 1)
    _ep(db, news, 1)
    _ep(db, off, 1)
    db.toggle_feed(off)
    assert [f["id"] for f in db.rotation_feeds_with_new()] == [plain]
    assert empty not in [f["id"] for f in db.rotation_feeds_with_new()]

def test_fresh_news_ordering_and_prune(db: Database) -> None:
    news = db.add_feed("https://b/rss", "News", None, True)
    _ep(db, news, 2)
    _ep(db, news, 1)
    fresh = db.fresh_news("2026-01-01T00:00:00Z")
    assert [e["guid"] for e in fresh] == [f"guid-{news}-1", f"guid-{news}-2"]
    assert db.prune_stale_news("2026-01-02T00:00:00Z") == 1
    assert db.get_episode(fresh[0]["id"])["status"] == "skipped"

def test_prune_ignores_non_news(db: Database) -> None:
    plain = db.add_feed("https://a/rss", "A", None, False)
    _ep(db, plain, 1)
    assert db.prune_stale_news("2027-01-01T00:00:00Z") == 0

def test_counts_by_feed(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "A", None, False)
    _ep(db, fid, 1)
    eid = _ep(db, fid, 2)
    db.mark_queued(eid, "u")
    counts = db.counts_by_feed()
    assert counts[fid] == {"new": 1, "queued": 1}

def test_recently_played_includes_feed_title(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "My Show", None, False)
    eid = _ep(db, fid, 1)
    db.mark_queued(eid, "u")
    db.mark_played(eid, utcnow_iso())
    rows = db.recently_played()
    assert rows[0]["feed_title"] == "My Show"

def test_kv(db: Database) -> None:
    assert db.kv_get("k") is None
    db.kv_set("k", "v1")
    db.kv_set("k", "v2")
    assert db.kv_get("k") == "v2"
    db.kv_del("k")
    assert db.kv_get("k") is None

def test_schedules(db: Database) -> None:
    sid = db.add_schedule("08:00", [0, 1, 2, 3, 4])
    s = db.list_schedules()[0]
    assert s["id"] == sid and s["time"] == "08:00" and s["days"] == "0,1,2,3,4"
    assert s["enabled"] == 1 and s["last_fired_date"] is None
    db.set_last_fired(sid, "2026-07-05")
    db.toggle_schedule(sid)
    s = db.list_schedules()[0]
    assert s["last_fired_date"] == "2026-07-05" and s["enabled"] == 0
    db.delete_schedule(sid)
    assert db.list_schedules() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Write db.py**

```python
"""SQLite persistence. Every episode status transition lives here —
no raw status UPDATEs anywhere else in the codebase."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    is_news INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL
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

class Database:
    """One short-lived connection per operation; WAL lets the web and DJ
    threads share the file safely."""

    def __init__(self, path: str) -> None:
        self.path = str(path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ── feeds ─────────────────────────────────────────────────────────
    def add_feed(self, url: str, title: str, image_url: str | None, is_news: bool) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO feeds (url, title, image_url, is_news, added_at) VALUES (?,?,?,?,?)",
                (url, title, image_url, int(is_news), utcnow_iso()),
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

    # ── episodes ──────────────────────────────────────────────────────
    def insert_episode(self, feed_id: int, guid: str, title: str, audio_url: str,
                       published_at: str, status: str = "new") -> bool:
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO episodes"
                " (feed_id, guid, title, audio_url, published_at, status)"
                " VALUES (?,?,?,?,?,?)",
                (feed_id, guid, title, audio_url, published_at, status),
            )
            return cur.rowcount == 1

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

    def oldest_new_for_feed(self, feed_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM episodes WHERE feed_id=? AND status='new'"
                " ORDER BY published_at ASC, id ASC LIMIT 1",
                (feed_id,),
            ).fetchone()

    def rotation_feeds_with_new(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT f.* FROM feeds f WHERE f.enabled=1 AND f.is_news=0 AND EXISTS"
                " (SELECT 1 FROM episodes e WHERE e.feed_id=f.id AND e.status='new')"
                " ORDER BY f.id",
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
        with self._conn() as c:
            c.execute(
                "UPDATE episodes SET status='queued', play_uri=?, local_path=? WHERE id=?",
                (play_uri, local_path, episode_id),
            )

    def mark_played(self, episode_id: int, played_at: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT local_path FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            c.execute(
                "UPDATE episodes SET status='played', played_at=?, resume_seconds=NULL,"
                " play_uri=NULL, local_path=NULL WHERE id=?",
                (played_at, episode_id),
            )
            return row["local_path"] if row else None

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -q`
Expected: `16 passed`

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py tests/conftest.py
git commit -m "feat: SQLite schema and Database with centralized status transitions"
```

---

### Task 3: feeds.py — ingest, catalog scope, refresh, news pruning

**Files:**
- Create: `feeds.py`
- Test: `tests/test_feeds.py`, `tests/rss_fixtures.py`

**Interfaces:**
- Consumes: `Database` methods and `utcnow_iso` from Task 2; `Config` from Task 1.
- Produces: `FeedError(Exception)`; `fetch_feed(url: str, user_agent: str) -> feedparser.FeedParserDict`; `entry_audio_url(entry) -> str | None`; `entry_published_iso(entry) -> str`; `ingest_entries(db: Database, feed_id: int, parsed) -> int`; `add_feed_from_parsed(db: Database, parsed, url: str, is_news: bool, include: str = "latest", last_n: int | None = None) -> int`; `add_feed(db: Database, cfg: Config, url: str, is_news: bool, include: str = "latest", last_n: int | None = None) -> int`; `news_cutoff_iso(cfg: Config) -> str`; `refresh_all(db: Database, cfg: Config) -> None`. `INCLUDE_MODES = ("new_only", "latest", "last_n", "all")`.

- [ ] **Step 1: Write the RSS fixture builder and failing tests**

`tests/rss_fixtures.py`:
```python
"""Build RSS strings for tests. guid and audio URL derive from the title."""
from datetime import datetime
from email.utils import format_datetime

def slug(title: str) -> str:
    return title.lower().replace(" ", "-")

def rss(feed_title: str, items: list[tuple[str, datetime]]) -> str:
    parts = []
    for title, pub in items:
        s = slug(title)
        parts.append(
            f"<item><title>{title}</title><guid>guid-{s}</guid>"
            f"<pubDate>{format_datetime(pub)}</pubDate>"
            f'<enclosure url="https://cdn.example.com/{s}.mp3" length="1" type="audio/mpeg"/>'
            f"</item>"
        )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>{feed_title}</title>{''.join(parts)}</channel></rss>"
    )
```

`tests/test_feeds.py`:
```python
from datetime import datetime, timedelta, timezone

import feedparser

from config import Config
from db import Database
from feeds import add_feed_from_parsed, ingest_entries, news_cutoff_iso, refresh_all
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

def test_refresh_is_idempotent_and_new_drops_are_playable(db: Database) -> None:
    parsed = feedparser.parse(rss("Show", _items(2)))
    fid = add_feed_from_parsed(db, parsed, "https://x/rss", False, include="new_only")
    assert ingest_entries(db, fid, parsed) == 0  # re-ingest inserts nothing
    later = feedparser.parse(rss("Show", _items(3)))  # Part 3 just dropped
    assert ingest_entries(db, fid, later) == 1
    assert _statuses(db, fid)["guid-part-3"] == "new"  # playable despite new_only

def test_refresh_all_prunes_stale_news(db: Database, cfg: Config, monkeypatch) -> None:
    stale = NOW - timedelta(hours=48)
    parsed = feedparser.parse(rss("News", [("Old news", stale), ("Fresh news", NOW)]))
    fid = add_feed_from_parsed(db, parsed, "https://n/rss", True, include="all")
    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", lambda url, ua: parsed)
    refresh_all(db, cfg)
    st = _statuses(db, fid)
    assert st["guid-old-news"] == "skipped" and st["guid-fresh-news"] == "new"

def test_refresh_all_survives_a_broken_feed(db: Database, cfg: Config, monkeypatch) -> None:
    good = feedparser.parse(rss("Good", _items(1)))
    add_feed_from_parsed(db, good, "https://good/rss", False, include="new_only")
    db.add_feed("https://broken/rss", "Broken", None, False)
    calls: list[str] = []

    def fake_fetch(url: str, ua: str):
        calls.append(url)
        if "broken" in url:
            raise OSError("connection refused")
        return feedparser.parse(rss("Good", _items(2)))

    import feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "fetch_feed", fake_fetch)
    refresh_all(db, cfg)  # must not raise
    assert len(calls) == 2

def test_news_cutoff_format(cfg: Config) -> None:
    cutoff = news_cutoff_iso(cfg)
    assert len(cutoff) == 20 and cutoff.endswith("Z")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_feeds.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'feeds'`

- [ ] **Step 3: Write feeds.py**

```python
"""Feed fetching and ingest. Dates are normalized to UTC
'%Y-%m-%dT%H:%M:%SZ' at ingest so string comparison == time comparison."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from config import Config
from db import Database, utcnow_iso

logger = logging.getLogger(__name__)

INCLUDE_MODES = ("new_only", "latest", "last_n", "all")

class FeedError(Exception):
    pass

def fetch_feed(url: str, user_agent: str) -> feedparser.FeedParserDict:
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if not parsed.entries and parsed.bozo:
        raise FeedError(f"could not parse feed: {url}")
    return parsed

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

def ingest_entries(db: Database, feed_id: int, parsed) -> int:
    inserted = 0
    for entry in parsed.entries:
        audio_url = entry_audio_url(entry)
        if not audio_url:
            continue
        guid = entry.get("id") or audio_url
        if db.insert_episode(feed_id, guid, entry.get("title", ""), audio_url,
                             entry_published_iso(entry)):
            inserted += 1
    return inserted

def add_feed_from_parsed(db: Database, parsed, url: str, is_news: bool,
                         include: str = "latest", last_n: int | None = None) -> int:
    if include not in INCLUDE_MODES:
        raise ValueError(f"bad include mode: {include}")
    title = parsed.feed.get("title") or url
    image = (parsed.feed.get("image") or {}).get("href")
    feed_id = db.add_feed(url, title, image, is_news)
    ingest_entries(db, feed_id, parsed)
    episodes = db.episodes_for_feed(feed_id)  # newest first
    keep = {"new_only": 0, "latest": 1,
            "last_n": max(1, int(last_n or 1)), "all": len(episodes)}[include]
    for episode in episodes[keep:]:
        db.archive_episode(episode["id"])
    return feed_id

def add_feed(db: Database, cfg: Config, url: str, is_news: bool,
             include: str = "latest", last_n: int | None = None) -> int:
    return add_feed_from_parsed(db, fetch_feed(url, cfg.user_agent),
                                url, is_news, include, last_n)

def news_cutoff_iso(cfg: Config) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.news_max_age_hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

def refresh_all(db: Database, cfg: Config) -> None:
    for feed in db.list_feeds():
        if not feed["enabled"]:
            continue
        try:
            ingest_entries(db, feed["id"], fetch_feed(feed["url"], cfg.user_agent))
        except Exception:
            logger.exception("refresh failed for feed %s (%s)", feed["id"], feed["url"])
    pruned = db.prune_stale_news(news_cutoff_iso(cfg))
    if pruned:
        logger.info("pruned %d stale news episodes", pruned)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_feeds.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add feeds.py tests/test_feeds.py tests/rss_fixtures.py
git commit -m "feat: feed ingest with catalog scopes, idempotent refresh, news pruning"
```

---

### Task 4: audio.py — URI matching, resolution, downloads

**Files:**
- Create: `audio.py`
- Test: `tests/test_audio.py`

**Interfaces:**
- Consumes: nothing internal (requests, stdlib only).
- Produces: `guess_mime(url: str) -> str`; `normalize_uri(uri: str) -> str`; `uris_match(a: str, b: str) -> bool`; `resolve_audio_url(url: str, user_agent: str) -> str`; `download_episode(url: str, media_dir: str, episode_id: int, user_agent: str) -> str` (returns bare filename); `delete_local(path: str) -> None`; `media_url(base_url: str, filename: str) -> str`; `detect_base_url(speaker_ip: str, port: int) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_audio.py`:
```python
from audio import delete_local, guess_mime, media_url, normalize_uri, uris_match

def test_guess_mime() -> None:
    assert guess_mime("https://x/a.mp3") == "audio/mpeg"
    assert guess_mime("https://x/a.m4a?tok=1") == "audio/mp4"
    assert guess_mime("https://x/a.ogg") == "audio/ogg"
    assert guess_mime("https://x/whatknows") == "audio/mpeg"

def test_normalize_strips_query_and_lowercases_host() -> None:
    assert normalize_uri("HTTPS://CDN.X.com/a/B.mp3?token=zzz#f") == "https://cdn.x.com/a/B.mp3"

def test_uris_match_exact_and_normalized() -> None:
    assert uris_match("https://x/a.mp3", "https://x/a.mp3")
    assert uris_match("https://x/a.mp3?tok=1", "https://x/a.mp3?tok=2")

def test_uris_match_path_only_fallback() -> None:
    # Sonos rewrites the scheme/host for some streams; the path survives.
    assert uris_match("x-rincon-mp3radio://cdn.x.com/shows/ep1.mp3",
                      "https://other-edge.x.com/shows/ep1.mp3")
    assert not uris_match("https://x/a.mp3", "https://x/b.mp3")
    assert not uris_match("https://x/", "https://y/")

def test_media_url() -> None:
    assert media_url("http://10.0.0.5:8080/", "ep7.mp3") == "http://10.0.0.5:8080/media/ep7.mp3"

def test_delete_local_missing_file_is_silent(tmp_path) -> None:
    delete_local(str(tmp_path / "nope.mp3"))  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_audio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'audio'`

- [ ] **Step 3: Write audio.py**

```python
"""Audio URI plumbing: matching, redirect resolution, download mode."""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import requests

AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac",
}

def guess_mime(url: str) -> str:
    path = urlparse(url).path.lower()
    return next((mime for ext, mime in AUDIO_MIME.items() if path.endswith(ext)), "audio/mpeg")

def normalize_uri(uri: str) -> str:
    p = urlparse(uri)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}"

def uris_match(a: str, b: str) -> bool:
    """Exact/normalized match, falling back to path-only — some hosts append
    changing auth tokens, and Sonos rewrites schemes on queue items."""
    if a == b or normalize_uri(a) == normalize_uri(b):
        return True
    pa, pb = urlparse(a).path, urlparse(b).path
    return pa not in ("", "/") and pa == pb

def resolve_audio_url(url: str, user_agent: str) -> str:
    """Follow the redirect chain to a final direct URL (HEAD, falling back
    to a 1-byte ranged GET for hosts that reject HEAD)."""
    headers = {"User-Agent": user_agent}
    try:
        resp = requests.head(url, headers=headers, allow_redirects=True, timeout=20)
        resp.raise_for_status()
        return resp.url
    except requests.RequestException:
        resp = requests.get(url, headers={**headers, "Range": "bytes=0-0"},
                            allow_redirects=True, timeout=20, stream=True)
        resp.raise_for_status()
        final = resp.url
        resp.close()
        return final

def download_episode(url: str, media_dir: str, episode_id: int, user_agent: str) -> str:
    os.makedirs(media_dir, exist_ok=True)
    ext = os.path.splitext(urlparse(url).path)[1] or ".mp3"
    filename = f"ep{episode_id}{ext}"
    dest = os.path.join(media_dir, filename)
    with requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return filename

def delete_local(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

def media_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/media/{filename}"

def detect_base_url(speaker_ip: str, port: int) -> str:
    """The server's LAN IP as the speaker sees it: open a UDP socket toward
    the speaker and read the local address. No packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((speaker_ip, 1400))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return f"http://{local_ip}:{port}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_audio.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add audio.py tests/test_audio.py
git commit -m "feat: audio URI matching, redirect resolution, and download-mode helpers"
```

---

### Task 5: sonos_ctl.py — SonosPlayer, discovery, provider

**Files:**
- Create: `sonos_ctl.py`
- Test: `tests/test_sonos_ctl.py`

**Interfaces:**
- Consumes: `guess_mime` (Task 4), `Database.kv_get/kv_set` (Task 2), `Config` (Task 1).
- Produces: `hms_to_seconds(value: str | None) -> int`; `seconds_to_hms(seconds: int) -> str`; dataclass `TrackInfo(uri: str, queue_index: int, position: int, duration: int, title: str)`; class `SonosPlayer(device)` with `.ip`, `.name`, `transport_state() -> str`, `current() -> TrackInfo | None`, `queue_uris() -> list[str]`, `queue_length() -> int`, `add_to_queue(uri: str, title: str, show: str, index0: int | None = None)`, `remove_from_queue(index0: int)`, `clear_queue()`, `play_from_queue(index0: int)`, `play()`, `pause()`, `stop()`, `next()`, `seek_seconds(seconds: int)`, `group_all()`; `discover_speakers(timeout: int = 5) -> list[dict[str, str]]` (`{"name","ip"}`); `find_speaker(db: Database, cfg: Config) -> SonosPlayer | None`; `make_player_provider(db: Database, cfg: Config, retry_seconds: int = 30) -> Callable[[], SonosPlayer | None]`.
- **The FakeSonosPlayer in Task 6 must mirror this exact surface** (minus the SoCo internals).

- [ ] **Step 1: Write the failing tests** (pure helpers + no-Sonos degradation; the SoCo-wrapping methods are exercised live in Task 14)

`tests/test_sonos_ctl.py`:
```python
import sonos_ctl
from config import Config
from db import Database
from sonos_ctl import find_speaker, hms_to_seconds, make_player_provider, seconds_to_hms

def test_hms_to_seconds() -> None:
    assert hms_to_seconds("0:12:34") == 754
    assert hms_to_seconds("1:00:05") == 3605
    assert hms_to_seconds("12:34") == 754
    assert hms_to_seconds("") == 0
    assert hms_to_seconds(None) == 0
    assert hms_to_seconds("NOT_IMPLEMENTED") == 0

def test_seconds_to_hms_roundtrip() -> None:
    assert seconds_to_hms(754) == "0:12:34"
    assert seconds_to_hms(3605) == "1:00:05"
    assert seconds_to_hms(-5) == "0:00:00"
    assert hms_to_seconds(seconds_to_hms(9999)) == 9999

def test_find_speaker_none_when_discovery_empty(db: Database, monkeypatch) -> None:
    cfg = Config.from_env({})
    monkeypatch.setattr(sonos_ctl.soco, "discover", lambda timeout=5: None)
    assert find_speaker(db, cfg) is None

def test_provider_caches_failed_discovery(db: Database, monkeypatch) -> None:
    cfg = Config.from_env({})
    calls: list[int] = []

    def fake_discover(timeout=5):
        calls.append(1)
        return None

    monkeypatch.setattr(sonos_ctl.soco, "discover", fake_discover)
    provider = make_player_provider(db, cfg, retry_seconds=3600)
    assert provider() is None
    assert provider() is None  # within retry window: no second discovery
    assert len(calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sonos_ctl.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sonos_ctl'`

- [ ] **Step 3: Write sonos_ctl.py**

```python
"""Everything SoCo. All index-base conversions live here: callers think in
0-based ints; SoCo's mixed 0-/1-based APIs never leak out."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import soco
from soco.data_structures import DidlItem, DidlResource

from audio import guess_mime
from config import Config
from db import Database

logger = logging.getLogger(__name__)

def hms_to_seconds(value: str | None) -> int:
    if not value or ":" not in value:
        return 0
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s

def seconds_to_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

@dataclass
class TrackInfo:
    uri: str
    queue_index: int  # 0-based; -1 when Sonos reports no playlist position
    position: int     # seconds
    duration: int     # seconds
    title: str

class SonosPlayer:
    """Wraps one SoCo device; every call goes through the group coordinator
    so grouped speakers behave."""

    def __init__(self, device: soco.SoCo) -> None:
        self._device = device

    @property
    def _co(self) -> soco.SoCo:
        return self._device.group.coordinator

    @property
    def ip(self) -> str:
        return self._device.ip_address

    @property
    def name(self) -> str:
        return self._device.player_name

    def transport_state(self) -> str:
        return self._co.get_current_transport_info()["current_transport_state"]

    def current(self) -> TrackInfo | None:
        info = self._co.get_current_track_info()
        uri = info.get("uri") or ""
        if not uri:
            return None
        # playlist_position is a 1-based STRING; '0'/'' means not from queue
        pos1 = int(info.get("playlist_position") or 0)
        return TrackInfo(
            uri=uri,
            queue_index=pos1 - 1,
            position=hms_to_seconds(info.get("position")),
            duration=hms_to_seconds(info.get("duration")),
            title=info.get("title", ""),
        )

    def queue_uris(self) -> list[str]:
        # queue items expose their URI at item.resources[0].uri
        return [item.resources[0].uri
                for item in self._co.get_queue(max_items=500)
                if item.resources]

    def queue_length(self) -> int:
        return len(self.queue_uris())

    def add_to_queue(self, uri: str, title: str, show: str, index0: int | None = None) -> None:
        # SoCo add_to_queue position is 1-based where 0 means append
        pos1 = 0 if index0 is None else index0 + 1
        try:
            res = [DidlResource(uri=uri, protocol_info=f"http-get:*:{guess_mime(uri)}:*")]
            item = DidlItem(title=title, parent_id="-1", item_id="-1",
                            creator=show, resources=res)
            self._co.add_to_queue(item, position=pos1)
        except Exception:
            logger.warning("DIDL enqueue failed for %s; using add_uri_to_queue", uri)
            self._co.add_uri_to_queue(uri, position=pos1)

    def remove_from_queue(self, index0: int) -> None:
        self._co.remove_from_queue(index0)  # SoCo's remove_from_queue is 0-based

    def clear_queue(self) -> None:
        self._co.clear_queue()

    def play_from_queue(self, index0: int) -> None:
        self._co.play_from_queue(index0)  # 0-based

    def play(self) -> None:
        self._co.play()

    def pause(self) -> None:
        self._co.pause()

    def stop(self) -> None:
        self._co.stop()

    def next(self) -> None:
        self._co.next()

    def seek_seconds(self, seconds: int) -> None:
        self._co.seek(seconds_to_hms(seconds))

    def group_all(self) -> None:
        self._device.partymode()

def discover_speakers(timeout: int = 5) -> list[dict[str, str]]:
    zones = soco.discover(timeout=timeout) or set()
    return sorted(
        ({"name": z.player_name, "ip": z.ip_address} for z in zones),
        key=lambda s: s["name"],
    )

def find_speaker(db: Database, cfg: Config) -> SonosPlayer | None:
    """Selection order: saved IP in kv -> SONOS_IP -> SONOS_SPEAKER name
    match -> first discovered -> None (the app must boot fine anyway)."""
    ip = db.kv_get("speaker_ip") or cfg.sonos_ip
    if ip:
        try:
            device = soco.SoCo(ip)
            device.player_name  # probe reachability
            return SonosPlayer(device)
        except Exception:
            logger.warning("saved speaker %s unreachable; rediscovering", ip)
    zones = soco.discover(timeout=5) or set()
    if cfg.sonos_speaker:
        for zone in zones:
            if zone.player_name.lower() == cfg.sonos_speaker.lower():
                return SonosPlayer(zone)
    first = next(iter(zones), None)
    return SonosPlayer(first) if first else None

def make_player_provider(db: Database, cfg: Config,
                         retry_seconds: int = 30) -> Callable[[], SonosPlayer | None]:
    """Caches the found player so the 5-second status poll doesn't re-run
    SSDP discovery; failed lookups are retried at most every retry_seconds,
    except when the wanted IP changes (user picked a new speaker)."""
    state: dict = {"ip": None, "player": None, "wanted": None, "last_try": 0.0}

    def provider() -> SonosPlayer | None:
        wanted = db.kv_get("speaker_ip") or cfg.sonos_ip
        if state["player"] is not None and (wanted is None or state["ip"] == wanted):
            return state["player"]
        if wanted == state["wanted"] and time.monotonic() - state["last_try"] < retry_seconds:
            return None
        state["wanted"] = wanted
        state["last_try"] = time.monotonic()
        player = find_speaker(db, cfg)
        if player is not None:
            state["ip"] = player.ip
            state["player"] = player
            db.kv_set("speaker_ip", player.ip)
        else:
            state["ip"] = None
            state["player"] = None
        return player

    return provider
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sonos_ctl.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add sonos_ctl.py tests/test_sonos_ctl.py
git commit -m "feat: SonosPlayer wrapper with coordinator control and cached discovery"
```

---

### Task 6: pick_next + FakeSonosPlayer

**Files:**
- Create: `dj.py` (just `pick_next` for now), `tests/fake_player.py`
- Test: `tests/test_pick.py`

**Interfaces:**
- Consumes: `Database.rotation_feeds_with_new()`, `oldest_new_for_feed()`, `kv_get/kv_set` (Task 2); `TrackInfo` (Task 5).
- Produces: `dj.pick_next(db: Database) -> sqlite3.Row | None` (kv key `last_feed_id`); `tests/fake_player.FakeSonosPlayer` mirroring the `SonosPlayer` surface with test helpers `advance_to(index0: int)` and `hijack(uris: list[str])`. Every DJ test from here on drives this fake.

- [ ] **Step 1: Write the FakeSonosPlayer**

`tests/fake_player.py`:
```python
"""In-memory stand-in for sonos_ctl.SonosPlayer. Same surface, no Sonos."""
from __future__ import annotations

from sonos_ctl import TrackInfo

class FakeSonosPlayer:
    ip = "10.0.0.99"
    name = "Fake Room"

    def __init__(self) -> None:
        self.queue: list[dict[str, str]] = []  # {"uri","title","show"}
        self.index = 0                          # current queue index, 0-based
        self.state = "STOPPED"                  # PLAYING/PAUSED_PLAYBACK/STOPPED
        self.position = 0
        self.duration = 1800
        self.seeks: list[int] = []
        self.grouped = False

    # ── SonosPlayer surface ───────────────────────────────────────────
    def transport_state(self) -> str:
        return self.state

    def current(self) -> TrackInfo | None:
        if not self.queue or self.index >= len(self.queue):
            return None
        item = self.queue[self.index]
        return TrackInfo(uri=item["uri"], queue_index=self.index,
                         position=self.position, duration=self.duration,
                         title=item["title"])

    def queue_uris(self) -> list[str]:
        return [item["uri"] for item in self.queue]

    def queue_length(self) -> int:
        return len(self.queue)

    def add_to_queue(self, uri: str, title: str, show: str,
                     index0: int | None = None) -> None:
        item = {"uri": uri, "title": title, "show": show}
        if index0 is None:
            self.queue.append(item)
        else:
            self.queue.insert(index0, item)

    def remove_from_queue(self, index0: int) -> None:
        del self.queue[index0]
        if index0 < self.index:
            self.index -= 1

    def clear_queue(self) -> None:
        self.queue = []
        self.index = 0
        self.state = "STOPPED"

    def play_from_queue(self, index0: int) -> None:
        self.index = index0
        self.position = 0
        self.state = "PLAYING"

    def play(self) -> None:
        self.state = "PLAYING"

    def pause(self) -> None:
        self.state = "PAUSED_PLAYBACK"

    def stop(self) -> None:
        self.state = "STOPPED"

    def next(self) -> None:
        if self.index + 1 < len(self.queue):
            self.play_from_queue(self.index + 1)
        else:
            self.state = "STOPPED"

    def seek_seconds(self, seconds: int) -> None:
        self.position = seconds
        self.seeks.append(seconds)

    def group_all(self) -> None:
        self.grouped = True

    # ── test helpers ──────────────────────────────────────────────────
    def advance_to(self, index0: int) -> None:
        """Simulate Sonos having moved on (tracks before index0 finished)."""
        self.index = index0
        self.position = 0

    def hijack(self, uris: list[str]) -> None:
        """Simulate someone starting Spotify: our queue is replaced."""
        self.queue = [{"uri": u, "title": "spotify", "show": ""} for u in uris]
        self.index = 0
```

- [ ] **Step 2: Write the failing pick_next tests**

`tests/test_pick.py`:
```python
from db import Database
from dj import pick_next

def _feed_with_eps(db: Database, name: str, n: int, is_news: bool = False) -> int:
    fid = db.add_feed(f"https://{name}/rss", name, None, is_news)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{name}-{i}", f"{name} {i}",
                          f"https://cdn/{name}/{i}.mp3", f"2026-01-{i:02d}T00:00:00Z")
    return fid

def test_returns_none_with_no_rotation_feeds(db: Database) -> None:
    _feed_with_eps(db, "news-only", 2, is_news=True)
    assert pick_next(db) is None

def test_returns_oldest_new_episode(db: Database) -> None:
    _feed_with_eps(db, "show", 3)
    ep = pick_next(db)
    assert ep["guid"] == "g-show-1"

def test_avoids_last_feed_when_alternative_exists(db: Database) -> None:
    a = _feed_with_eps(db, "aaa", 2)
    b = _feed_with_eps(db, "bbb", 2)
    db.kv_set("last_feed_id", str(a))
    for _ in range(10):  # random, so hammer it: must never pick a
        db.kv_set("last_feed_id", str(a))
        assert pick_next(db)["feed_id"] == b

def test_repeats_when_no_alternative(db: Database) -> None:
    a = _feed_with_eps(db, "aaa", 2)
    db.kv_set("last_feed_id", str(a))
    assert pick_next(db)["feed_id"] == a

def test_remembers_last_feed(db: Database) -> None:
    a = _feed_with_eps(db, "aaa", 1)
    pick_next(db)
    assert db.kv_get("last_feed_id") == str(a)

def test_series_parts_come_in_order_across_picks(db: Database) -> None:
    a = _feed_with_eps(db, "series", 3)
    order = []
    for _ in range(3):
        ep = pick_next(db)
        order.append(ep["guid"])
        db.mark_queued(ep["id"], ep["audio_url"])  # consume it
    assert order == ["g-series-1", "g-series-2", "g-series-3"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pick.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'dj'`

- [ ] **Step 4: Write dj.py with pick_next only**

```python
"""The DJ engine: show rotation, news-first, queue reconciliation."""
from __future__ import annotations

import logging
import random
import sqlite3

from db import Database

logger = logging.getLogger(__name__)

def pick_next(db: Database) -> sqlite3.Row | None:
    """Random enabled non-news feed with unplayed episodes, avoiding
    last_feed_id when an alternative exists; that feed's oldest new
    episode wins — a multi-part series plays 1 -> 2 -> 3."""
    candidates = db.rotation_feeds_with_new()
    if not candidates:
        return None
    last = db.kv_get("last_feed_id")
    others = [f for f in candidates if str(f["id"]) != last]
    feed = random.choice(others or candidates)
    db.kv_set("last_feed_id", str(feed["id"]))
    return db.oldest_new_for_feed(feed["id"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pick.py -q`
Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add dj.py tests/test_pick.py tests/fake_player.py
git commit -m "feat: pick_next show rotation and FakeSonosPlayer test double"
```

---

### Task 7: DJ start() and tick()

**Files:**
- Modify: `dj.py` (add the `DJ` class)
- Test: `tests/test_dj.py`

**Interfaces:**
- Consumes: everything above — `Database` transitions, `pick_next`, `audio.uris_match/resolve_audio_url/download_episode/media_url/detect_base_url/delete_local`, `feeds.news_cutoff_iso`, player surface from Task 5/6.
- Produces: `class DJ(db: Database, cfg: Config, get_player: Callable[[], SonosPlayer | None])` with `start() -> str | None`, `tick() -> None`, and internals `_enqueue(player, episode, index0=None) -> bool`, `_top_up(player, current_index)`, `_match_queue(player) -> tuple[list[str], list[Row | None], int]`, `_finish(episode)`, `_stage_uri(episode, player) -> tuple[str, str | None]`, `_save_resume(player)`. KV keys: `dj_state` (`playing`/`stopped`), `resume_episode_id`, `last_feed_id`. Task 8 adds the transport methods to this same class.

- [ ] **Step 1: Write the failing tests**

`tests/test_dj.py`:
```python
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer

NOW = datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

@pytest.fixture
def player() -> FakeSonosPlayer:
    return FakeSonosPlayer()

@pytest.fixture
def dj(db: Database, cfg: Config, player: FakeSonosPlayer) -> DJ:
    return DJ(db, cfg, lambda: player)

def make_feed(db: Database, name: str, n: int, is_news: bool = False,
              hours_old: int = 1) -> int:
    """n episodes, episode 1 oldest; newest is hours_old hours old."""
    fid = db.add_feed(f"https://{name}/rss", name, None, is_news)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{name}-{i}", f"{name} {i}",
                          f"https://cdn/{name}/{i}.mp3",
                          iso(NOW - timedelta(hours=hours_old + n - i)))
    return fid

def uris_of(player: FakeSonosPlayer) -> list[str]:
    return player.queue_uris()

class TestStart:
    def test_no_speaker_returns_error(self, db: Database, cfg: Config) -> None:
        d = DJ(db, cfg, lambda: None)
        assert "speaker" in d.start().lower()

    def test_nothing_to_play_returns_error(self, dj: DJ) -> None:
        assert "station" in dj.start().lower()

    def test_news_first_oldest_first_then_rotation(self, db, cfg, player, dj) -> None:
        make_feed(db, "news", 2, is_news=True)
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        assert dj.start() is None
        q = uris_of(player)
        assert q[0] == "https://cdn/news/1.mp3" and q[1] == "https://cdn/news/2.mp3"
        # news (2) + top-up to QUEUE_AHEAD(3)+1 total minimum
        assert len(q) >= cfg.queue_ahead + 1
        assert player.state == "PLAYING" and player.index == 0
        assert db.kv_get("dj_state") == "playing"
        assert len(db.episodes_with_status("queued")) == len(q)

    def test_stale_news_not_queued(self, db, cfg, player, dj) -> None:
        make_feed(db, "news", 1, is_news=True, hours_old=48)
        make_feed(db, "showa", 3)
        dj.start()
        assert "https://cdn/news/1.mp3" not in uris_of(player)

    def test_failed_episode_is_skipped_and_replaced(self, db, cfg, player, dj,
                                                    monkeypatch) -> None:
        make_feed(db, "showa", 5)
        import audio

        def flaky(url: str, user_agent: str) -> str:
            if url == "https://cdn/showa/1.mp3":
                raise OSError("dead CDN")
            return url

        monkeypatch.setattr(audio, "resolve_audio_url", flaky)
        assert dj.start() is None
        assert "https://cdn/showa/1.mp3" not in uris_of(player)
        assert len(uris_of(player)) == cfg.queue_ahead + 1
        bad = [e for e in db.episodes_for_feed(1) if e["guid"] == "g-showa-1"][0]
        assert bad["status"] == "skipped"

class TestTick:
    def test_noop_when_stopped(self, db, dj, player) -> None:
        dj.tick()  # dj_state is unset -> treated as stopped; must not raise
        assert player.state == "STOPPED"

    def test_marks_passed_episodes_played(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        dj.start()
        player.advance_to(2)
        dj.tick()
        played = db.recently_played()
        assert len(played) == 2
        assert len(db.episodes_with_status("queued")) >= 1

    def test_hijack_stands_down(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        dj.start()
        player.hijack(["x-sonos-spotify:track1", "x-sonos-spotify:track2"])
        dj.tick()
        assert db.kv_get("dj_state") == "stopped"
        assert db.episodes_with_status("queued") == []
        assert len(db.episodes_with_status("new")) == 5

    def test_news_inserted_after_current(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        make_feed(db, "showb", 5)
        dj.start()
        player.advance_to(1)
        nid = make_feed(db, "news", 2, is_news=True)
        dj.tick()
        q = uris_of(player)
        assert q[2] == "https://cdn/news/1.mp3" and q[3] == "https://cdn/news/2.mp3"

    def test_news_not_reinserted_when_next_is_news(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 5)
        dj.start()
        make_feed(db, "news", 1, is_news=True)
        dj.tick()
        before = uris_of(player)
        dj.tick()  # news already next -> no duplicate insert
        assert uris_of(player) == before

    def test_top_up_keeps_queue_ahead(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 10)
        make_feed(db, "showb", 10)
        dj.start()
        player.advance_to(2)
        dj.tick()
        assert player.queue_length() - player.index - 1 >= cfg.queue_ahead

    def test_resumes_when_stopped_mid_queue(self, db, cfg, player, dj) -> None:
        make_feed(db, "showa", 10)
        make_feed(db, "showb", 10)
        dj.start()
        player.advance_to(1)
        player.state = "STOPPED"
        dj.tick()
        assert player.state == "PLAYING" and player.index == 1

    def test_resume_seek_happens_once_then_tracks_position(self, db, cfg, player,
                                                           dj) -> None:
        fid = make_feed(db, "showa", 2)
        make_feed(db, "showb", 2)
        dj.start()
        current = db.episodes_with_status("queued")[0]
        cur_uri = player.queue[player.index]["uri"]
        ep = next(e for e in db.episodes_with_status("queued")
                  if e["play_uri"] == cur_uri)
        db.set_resume(ep["id"], 300)
        dj.tick()
        assert player.seeks[-1] == 290  # resume minus ~10s of context
        assert db.get_episode(ep["id"])["resume_seconds"] is None
        player.position = 42
        dj.tick()
        assert db.get_episode(ep["id"])["resume_seconds"] == 42
        assert db.kv_get("resume_episode_id") == str(ep["id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dj.py -q`
Expected: FAIL — `ImportError: cannot import name 'DJ' from 'dj'`

- [ ] **Step 3: Add the DJ class to dj.py**

Extend the imports at the top of `dj.py`:

```python
import os
import threading
from typing import Callable

import audio
import feeds
from config import Config
from db import Database, utcnow_iso
```

Then add the class:

```python
class DJ:
    """Owns the Sonos queue while on air. The web thread and the DJ loop
    both call in, so every public method takes the lock."""

    def __init__(self, db: Database, cfg: Config,
                 get_player: Callable[[], "SonosPlayer | None"]) -> None:
        self.db = db
        self.cfg = cfg
        self.get_player = get_player
        self._lock = threading.RLock()

    # ── staging ───────────────────────────────────────────────────────
    def _stage_uri(self, episode, player) -> tuple[str, str | None]:
        """Resolve (stream mode) or download (download mode) one episode.
        Returns (playable uri, local_path or None)."""
        if self.cfg.download_mode:
            filename = audio.download_episode(
                episode["audio_url"], self.cfg.media_dir, episode["id"],
                self.cfg.user_agent)
            base = self.cfg.base_url or audio.detect_base_url(player.ip, self.cfg.port)
            return audio.media_url(base, filename), os.path.join(self.cfg.media_dir, filename)
        return audio.resolve_audio_url(episode["audio_url"], self.cfg.user_agent), None

    def _enqueue(self, player, episode, index0: int | None = None) -> bool:
        """Stage one episode on the Sonos queue. On any failure the episode
        is marked skipped — a dead CDN link must never stall the station."""
        try:
            uri, local_path = self._stage_uri(episode, player)
            feed = self.db.get_feed(episode["feed_id"])
            player.add_to_queue(uri, episode["title"],
                                feed["title"] if feed else "", index0)
        except Exception:
            logger.exception("failed to stage episode %s", episode["id"])
            self.db.mark_skipped(episode["id"])
            return False
        self.db.mark_queued(episode["id"], uri, local_path)
        return True

    def _top_up(self, player, current_index: int) -> None:
        while player.queue_length() - current_index - 1 < self.cfg.queue_ahead:
            episode = pick_next(self.db)
            if episode is None:
                break
            self._enqueue(player, episode)  # failure marked it skipped; loop replaces

    # ── queue reconciliation ──────────────────────────────────────────
    def _match_queue(self, player):
        """(queue uris, per-slot matched queued episode or None, current index)."""
        queue = player.queue_uris()
        queued = self.db.episodes_with_status("queued")

        def match(uri: str):
            for ep in queued:
                if ep["play_uri"] and audio.uris_match(uri, ep["play_uri"]):
                    return ep
            return None

        matches = [match(u) for u in queue]
        cur = player.current()
        cur_idx = cur.queue_index if cur and cur.queue_index >= 0 else 0
        return queue, matches, min(cur_idx, max(len(queue) - 1, 0))

    def _finish(self, episode) -> None:
        local_path = self.db.mark_played(episode["id"], utcnow_iso())
        if self.db.kv_get("resume_episode_id") == str(episode["id"]):
            self.db.kv_del("resume_episode_id")
        if local_path:
            audio.delete_local(local_path)

    def _save_resume(self, player) -> None:
        try:
            _, matches, cur_idx = self._match_queue(player)
            cur = player.current()
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            if episode is not None and cur is not None:
                self.db.set_resume(episode["id"], cur.position)
                self.db.kv_set("resume_episode_id", str(episode["id"]))
        except Exception:
            logger.exception("could not save resume position")

    # ── on air ────────────────────────────────────────────────────────
    def start(self) -> str | None:
        """On air. Returns an error message, or None on success."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return "No Sonos speaker available — pick one first"
            resume_id = self.db.kv_get("resume_episode_id")
            self.db.revert_all_queued()
            player.clear_queue()
            for episode in self.db.fresh_news(feeds.news_cutoff_iso(self.cfg)):
                self._enqueue(player, episode)
            if resume_id:  # the interrupted episode plays right after the news
                episode = self.db.get_episode(int(resume_id))
                if episode is not None and episode["status"] == "new":
                    self._enqueue(player, episode)
            self._top_up(player, 0)
            if player.queue_length() == 0:
                return "Nothing to play — add a station first"
            player.play_from_queue(0)
            self.db.kv_set("dj_state", "playing")
            return None

    # ── the reconcile loop ────────────────────────────────────────────
    def tick(self) -> None:
        with self._lock:
            if self.db.kv_get("dj_state") != "playing":
                return
            player = self.get_player()
            if player is None:
                return
            try:
                queue, matches, cur_idx = self._match_queue(player)
                transport = player.transport_state()
                cur = player.current()
            except Exception:
                logger.exception("tick: cannot read player state")
                return
            queued = self.db.episodes_with_status("queued")
            # hijack detection: none of our staged items remain on the queue
            if queued and not any(m is not None for m in matches):
                logger.info("queue no longer ours — standing down")
                self.db.revert_all_queued()
                self.db.kv_set("dj_state", "stopped")
                return
            # everything before the current position has been listened past
            for i in range(min(cur_idx, len(matches))):
                if matches[i] is not None:
                    self._finish(matches[i])
            # current episode: one-shot resume seek, then position tracking
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            if episode is not None and cur is not None:
                if episode["resume_seconds"] is not None:
                    player.seek_seconds(max(0, int(episode["resume_seconds"]) - 10))
                    self.db.set_resume(episode["id"], None)
                elif transport == "PLAYING":
                    self.db.set_resume(episode["id"], cur.position)
                self.db.kv_set("resume_episode_id", str(episode["id"]))
            # news insertion: fresh news exists and the next track isn't news
            news = self.db.fresh_news(feeds.news_cutoff_iso(self.cfg))
            if news:
                nxt = matches[cur_idx + 1] if cur_idx + 1 < len(matches) else None
                next_is_news = False
                if nxt is not None:
                    feed = self.db.get_feed(nxt["feed_id"])
                    next_is_news = bool(feed and feed["is_news"])
                if not next_is_news:
                    insert_at = cur_idx + 1
                    for ep in news:
                        if self._enqueue(player, ep, insert_at):
                            insert_at += 1
            # keep QUEUE_AHEAD tracks ahead of the needle
            self._top_up(player, cur_idx)
            # survive hitting the end of the queue between ticks
            if transport == "STOPPED" and player.queue_length() - cur_idx - 1 > 0:
                player.play_from_queue(cur_idx)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dj.py -q`
Expected: `13 passed` (run the whole suite too: `.venv/bin/pytest -q` — everything green)

- [ ] **Step 5: Commit**

```bash
git add dj.py tests/test_dj.py
git commit -m "feat: DJ start and tick reconciliation (news-first, hijack, top-up, resume)"
```

---

### Task 8: DJ transport controls

**Files:**
- Modify: `dj.py` (add methods to `DJ`)
- Test: `tests/test_dj_controls.py`

**Interfaces:**
- Consumes: `DJ` internals from Task 7.
- Produces: on `DJ`: `play() -> str | None` (start or resume), `pause() -> str | None`, `stop_off_air() -> None` (Off air), `seek_abs(seconds: int) -> str | None`, `seek_rel(delta: int) -> str | None`, `skip_later() -> str | None`, `skip_done() -> str | None`, `group_all() -> str | None`. All return an error string or None; web maps `restart -> seek_abs(0)`, `back_15 -> seek_rel(-15)`, `fwd_30 -> seek_rel(30)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_dj_controls.py`:
```python
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer

NOW = datetime.now(timezone.utc)

@pytest.fixture
def player() -> FakeSonosPlayer:
    return FakeSonosPlayer()

@pytest.fixture
def dj(db: Database, cfg: Config, player: FakeSonosPlayer) -> DJ:
    return DJ(db, cfg, lambda: player)

def make_feed(db: Database, name: str, n: int) -> int:
    fid = db.add_feed(f"https://{name}/rss", name, None, False)
    for i in range(1, n + 1):
        db.insert_episode(fid, f"g-{name}-{i}", f"{name} {i}",
                          f"https://cdn/{name}/{i}.mp3",
                          (NOW - timedelta(hours=n - i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return fid

def current_episode(db: Database, player: FakeSonosPlayer):
    uri = player.queue[player.index]["uri"]
    return next(e for e in db.episodes_with_status("queued") if e["play_uri"] == uri)

def test_play_starts_when_stopped(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    assert dj.play() is None
    assert player.state == "PLAYING" and db.kv_get("dj_state") == "playing"

def test_pause_then_play_resumes_without_restart(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    queue_before = player.queue_uris()
    player.position = 500
    assert dj.pause() is None
    assert player.state == "PAUSED_PLAYBACK"
    ep = current_episode(db, player)
    assert db.get_episode(ep["id"])["resume_seconds"] == 500
    assert dj.play() is None
    assert player.state == "PLAYING"
    assert player.queue_uris() == queue_before  # resumed, not re-started

def test_seek_abs_clamps_to_duration(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 2)
    dj.start()
    player.duration = 100
    dj.seek_abs(500)
    assert player.seeks[-1] == 99
    dj.seek_abs(-3)
    assert player.seeks[-1] == 0

def test_seek_rel_back_and_forward(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 2)
    dj.start()
    player.position = 60
    player.duration = 1800
    dj.seek_rel(-15)
    assert player.seeks[-1] == 45
    player.position = 45
    dj.seek_rel(30)
    assert player.seeks[-1] == 75

def test_seek_without_speaker_errors(db, cfg) -> None:
    d = DJ(db, cfg, lambda: None)
    assert d.seek_abs(0) is not None

def test_skip_later_returns_episode_to_rotation(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    make_feed(db, "showb", 5)
    dj.start()
    ep = current_episode(db, player)
    skipped_uri = player.queue[player.index]["uri"]
    assert dj.skip_later() is None
    after = db.get_episode(ep["id"])
    assert after["status"] == "new" and after["resume_seconds"] is None
    assert skipped_uri not in player.queue_uris()  # removed from Sonos queue
    assert player.state == "PLAYING"

def test_skip_done_marks_played(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    make_feed(db, "showb", 5)
    dj.start()
    ep = current_episode(db, player)
    assert dj.skip_done() is None
    assert db.get_episode(ep["id"])["status"] == "played"
    assert player.state == "PLAYING"

def test_skip_on_last_queued_tops_up_first(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 1)
    dj.start()  # queue holds exactly 1 track
    make_feed(db, "showb", 3)  # new material arrives
    assert dj.skip_done() is None
    assert player.state == "PLAYING"
    assert player.queue_length() >= 1

def test_stop_off_air_reverts_and_saves_resume(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    dj.start()
    ep = current_episode(db, player)
    player.position = 321
    dj.stop_off_air()
    assert player.state == "STOPPED"
    assert db.kv_get("dj_state") == "stopped"
    assert db.episodes_with_status("queued") == []
    after = db.get_episode(ep["id"])
    assert after["status"] == "new" and after["resume_seconds"] == 321
    assert db.kv_get("resume_episode_id") == str(ep["id"])

def test_group_all(db, cfg, player, dj) -> None:
    assert dj.group_all() is None
    assert player.grouped is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dj_controls.py -q`
Expected: FAIL — `AttributeError: 'DJ' object has no attribute 'play'`

- [ ] **Step 3: Add the control methods to DJ**

Append inside the `DJ` class in `dj.py`:

```python
    # ── transport controls ────────────────────────────────────────────
    _NO_SPEAKER = "No Sonos speaker available — pick one first"

    def play(self) -> str | None:
        """The dashboard's single play/pause toggle calls play or pause
        based on transport state; play = start or resume."""
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            if (self.db.kv_get("dj_state") == "playing"
                    and player.transport_state() == "PAUSED_PLAYBACK"):
                player.play()
                return None
            return self.start()

    def pause(self) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            self._save_resume(player)
            player.pause()
            return None

    def stop_off_air(self) -> None:
        with self._lock:
            player = self.get_player()
            if player is not None:
                self._save_resume(player)
                try:
                    player.stop()
                except Exception:
                    logger.exception("stop failed")
            self.db.revert_all_queued()
            self.db.kv_set("dj_state", "stopped")

    # ── seeking ───────────────────────────────────────────────────────
    def seek_abs(self, seconds: int) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            cur = player.current()
            if cur is None:
                return "Nothing is playing"
            upper = cur.duration - 1 if cur.duration > 0 else int(seconds)
            player.seek_seconds(max(0, min(int(seconds), max(0, upper))))
            return None

    def seek_rel(self, delta: int) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            cur = player.current()
            if cur is None:
                return "Nothing is playing"
            return self.seek_abs(cur.position + delta)

    # ── the two skips ─────────────────────────────────────────────────
    def _skip(self, done: bool) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            queue, matches, cur_idx = self._match_queue(player)
            episode = matches[cur_idx] if cur_idx < len(matches) else None
            if episode is None:
                return "Current track isn't a station episode"
            if player.queue_length() - cur_idx - 1 == 0:
                self._top_up(player, cur_idx)  # top up first if it's the last one
            if player.queue_length() - cur_idx - 1 > 0:
                player.play_from_queue(cur_idx + 1)  # advance
            else:
                player.stop()
            player.remove_from_queue(cur_idx)  # then drop the skipped item
            if done:
                self._finish(episode)
            else:
                self.db.revert_to_new(episode["id"])
                self.db.set_resume(episode["id"], None)  # replays fresh another day
                if self.db.kv_get("resume_episode_id") == str(episode["id"]):
                    self.db.kv_del("resume_episode_id")
            return None

    def skip_later(self) -> str | None:
        return self._skip(done=False)

    def skip_done(self) -> str | None:
        return self._skip(done=True)

    def group_all(self) -> str | None:
        with self._lock:
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            try:
                player.group_all()
                return None
            except Exception:
                logger.exception("partymode failed")
                return "Grouping failed"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dj_controls.py -q`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add dj.py tests/test_dj_controls.py
git commit -m "feat: transport controls — play/pause, seeks, two skips, off air, group all"
```

---

### Task 9: Wake schedules

**Files:**
- Modify: `dj.py` (module functions + `DJ.check_schedules`)
- Test: `tests/test_schedules.py`

**Interfaces:**
- Consumes: `Database` schedule methods (Task 2), `DJ.start()` (Task 7), `feeds.refresh_all` (Task 3).
- Produces: in `dj.py`: `parse_days(days: str) -> set[int]`; `schedule_due(schedule: Row, now: datetime, grace_minutes: int) -> bool`; `next_start(schedules: list[Row], now: datetime) -> datetime | None`; on `DJ`: `check_schedules(now: datetime | None = None) -> None` and `_fire_wake() -> None`. `now` defaults to `datetime.now().astimezone()` — **local** time; the compose file must pass `TZ`.

- [ ] **Step 1: Write the failing tests**

`tests/test_schedules.py`:
```python
from datetime import datetime, timedelta

import pytest

from config import Config
from db import Database
from dj import DJ, next_start, parse_days, schedule_due
from fake_player import FakeSonosPlayer

# Mon 2026-07-06 08:03 local — a Monday
MON_0803 = datetime(2026, 7, 6, 8, 3)

def make_schedule(db: Database, time_str: str = "08:00",
                  days: list[int] = [0, 1, 2, 3, 4]):
    db.add_schedule(time_str, days)
    return db.list_schedules()[-1]

def test_parse_days() -> None:
    assert parse_days("0,2,6") == {0, 2, 6}

def test_due_within_grace(db: Database) -> None:
    s = make_schedule(db)
    assert schedule_due(s, MON_0803, grace_minutes=10) is True

def test_not_due_before_time(db: Database) -> None:
    s = make_schedule(db)
    assert schedule_due(s, MON_0803.replace(hour=7), 10) is False

def test_not_due_past_grace(db: Database) -> None:
    s = make_schedule(db)
    # server down all morning: 15:00 must not blast podcasts
    assert schedule_due(s, MON_0803.replace(hour=15, minute=0), 10) is False

def test_not_due_wrong_day(db: Database) -> None:
    s = make_schedule(db, days=[5, 6])  # weekend alarm, Monday now
    assert schedule_due(s, MON_0803, 10) is False

def test_not_due_when_disabled_or_already_fired(db: Database) -> None:
    s = make_schedule(db)
    db.set_last_fired(s["id"], MON_0803.date().isoformat())
    assert schedule_due(db.list_schedules()[0], MON_0803, 10) is False
    s2 = make_schedule(db, time_str="08:01")
    db.toggle_schedule(s2["id"])
    assert schedule_due(db.list_schedules()[-1], MON_0803, 10) is False

def test_next_start_same_day_and_week_wrap(db: Database) -> None:
    make_schedule(db, "09:00", [0])          # later today (Mon)
    assert next_start(db.list_schedules(), MON_0803).hour == 9
    db.delete_schedule(db.list_schedules()[0]["id"])
    make_schedule(db, "07:00", [0])          # already passed -> next Monday
    nxt = next_start(db.list_schedules(), MON_0803)
    assert nxt.weekday() == 0 and (nxt.date() - MON_0803.date()).days == 7

def test_next_start_none_without_schedules(db: Database) -> None:
    assert next_start(db.list_schedules(), MON_0803) is None

@pytest.fixture
def wake_env(db: Database, cfg: Config, monkeypatch):
    player = FakeSonosPlayer()
    dj = DJ(db, cfg, lambda: player)
    import dj as dj_mod
    monkeypatch.setattr(dj_mod.feeds, "refresh_all", lambda db_, cfg_: None)
    fid = db.add_feed("https://showa/rss", "showa", None, False)
    for i in range(1, 4):
        db.insert_episode(fid, f"g{i}", f"ep{i}", f"https://cdn/showa/{i}.mp3",
                          f"2026-01-0{i}T00:00:00Z")
    return db, dj, player

def test_fire_starts_when_stopped(wake_env) -> None:
    db, dj, player = wake_env
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.state == "PLAYING"
    assert db.kv_get("dj_state") == "playing"
    assert db.list_schedules()[0]["last_fired_date"] == MON_0803.date().isoformat()

def test_fire_only_once_per_day(wake_env) -> None:
    db, dj, player = wake_env
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    player.stop()
    db.kv_set("dj_state", "stopped")
    dj.check_schedules(now=MON_0803 + timedelta(minutes=2))
    assert player.state == "STOPPED"  # did not refire

def test_fire_leaves_active_playback_alone(wake_env) -> None:
    db, dj, player = wake_env
    dj.start()
    queue_before = player.queue_uris()
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.queue_uris() == queue_before  # no jarring interruption

def test_fire_restarts_when_paused_mid_episode(wake_env) -> None:
    db, dj, player = wake_env
    dj.start()
    player.position = 200
    dj.pause()
    interrupted = db.kv_get("resume_episode_id")
    make_schedule(db)
    dj.check_schedules(now=MON_0803)
    assert player.state == "PLAYING" and player.index == 0
    # interrupted episode is staged (news would come first if any existed)
    staged = [e["id"] for e in db.episodes_with_status("queued")]
    assert int(interrupted) in staged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedules.py -q`
Expected: FAIL — `ImportError: cannot import name 'next_start' from 'dj'`

- [ ] **Step 3: Add schedule logic to dj.py**

Add to imports: `from datetime import datetime, time as dtime, timedelta`

Module-level functions:

```python
def parse_days(days: str) -> set[int]:
    return {int(d) for d in days.split(",") if d != ""}

def schedule_due(schedule, now: datetime, grace_minutes: int) -> bool:
    """Fire when the time has passed today, it hasn't fired today, and we're
    within the grace window — a reboot at 8:03 still catches the 8:00 start,
    but a server down all morning doesn't blast podcasts at 3 pm."""
    if not schedule["enabled"] or now.weekday() not in parse_days(schedule["days"]):
        return False
    if schedule["last_fired_date"] == now.date().isoformat():
        return False
    hh, mm = (int(p) for p in schedule["time"].split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return target <= now <= target + timedelta(minutes=grace_minutes)

def next_start(schedules, now: datetime) -> datetime | None:
    upcoming = []
    for s in schedules:
        if not s["enabled"]:
            continue
        hh, mm = (int(p) for p in s["time"].split(":"))
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            if day.weekday() not in parse_days(s["days"]):
                continue
            candidate = datetime.combine(day, dtime(hh, mm), tzinfo=now.tzinfo)
            if candidate > now:
                upcoming.append(candidate)
                break
    return min(upcoming) if upcoming else None
```

Methods on `DJ`:

```python
    # ── wake schedules ────────────────────────────────────────────────
    def check_schedules(self, now: datetime | None = None) -> None:
        now = now or datetime.now().astimezone()  # LOCAL time — TZ matters in Docker
        for schedule in self.db.list_schedules():
            if schedule_due(schedule, now, self.cfg.grace_minutes):
                logger.info("wake schedule %s (%s) firing", schedule["id"], schedule["time"])
                self.db.set_last_fired(schedule["id"], now.date().isoformat())
                self._fire_wake()

    def _fire_wake(self) -> None:
        feeds.refresh_all(self.db, self.cfg)  # catch news published minutes ago
        player = self.get_player()
        actively_playing = False
        if player is not None:
            try:
                actively_playing = (self.db.kv_get("dj_state") == "playing"
                                    and player.transport_state() == "PLAYING")
            except Exception:
                logger.exception("wake: player unreachable")
        if actively_playing:
            return  # standing news-insertion rule handles it
        error = self.start()  # news first, then the interrupted episode, then rotation
        if error:
            logger.warning("wake start failed: %s", error)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedules.py -q`
Expected: `12 passed` (and full suite green: `.venv/bin/pytest -q`)

- [ ] **Step 5: Commit**

```bash
git add dj.py tests/test_schedules.py
git commit -m "feat: wake schedules with grace window, once-per-day guard, news-first fire"
```

---

### Task 10: DJ.status() + web.py routes

**Files:**
- Modify: `dj.py` (add `DJ.status()`)
- Create: `web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `DJ.status() -> dict` (shape below — the dashboard's poll payload) and `web.create_app(db: Database, dj: DJ, cfg: Config) -> Flask`. Every POST returns `{"ok": bool, "error": str | null}` with HTTP 200 (404 only for unknown routes/ids); the dashboard JS shows `error` as a flash toast.

`status()` payload shape (keys always present):
```json
{
  "dj_state": "playing|stopped",
  "transport": "PLAYING|PAUSED_PLAYBACK|STOPPED|null",
  "speaker": {"name": "...", "ip": "..."},
  "now_playing": {"episode_id": 1, "title": "...", "show": "...",
                   "position": 120, "duration": 1800, "is_news": false},
  "up_next": [{"episode_id": 2, "title": "...", "show": "...", "is_news": true}],
  "stations": [{"id": 1, "title": "...", "url": "...", "image_url": null,
                 "is_news": false, "enabled": true,
                 "counts": {"new": 3, "queued": 1, "played": 7, "skipped": 0, "archived": 90}}],
  "schedules": [{"id": 1, "time": "08:00", "days": [0,1,2,3,4], "enabled": true}],
  "next_start": "Mon 08:00",
  "recently_played": [{"title": "...", "show": "...", "played_at": "..."}],
  "download_mode": false
}
```
`speaker`/`now_playing`/`transport`/`next_start` are `null` when unavailable.

- [ ] **Step 1: Write the failing tests**

`tests/test_web.py`:
```python
import pytest

from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer
from web import create_app

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
    db.insert_episode(fid, "g1", "e", "https://a/1.mp3",
                      "2026-01-01T00:00:00Z", status="archived")
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'web'`

- [ ] **Step 3: Add DJ.status() to dj.py**

```python
    # ── status for the dashboard poll ─────────────────────────────────
    def status(self) -> dict:
        now = datetime.now().astimezone()
        counts = self.db.counts_by_feed()
        schedules = self.db.list_schedules()
        data: dict = {
            "dj_state": self.db.kv_get("dj_state") or "stopped",
            "transport": None,
            "speaker": None,
            "now_playing": None,
            "up_next": [],
            "stations": [
                {
                    "id": f["id"], "title": f["title"], "url": f["url"],
                    "image_url": f["image_url"],
                    "is_news": bool(f["is_news"]), "enabled": bool(f["enabled"]),
                    "counts": {s: counts.get(f["id"], {}).get(s, 0)
                               for s in ("new", "queued", "played", "skipped", "archived")},
                }
                for f in self.db.list_feeds()
            ],
            "schedules": [
                {"id": s["id"], "time": s["time"],
                 "days": sorted(parse_days(s["days"])), "enabled": bool(s["enabled"])}
                for s in schedules
            ],
            "next_start": None,
            "recently_played": [
                {"title": e["title"], "show": e["feed_title"], "played_at": e["played_at"]}
                for e in self.db.recently_played()
            ],
            "download_mode": self.cfg.download_mode,
        }
        upcoming = next_start(schedules, now)
        data["next_start"] = upcoming.strftime("%a %H:%M") if upcoming else None
        try:
            player = self.get_player()
        except Exception:
            player = None
        if player is None:
            return data
        try:
            data["speaker"] = {"name": player.name, "ip": player.ip}
            data["transport"] = player.transport_state()
            queue, matches, cur_idx = self._match_queue(player)
            cur = player.current()

            def entry(ep) -> dict:
                feed = self.db.get_feed(ep["feed_id"])
                return {"episode_id": ep["id"], "title": ep["title"],
                        "show": feed["title"] if feed else "",
                        "is_news": bool(feed and feed["is_news"])}

            if cur is not None and cur_idx < len(matches) and matches[cur_idx] is not None:
                data["now_playing"] = {**entry(matches[cur_idx]),
                                       "position": cur.position, "duration": cur.duration}
            data["up_next"] = [entry(ep) for ep in matches[cur_idx + 1:] if ep is not None]
        except Exception:
            logger.exception("status: player unreachable")
        return data
```

- [ ] **Step 4: Write web.py**

```python
"""Flask routes. Thin: parse/validate input, call dj/db/feeds, return
{"ok", "error"} JSON the dashboard turns into flash messages."""
from __future__ import annotations

import re

from flask import Flask, jsonify, render_template, request, send_from_directory

import feeds as feeds_mod
import sonos_ctl
from config import Config
from db import Database
from dj import DJ

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

def create_app(db: Database, dj: DJ, cfg: Config) -> Flask:
    app = Flask(__name__)

    def result(error: str | None = None):
        return jsonify({"ok": error is None, "error": error})

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def api_status():
        return jsonify(dj.status())

    @app.get("/api/speakers")
    def api_speakers():
        return jsonify({"speakers": sonos_ctl.discover_speakers()})

    @app.post("/api/speaker")
    def api_speaker():
        ip = ((request.get_json(silent=True) or {}).get("ip") or "").strip()
        if not ip:
            return result("missing ip")
        db.kv_set("speaker_ip", ip)
        return result()

    @app.post("/player/<action>")
    def player_action(action: str):
        actions = {
            "play": dj.play,
            "pause": dj.pause,
            "restart": lambda: dj.seek_abs(0),
            "back_15": lambda: dj.seek_rel(-15),
            "fwd_30": lambda: dj.seek_rel(30),
            "skip_later": dj.skip_later,
            "skip_done": dj.skip_done,
            "stop": dj.stop_off_air,
            "group_all": dj.group_all,
        }
        fn = actions.get(action)
        if fn is None:
            return result(f"unknown action: {action}"), 404
        return result(fn())

    @app.post("/player/seek")
    def player_seek():
        body = request.get_json(silent=True) or {}
        try:
            seconds = int(body["seconds"])
        except (KeyError, TypeError, ValueError):
            return result("seconds (int) required")
        return result(dj.seek_abs(seconds))

    @app.post("/feeds")
    def add_feed():
        body = request.get_json(silent=True) or request.form
        url = (body.get("url") or "").strip()
        if not url:
            return result("url required")
        include = body.get("include") or "latest"
        if include not in feeds_mod.INCLUDE_MODES:
            return result(f"include must be one of {feeds_mod.INCLUDE_MODES}")
        is_news = str(body.get("is_news", "")).lower() in ("1", "true", "on", "yes")
        try:
            last_n = int(body.get("count") or 0) or None
        except (TypeError, ValueError):
            last_n = None
        try:
            feeds_mod.add_feed(db, cfg, url, is_news, include, last_n)
        except Exception as exc:
            return result(f"could not add feed: {exc}")
        return result()

    @app.post("/feeds/<int:feed_id>/<action>")
    def feed_action(feed_id: int, action: str):
        feed = db.get_feed(feed_id)
        if feed is None:
            return result("no such feed"), 404
        if action == "delete":
            db.delete_feed(feed_id)
        elif action == "toggle":
            db.toggle_feed(feed_id)
        elif action == "news":
            db.set_feed_news(feed_id, not feed["is_news"])
        elif action == "unarchive":
            db.unarchive_feed(feed_id)
        else:
            return result(f"unknown action: {action}"), 404
        return result()

    @app.post("/schedules")
    def add_schedule():
        body = request.get_json(silent=True) or {}
        time_str = (body.get("time") or "").strip()
        days = body.get("days") or []
        if not TIME_RE.match(time_str):
            return result("time must be HH:MM")
        try:
            day_ints = sorted({int(d) for d in days})
        except (TypeError, ValueError):
            day_ints = []
        if not day_ints or not all(0 <= d <= 6 for d in day_ints):
            return result("pick at least one day")
        db.add_schedule(time_str, day_ints)
        return result()

    @app.post("/schedules/<int:schedule_id>/<action>")
    def schedule_action(schedule_id: int, action: str):
        if not any(s["id"] == schedule_id for s in db.list_schedules()):
            return result("no such schedule"), 404
        if action == "toggle":
            db.toggle_schedule(schedule_id)
        elif action == "delete":
            db.delete_schedule(schedule_id)
        else:
            return result(f"unknown action: {action}"), 404
        return result()

    @app.get("/media/<path:filename>")
    def media(filename: str):
        # conditional=True serves HTTP Range requests — what makes Sonos
        # seeking work on local files in download mode
        return send_from_directory(cfg.media_dir, filename, conditional=True)

    return app
```

Also create a placeholder `templates/index.html` so `GET /` renders (fully built in Task 11):

```html
<title>Sonos Talk Radio</title>
<h1>Sonos Talk Radio</h1>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -q`
Expected: `11 passed` (full suite green too)

- [ ] **Step 6: Commit**

```bash
git add dj.py web.py templates/index.html tests/test_web.py
git commit -m "feat: status payload and Flask control surface"
```

---

### Task 11: templates/index.html — receiver-faceplate dashboard

**Files:**
- Modify: `templates/index.html` (replace the Task 10 placeholder with the full page)
- Test: existing `tests/test_web.py::test_dashboard_loads_without_sonos` keeps passing

**Interfaces:**
- Consumes: `GET /api/status` payload (Task 10 shape), all POST routes.
- Produces: the single self-contained page. No build step, no external assets, system fonts only.

**Design intent (from the prompt):** hi-fi receiver faceplate — warm near-black `#171310`, amber dial accent `#f0a43a`, red only for the pulsing ON AIR lamp and news tags `#d4553f`, monospace timestamps/counters, tuner-strip progress bar with glowing needle as the signature element. Touch surface: ≥44px tap targets on the transport row, readable across the room, no hover-dependent interactions, instant visual feedback on button press (`.pressed` class + `:active`), `prefers-reduced-motion` respected. This is a wall-tablet remote, not an admin template.

- [ ] **Step 1: Write the full page**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sonos Talk Radio</title>
<style>
:root {
  --bg: #171310; --panel: #201a15; --panel2: #26201a; --line: #3a2f24;
  --amber: #f0a43a; --amber-dim: #8a6a35; --red: #d4553f;
  --text: #e8ddcc; --muted: #9a8c74;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font: 16px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
  max-width: 1100px; margin: 0 auto; padding: 16px;
}
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
h2 {
  color: var(--amber); font-size: 13px; letter-spacing: .18em;
  text-transform: uppercase; margin-bottom: 10px; font-weight: 600;
}
section {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px; margin-bottom: 14px;
}
button {
  background: var(--panel2); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; min-height: 44px; min-width: 44px; padding: 0 14px;
  font-size: 15px; cursor: pointer;
}
button:active, button.pressed { background: var(--amber); color: #171310; }
button.danger:active, button.danger.pressed { background: var(--red); color: #fff; }
input, select {
  background: var(--panel2); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; min-height: 44px; padding: 0 10px; font-size: 15px;
}
header { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; flex-wrap: wrap; }
header h1 { font-size: 20px; letter-spacing: .28em; font-weight: 700; }
.lamp {
  display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px;
  border: 1px solid var(--line); border-radius: 999px; font-size: 12px;
  letter-spacing: .2em; color: var(--muted);
}
.lamp .dot { width: 10px; height: 10px; border-radius: 50%; background: #443a2e; }
.lamp.on { color: var(--red); border-color: var(--red); }
.lamp.on .dot { background: var(--red); box-shadow: 0 0 10px var(--red); animation: pulse 1.6s infinite; }
@keyframes pulse { 50% { opacity: .45; } }
@media (prefers-reduced-motion: reduce) { .lamp.on .dot { animation: none; } }

#np-show { color: var(--muted); font-size: 14px; }
#np-title { font-size: 22px; margin: 2px 0 12px; min-height: 28px; }
.tuner {
  position: relative; height: 46px; background: var(--panel2);
  border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
  background-image: repeating-linear-gradient(90deg, var(--line) 0 1px, transparent 1px 9%);
}
.tuner .fill { position: absolute; inset: 0; width: 0; background: rgba(240,164,58,.14); border-radius: 8px; }
.tuner .needle {
  position: absolute; top: -4px; bottom: -4px; left: 0; width: 3px;
  background: var(--amber); box-shadow: 0 0 12px var(--amber);
}
.times { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; margin-top: 6px; }
.transport { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.transport button { flex: 1 1 auto; min-height: 56px; font-size: 16px; }
#btn-onair { border-color: var(--red); color: var(--red); }
#btn-toggle { min-width: 110px; border-color: var(--amber); color: var(--amber); font-size: 22px; }

ul { list-style: none; }
li.row {
  display: flex; align-items: center; gap: 10px; padding: 10px 4px;
  border-bottom: 1px solid var(--line); flex-wrap: wrap;
}
li.row:last-child { border-bottom: none; }
.tag {
  background: var(--red); color: #fff; font-size: 11px; letter-spacing: .1em;
  border-radius: 4px; padding: 2px 7px;
}
.tag.off { background: var(--line); color: var(--muted); }
.grow { flex: 1 1 200px; }
.counts { color: var(--muted); font-size: 13px; }
.qnum { color: var(--amber-dim); width: 24px; text-align: right; }
.chips { display: flex; gap: 6px; }
.chip {
  min-width: 44px; min-height: 44px; border-radius: 50%; font-size: 13px;
}
.chip.on { background: var(--amber); color: #171310; border-color: var(--amber); }
form.inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
.muted { color: var(--muted); font-size: 14px; }
#flash {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
  background: var(--red); color: #fff; padding: 12px 20px; border-radius: 8px;
  display: none; max-width: 90vw; z-index: 9;
}
#flash.ok { background: var(--amber); color: #171310; }
</style>
</head>
<body>
<header>
  <span class="lamp" id="lamp"><span class="dot"></span>ON AIR</span>
  <h1>SONOS TALK RADIO</h1>
  <span class="grow"></span>
  <select id="speakers" aria-label="Speaker"></select>
  <button id="btn-scan">Scan</button>
  <button data-player="group_all">Group all</button>
</header>

<section id="now-playing">
  <div id="np-show">—</div>
  <div id="np-title">Nothing playing</div>
  <div class="tuner" id="tuner" title="Tap to seek">
    <div class="fill" id="tuner-fill"></div>
    <div class="needle" id="needle"></div>
  </div>
  <div class="times mono"><span id="t-pos">0:00</span><span id="t-dur">0:00</span></div>
  <div class="transport">
    <button id="btn-onair" data-player="play">ON AIR</button>
    <button data-player="restart" title="Restart episode">&#9198;</button>
    <button data-player="back_15">&minus;15s</button>
    <button id="btn-toggle">&#9654;</button>
    <button data-player="fwd_30">+30s</button>
    <button data-player="skip_later" title="Back into rotation another day">Skip &middot; later</button>
    <button data-player="skip_done" title="Never play again">Skip &middot; done</button>
    <button class="danger" data-player="stop">OFF AIR</button>
  </div>
</section>

<section><h2>Up next</h2><ul id="up-next"></ul></section>

<section>
  <h2>Stations</h2><ul id="stations"></ul>
  <form class="inline" id="add-feed">
    <input type="url" id="feed-url" placeholder="RSS URL" required class="grow">
    <label><input type="checkbox" id="feed-news"> news</label>
    <select id="feed-include">
      <option value="latest" selected>Latest episode</option>
      <option value="new_only">New episodes only</option>
      <option value="last_n">Last N episodes</option>
      <option value="all">Entire back catalog</option>
    </select>
    <input type="number" id="feed-count" min="1" value="5" style="display:none;width:80px" aria-label="How many">
    <button type="submit">Add station</button>
  </form>
</section>

<section>
  <h2>Wake schedule</h2>
  <div class="muted" id="next-start"></div>
  <ul id="schedules"></ul>
  <form class="inline" id="add-schedule">
    <input type="time" id="sched-time" required value="08:00">
    <span class="chips" id="sched-days"></span>
    <button type="submit">Add alarm</button>
  </form>
</section>

<section><h2>Recently played</h2><ul id="recent"></ul></section>
<div id="flash" role="alert"></div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let status = null;
let flashTimer = null;

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function flash(msg, ok = false) {
  const el = $("flash");
  el.textContent = msg;
  el.className = ok ? "ok" : "";
  el.style.display = "block";
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { el.style.display = "none"; }, 4000);
}
function fmt(sec) {
  sec = Math.max(0, sec | 0);
  const h = (sec / 3600) | 0, m = ((sec % 3600) / 60) | 0, s = sec % 60;
  const ms = String(s).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ms}` : `${m}:${ms}`;
}
async function post(path, body, btn) {
  if (btn) { btn.classList.add("pressed"); setTimeout(() => btn.classList.remove("pressed"), 350); }
  try {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json();
    if (!data.ok) flash(data.error || "Something went wrong");
    await poll();
    return data.ok;
  } catch (err) { flash("Server unreachable"); return false; }
}

// ── render ─────────────────────────────────────────────────────────
function render() {
  const s = status;
  const onAir = s.dj_state === "playing";
  $("lamp").className = "lamp" + (onAir ? " on" : "");
  const np = s.now_playing;
  $("np-show").textContent = np ? np.show : "—";
  $("np-title").textContent = np ? np.title : (onAir ? "…" : "Nothing playing");
  const pct = np && np.duration ? Math.min(100, 100 * np.position / np.duration) : 0;
  $("needle").style.left = `calc(${pct}% - 1px)`;
  $("tuner-fill").style.width = `${pct}%`;
  $("t-pos").textContent = np ? fmt(np.position) : "0:00";
  $("t-dur").textContent = np ? fmt(np.duration) : "0:00";
  $("btn-toggle").innerHTML = s.transport === "PLAYING" ? "&#10074;&#10074;" : "&#9654;";

  $("up-next").innerHTML = s.up_next.map((e, i) =>
    `<li class="row"><span class="qnum mono">${i + 1}</span>` +
    (e.is_news ? '<span class="tag">NEWS</span>' : "") +
    `<span class="grow">${esc(e.title)}</span><span class="muted">${esc(e.show)}</span></li>`
  ).join("") || '<li class="row muted">Queue empty</li>';

  $("stations").innerHTML = s.stations.map((f) => {
    const c = f.counts;
    return `<li class="row">` +
      (f.is_news ? '<span class="tag">NEWS</span>' : "") +
      (f.enabled ? "" : '<span class="tag off">PAUSED</span>') +
      `<span class="grow">${esc(f.title)}</span>` +
      `<span class="counts mono">${c.new} new · ${c.played} played · ${c.archived} archived</span>` +
      `<button data-feed="${f.id}/news">${f.is_news ? "Not news" : "Mark news"}</button>` +
      `<button data-feed="${f.id}/toggle">${f.enabled ? "Pause" : "Enable"}</button>` +
      (c.archived ? `<button data-feed="${f.id}/unarchive">Add back catalog</button>` : "") +
      `<button class="danger" data-feed="${f.id}/delete" data-confirm="Remove ${esc(f.title)}?">Remove</button>` +
      `</li>`;
  }).join("") || '<li class="row muted">No stations yet — add an RSS URL below</li>';

  $("next-start").textContent = s.next_start ? `Next start: ${s.next_start}` : "No upcoming starts";
  $("schedules").innerHTML = s.schedules.map((a) =>
    `<li class="row"><span class="mono" style="font-size:20px">${esc(a.time)}</span>` +
    `<span class="muted">${a.days.map((d) => DAYS[d]).join(" ")}</span><span class="grow"></span>` +
    `<button data-sched="${a.id}/toggle">${a.enabled ? "On" : "Off"}</button>` +
    `<button class="danger" data-sched="${a.id}/delete">Delete</button></li>`
  ).join("") || '<li class="row muted">No alarms</li>';

  $("recent").innerHTML = s.recently_played.map((e) =>
    `<li class="row"><span class="grow">${esc(e.title)}</span>` +
    `<span class="muted">${esc(e.show)}</span></li>`
  ).join("") || '<li class="row muted">Nothing yet</li>';

  const sel = $("speakers");
  if (s.speaker && !sel.options.length) {
    sel.innerHTML = `<option value="${esc(s.speaker.ip)}">${esc(s.speaker.name)}</option>`;
  }
}

async function poll() {
  try {
    status = await (await fetch("/api/status")).json();
    render();
  } catch (err) { /* transient: keep last render */ }
}

// ── wire up ────────────────────────────────────────────────────────
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  if (btn.dataset.player) return void post(`/player/${btn.dataset.player}`, {}, btn);
  if (btn.dataset.feed) return void post(`/feeds/${btn.dataset.feed}`, {}, btn);
  if (btn.dataset.sched) return void post(`/schedules/${btn.dataset.sched}`, {}, btn);
});

$("btn-toggle").addEventListener("click", (ev) => {
  const action = status && status.transport === "PLAYING" ? "pause" : "play";
  post(`/player/${action}`, {}, ev.currentTarget);
});

$("tuner").addEventListener("click", (ev) => {
  const np = status && status.now_playing;
  if (!np || !np.duration) return;
  const rect = $("tuner").getBoundingClientRect();
  const frac = (ev.clientX - rect.left) / rect.width;
  post("/player/seek", { seconds: Math.round(frac * np.duration) });
});

$("btn-scan").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.classList.add("pressed");
  btn.textContent = "Scanning…";
  try {
    const data = await (await fetch("/api/speakers")).json();
    const sel = $("speakers");
    sel.innerHTML = data.speakers.map((sp) =>
      `<option value="${esc(sp.ip)}">${esc(sp.name)}</option>`).join("");
    if (!data.speakers.length) flash("No speakers found — same network? host networking?");
  } finally {
    btn.classList.remove("pressed");
    btn.textContent = "Scan";
  }
});
$("speakers").addEventListener("change", (ev) => post("/api/speaker", { ip: ev.target.value }));

$("feed-include").addEventListener("change", (ev) => {
  $("feed-count").style.display = ev.target.value === "last_n" ? "" : "none";
});
$("add-feed").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = await post("/feeds", {
    url: $("feed-url").value,
    is_news: $("feed-news").checked,
    include: $("feed-include").value,
    count: $("feed-count").value,
  });
  if (ok) { $("feed-url").value = ""; flash("Station added", true); }
});

const dayChips = DAYS.map((d, i) => {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "chip" + (i < 5 ? " on" : "");
  b.textContent = d[0];
  b.title = d;
  b.addEventListener("click", () => b.classList.toggle("on"));
  $("sched-days").appendChild(b);
  return b;
});
$("add-schedule").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const days = dayChips.flatMap((b, i) => (b.classList.contains("on") ? [i] : []));
  post("/schedules", { time: $("sched-time").value, days });
});

poll();
setInterval(poll, 5000);
</script>
</body>
</html>
```

- [ ] **Step 2: Verify tests still pass and eyeball it**

Run: `.venv/bin/pytest -q`
Expected: full suite green (the dashboard test asserts the title string).

Run: `DATA_DIR=/tmp/radio-smoke .venv/bin/python main.py` is not available yet (Task 12) — instead spot-check rendering with:
`.venv/bin/python -c "from db import Database; from config import Config; from dj import DJ; from web import create_app; import tempfile, os; p=os.path.join(tempfile.mkdtemp(),'t.db'); d=Database(p); d.init(); app=create_app(d, DJ(d, Config.from_env({}), lambda: None), Config.from_env({})); c=app.test_client(); html=c.get('/').data.decode(); assert 'tuner' in html and 'ON AIR' in html; print('dashboard renders,', len(html), 'bytes')"`
Expected: `dashboard renders, <n> bytes`

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: receiver-faceplate dashboard — tuner bar, transport, stations, alarms"
```

---

### Task 12: main.py — wiring and the background loop

**Files:**
- Create: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `dj_loop(db: Database, cfg: Config, dj_instance: DJ, stop_event: threading.Event) -> None` (importable for tests) and `main() -> None` (entry point: init dirs + DB, start daemon thread, run Flask with `use_reloader=False`).

- [ ] **Step 1: Write the failing tests**

`tests/test_main.py`:
```python
import threading

from config import Config
from db import Database
from main import dj_loop

class ExplodingDJ:
    """Every call raises — the loop must swallow and keep going."""
    calls = 0

    def check_schedules(self) -> None:
        type(self).calls += 1
        raise RuntimeError("boom")

    def tick(self) -> None:
        raise RuntimeError("boom")

def test_loop_survives_exceptions_and_stops(db: Database, monkeypatch) -> None:
    cfg = Config.from_env({"TICK_SECONDS": "0", "REFRESH_MINUTES": "9999"})
    import main as main_mod
    monkeypatch.setattr(main_mod.feeds, "refresh_all", lambda db_, cfg_: None)
    stop = threading.Event()
    dj = ExplodingDJ()
    t = threading.Thread(target=dj_loop, args=(db, cfg, dj, stop), daemon=True)
    t.start()
    for _ in range(200):
        if dj.calls >= 2:
            break
        threading.Event().wait(0.01)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert dj.calls >= 2  # kept looping despite exceptions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_main.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 3: Write main.py**

```python
"""Entry point: init DB, start the DJ loop thread, run Flask."""
from __future__ import annotations

import logging
import os
import threading
import time

import feeds
import sonos_ctl
from config import Config
from db import Database
from dj import DJ
from web import create_app

logger = logging.getLogger(__name__)

def dj_loop(db: Database, cfg: Config, dj_instance: DJ,
            stop_event: threading.Event) -> None:
    """Refresh feeds every REFRESH_MINUTES; reconcile + check wake
    schedules every TICK_SECONDS. One broken iteration never kills it."""
    last_refresh = 0.0
    while not stop_event.is_set():
        try:
            if time.monotonic() - last_refresh >= cfg.refresh_minutes * 60:
                feeds.refresh_all(db, cfg)
                last_refresh = time.monotonic()
            dj_instance.check_schedules()
            dj_instance.tick()
        except Exception:
            logger.exception("dj loop iteration failed")
        stop_event.wait(cfg.tick_seconds)

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.media_dir, exist_ok=True)
    db = Database(cfg.db_path)
    db.init()
    dj_instance = DJ(db, cfg, sonos_ctl.make_player_provider(db, cfg))
    stop_event = threading.Event()
    threading.Thread(target=dj_loop, args=(db, cfg, dj_instance, stop_event),
                     daemon=True, name="dj-loop").start()
    app = create_app(db, dj_instance, cfg)
    app.run(host=cfg.host, port=cfg.port, use_reloader=False)

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, then boot the real app with no Sonos**

Run: `.venv/bin/pytest -q`
Expected: full suite green.

Run: `DATA_DIR=/tmp/radio-smoke PORT=8099 .venv/bin/python main.py & sleep 3 && curl -s http://127.0.0.1:8099/api/status | head -c 300; kill %1`
Expected: JSON with `"dj_state": "stopped"` and `"speaker": null` — boots fine with no Sonos reachable.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: entry point with crash-proof DJ loop thread"
```

---

### Task 13: Dockerfile, docker-compose.yml, README.md

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `README.md`

**Interfaces:**
- Consumes: `main.py` entry point, env vars from `Config`.
- Produces: deployment artifacts; no code interfaces.

- [ ] **Step 1: Write Dockerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data
EXPOSE 8080
CMD ["python", "main.py"]
```

- [ ] **Step 2: Write docker-compose.yml**

```yaml
services:
  talk-radio:
    build: .
    # Host networking is required: Sonos discovery uses multicast/SSDP,
    # which doesn't cross Docker's default bridge network.
    network_mode: host
    restart: unless-stopped
    environment:
      # Containers default to UTC. Wake alarms fire in THIS timezone —
      # set it or your 8:00 alarm rings at 8:00 UTC.
      - TZ=${TZ:-America/Chicago}
      - DATA_DIR=/data
      # - SONOS_IP=192.168.1.50      # skip discovery entirely
      # - DOWNLOAD_MODE=1            # for CDNs Sonos won't stream from
    volumes:
      - ./data:/data
```

- [ ] **Step 3: Write README.md**

```markdown
# Sonos Talk Radio

A self-hosted podcast DJ for your Sonos system. Add your science/history/
philosophy shows plus a couple of news feeds, press **On air**, and it
programs the speakers like a personal talk-radio station — forever.

## The three DJ rules

1. **Random show rotation.** The DJ picks a random *show*, not a random
   episode, and won't play the same show twice in a row while another show
   has unplayed episodes.
2. **Multi-part episodes stay in order.** Within a show, it always plays the
   oldest unplayed episode — a 3-part series arrives 1 → 2 → 3, naturally
   interleaved with other shows.
3. **News always plays first.** Feeds flagged "news" jump to the front of
   Up Next, oldest first. News older than 24 h (configurable) is dropped —
   you'll never hear Tuesday's headlines on Thursday.

## Quickstart — Docker (recommended)

    git clone <this repo> && cd talk-radio
    TZ=America/Chicago docker compose up -d --build

Open http://<server>:8080.

## Quickstart — plain venv

    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python main.py

## First run

1. **Pick a speaker** — hit *Scan*, choose one (grouped rooms follow the
   coordinator automatically; *Group all* groups every speaker).
2. **Add stations** — paste RSS URLs. Check *news* for news feeds. Pick how
   much back catalog to include:
   - *New episodes only* — nothing until the next episode drops
   - *Latest episode* (default) — start from the newest
   - *Last N episodes* — the newest N
   - *Entire back catalog* — starts the show at episode 1
   You can release the archive later with *Add back catalog*.
3. **Set wake times** — alarm-style rows (time + day chips), e.g. 08:00
   Mon–Fri and 10:00 Sat–Sun.
4. Press **ON AIR**.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | SQLite + downloaded media live here |
| `DB_PATH` | `$DATA_DIR/radio.db` | SQLite file |
| `MEDIA_DIR` | `$DATA_DIR/media` | download-mode storage |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | web bind |
| `SONOS_SPEAKER` | — | speaker name to prefer at discovery |
| `SONOS_IP` | — | skip discovery, use this IP |
| `TICK_SECONDS` | `15` | reconcile loop interval |
| `REFRESH_MINUTES` | `30` | feed refresh interval |
| `QUEUE_AHEAD` | `3` | tracks kept queued ahead of the needle |
| `NEWS_MAX_AGE_HOURS` | `24` | news older than this is skipped |
| `DOWNLOAD_MODE` | `0` | `1` = download & serve locally (see below) |
| `BASE_URL` | auto | how the speaker reaches this server |
| `GRACE_MINUTES` | `10` | wake-alarm catch-up window after downtime |
| `TZ` | — | timezone wake alarms fire in (**set this in Docker**) |
| `USER_AGENT` | `SonosTalkRadio/1.0` | for feed/audio requests |

## DOWNLOAD_MODE

By default episodes stream straight from the podcast CDN. Some CDNs
(token-guarded URLs, long redirect chains) won't play on Sonos. Set
`DOWNLOAD_MODE=1` and the app downloads each episode and serves it from
`/media/` with HTTP Range support (that's what makes Sonos seeking work on
local files), deleting it once played. `BASE_URL` is auto-detected from the
server's LAN address as the speaker sees it; set it explicitly if detection
guesses wrong.

## Wake schedule (and why TZ matters)

Alarms fire on **local time inside the container** — and containers default
to UTC. Pass `TZ` in compose or your 8:00 alarm fires at 8:00 UTC. Firing
refreshes feeds first, so headlines published 20 minutes earlier are
included; if the station was paused mid-episode the night before, morning
flow is: fresh news → the interrupted episode right where you left off →
normal rotation. A `GRACE_MINUTES` window means a reboot at 8:03 still
catches the 8:00 start, but a server down all morning stays quiet at 3 pm.

The transport API is plain HTTP, so external automation works too:

    curl -X POST http://server:8080/player/play

Actions: `play` `pause` `restart` `back_15` `fwd_30` `skip_later`
`skip_done` `stop` `group_all`.

## Troubleshooting

- **Discovery finds nothing** — the container must share the LAN
  (`network_mode: host`), or set `SONOS_IP` and skip discovery.
- **An episode won't play / instantly skips** — that CDN doesn't stream to
  Sonos; try `DOWNLOAD_MODE=1`.
- **Alarms fire at odd hours** — `TZ` isn't set in the container.
- **Someone started Spotify** — the DJ notices its queue is gone and stands
  down; press ON AIR to take back over.
```

- [ ] **Step 4: Verify the Docker build**

Run: `docker build -t talk-radio-test . && docker run --rm talk-radio-test python -c "import main; print('imports ok')"`
Expected: image builds; prints `imports ok`. (Skip gracefully if Docker isn't running locally — note it for deploy-time verification.)

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml README.md
git commit -m "feat: Docker deployment (host networking, TZ) and README"
```

---

### Task 14: Live verification against the real Sonos

**Files:** none (verification only; fix bugs found as their own commits)

**Interfaces:** consumes the running app end-to-end.

This is the `## Verification` section of the design doc, executed with the real speaker on this network. Use the `verify` skill's spirit: drive the actual flow, observe behavior, don't just trust tests.

- [ ] **Step 1: Boot and pick the speaker**

Run: `DATA_DIR=./data PORT=8080 .venv/bin/python main.py` (background it), then
`curl -s http://127.0.0.1:8080/api/speakers` — expect the household's speakers listed. Select one:
`curl -s -X POST http://127.0.0.1:8080/api/speaker -H 'Content-Type: application/json' -d '{"ip": "<chosen>"}'`

- [ ] **Step 2: Add real feeds** — one news (NPR News Now: `https://feeds.npr.org/500005/podcast.xml`, flagged news) and two shows with different catalog scopes, via `POST /feeds` or the dashboard.

- [ ] **Step 3: On air** — `POST /player/play`. Verify on the speaker and in `/api/status`: news first (oldest first), then rotation; queue holds `QUEUE_AHEAD+1` tracks; two consecutive rotation picks are different shows.

- [ ] **Step 4: Transport** — from the dashboard on a phone/tablet: play/pause toggle, tap the tuner bar to seek, −15 s/+30 s/restart, *Skip — later* (episode back in Stations' new count, gone from queue), *Skip — done* (lands in Recently played).

- [ ] **Step 5: Native-app skip** — press next in the Sonos app; within a tick the passed episode is marked played and the queue tops back up.

- [ ] **Step 6: Hijack** — start any Spotify/other content on the speaker; within a tick `/api/status` shows `dj_state: stopped` and no `queued` episodes remain.

- [ ] **Step 7: Wake schedule** — add an alarm 2 minutes out for today; press pause mid-episode; wait. Expect: feeds refresh, then playback restarts with (any fresh) news first and the interrupted episode resuming ~10 s before where it paused.

- [ ] **Step 8: Off air + reboot resilience** — `POST /player/stop`; restart the process; dashboard loads, state intact, ON AIR works again.

- [ ] **Step 9: Record findings** — fix anything broken (each fix = its own TDD cycle + commit), then final commit:

```bash
git add -A && git commit -m "chore: live verification fixes against real Sonos"
```

---

## Plan Self-Review Notes

- **Spec coverage:** all prompt sections map to tasks — product rules (T6/T7), data model (T2), DJ engine (T7/T8), catalog scope (T3), wake schedule (T9), audio delivery (T4/T7), SoCo gotchas (T5), dashboard (T11), routes (T10), config (T1), Docker/README (T13), tests (every task), acceptance criteria (T14).
- **Delta from prompt data model, intentional:** `episodes.play_uri` column added — reconciliation matches Sonos queue URIs against the *resolved/enqueued* URI, which differs from `audio_url` after redirect resolution or download-mode rewriting. `episodes.played_at` added for Recently-played ordering. `kv` keys used: `speaker_ip`, `last_feed_id`, `dj_state`, `resume_episode_id`.
- **Type consistency verified:** `Database` method names/signatures match between Task 2 definitions and Tasks 3/6/7/8/9/10 call sites; `FakeSonosPlayer` mirrors `SonosPlayer`'s surface exactly; `DJ` control methods match the `web.py` action map.
