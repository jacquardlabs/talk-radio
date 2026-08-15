# Per-Show Playback Mode Implementation Plan

**Goal:** Make the within-show pick per-show configurable — `in_order` (today's oldest-unplayed-first) or `random` (any unplayed episode, arc-guarded so a multi-part story never starts mid-arc).

**Architecture:** A `playback_mode` column on `feeds` branches `pick_next()`'s within-show step. Arc detection is a pure title function `arc_key()` living in `feeds.py` (title normalization is boundary work, and `dj.py` already imports `feeds` — the reverse import would be circular). New feeds auto-classify at ingest; a `playback` action on the existing feed dispatcher and one state-labeled button in the station sheet complete the surface.

**Tech Stack:** Python 3.12, Flask, SQLite (stdlib `sqlite3`), pytest, vanilla JS (no build step, no frontend test harness).

## Global Constraints

- Two modes exactly: `'in_order'` and `'random'`, enforced by a CHECK constraint. Default `'in_order'` — deploying is a zero-behavior-change event.
- Show selection in `pick_next()` (random feed, no back-to-back repeat, category/enabled/news filters) is UNTOUCHED — only the within-show step branches.
- `arc_key()` returns `None` when no part marker is detected; grouping only happens between non-None equal keys, within one feed, among unplayed episodes. Leading episode numbering ("437. ", "Show 66 - ") is show-level info: stripped, but never counts as a marker.
- Subtitle truncation uses `:`, `–` (en dash), `—` (em dash) only — never plain hyphen (too common inside titles).
- On-add auto-classification: `itunes:type == serial` OR ≥50% of ingested titles carrying a leading number or part marker → `in_order`, else `random`. Empty feeds → `in_order`. Errors land on the safe side (in_order = pre-feature behavior).
- No categories/rollout data in the schema — assigning modes to the existing 55 stations is a post-deploy operational step (like the category rollout), not part of these tasks.
- Follow existing conventions exactly: type hints, one connection per DB op via `_conn()`, `result()` JSON shape, 404-on-unknown-id matching `feed_action`.
- No frontend test harness — Task 5 verifies via Flask test client + hand-tracing. Do not start a live server in the background (has hung this sandbox before).

---

### Task 1: DB layer — playback_mode column, toggle, unplayed-episodes query

**Files:**
- Modify: `db.py:16-25` (SCHEMA — feeds table gains the column)
- Modify: `db.py:80-95` (`init()` — migration for existing databases; reuse the `feed_cols` set already computed there)
- Modify: `db.py:96-102` (`add_feed` — optional `playback_mode` parameter)
- Modify: `db.py:116-118` (insert `toggle_feed_playback` after `toggle_feed`)
- Modify: `db.py:211-217` (insert `new_episodes_for_feed` after `oldest_new_for_feed`)
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Produces (Task 2 consumes): `new_episodes_for_feed(feed_id: int) -> list[sqlite3.Row]` — all `status='new'` episodes for the feed, ordered `published_at ASC, id ASC` (oldest first). Feed rows now carry `playback_mode`.
- Produces (Task 3 consumes): `toggle_feed_playback(feed_id: int) -> None`.
- Produces (Task 4 consumes): `add_feed(url, title, image_url, is_news, playback_mode: str = "in_order") -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py` (the file already imports `pytest` and `sqlite3` from the categories work — verify, add if missing):

```python
def test_playback_mode_default_and_toggle(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    assert db.get_feed(fid)["playback_mode"] == "in_order"
    db.toggle_feed_playback(fid)
    assert db.get_feed(fid)["playback_mode"] == "random"
    db.toggle_feed_playback(fid)
    assert db.get_feed(fid)["playback_mode"] == "in_order"

def test_add_feed_with_playback_mode(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False, playback_mode="random")
    assert db.get_feed(fid)["playback_mode"] == "random"

def test_playback_mode_check_constraint(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.add_feed("https://ex.com/rss", "Show", None, False, playback_mode="chaos")

def test_new_episodes_for_feed_ordering_and_filter(db: Database) -> None:
    fid = db.add_feed("https://ex.com/rss", "Show", None, False)
    _ep(db, fid, 3)
    _ep(db, fid, 1)
    played = _ep(db, fid, 2)
    db.mark_queued(played, "u")
    db.mark_played(played, utcnow_iso())
    rows = db.new_episodes_for_feed(fid)
    assert [r["guid"] for r in rows] == [f"guid-{fid}-1", f"guid-{fid}-3"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -k playback_mode -v`
Expected: FAIL with `sqlite3.OperationalError: no such column` / `AttributeError: 'Database' object has no attribute 'toggle_feed_playback'`.

- [ ] **Step 3: Schema + migration**

In `db.py`'s SCHEMA string, change the feeds table to:

```sql
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
        CHECK (playback_mode IN ('in_order','random'))
);
```

In `init()`, extend the existing migration block (the `feed_cols` set is already computed for the `category_id` migration — add after it):

```python
            if "playback_mode" not in feed_cols:
                c.execute(
                    "ALTER TABLE feeds ADD COLUMN playback_mode TEXT NOT NULL"
                    " DEFAULT 'in_order' CHECK (playback_mode IN ('in_order','random'))"
                )
```

- [ ] **Step 4: Methods**

Change `add_feed` to:

```python
    def add_feed(self, url: str, title: str, image_url: str | None, is_news: bool,
                 playback_mode: str = "in_order") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO feeds (url, title, image_url, is_news, added_at, playback_mode)"
                " VALUES (?,?,?,?,?,?)",
                (url, title, image_url, int(is_news), utcnow_iso(), playback_mode),
            )
            return int(cur.lastrowid)
```

Insert after `toggle_feed`:

```python
    def toggle_feed_playback(self, feed_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE feeds SET playback_mode = CASE playback_mode"
                " WHEN 'in_order' THEN 'random' ELSE 'in_order' END WHERE id=?",
                (feed_id,),
            )
```

Insert after `oldest_new_for_feed`:

```python
    def new_episodes_for_feed(self, feed_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM episodes WHERE feed_id=? AND status='new'"
                " ORDER BY published_at ASC, id ASC",
                (feed_id,),
            ).fetchall()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: all pass.

- [ ] **Step 6: Full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (default `'in_order'` means nothing else changes).

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: add per-feed playback_mode with toggle and unplayed-episodes query"
```

---

### Task 2: Title analysis + engine — arc_key() and the pick_next() branch

**Files:**
- Modify: `feeds.py:16` (add `arc_key` + its regexes after the `INCLUDE_MODES` constant, before `class FeedError`)
- Modify: `dj.py:20-31` (`pick_next` branches; new `_random_pick` helper below it)
- Test: Create `tests/test_arc_key.py`
- Test: `tests/test_pick.py` (append; add `import dj as dj_mod` and `import random` to its imports)

**Interfaces:**
- Consumes: `db.new_episodes_for_feed(feed_id)` and feed rows' `playback_mode` (Task 1).
- Produces (Task 4 consumes): `feeds.arc_key(title: str) -> str | None` and `feeds._LEADING_NUM_RE` (module-level regex, reused by title classification).

- [ ] **Step 1: Write the failing arc_key tests**

Create `tests/test_arc_key.py`:

```python
import pytest

from feeds import arc_key

@pytest.mark.parametrize("a, b", [
    # Hardcore History style: trailing roman numerals
    ("Show 66 - Supernova in the East V", "Show 67 - Supernova in the East VI"),
    # Behind the Bastards style: word-number prefix
    ("Part One: The Bastard Who Invented Ayn Rand",
     "Part Two: The Bastard Who Invented Ayn Rand"),
    # Rest Is History style: leading number + per-part subtitle + (Part N)
    ("437. The Fall of the Aztecs: Spaniards on the March (Part 1)",
     "440. The Fall of the Aztecs: The Great Escape (Part 4)"),
    # (N/M) style
    ("The Siege of Malta (1/3)", "The Siege of Malta (3/3)"),
    # plain Part N suffix
    ("History of Rome Part 3", "History of Rome Part 7"),
    # Pt. N style
    ("Dracula Pt. 2", "Dracula Pt. 3"),
])
def test_same_arc(a: str, b: str) -> None:
    assert arc_key(a) is not None
    assert arc_key(a) == arc_key(b)

@pytest.mark.parametrize("title", [
    "10.91- The End",              # leading number only — show numbering, not an arc
    "HoP 442 - Scholastic Metaphysics",  # embedded numbering, no marker
    "The Mothman Prophecies",      # plain title
    "437. A Plain Episode",        # leading number only
    "Part 2",                      # marker with empty remainder
    "",
])
def test_standalone(title: str) -> None:
    assert arc_key(title) is None

def test_distinct_arcs_do_not_collide() -> None:
    assert arc_key("The Fall of Rome (Part 1)") != arc_key("The Fall of Carthage (Part 1)")

def test_benign_roman_grouping_is_by_design() -> None:
    # "…World War II" keys with "…World War III" — a false group that merely
    # constrains those episodes to chronological order. Documented tradeoff.
    assert arc_key("The History of World War II") == arc_key("The History of World War III")

def test_lowercase_word_endings_are_not_romans() -> None:
    assert arc_key("Songs to remix") is None
    assert arc_key("What comes after xi") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_arc_key.py -v`
Expected: FAIL with `ImportError: cannot import name 'arc_key' from 'feeds'`.

- [ ] **Step 3: Implement arc_key in feeds.py**

Insert after `INCLUDE_MODES = (...)` (line 16) and before `class FeedError`:

```python
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

def arc_key(title: str) -> str | None:
    """Grouping key for multi-part arcs, or None when the title carries no
    part marker (the episode stands alone). Grouping is only ever done
    between equal non-None keys within one feed's unplayed episodes."""
    text = _LEADING_NUM_RE.sub("", title or "")
    found = False
    for pat in _PART_MARKER_RES:
        if pat.search(text):
            found = True
            text = pat.sub(" ", text)
    if _TRAILING_ROMAN_RE.search(text):
        found = True
        text = _TRAILING_ROMAN_RE.sub("", text)
    if not found:
        return None
    # a marker stripped from a prefix ("Part One: X") can leave its
    # separator behind — drop it before subtitle truncation
    text = text.lstrip(" \t:–—-.")
    # per-part subtitles ("The Fall of the Aztecs: The Great Escape")
    # truncate at the first real separator; plain hyphens are too common
    # inside titles to count
    positions = [i for i in (text.find(s) for s in (":", "–", "—")) if i > 0]
    if positions:
        text = text[:min(positions)]
    key = re.sub(r"\s+", " ", text).strip(" \t-–—:.,!?'\"()").lower()
    return key or None
```

(`import re` is not currently in `feeds.py` — add it to the stdlib import block at the top.)

- [ ] **Step 4: Run arc_key tests**

Run: `.venv/bin/pytest tests/test_arc_key.py -v`
Expected: all pass. If any parametrized case fails, fix the regexes — the test cases are the contract, taken from the real catalog.

- [ ] **Step 5: Write the failing pick_next tests**

Append to `tests/test_pick.py` (add `import dj as dj_mod` and `import random` to the imports):

```python
def test_random_mode_can_pick_nonchronologically(db: Database, monkeypatch) -> None:
    fid = _feed_with_eps(db, "epi", 6)
    db.toggle_feed_playback(fid)
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])
    assert pick_next(db)["guid"] == "g-epi-6"  # newest, not oldest

def test_in_order_mode_ignores_randomness(db: Database, monkeypatch) -> None:
    fid = _feed_with_eps(db, "serial", 5)  # default in_order
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])
    assert pick_next(db)["guid"] == "g-serial-1"  # still oldest

def _saga_feed(db: Database) -> int:
    fid = db.add_feed("https://saga/rss", "saga", None, False)
    titles = ["Intro", "The Long War (Part 1)", "The Long War (Part 2)",
              "The Long War (Part 3)"]
    for i, t in enumerate(titles, 1):
        db.insert_episode(fid, f"g-saga-{i}", t, f"https://cdn/saga/{i}.mp3",
                          f"2026-01-{i:02d}T00:00:00Z")
    return fid

def test_arc_guard_redirects_midarc_draw(db: Database, monkeypatch) -> None:
    fid = _saga_feed(db)
    db.toggle_feed_playback(fid)
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])  # draw Part 3
    assert pick_next(db)["title"] == "The Long War (Part 1)"

def test_arc_guard_resumes_partial_arc(db: Database, monkeypatch) -> None:
    fid = _saga_feed(db)
    db.toggle_feed_playback(fid)
    part1 = next(e for e in db.episodes_for_feed(fid)
                 if e["title"] == "The Long War (Part 1)")
    db.mark_queued(part1["id"], "u")
    db.mark_played(part1["id"], "2026-02-01T00:00:00Z")
    monkeypatch.setattr(dj_mod.random, "choice", lambda seq: seq[-1])  # draw Part 3
    assert pick_next(db)["title"] == "The Long War (Part 2)"  # not a replay of 1

def test_random_mode_seeded_draws_vary(db: Database) -> None:
    fid = _feed_with_eps(db, "epi", 30)
    db.toggle_feed_playback(fid)
    random.seed(42)
    picks = []
    for _ in range(10):
        ep = pick_next(db)
        picks.append(ep["guid"])
        db.mark_queued(ep["id"], ep["audio_url"])  # consume
    assert picks != [f"g-epi-{i}" for i in range(1, 11)]  # not chronological
    assert len(set(picks)) == 10  # consumed picks never repeat
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/pytest tests/test_pick.py -k "random_mode or arc_guard or in_order_mode" -v`
Expected: FAIL — `pick_next` ignores `playback_mode` (random-mode tests get the oldest episode).

- [ ] **Step 7: Implement the branch in dj.py**

Replace `pick_next` (dj.py:20-31) with:

```python
def pick_next(db: Database) -> sqlite3.Row | None:
    """Random enabled non-news feed with unplayed episodes, avoiding
    last_feed_id when an alternative exists. Within the show: the oldest
    new episode for in_order feeds; a random unplayed episode for random
    feeds, arc-guarded so a multi-part story never starts mid-arc."""
    candidates = db.rotation_feeds_with_new()
    if not candidates:
        return None
    last = db.kv_get("last_feed_id")
    others = [f for f in candidates if str(f["id"]) != last]
    feed = random.choice(others or candidates)
    db.kv_set("last_feed_id", str(feed["id"]))
    if feed["playback_mode"] == "random":
        return _random_pick(db, feed["id"])
    return db.oldest_new_for_feed(feed["id"])

def _random_pick(db: Database, feed_id: int) -> sqlite3.Row | None:
    """Draw a random unplayed episode; if its title carries an arc marker,
    play the arc's oldest unplayed member instead — never Part 3 before an
    unplayed Part 1. The episode list is oldest-first, so the first key
    match IS the arc's oldest unplayed member (the draw itself when nothing
    earlier matches). Also makes started arcs likelier to finish: any arc
    member drawn redirects to the next unplayed part."""
    episodes = db.new_episodes_for_feed(feed_id)
    if not episodes:
        return None
    draw = random.choice(episodes)
    key = feeds.arc_key(draw["title"])
    if key is None:
        return draw
    return next(ep for ep in episodes if feeds.arc_key(ep["title"]) == key)
```

(`dj.py` already has `import feeds` — no import change needed.)

- [ ] **Step 8: Run tests, then full suite**

Run: `.venv/bin/pytest tests/test_pick.py tests/test_arc_key.py -v` then `.venv/bin/pytest -q`
Expected: all pass, including every pre-existing rotation/news/category test (in_order default preserves old behavior everywhere).

- [ ] **Step 9: Commit**

```bash
git add feeds.py dj.py tests/test_arc_key.py tests/test_pick.py
git commit -m "feat: arc-aware random playback mode in pick_next"
```

---

### Task 3: API — playback toggle action and status field

**Files:**
- Modify: `web.py:151-165` (`feed_action` gains a `playback` action)
- Modify: `dj.py:474-484` (`status()` station entries gain `playback_mode`)
- Test: `tests/test_web.py` (append)

**Interfaces:**
- Consumes: `db.toggle_feed_playback` (Task 1).
- Produces (Task 5 consumes): `POST /feeds/<id>/playback` toggles the mode; each station in `/api/status` carries `"playback_mode": "in_order" | "random"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
def test_feed_playback_toggle_route(client) -> None:
    c, db, _ = client
    fid = db.add_feed("https://x/rss", "X", None, False)
    assert c.post(f"/feeds/{fid}/playback").get_json()["ok"] is True
    assert db.get_feed(fid)["playback_mode"] == "random"
    data = c.get("/api/status").get_json()
    station = next(s for s in data["stations"] if s["id"] == fid)
    assert station["playback_mode"] == "random"
    assert c.post(f"/feeds/{fid}/playback").get_json()["ok"] is True
    assert db.get_feed(fid)["playback_mode"] == "in_order"

def test_status_playback_mode_defaults_in_order(client) -> None:
    c, db, _ = client
    db.add_feed("https://x/rss", "X", None, False)
    data = c.get("/api/status").get_json()
    assert data["stations"][0]["playback_mode"] == "in_order"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_web.py -k playback -v`
Expected: FAIL — `playback` is an unknown action (404) and the status field is missing.

- [ ] **Step 3: Implement**

In `web.py`'s `feed_action`, add before the `else:` clause:

```python
        elif action == "playback":
            db.toggle_feed_playback(feed_id)
```

In `dj.py`'s `status()` stations block, add after `"category_id": f["category_id"],`:

```python
                    "playback_mode": f["playback_mode"],
```

- [ ] **Step 4: Run tests, then full suite**

Run: `.venv/bin/pytest tests/test_web.py -v` then `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web.py dj.py tests/test_web.py
git commit -m "feat: expose playback mode via status and feed toggle action"
```

---

### Task 4: On-add auto-classification

**Files:**
- Modify: `feeds.py` (add `detect_playback_mode` after `arc_key`; wire into `add_feed_from_parsed` at feeds.py:111-124)
- Test: `tests/test_feeds.py` (append)

**Interfaces:**
- Consumes: `feeds.arc_key`, `feeds._LEADING_NUM_RE` (Task 2); `db.add_feed(..., playback_mode=...)` (Task 1).
- Produces: `detect_playback_mode(parsed) -> str` (`"in_order"` or `"random"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_feeds.py` (the file already imports `feedparser`; add `detect_playback_mode` to its `from feeds import ...` line):

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_feeds.py -k detect -v`
Expected: FAIL with `ImportError: cannot import name 'detect_playback_mode'`.

- [ ] **Step 3: Implement**

In `feeds.py`, after `arc_key`:

```python
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
```

In `add_feed_from_parsed`, change the `db.add_feed` call to:

```python
    feed_id = db.add_feed(url, title, image, is_news,
                          playback_mode=detect_playback_mode(parsed))
```

- [ ] **Step 4: Run tests, then full suite**

Run: `.venv/bin/pytest tests/test_feeds.py -v` then `.venv/bin/pytest -q`
Expected: all pass. Note: pre-existing `test_feeds.py` fixtures may now classify as either mode — no existing test asserts `playback_mode`, so nothing should break; if one does, the fixture titles changed meaning and the assertion should be updated to match the detected mode, not the detector weakened.

- [ ] **Step 5: Commit**

```bash
git add feeds.py tests/test_feeds.py
git commit -m "feat: auto-classify playback mode when a feed is added"
```

---

### Task 5: Frontend button + README

**Files:**
- Modify: `templates/stations.html:134-140` (`sheetHeadHTML`'s `stn-admin` row gains the toggle button)
- Modify: `README.md:12-14` (DJ rule 2 rewritten to describe both modes)

**Interfaces:**
- Consumes: `POST /feeds/<id>/playback` and `playback_mode` in the status payload (Task 3). The dashboard's existing delegated `data-feed` click branch (`post('/feeds/' + btn.dataset.feed)`) already covers any `data-feed="<id>/<action>"` button — verify with `grep -n "dataset.feed" templates/*.html` which template holds it; **no new JS wiring is expected**.

- [ ] **Step 1: Add the button**

In `templates/stations.html`'s `sheetHeadHTML`, insert after the Pause/Enable button line (line 137):

```js
    `<button type="button" class="tab small" data-feed="${f.id}/playback" title="How the DJ picks this show's next episode">${f.playback_mode === "random" ? "Random" : "In order"}</button>` +
```

- [ ] **Step 2: Update the README**

Replace DJ rule 2 (README.md lines 12-14):

```markdown
2. **Multi-part episodes stay in order.** Shows set to *In order* always play
   their oldest unplayed episode. Shows set to *Random* draw any unplayed
   episode instead — but an arc guard still applies: when a draw belongs to a
   multi-part story ("Part 3", "(2/4)", a trailing "II"), the story's oldest
   unplayed part plays first, so a series still arrives 1 → 2 → 3. New shows
   are classified automatically on add (declared serials and heavily numbered
   feeds start *In order*); flip any show from its card in Stations.
```

- [ ] **Step 3: Verify with Flask's test client**

```bash
.venv/bin/python -c "
from config import Config
from db import Database
from dj import DJ
from web import create_app

cfg = Config.from_env({'DATA_DIR': '/tmp/verify-playback-mode'})
db = Database(cfg.db_path)
db.init()
dj = DJ(db, cfg, lambda: None)
app = create_app(db, dj, cfg)
app.config['TESTING'] = True
client = app.test_client()

html = client.get('/stations').get_data(as_text=True)
for marker in ['data-feed=', '/playback', 'playback_mode', 'In order', 'Random']:
    assert marker in html, f'MISSING: {marker}'
    print('OK:', marker)

fid = db.add_feed('https://x/rss', 'X Show', None, False)
assert client.post(f'/feeds/{fid}/playback').get_json()['ok'] is True
station = next(s for s in client.get('/api/status').get_json()['stations'] if s['id'] == fid)
assert station['playback_mode'] == 'random'
print('OK: end-to-end toggle reflected in status')
"
```

Expected: all `OK:` lines. Also confirm via `grep -n "dataset.feed" templates/*.html` that the shared delegated click branch exists and covers the new button (report which file holds it).

- [ ] **Step 4: Full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add templates/stations.html README.md
git commit -m "feat: playback mode toggle in the station sheet, document the arc guard"
```

---

## Rollout (post-deploy operational step, not a plan task)

After merge + deploy: classify all 55 existing stations (everything is `in_order` after migration). Proposed `in_order` set from the audit — Revolutions, History of Philosophy Without Any Gaps, Fall of Civilizations, Empire, Hardcore History, possibly The Ancients — is presented to the user for review first, then a script toggles the rest to `random` via `POST /feeds/<id>/playback` (read current modes from `/api/status`, toggle only where the target differs).

## Self-Review Notes

- **Spec coverage:** column+migration+default (T1), toggle+unplayed query (T1), arc_key with the exact pipeline/separator/None contract (T2), pick_next branch + emergent arc-completion property (T2), route+status (T3), on-add classification with the ≥50% threshold and empty→in_order (T4), state-labeled button in the station sheet + README (T5), rollout as operational step (non-task section) — all covered.
- **Placeholder scan:** clean — every step carries complete code and expected outputs.
- **Type consistency:** `arc_key(title: str) -> str | None` defined in feeds.py (T2), consumed as `feeds.arc_key` in dj.py (T2) and directly in `detect_playback_mode` (T4). `add_feed(..., playback_mode: str = "in_order")` matches T1's definition and T4's call. `toggle_feed_playback(feed_id)` matches T1/T3. `new_episodes_for_feed` matches T1/T2. Status field name `playback_mode` matches T3/T5.
