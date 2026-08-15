# Station Categories & Rotation Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each station carry one genre classification, and let a whole category be switched off from automatic rotation without disabling or deleting its stations.

**Architecture:** A new `categories` table (id, name, rotation_enabled) with `feeds.category_id` pointing to it. `rotation_feeds_with_new()` gains a filter clause so rotation-disabled categories' stations never get picked, while uncategorized stations stay eligible by default. New CRUD routes for categories plus a per-feed category-assignment route; `dj.status()` exposes the categories list and each station's `category_id` so the dashboard can render both a Categories management section and a per-station dropdown.

**Tech Stack:** Python 3.12, Flask, SQLite (stdlib `sqlite3`), pytest, vanilla JS (no build step, no frontend test harness — same as the rest of this app).

## Global Constraints

- One classification per station (not multi-tag) — a single nullable `category_id` column, not a join table.
- Categories are freely editable (add/rename/remove), not a fixed built-in list.
- No categories are pre-seeded in the schema — the 6-category scheme for this deployment's actual subscriptions is a one-time operational data-setup step run after this ships, not part of the codebase.
- Uncategorized feeds (`category_id IS NULL`) stay rotation-eligible by default — opt-out, not opt-in, consistent with how `enabled`/`is_news` already default to "on."
- Deleting a category unassigns its feeds (`ON DELETE SET NULL`) — never deletes the feeds themselves, never blocks the deletion.
- News feeds are already excluded from the rotation query (`is_news=0`) — a category's rotation state is irrelevant to news feeds; no special-casing needed.
- Follow existing code conventions exactly: type hints on all Python, one connection per DB operation via `Database._conn()`, `{"ok": bool, "error": str|None}` JSON shape via the existing `result()` helper for mutation routes, 404 on unknown ids matching `feed_action`/`schedule_action`.
- No frontend test harness exists in this repo — the frontend task is verified manually (Flask test client for markup/route checks, hand-traced JS), not with new automated tests. Do not start a live server in the background for verification — this has caused stuck processes for implementers in this sandbox before; use Flask's test client instead.

---

### Task 1: DB layer — categories table, CRUD, and rotation filtering

**Files:**
- Modify: `db.py:10-48` (SCHEMA string — add `categories` table, add `category_id` column to `feeds`)
- Modify: `db.py:74-80` (`init()` — add migration for existing databases)
- Modify: `db.py:119` (insert new category CRUD methods after `counts_by_feed`, before the `# ── episodes ──` section)
- Modify: `db.py:169-175` (`rotation_feeds_with_new()` — add category filter)
- Test: `tests/test_db.py` (append new tests; add `import pytest` and `import sqlite3`)
- Test: `tests/test_pick.py` (append a regression test)

**Interfaces:**
- Consumes: nothing new — uses the existing `Database._conn()` context manager.
- Produces (used by Task 2): `add_category(name: str) -> int`, `get_category(category_id: int) -> sqlite3.Row | None`, `list_categories() -> list[sqlite3.Row]` (rows include `station_count`), `rename_category(category_id: int, name: str) -> None`, `toggle_category_rotation(category_id: int) -> None`, `delete_category(category_id: int) -> None`, `set_feed_category(feed_id: int, category_id: int | None) -> None`.

- [ ] **Step 1: Write the failing tests**

Add `import pytest` and `import sqlite3` to the top of `tests/test_db.py` (alongside the existing `from db import Database, utcnow_iso`), then append:

