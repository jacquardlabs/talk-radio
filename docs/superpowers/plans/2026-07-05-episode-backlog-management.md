# Episode Backlog Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the dashboard browse a station's full episode list (paginated, searchable), release individual or multi-selected archived episodes instead of only all-at-once, force-play a specific episode next or immediately regardless of its current status, and search episodes globally across every station.

**Architecture:** New paginated read endpoints and mutation endpoints in `web.py`, backed by new `db.py` query/mutation methods — fetched on demand, never folded into the existing 5-second `/api/status` poll. One new `DJ.play_episode()` method composes existing `start()`/`_enqueue()`/`_skip()` machinery rather than adding new queue-reconciliation logic. The dashboard template gets a new global-search section and a per-station expandable episode panel; station-list rendering switches from full `innerHTML` replace to a keyed DOM patch so an open panel's live search input survives the poll.

**Tech Stack:** Python 3.12, Flask, SQLite (stdlib `sqlite3`), pytest, vanilla JS (no build step, no frontend test harness).

## Global Constraints

- No DB schema changes — the existing `episodes` table (`db.py:20-34`) has everything needed.
- Pagination page size is fixed at 25 (no user-configurable page size).
- SQLite `LIKE` is case-insensitive for ASCII by default — no extra normalization needed for search matching.
- The existing `POST /feeds/<id>/unarchive` (release-all) route is unchanged and stays available.
- All new mutation routes return the existing `{"ok": bool, "error": str|None}` JSON shape via the existing `result()` helper in `web.py`; transport-affecting routes (`play_next`/`play_now`) reuse the existing `call_player()` wrapper so a dead/unreachable speaker degrades to a JSON error, never a 500.
- No frontend test harness exists in this repo — frontend tasks are verified manually (this repo has no JS test runner configured), not with automated tests.
- Follow existing code conventions exactly: type hints on all Python, `from __future__ import annotations` already present per-file, one connection per DB operation via `Database._conn()`.

---

### Task 1: DB layer — pagination, search, and release methods

**Files:**
- Modify: `db.py:239` (insert new methods immediately after `unarchive_feed`, before `set_resume`)
- Test: `tests/test_db.py` (append new tests at end of file)

**Interfaces:**
- Consumes: nothing new — uses the existing `Database._conn()` context manager and `episodes`/`feeds` tables.
- Produces (used by Task 3):
  - `count_episodes_for_feed(feed_id: int, q: str | None = None) -> int`
  - `episodes_for_feed_page(feed_id: int, page: int, page_size: int, q: str | None = None) -> list[sqlite3.Row]`
  - `count_search_episodes(q: str) -> int`
  - `search_episodes(q: str, page: int, page_size: int) -> list[sqlite3.Row]` (rows include `feed_title`)
  - `release_episode(episode_id: int) -> bool` (True if it was archived and got released)
  - `release_episodes(episode_ids: list[int]) -> int` (count actually released)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