```python
def test_category_crud(db: Database) -> None:
    cid = db.add_category("History")
    cat = db.get_category(cid)
    assert cat["name"] == "History" and cat["rotation_enabled"] == 1
    db.rename_category(cid, "Ancient History")
    assert db.get_category(cid)["name"] == "Ancient History"
    db.toggle_category_rotation(cid)
    assert db.get_category(cid)["rotation_enabled"] == 0
    db.toggle_category_rotation(cid)
    assert db.get_category(cid)["rotation_enabled"] == 1


def test_add_category_duplicate_name_raises(db: Database) -> None:
    db.add_category("History")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_category("History")


def test_list_categories_includes_station_count(db: Database) -> None:
    cid = db.add_category("History")
    db.add_category("Comedy")
    fid1 = db.add_feed("https://a/rss", "Show A", None, False)
    fid2 = db.add_feed("https://b/rss", "Show B", None, False)
    db.set_feed_category(fid1, cid)
    db.set_feed_category(fid2, cid)
    counts = {c["name"]: c["station_count"] for c in db.list_categories()}
    assert counts["History"] == 2 and counts["Comedy"] == 0


def test_delete_category_unassigns_feeds_without_deleting_them(db: Database) -> None:
    cid = db.add_category("History")
    fid = db.add_feed("https://a/rss", "Show A", None, False)
    db.set_feed_category(fid, cid)
    db.delete_category(cid)
    feed = db.get_feed(fid)
    assert feed is not None and feed["category_id"] is None


def test_set_feed_category_invalid_id_raises(db: Database) -> None:
    fid = db.add_feed("https://a/rss", "Show A", None, False)
    with pytest.raises(sqlite3.IntegrityError):
        db.set_feed_category(fid, 999)


def test_rotation_feeds_excludes_rotation_disabled_category(db: Database) -> None:
    on_cat = db.add_category("History")
    off_cat = db.add_category("Comedy")
    db.toggle_category_rotation(off_cat)  # now disabled
    on_feed = db.add_feed("https://a/rss", "On Show", None, False)
    off_feed = db.add_feed("https://b/rss", "Off Show", None, False)
    uncategorized_feed = db.add_feed("https://c/rss", "Uncat Show", None, False)
    db.set_feed_category(on_feed, on_cat)
    db.set_feed_category(off_feed, off_cat)
    _ep(db, on_feed, 1)
    _ep(db, off_feed, 1)
    _ep(db, uncategorized_feed, 1)
    ids = {f["id"] for f in db.rotation_feeds_with_new()}
    assert on_feed in ids
    assert uncategorized_feed in ids
    assert off_feed not in ids
```

Append to `tests/test_pick.py`:

```python
def test_pick_next_never_picks_rotation_disabled_category(db: Database) -> None:
    off_cat = db.add_category("Comedy")
    db.toggle_category_rotation(off_cat)
    off_feed = _feed_with_eps(db, "off-show", 3)
    db.set_feed_category(off_feed, off_cat)
    on_feed = _feed_with_eps(db, "on-show", 1)
    for _ in range(10):  # random rotation, so hammer it
        ep = pick_next(db)
        assert ep["feed_id"] == on_feed
        db.mark_queued(ep["id"], ep["audio_url"])
        db.mark_played(ep["id"], "2026-01-01T00:00:00Z")
        db.insert_episode(on_feed, ep["guid"] + "-again", ep["title"],
                          ep["audio_url"], "2026-01-02T00:00:00Z")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -k category -v tests/test_pick.py -k rotation_disabled -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'add_category'` (and similar for the other new methods).

- [ ] **Step 3: Update the schema**

In `db.py`, replace the `SCHEMA` string (lines 10-48):

```python
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
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
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
```

- [ ] **Step 4: Add the migration for existing databases**

In `db.py`, change `init()` (lines 74-80) from:

```python
    def init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            # migration for databases created before duration_seconds existed
            cols = {r["name"] for r in c.execute("PRAGMA table_info(episodes)")}
            if "duration_seconds" not in cols:
                c.execute("ALTER TABLE episodes ADD COLUMN duration_seconds INTEGER")
```

to:

```python
    def init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            # migration for databases created before duration_seconds existed
            cols = {r["name"] for r in c.execute("PRAGMA table_info(episodes)")}
            if "duration_seconds" not in cols:
                c.execute("ALTER TABLE episodes ADD COLUMN duration_seconds INTEGER")
            # migration for databases created before categories existed
            feed_cols = {r["name"] for r in c.execute("PRAGMA table_info(feeds)")}
            if "category_id" not in feed_cols:
                c.execute(
                    "ALTER TABLE feeds ADD COLUMN category_id INTEGER"
                    " REFERENCES categories(id) ON DELETE SET NULL"
                )
```

- [ ] **Step 5: Add the category CRUD methods**

In `db.py`, insert immediately after `counts_by_feed` (ends at line 119) and before the `# ── episodes ──` comment (line 121):

```python

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
```

- [ ] **Step 6: Add the rotation filter**

In `db.py`, change `rotation_feeds_with_new` (lines 169-175) from:

```python
    def rotation_feeds_with_new(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT f.* FROM feeds f WHERE f.enabled=1 AND f.is_news=0 AND EXISTS"
                " (SELECT 1 FROM episodes e WHERE e.feed_id=f.id AND e.status='new')"
                " ORDER BY f.id",
            ).fetchall()
```

to:

```python
    def rotation_feeds_with_new(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT f.* FROM feeds f LEFT JOIN categories cat ON cat.id=f.category_id"
                " WHERE f.enabled=1 AND f.is_news=0"
                " AND (f.category_id IS NULL OR cat.rotation_enabled=1) AND EXISTS"
                " (SELECT 1 FROM episodes e WHERE e.feed_id=f.id AND e.status='new')"
                " ORDER BY f.id",
            ).fetchall()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py tests/test_pick.py -v`
Expected: all tests pass, including the new ones.