def test_episodes_for_feed_page_paginates_and_filters(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    for i in range(1, 8):
        _ep(db, fid, i)
    assert db.count_episodes_for_feed(fid) == 7
    page1 = db.episodes_for_feed_page(fid, 1, 5)
    page2 = db.episodes_for_feed_page(fid, 2, 5)
    assert len(page1) == 5 and len(page2) == 2
    assert page1[0]["guid"] == f"guid-{fid}-7"  # newest first
    assert db.count_episodes_for_feed(fid, q="Ep 3") == 1
    assert db.episodes_for_feed_page(fid, 1, 5, q="Ep 3")[0]["guid"] == f"guid-{fid}-3"


def test_search_episodes_matches_title_or_feed_title(db: Database) -> None:
    a = db.add_feed("https://a/rss", "Mothman Museum Hour", None, False)
    b = db.add_feed("https://b/rss", "Other Show", None, False)
    _ep(db, a, 1)  # title "Ep 1"
    db.insert_episode(b, "g-b-1", "The Mothman Prophecies", "https://cdn/b/1.mp3",
                      "2026-01-01T00:00:00Z")
    assert db.count_search_episodes("mothman") == 2
    results = db.search_episodes("mothman", 1, 25)
    titles = {r["feed_title"] for r in results}
    assert titles == {"Mothman Museum Hour", "Other Show"}


def test_search_episodes_paginates(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "Cryptid Hour", None, False)
    for i in range(1, 4):
        _ep(db, fid, i)
    assert db.count_search_episodes("Ep") == 3
    assert len(db.search_episodes("Ep", 1, 2)) == 2
    assert len(db.search_episodes("Ep", 2, 2)) == 1


def test_release_episode_only_affects_archived(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    new_id = _ep(db, fid, 1)
    archived_id = _ep(db, fid, 2)
    db.archive_episode(archived_id)
    assert db.release_episode(new_id) is False
    assert db.get_episode(new_id)["status"] == "new"
    assert db.release_episode(archived_id) is True
    assert db.get_episode(archived_id)["status"] == "new"


def test_release_episodes_bulk_only_releases_archived(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    ids = [_ep(db, fid, i) for i in range(1, 4)]
    db.archive_episode(ids[0])
    db.archive_episode(ids[1])
    # ids[2] stays "new" -- bulk release must skip it, not error
    assert db.release_episodes(ids) == 2
    assert db.get_episode(ids[0])["status"] == "new"
    assert db.get_episode(ids[1])["status"] == "new"
    assert db.get_episode(ids[2])["status"] == "new"  # unchanged, was never archived


def test_release_episodes_bulk_empty_list(db: Database) -> None:
    assert db.release_episodes([]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -k "page or search_episodes or release" -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'count_episodes_for_feed'` (and similar for the other new methods).

- [ ] **Step 3: Implement the new methods**

In `db.py`, insert immediately after `unarchive_feed` (ends at line 239, right before `def set_resume`):

```python
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

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: all tests pass, including the new ones.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: add episode pagination, search, and per-episode release to db.py"
```

---

### Task 2: DJ engine — force-play a specific episode

**Files:**
- Modify: `dj.py:10` (add `Literal` to the `typing` import)
- Modify: `dj.py:376` (insert `play_episode` after `group_all`, before the `## wake schedules` section)
- Test: `tests/test_dj_controls.py` (append new tests at end of file)

**Interfaces:**
- Consumes: `DJ._lock`, `DJ.start()`, `DJ._match_queue()`, `DJ._enqueue()`, `DJ._skip()`, `DJ.seek_abs()`, `DJ._NO_SPEAKER` (all existing, unchanged).
- Produces (used by Task 3): `DJ.play_episode(episode_id: int, mode: Literal["next", "now"]) -> str | None` — returns `None` on success or an error string (`"no such episode"`, `DJ._NO_SPEAKER`, or a staging failure message) on failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dj_controls.py`:

```python
def test_play_episode_next_queues_right_after_current(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)  # excluded from rotation -- proves force-play works on any status
    dj.start()
    cur_idx = player.index
    assert dj.play_episode(archived_id, "next") is None
    assert player.queue[cur_idx + 1]["uri"] == "https://cdn/showb/1.mp3"
    assert player.index == cur_idx  # current track undisturbed
    assert db.get_episode(archived_id)["status"] == "queued"


def test_play_episode_now_interrupts_and_recycles_current(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)
    dj.start()
    interrupted = current_episode(db, player)
    assert dj.play_episode(archived_id, "now") is None
    assert player.queue[player.index]["uri"] == "https://cdn/showb/1.mp3"
    after = db.get_episode(interrupted["id"])
    assert after["status"] == "new" and after["resume_seconds"] is None  # same as skip_later
    assert db.get_episode(archived_id)["status"] == "queued"


def test_play_episode_can_replay_already_played_episode(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 2)
    dj.start()
    first = current_episode(db, player)
    dj.skip_done()
    assert db.get_episode(first["id"])["status"] == "played"
    assert dj.play_episode(first["id"], "now") is None
    assert player.queue[player.index]["uri"] == first["audio_url"]
    assert db.get_episode(first["id"])["status"] == "queued"


def test_play_episode_turns_on_air_when_stopped(db, cfg, player, dj) -> None:
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    assert db.kv_get("dj_state") != "playing"
    assert dj.play_episode(target["id"], "now") is None
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING"
    assert player.queue[player.index]["uri"] == "https://cdn/showa/1.mp3"


def test_play_episode_off_air_with_no_other_content(db, cfg, player, dj) -> None:
    """Regression: if start() finds nothing to auto-queue (no news, no
    resume, no other 'new' rotation content), play_episode must still come
    on air using only the explicitly chosen episode -- not bubble up
    start()'s "nothing to play" error."""
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    db.archive_episode(target["id"])  # nothing left for start()'s own rotation
    assert dj.play_episode(target["id"], "now") is None
    assert db.kv_get("dj_state") == "playing"
    assert player.state == "PLAYING"
    assert player.queue[player.index]["uri"] == "https://cdn/showa/1.mp3"


def test_play_episode_now_on_already_current_restarts_in_place(db, cfg, player, dj) -> None:
    """Regression: forcing "now" on the episode that's already current must
    not re-enqueue+skip itself (which would corrupt its DB status back to
    "new" while it's still the uri actually playing on the speaker) -- it
    should just restart from 0:00."""
    make_feed(db, "showa", 1)
    dj.start()
    current = current_episode(db, player)
    player.position = 500
    assert dj.play_episode(current["id"], "now") is None
    assert player.seeks[-1] == 0
    assert db.get_episode(current["id"])["status"] == "queued"
    assert len(player.queue) == 1  # no duplicate inserted


def test_play_episode_updates_last_feed_id_for_rotation(db, cfg, player, dj) -> None:
    make_feed(db, "showa", 5)
    other = make_feed(db, "showb", 1)
    archived_id = db.oldest_new_for_feed(other)["id"]
    db.archive_episode(archived_id)
    dj.start()
    dj.play_episode(archived_id, "next")
    assert db.kv_get("last_feed_id") == str(other)


def test_play_episode_unknown_id_errors(db, cfg, player, dj) -> None:
    assert dj.play_episode(999, "next") == "no such episode"


def test_play_episode_no_speaker_errors(db, cfg) -> None:
    fid = make_feed(db, "showa", 1)
    target = db.oldest_new_for_feed(fid)
    d = DJ(db, cfg, lambda: None)
    assert "speaker" in d.play_episode(target["id"], "next").lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dj_controls.py -k play_episode -v`
Expected: FAIL with `AttributeError: 'DJ' object has no attribute 'play_episode'`.

- [ ] **Step 3: Implement `play_episode`**

In `dj.py:10`, change:

```python
from typing import Callable
```

to:

```python
from typing import Callable, Literal
```

In `dj.py`, insert immediately after `group_all()` (ends at line 376) and before the `# ── wake schedules ──` comment (line 378):

```python
    def play_episode(self, episode_id: int, mode: Literal["next", "now"]) -> str | None:
        """Force-play a specific episode, any status. mode="next" queues it
        right after the current track; mode="now" interrupts immediately.
        Both reuse existing machinery: start() for the on-air bootstrap,
        _enqueue() to stage it, and (mode="now") the same _skip(done=False)
        path the "Skip - later" button uses, so the interrupted episode is
        recycled to the new pool exactly like a manual skip, not silently
        lost."""
        with self._lock:
            episode = self.db.get_episode(episode_id)
            if episode is None:
                return "no such episode"
            if self.db.kv_get("dj_state") != "playing":
                error = self.start()
                if error == self._NO_SPEAKER:
                    return error
                # error may be "Nothing to play" if start()'s own rotation/
                # news/resume bootstrap found nothing -- irrelevant here,
                # we have an explicit episode to play regardless.
                self.db.kv_set("dj_state", "playing")
            player = self.get_player()
            if player is None:
                return self._NO_SPEAKER
            _, matches, cur_idx = self._match_queue(player)
            current = matches[cur_idx] if cur_idx < len(matches) else None
            if mode == "now" and current is not None and current["id"] == episode_id:
                return self.seek_abs(0)  # already playing it -- just restart
            insert_at = cur_idx + 1 if current is not None else 0
            if not self._enqueue(player, episode, insert_at):
                return "Could not queue that episode"
            self.db.kv_set("last_feed_id", str(episode["feed_id"]))
            if mode == "now":
                if current is not None:
                    return self._skip(done=False)
                player.play_from_queue(insert_at)
            return None

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dj_controls.py tests/test_dj.py -v`
Expected: all tests pass (including the pre-existing ones — confirms `play_episode` didn't disturb anything else).

- [ ] **Step 5: Commit**

```bash
git add dj.py tests/test_dj_controls.py
git commit -m "feat: add DJ.play_episode() to force-play a specific episode next or now"
```

---

### Task 3: Web API — episode listing, search, release, and force-play routes

**Files:**
- Modify: `web.py:16` (add a page-size constant near `TIME_RE`)
- Modify: `web.py:18` (add an `_episode_json` module-level helper before `create_app`)
- Modify: `web.py:122` (insert new routes after `feed_action`, before `add_schedule`)
- Test: `tests/test_web.py` (append new tests at end of file)

**Interfaces:**
- Consumes: `db.count_episodes_for_feed`, `db.episodes_for_feed_page`, `db.count_search_episodes`, `db.search_episodes`, `db.release_episode`, `db.release_episodes` (Task 1); `dj.play_episode` (Task 2); existing `result()` and `call_player()` closures in `create_app`.
- Produces (used by Task 4 & 5 frontend work): the six routes below, all returning JSON.

Routes:
- `GET /api/feeds/<int:feed_id>/episodes?page=&q=` → `{"episodes": [...], "page": int, "page_size": int, "total": int}`, or `{"error": "no such feed"}, 404`
- `GET /api/episodes/search?q=&page=` → same shape (empty results if `q` is blank)
- `POST /episodes/<int:episode_id>/release` → `{"ok": bool, "error": str|None}`, 404 if unknown id
- `POST /episodes/release` (body `{"ids": [...]}`) → `{"ok": true, "error": null}` always (invalid entries are silently skipped, matching `release_episodes`' no-op-per-row semantics)
- `POST /episodes/<int:episode_id>/play_next` → `{"ok": bool, "error": str|None}`, 404 if unknown id
- `POST /episodes/<int:episode_id>/play_now` → same shape

Each episode JSON object: `{"id": int, "title": str, "published_at": str, "status": str, "show": str}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -k "episode" -v`
Expected: FAIL with 404s / `AttributeError` — the routes don't exist yet.

- [ ] **Step 3: Implement the routes**

In `web.py:16`, change:

```python
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
logger = logging.getLogger(__name__)
```

to:

```python
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
logger = logging.getLogger(__name__)
EPISODES_PAGE_SIZE = 25


def _episode_json(e, feed_title: str) -> dict:
    return {"id": e["id"], "title": e["title"], "published_at": e["published_at"],
            "status": e["status"], "show": feed_title}


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        return 1
```

(`request` here is the module-level Flask proxy already imported at the top of `web.py` — no need to pass it in.)

In `web.py`, insert immediately after the `feed_action` route (ends at line 122) and before `@app.post("/schedules")` (line 124):

```python
    @app.get("/api/feeds/<int:feed_id>/episodes")
    def api_feed_episodes(feed_id: int):
        feed = db.get_feed(feed_id)
        if feed is None:
            return jsonify({"error": "no such feed"}), 404
        q = (request.args.get("q") or "").strip() or None
        page = _page_arg()
        total = db.count_episodes_for_feed(feed_id, q)
        episodes = db.episodes_for_feed_page(feed_id, page, EPISODES_PAGE_SIZE, q)
        return jsonify({
            "episodes": [_episode_json(e, feed["title"]) for e in episodes],
            "page": page, "page_size": EPISODES_PAGE_SIZE, "total": total,
        })

    @app.get("/api/episodes/search")
    def api_episode_search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"episodes": [], "page": 1,
                            "page_size": EPISODES_PAGE_SIZE, "total": 0})
        page = _page_arg()
        total = db.count_search_episodes(q)
        episodes = db.search_episodes(q, page, EPISODES_PAGE_SIZE)
        return jsonify({
            "episodes": [_episode_json(e, e["feed_title"]) for e in episodes],
            "page": page, "page_size": EPISODES_PAGE_SIZE, "total": total,
        })

    @app.post("/episodes/<int:episode_id>/release")
    def release_episode(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        db.release_episode(episode_id)
        return result()

    @app.post("/episodes/release")
    def release_episodes_bulk():
        body = request.get_json(silent=True) or {}
        try:
            ids = [int(i) for i in (body.get("ids") or [])]
        except (TypeError, ValueError):
            return result("ids must be a list of integers")
        db.release_episodes(ids)
        return result()

    @app.post("/episodes/<int:episode_id>/play_next")
    def play_episode_next(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.play_episode(episode_id, "next"))

    @app.post("/episodes/<int:episode_id>/play_now")
    def play_episode_now(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.play_episode(episode_id, "now"))

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: all tests pass.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (DB + DJ + web layers together).

- [ ] **Step 6: Commit**

```bash
git add web.py tests/test_web.py
git commit -m "feat: add episode listing, search, release, and force-play routes"
```

---

### Task 4: Frontend — shared episode row rendering + global search section

**Files:**
- Modify: `templates/index.html:96` (CSS additions, before `</style>`)
- Modify: `templates/index.html:130` (new "Find an episode" section, before the Stations section)
- Modify: `templates/index.html:166` (new JS state variables, after `let flashTimer = null;`)
- Modify: `templates/index.html:200` (new shared render-helper functions, after `post()`, before the `// ── render ──` comment)
- Modify: `templates/index.html:271` (wire up the global search input + pagination clicks in the existing delegated listeners)

**Interfaces:**
- Consumes: `GET /api/episodes/search`, `POST /episodes/<id>/release`, `POST /episodes/<id>/play_next`, `POST /episodes/<id>/play_now` (Task 3); existing `esc()`, `post()`, `flash()` helpers already in the file.
- Produces (used by Task 5): `episodeRow(e, opts)`, `paginationHTML(page, pageSize, total, kind, key)`, `postEpisodeAction(id, action, btn)` — Task 5's per-station panel reuses all three verbatim.

- [ ] **Step 1: Add CSS**

In `templates/index.html`, insert immediately before the closing `</style>` tag (line 97, right after `#flash.ok { background: var(--amber); color: #171310; }`):

```css
.ep-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.ep-toolbar input[type="search"] { flex: 1 1 200px; }
li.episode-panel { padding: 10px 4px; border-bottom: 1px solid var(--line); }
.pagination { display: flex; gap: 10px; align-items: center; margin-top: 8px; }
```

- [ ] **Step 2: Add the "Find an episode" section markup**

In `templates/index.html`, insert immediately before the Stations `<section>` (currently starts at line 131 with `<section>\n  <h2>Stations</h2>`):

```html
<section id="episode-search">
  <h2>Find an episode</h2>
  <input type="search" id="global-search-input" placeholder="Search all stations by episode or show title" class="grow">
  <ul id="global-search-results"></ul>
  <div id="global-search-pagination"></div>
</section>

```

- [ ] **Step 3: Add JS state**

In `templates/index.html`, right after `let flashTimer = null;` (line 166), insert:

```js
const globalSearch = { q: "", page: 1 };
const debounceTimers = new WeakMap();
function debounce(el, fn, ms = 300) {
  clearTimeout(debounceTimers.get(el));
  debounceTimers.set(el, setTimeout(fn, ms));
}
```

- [ ] **Step 4: Add shared render helpers**

In `templates/index.html`, right after the closing brace of `post()` (line 200, `}`) and before the `// ── render ─────` comment, insert:

```js
// ── shared episode rendering (station panel + global search) ────────
function episodeRow(e, opts) {
  opts = opts || {};
  const checkbox = opts.checkbox
    ? `<input type="checkbox" class="chip" data-episode-check="${e.id}"${opts.selected ? " checked" : ""}>`
    : "";
  return `<li class="row">` +
    checkbox +
    `<span class="tag off">${esc(e.status)}</span>` +
    (opts.showBadge ? `<span class="muted">${esc(e.show)}</span>` : "") +
    `<span class="grow">${esc(e.title)}</span>` +
    `<span class="muted mono">${esc((e.published_at || "").slice(0, 10))}</span>` +
    `<button data-episode="${e.id}/play_next" title="Play after current">Play next</button>` +
    `<button data-episode="${e.id}/play_now" title="Interrupt and play now">Play now</button>` +
    (e.status === "archived"
      ? `<button data-episode="${e.id}/release">Release</button>` : "") +
    `</li>`;
}

function paginationHTML(page, pageSize, total, kind, key) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return `<div class="pagination">` +
    `<button data-page="${kind}:${key}:${page - 1}" ${page <= 1 ? "disabled" : ""}>Prev</button>` +
    `<span class="muted">Page ${page} of ${pages} (${total})</span>` +
    `<button data-page="${kind}:${key}:${page + 1}" ${page >= pages ? "disabled" : ""}>Next</button>` +
    `</div>`;
}

async function postEpisodeAction(id, action, btn) {
  const ok = await post(`/episodes/${id}/${action}`, {}, btn);
  if (ok) {
    if (typeof expandedFeedId !== "undefined" && expandedFeedId != null) renderEpisodePanel(expandedFeedId);
    if (globalSearch.q) renderGlobalSearch();
  }
}

async function renderGlobalSearch() {
  const results = $("global-search-results");
  const pager = $("global-search-pagination");
  if (!globalSearch.q) {
    results.innerHTML = "";
    pager.innerHTML = "";
    return;
  }
  const data = await (await fetch(
    `/api/episodes/search?q=${encodeURIComponent(globalSearch.q)}&page=${globalSearch.page}`
  )).json();
  results.innerHTML = data.episodes.map((e) => episodeRow(e, { showBadge: true })).join("")
    || '<li class="row muted">No matching episodes</li>';
  pager.innerHTML = data.total ? paginationHTML(data.page, data.page_size, data.total, "global", "q") : "";
}
```

Note: `renderEpisodePanel` and `expandedFeedId` are defined in Task 5 — `postEpisodeAction` guards with `typeof expandedFeedId !== "undefined"` so this task's code works and is testable standalone before Task 5 lands. (Task 5 will remove the guard once both pieces coexist — see Task 5 Step 6.)

- [ ] **Step 5: Wire up global search input and pagination clicks**

In `templates/index.html`, the existing delegated click listener (lines 264-271) currently ends with:

```js
  if (btn.dataset.sched) return void post(`/schedules/${btn.dataset.sched}`, {}, btn);
});
```

Change it to:

```js
  if (btn.dataset.sched) return void post(`/schedules/${btn.dataset.sched}`, {}, btn);
  if (btn.dataset.episode) {
    const [id, action] = btn.dataset.episode.split("/");
    return void postEpisodeAction(id, action, btn);
  }
  if (btn.dataset.page) {
    const [kind, key, page] = btn.dataset.page.split(":");
    const p = Number(page);
    if (p < 1) return;
    if (kind === "global") { globalSearch.page = p; renderGlobalSearch(); }
    return;
  }
});

document.addEventListener("input", (ev) => {
  const el = ev.target;
  if (el.id === "global-search-input") {
    debounce(el, () => {
      globalSearch.q = el.value;
      globalSearch.page = 1;
      renderGlobalSearch();
    });
  }
});
```

(Task 5 Step 7 extends this same `input` listener with the per-station search case, and the `data-page` branch with the `"station"` kind.)

- [ ] **Step 6: Manual verification**

Run the app (`.venv/bin/python main.py` or via Docker per the README), open the dashboard, and confirm:
- Typing a query into "Find an episode" shows matching episodes across stations after a short pause, with each result showing its show name.
- Pagination Prev/Next buttons work and disable at the first/last page.
- Clicking "Play next" / "Play now" / "Release" (on an archived result) triggers the corresponding action and the flash message confirms success.

- [ ] **Step 7: Commit**

```bash
git add templates/index.html
git commit -m "feat: add global episode search to the dashboard"
```

---

### Task 5: Frontend — per-station episode panel with keyed rendering and bulk release

**Files:**
- Modify: `templates/index.html` (station list rendering, new panel functions, event wiring)

**Interfaces:**
- Consumes: `GET /api/feeds/<id>/episodes` (Task 3); `episodeRow`, `paginationHTML`, `postEpisodeAction` (Task 4).
- Produces: `expandedFeedId`, `renderEpisodePanel(feedId, panelLi?)`, `episodeState(feedId)`, `renderStations(stations)` — replaces the old inline stations-map rendering in `render()`.

- [ ] **Step 1: Add per-station state**

In `templates/index.html`, right after the `debounce()` function added in Task 4 Step 3, insert:

```js
let expandedFeedId = null;
const stationSearch = {};   // feedId -> { q, page }
const selectMode = {};      // feedId -> bool
const selectedIds = {};     // feedId -> Set<number>

function episodeState(feedId) {
  return stationSearch[feedId] || (stationSearch[feedId] = { q: "", page: 1 });
}
```

- [ ] **Step 2: Add the station-row and panel renderers**

In `templates/index.html`, right after the `paginationHTML()` function added in Task 4 Step 4 (and before `postEpisodeAction`), insert:

```js
function stationRowHTML(f) {
  const c = f.counts;
  return (f.is_news ? '<span class="tag">NEWS</span>' : "") +
    (f.enabled ? "" : '<span class="tag off">PAUSED</span>') +
    `<span class="grow">${esc(f.title)}</span>` +
    `<span class="counts mono">${c.new} new · ${c.played} played · ${c.archived} archived</span>` +
    `<button data-feed="${f.id}/news">${f.is_news ? "Not news" : "Mark news"}</button>` +
    `<button data-feed="${f.id}/toggle">${f.enabled ? "Pause" : "Enable"}</button>` +
    (c.archived ? `<button data-feed="${f.id}/unarchive">Add back catalog</button>` : "") +
    `<button data-episodes-toggle="${f.id}">${expandedFeedId === f.id ? "Hide episodes" : "Episodes"}</button>` +
    `<button class="danger" data-feed="${f.id}/delete" data-confirm="Remove ${esc(f.title)}?">Remove</button>`;
}

async function renderEpisodePanel(feedId, panelLi) {
  panelLi = panelLi || $("stations").querySelector(`li[data-episodes-panel="${feedId}"]`);
  if (!panelLi) return;
  const st = episodeState(feedId);
  const data = await (await fetch(
    `/api/feeds/${feedId}/episodes?page=${st.page}&q=${encodeURIComponent(st.q)}`
  )).json();
  const sel = !!selectMode[feedId];
  const selected = selectedIds[feedId] || (selectedIds[feedId] = new Set());
  panelLi.innerHTML =
    `<div class="ep-toolbar">` +
    `<input type="search" placeholder="Filter this station's episodes" value="${esc(st.q)}" data-station-search="${feedId}">` +
    `<button data-select-toggle="${feedId}">${sel ? "Done selecting" : "Select"}</button>` +
    (sel ? `<button data-release-selected="${feedId}">Release selected (${selected.size})</button>` : "") +
    `</div>` +
    `<ul class="ep-list">` +
    (data.episodes.map((e) => episodeRow(e, {
      checkbox: sel && e.status === "archived", selected: selected.has(e.id),
    })).join("") || '<li class="row muted">No episodes match</li>') +
    `</ul>` +
    paginationHTML(st.page, data.page_size, data.total, "station", feedId);
}

function renderStations(stations) {
  const ul = $("stations");
  if (!stations.length) {
    ul.innerHTML = '<li class="row muted">No stations yet — add an RSS URL below</li>';
    return;
  }
  const seen = new Set();
  let prevEl = null;
  for (const f of stations) {
    seen.add(String(f.id));
    let li = ul.querySelector(`li[data-station="${f.id}"]`);
    if (!li) {
      li = document.createElement("li");
      li.className = "row";
      li.dataset.station = f.id;
    }
    li.innerHTML = stationRowHTML(f);
    if (prevEl) prevEl.after(li); else ul.prepend(li);
    prevEl = li;

    let panelLi = ul.querySelector(`li[data-episodes-panel="${f.id}"]`);
    if (expandedFeedId === f.id) {
      if (!panelLi) {
        panelLi = document.createElement("li");
        panelLi.className = "episode-panel";
        panelLi.dataset.episodesPanel = f.id;
        li.after(panelLi);
        renderEpisodePanel(f.id, panelLi);
      } else {
        li.after(panelLi);  // reposition only -- never rebuild while open, or a live search input loses focus/state
      }
      prevEl = panelLi;
    } else if (panelLi) {
      panelLi.remove();
    }
  }
  for (const li of Array.from(ul.querySelectorAll("li[data-station]"))) {
    if (!seen.has(li.dataset.station)) li.remove();
  }
  for (const li of Array.from(ul.querySelectorAll("li[data-episodes-panel]"))) {
    if (!seen.has(li.dataset.episodesPanel)) li.remove();
  }
}
```

- [ ] **Step 3: Remove the `typeof` guard added in Task 4**

In `templates/index.html`, in `postEpisodeAction` (added in Task 4 Step 4), change:

```js
    if (typeof expandedFeedId !== "undefined" && expandedFeedId != null) renderEpisodePanel(expandedFeedId);
```

to:

```js
    if (expandedFeedId != null) renderEpisodePanel(expandedFeedId);
```

- [ ] **Step 4: Switch `render()` to use the keyed station renderer**

In `templates/index.html`, find this block inside `render()`:

```js
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
```

Replace it with:

```js
  renderStations(s.stations);
```

- [ ] **Step 5: Wire up expand/collapse, select-mode, checkboxes, and bulk release**

In `templates/index.html`, in the delegated click listener extended in Task 4 Step 5, insert new branches right before the final `});` of that listener:

```js
  if (btn.dataset.episodesToggle) {
    const id = Number(btn.dataset.episodesToggle);
    expandedFeedId = expandedFeedId === id ? null : id;
    return void render();
  }
  if (btn.dataset.selectToggle) {
    const id = Number(btn.dataset.selectToggle);
    selectMode[id] = !selectMode[id];
    if (!selectMode[id] && selectedIds[id]) selectedIds[id].clear();
    return void renderEpisodePanel(id);
  }
  if (btn.dataset.releaseSelected) {
    const id = Number(btn.dataset.releaseSelected);
    const ids = Array.from(selectedIds[id] || []);
    if (!ids.length) return;
    return void post("/episodes/release", { ids }, btn).then((ok) => {
      if (ok) { selectedIds[id].clear(); selectMode[id] = false; }
      renderEpisodePanel(id);
    });
  }
```

Also extend the `data-page` branch (added in Task 4 Step 5) to handle the `"station"` kind. Change:

```js
  if (btn.dataset.page) {
    const [kind, key, page] = btn.dataset.page.split(":");
    const p = Number(page);
    if (p < 1) return;
    if (kind === "global") { globalSearch.page = p; renderGlobalSearch(); }
    return;
  }
```

to:

```js
  if (btn.dataset.page) {
    const [kind, key, page] = btn.dataset.page.split(":");
    const p = Number(page);
    if (p < 1) return;
    if (kind === "global") { globalSearch.page = p; renderGlobalSearch(); }
    else if (kind === "station") { episodeState(Number(key)).page = p; renderEpisodePanel(Number(key)); }
    return;
  }
```

Add a `change` listener (for the archived-episode checkboxes) right after the `input` listener added in Task 4 Step 5:

```js
document.addEventListener("change", (ev) => {
  const cb = ev.target.closest("input[data-episode-check]");
  if (!cb) return;
  const panelLi = cb.closest("li[data-episodes-panel]");
  const feedId = Number(panelLi.dataset.episodesPanel);
  const epId = Number(cb.dataset.episodeCheck);
  const set = selectedIds[feedId] || (selectedIds[feedId] = new Set());
  if (cb.checked) set.add(epId); else set.delete(epId);
  const releaseBtn = panelLi.querySelector("[data-release-selected]");
  if (releaseBtn) releaseBtn.textContent = `Release selected (${set.size})`;
});
```

Extend the `input` listener added in Task 4 Step 5 to handle per-station search boxes. Change:

```js
document.addEventListener("input", (ev) => {
  const el = ev.target;
  if (el.id === "global-search-input") {
    debounce(el, () => {
      globalSearch.q = el.value;
      globalSearch.page = 1;
      renderGlobalSearch();
    });
  }
});
```

to:

```js
document.addEventListener("input", (ev) => {
  const el = ev.target;
  if (el.id === "global-search-input") {
    debounce(el, () => {
      globalSearch.q = el.value;
      globalSearch.page = 1;
      renderGlobalSearch();
    });
  } else if (el.dataset.stationSearch) {
    const feedId = Number(el.dataset.stationSearch);
    debounce(el, () => {
      const st = episodeState(feedId);
      st.q = el.value;
      st.page = 1;
      renderEpisodePanel(feedId);
    });
  }
});
```

- [ ] **Step 6: Manual verification**

Run the app, open the dashboard, and confirm:
- Clicking "Episodes" on a station expands a panel under that row listing its episodes, newest first, paginated.
- Typing in that panel's search box filters to matching titles after a short pause.
- **Leave the local search box focused and type a partial query, then wait through at least two 5-second status polls without pressing anything** — confirm the input keeps focus and your typed text is not cleared (this is the regression the keyed-patch rendering exists to prevent).
- Clicking "Release" on an archived episode moves it out of the archived count and it disappears from the archived filter.
- Clicking "Select" shows checkboxes on archived rows; checking a few and clicking "Release selected (N)" releases exactly those and exits select mode.
- Clicking "Play next" on an episode queues it right after the current track (check "Up next"); clicking "Play now" on a different episode interrupts playback immediately and the previously-playing episode reappears with `new` status.
- Collapsing a station (click "Hide episodes") and expanding a different one works, and only one panel is open at a time.

- [ ] **Step 7: Run the full test suite one more time**

Run: `.venv/bin/pytest -v`
Expected: all tests still pass (this task touches only the untested template file, but this confirms Tasks 1-3 remain intact).

- [ ] **Step 8: Commit**

```bash
git add templates/index.html
git commit -m "feat: add per-station episode panel with search, pagination, and bulk release"
```

---

## Self-Review Notes

- **Spec coverage:** pagination + search (Task 1, 3, 4, 5), per-episode and bulk release (Task 1, 3, 5), force-play next/now including off-air bootstrap and any-status replay (Task 2, 3, 5), global search by title or show (Task 3, 4), keyed DOM patch to protect the local search input from the 5s poll (Task 5) — all covered.
- **Additional correctness fix found during planning, not explicitly in the spec but required to satisfy it:** `play_episode` guards against (a) `start()`'s "nothing to play" error when the only content is the explicitly-chosen episode, and (b) forcing "now" on an episode that's already the current one (which would otherwise corrupt its DB status back to `new` while it's still the uri actually playing). Both are covered by dedicated regression tests in Task 2.
- **Type/signature consistency checked:** `play_episode(episode_id: int, mode: Literal["next", "now"]) -> str | None` matches its Task 2 definition, Task 3's route calls, and the spec. `episodeRow(e, opts)` / `paginationHTML(...)` / `postEpisodeAction(...)` signatures are identical between where they're defined (Task 4) and where they're reused (Task 5).