- [ ] **Step 8: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (confirms the schema change and `rotation_feeds_with_new` change didn't disturb anything else — `pick_next`, `refresh_all`, and every existing rotation test still work unchanged since `category_id` defaults to `NULL` and uncategorized feeds stay eligible).

- [ ] **Step 9: Commit**

```bash
git add db.py tests/test_db.py tests/test_pick.py
git commit -m "feat: add categories table, CRUD, and rotation filtering by category"
```

---

### Task 2: API layer — status payload + category routes

**Files:**
- Modify: `dj.py:454-463` (`status()` — add `category_id` to each station entry, add a `categories` list)
- Modify: `web.py:156` (insert category routes after `feed_action`, before `api_feed_episodes`)
- Test: `tests/test_web.py` (append new tests)

**Interfaces:**
- Consumes: `db.list_categories()`, `db.get_category()`, `db.add_category()`, `db.rename_category()`, `db.toggle_category_rotation()`, `db.delete_category()`, `db.set_feed_category()` (Task 1).
- Produces (used by Task 3): `dj.status()`'s payload gains `"categories": [{"id", "name", "rotation_enabled", "station_count"}, ...]` and each entry in `"stations"` gains `"category_id"`. New routes:

```
POST /categories                    body: {name}          → create
POST /categories/<id>/toggle                               → flip rotation_enabled
POST /categories/<id>/rename        body: {name}           → rename
POST /categories/<id>/delete                                → delete (feeds unassigned, not removed)
POST /feeds/<id>/category           body: {category_id}    → assign/change/clear (null clears)
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -k categor -v`
Expected: FAIL — routes don't exist yet (404s / `KeyError: 'categories'`).

- [ ] **Step 3: Update `dj.status()`**

In `dj.py`, change the `"stations"` entry (lines 454-463) from:

```python
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
```

to:

```python
            "stations": [
                {
                    "id": f["id"], "title": f["title"], "url": f["url"],
                    "image_url": f["image_url"],
                    "is_news": bool(f["is_news"]), "enabled": bool(f["enabled"]),
                    "category_id": f["category_id"],
                    "counts": {s: counts.get(f["id"], {}).get(s, 0)
                               for s in ("new", "queued", "played", "skipped", "archived")},
                }
                for f in self.db.list_feeds()
            ],
            "categories": [
                {"id": cat["id"], "name": cat["name"],
                 "rotation_enabled": bool(cat["rotation_enabled"]),
                 "station_count": cat["station_count"]}
                for cat in self.db.list_categories()
            ],
```

- [ ] **Step 4: Add the routes**

In `web.py`, insert immediately after `feed_action` (ends at line 156) and before `@app.get("/api/feeds/<int:feed_id>/episodes")` (line 158):

```python
    @app.post("/categories")
    def add_category():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return result("name required")
        try:
            db.add_category(name)
        except Exception as exc:
            return result(f"could not add category: {exc}")
        return result()

    @app.post("/categories/<int:category_id>/<action>")
    def category_action(category_id: int, action: str):
        category = db.get_category(category_id)
        if category is None:
            return result("no such category"), 404
        if action == "toggle":
            db.toggle_category_rotation(category_id)
        elif action == "delete":
            db.delete_category(category_id)
        elif action == "rename":
            body = request.get_json(silent=True) or {}
            name = (body.get("name") or "").strip()
            if not name:
                return result("name required")
            try:
                db.rename_category(category_id, name)
            except Exception as exc:
                return result(f"could not rename category: {exc}")
        else:
            return result(f"unknown action: {action}"), 404
        return result()

    @app.post("/feeds/<int:feed_id>/category")
    def set_feed_category(feed_id: int):
        if db.get_feed(feed_id) is None:
            return result("no such feed"), 404
        body = request.get_json(silent=True) or {}
        raw = body.get("category_id")
        try:
            category_id = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return result("category_id must be an integer or null")
        try:
            db.set_feed_category(feed_id, category_id)
        except Exception as exc:
            return result(f"could not set category: {exc}")
        return result()

```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: all tests pass, including the new ones.

- [ ] **Step 6: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (backend layers together).

- [ ] **Step 7: Commit**

```bash
git add dj.py web.py tests/test_web.py
git commit -m "feat: expose categories via status payload and add category routes"
```

---

### Task 3: Frontend — Categories section and per-station dropdown

**Files:**
- Modify: `templates/index.html:255` (new "Categories" section markup, after the add-feed form, before Wake schedule)
- Modify: `templates/index.html:366-379` (`stationRowHTML` — add the per-station category `<select>`)
- Modify: `templates/index.html:379` (new `categoryRowHTML` helper, right after `stationRowHTML`)
- Modify: `templates/index.html:525` (`render()` — render the Categories list)
- Modify: `templates/index.html:700` (click listener — category toggle/delete/rename)
- Modify: `templates/index.html:723-733` (change listener — per-station category `<select>`)
- Modify: `templates/index.html:779` (add-category form submit handler)

**Interfaces:**
- Consumes: `GET /api/status`'s new `categories` list and each station's `category_id` (Task 2); `POST /categories`, `POST /categories/<id>/{toggle,rename,delete}`, `POST /feeds/<id>/category` (Task 2); existing `esc()`, `post()`, `flash()`, `$()` helpers (all already in the file, unchanged).
- Produces: nothing consumed by later tasks — this is the last piece of this feature.

- [ ] **Step 1: Add the Categories section markup**

In `templates/index.html`, find this block (currently ending at line 255, right after the add-feed form's closing `</form>`):

```html
</form>

<div class="eyebrow"><span>Wake schedule</span><span id="next-start"></span></div>
```

Insert a new section between them, so it reads:

```html
</form>

<div class="eyebrow"><span>Categories</span></div>
<div class="svc" id="categories"></div>
<form class="inline" id="add-category">
  <input type="text" id="category-name" placeholder="New category name" required>
  <button type="submit" class="tab">Add category</button>
</form>

<div class="eyebrow"><span>Wake schedule</span><span id="next-start"></span></div>
```

- [ ] **Step 2: Add the per-station category dropdown and a category row renderer**

In `templates/index.html`, change `stationRowHTML` (currently):

```js
function stationRowHTML(f) {
  const c = f.counts;
  const unplayed = c.new + c.queued;
  return (f.is_news ? '<span class="pri">NEWS</span>' : "") +
    `<span class="glow">${esc(f.title)}</span>` +
    (f.enabled ? "" : '<span class="m">[HELD]</span>') +
    `<span class="m">${unplayed} unplayed &middot; ${c.played} played &middot; ${c.archived} archived</span>` +
    `<span class="spacer"></span>` +
    `<button type="button" class="tab small" data-feed="${f.id}/news">${f.is_news ? "Not news" : "Mark news"}</button>` +
    `<button type="button" class="tab small" data-feed="${f.id}/toggle">${f.enabled ? "Pause" : "Enable"}</button>` +
    (c.archived ? `<button type="button" class="tab small" data-feed="${f.id}/unarchive">Add back catalog</button>` : "") +
    `<button type="button" class="tab small" data-episodes-toggle="${f.id}">${expandedFeedId === f.id ? "Hide episodes" : "Episodes"}</button>` +
    `<button type="button" class="tab small red" data-feed="${f.id}/delete" data-confirm="Remove ${esc(f.title)}?">Remove</button>`;
}
```

to:

```js
function stationRowHTML(f) {
  const c = f.counts;
  const unplayed = c.new + c.queued;
  const categories = (status && status.categories) || [];
  const categoryOptions = `<option value=""${f.category_id == null ? " selected" : ""}>Uncategorized</option>` +
    categories.map((cat) =>
      `<option value="${cat.id}"${f.category_id === cat.id ? " selected" : ""}>${esc(cat.name)}</option>`
    ).join("");
  return (f.is_news ? '<span class="pri">NEWS</span>' : "") +
    `<span class="glow">${esc(f.title)}</span>` +
    (f.enabled ? "" : '<span class="m">[HELD]</span>') +
    `<span class="m">${unplayed} unplayed &middot; ${c.played} played &middot; ${c.archived} archived</span>` +
    `<select data-feed-category="${f.id}">${categoryOptions}</select>` +
    `<span class="spacer"></span>` +
    `<button type="button" class="tab small" data-feed="${f.id}/news">${f.is_news ? "Not news" : "Mark news"}</button>` +
    `<button type="button" class="tab small" data-feed="${f.id}/toggle">${f.enabled ? "Pause" : "Enable"}</button>` +
    (c.archived ? `<button type="button" class="tab small" data-feed="${f.id}/unarchive">Add back catalog</button>` : "") +
    `<button type="button" class="tab small" data-episodes-toggle="${f.id}">${expandedFeedId === f.id ? "Hide episodes" : "Episodes"}</button>` +
    `<button type="button" class="tab small red" data-feed="${f.id}/delete" data-confirm="Remove ${esc(f.title)}?">Remove</button>`;
}

function categoryRowHTML(c) {
  return `<span class="glow">${esc(c.name)}</span>` +
    `<span class="m">${c.station_count} station${c.station_count === 1 ? "" : "s"}</span>` +
    `<span class="spacer"></span>` +
    `<button type="button" class="tab small" data-category="${c.id}/toggle">${c.rotation_enabled ? "On" : "Off"}</button>` +
    `<button type="button" class="tab small" data-category-rename="${c.id}">Rename</button>` +
    `<button type="button" class="tab small red" data-category="${c.id}/delete" data-confirm="Remove ${esc(c.name)}? ${c.station_count} station(s) will become uncategorized.">Remove</button>`;
}
```

Note: `category_id` from the server is a JSON integer or `null`; `<option value="${cat.id}">` compares correctly against `f.category_id === cat.id` (both numbers) — `f.category_id == null` (loose equality) correctly matches both `null` and `undefined`.

- [ ] **Step 3: Render the Categories list**

In `templates/index.html`, inside `render()`, change:

```js
  // stations
  renderStations(s.stations);

  // schedules
```

to:

```js
  // stations
  renderStations(s.stations);

  // categories
  $("categories").innerHTML = (s.categories || []).map((c) =>
    `<div class="svc-row">${categoryRowHTML(c)}</div>`
  ).join("") || '<div class="empty">NO CATEGORIES YET</div>';

  // schedules
```

- [ ] **Step 4: Wire up category toggle/rename/delete clicks**

In `templates/index.html`, in the delegated click listener, find:

```js
  if (btn.id === "podcast-picked-change") {
    pickedPodcast = null;
    $("feed-url").value = "";
    $("podcast-picked-block").style.display = "none";
    $("podcast-search-block").style.display = "";
    return;
  }
});
```

Change it to:

```js
  if (btn.id === "podcast-picked-change") {
    pickedPodcast = null;
    $("feed-url").value = "";
    $("podcast-picked-block").style.display = "none";
    $("podcast-search-block").style.display = "";
    return;
  }
  if (btn.dataset.category) return void post(`/categories/${btn.dataset.category}`, {}, btn);
  if (btn.dataset.categoryRename) {
    const id = btn.dataset.categoryRename;
    const cat = (status.categories || []).find((x) => String(x.id) === id);
    const name = prompt("Rename category", cat ? cat.name : "");
    if (name && name.trim()) post(`/categories/${id}/rename`, { name: name.trim() }, btn);
    return;
  }
});
```

(The existing `if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;` check at the top of this listener already covers the Remove button's confirmation — no separate handling needed for that.)

- [ ] **Step 5: Wire up the per-station category dropdown**

In `templates/index.html`, change the `change` listener from:

```js
document.addEventListener("change", (ev) => {
  const cb = ev.target.closest("input[data-episode-check]");
  if (!cb) return;
  const panel = cb.closest("[data-episodes-panel]");
  const feedId = Number(panel.dataset.episodesPanel);
  const epId = Number(cb.dataset.episodeCheck);
  const set = selectedIds[feedId] || (selectedIds[feedId] = new Set());
  if (cb.checked) set.add(epId); else set.delete(epId);
  const releaseBtn = panel.querySelector("[data-release-selected]");
  if (releaseBtn) releaseBtn.textContent = `Release selected (${set.size})`;
});
```

to:

```js
document.addEventListener("change", (ev) => {
  const cb = ev.target.closest("input[data-episode-check]");
  if (cb) {
    const panel = cb.closest("[data-episodes-panel]");
    const feedId = Number(panel.dataset.episodesPanel);
    const epId = Number(cb.dataset.episodeCheck);
    const set = selectedIds[feedId] || (selectedIds[feedId] = new Set());
    if (cb.checked) set.add(epId); else set.delete(epId);
    const releaseBtn = panel.querySelector("[data-release-selected]");
    if (releaseBtn) releaseBtn.textContent = `Release selected (${set.size})`;
    return;
  }
  const sel = ev.target.closest("select[data-feed-category]");
  if (sel) {
    post(`/feeds/${sel.dataset.feedCategory}/category`, { category_id: sel.value || null });
  }
});
```

- [ ] **Step 6: Wire up the add-category form**

In `templates/index.html`, find the `$("add-feed")` submit handler's closing `});` (currently followed by the `dayChips` block):

```js
  if (ok) {
    $("feed-url").value = "";
    pickedPodcast = null;
    $("podcast-search-input").value = "";
    $("podcast-search-results").innerHTML = "";
    setAddMode(addMode);
    flash("Station added", true);
  }
});

const dayChips = DAYS.map((d, i) => {
```

Insert a new handler between them:

```js
  if (ok) {
    $("feed-url").value = "";
    pickedPodcast = null;
    $("podcast-search-input").value = "";
    $("podcast-search-results").innerHTML = "";
    setAddMode(addMode);
    flash("Station added", true);
  }
});

$("add-category").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = await post("/categories", { name: $("category-name").value });
  if (ok) { $("category-name").value = ""; flash("Category added", true); }
});

const dayChips = DAYS.map((d, i) => {
```

- [ ] **Step 7: Verify with Flask's test client**

Run this to confirm the markup and wiring are structurally correct (no live server, no background process):

```bash
.venv/bin/python -c "
from config import Config
from db import Database
from dj import DJ
from web import create_app

cfg = Config.from_env({'DATA_DIR': '/tmp/verify-station-categories'})
db = Database(cfg.db_path)
db.init()
dj = DJ(db, cfg, lambda: None)
app = create_app(db, dj, cfg)
app.config['TESTING'] = True
client = app.test_client()

resp = client.get('/')
html = resp.get_data(as_text=True)
for marker in [
    'id=\"categories\"', 'id=\"add-category\"', 'id=\"category-name\"',
    'function categoryRowHTML(', 'data-feed-category', 'data-category-rename',
    'data-category=',
]:
    assert marker in html, f'MISSING: {marker}'
    print('OK:', marker)

fid = db.add_feed('https://x/rss', 'X Show', None, False)
cid = db.add_category('History')
r = client.post(f'/feeds/{fid}/category', json={'category_id': cid})
assert r.get_json()['ok'] is True
status_data = client.get('/api/status').get_json()
assert status_data['categories'][0]['name'] == 'History'
station = next(s for s in status_data['stations'] if s['id'] == fid)
assert station['category_id'] == cid
print('OK: end-to-end category assignment reflected in status')
"
```

Expected: all `OK:` lines print, no assertion errors. Since `render_template` serves the file's raw text (nothing executes server-side), the JS template-literal source itself — including `data-category="${...}"` — is present verbatim in the HTTP response body, so a plain substring check against it is valid.

Also hand-trace: confirm `data-feed-category`, `data-category`, and `data-category-rename` are each read back correctly (`sel.dataset.feedCategory`, `btn.dataset.category`, `btn.dataset.categoryRename`) in the listeners from Steps 4-5.

- [ ] **Step 8: Run the full pytest suite one more time**

Run: `.venv/bin/pytest -q`
Expected: all tests still pass (this task touches only the untested template file — confirms Tasks 1-2 remain intact).

- [ ] **Step 9: Commit**

```bash
git add templates/index.html
git commit -m "feat: add Categories section and per-station category dropdown"
```

---

## Rollout (not part of this plan's tasks — a post-deploy data step)

After this ships, run a one-time script against the live server creating the 6 categories (History, Philosophy & Literature, AI & Tech, Comedy & Trivia, Business & Law, Science & Curiosity) and assigning every current station per the classification worked out during brainstorming — the same kind of operational step as the earlier OPML import and podcast-recommendation additions, not a code feature.

## Self-Review Notes

- **Spec coverage:** categories table + CRUD + rotation filtering (Task 1), status payload exposure + category routes (Task 2), Categories UI section + per-station dropdown (Task 3), error handling for duplicate names/unknown ids/invalid category assignment (Task 1's DB-level IntegrityError + Task 2's route-level catches), non-destructive category deletion (Task 1's `ON DELETE SET NULL` + regression test) — all covered.
- **Placeholder scan:** none found — every step has complete code.
- **Type/signature consistency:** `add_category(name: str) -> int`, `get_category(category_id: int) -> sqlite3.Row | None`, `list_categories() -> list[sqlite3.Row]`, `rename_category`, `toggle_category_rotation`, `delete_category`, `set_feed_category(feed_id: int, category_id: int | None) -> None` all match between Task 1's definitions, Task 2's route call sites, and the test files. The `categories` JSON shape (`id`, `name`, `rotation_enabled`, `station_count`) matches between Task 2's `status()` output and Task 3's `categoryRowHTML`/dropdown consumption.
