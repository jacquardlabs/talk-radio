# Podcast Name Search Implementation Plan

**Goal:** Let the dashboard add a station by searching a podcast's name (via Apple's iTunes Search API) instead of only pasting an RSS URL directly.

**Architecture:** A new `feeds.search_podcasts()` function proxies to Apple's iTunes Search API server-side (avoids browser CORS issues and keeps all external HTTP calls server-side, matching every other integration in this app). A new `GET /api/podcasts/search` route exposes it with graceful degradation on failure. The dashboard's existing "Add station" form gains a Search/Paste-URL toggle; picking a search result silently fills the existing (now-hidden) URL field, so the actual add-feed submit path is completely unchanged.

**Tech Stack:** Python 3.12, Flask, `requests` (already a dependency), vanilla JS (no build step, no frontend test harness — same as the rest of this app).

## Global Constraints

- No new dependencies, no API key/signup required — Apple's iTunes Search API (`https://itunes.apple.com/search`) needs neither.
- No DB schema changes — this feature only helps populate the URL the existing `add_feed` flow already accepts; it never touches the `feeds`/`episodes` tables directly.
- The direct-URL path must keep working exactly as today — this is additive (a toggle), not a replacement.
- Follow existing error-handling conventions exactly: network/API failures degrade gracefully (200 response with an `error` field or empty results), never a 500 — matches how `dj.status()`, transport routes, and the episode-search route already behave.
- No mocking library in this repo (`requirements-dev.txt` is just `pytest`) — tests hand-roll small fakes or monkeypatch the module-level function under test, matching `test_feeds.py`'s existing pattern (`monkeypatch.setattr(feeds_mod, "fetch_feed", ...)`).
- No frontend test harness — the frontend task is verified manually (Flask test client for markup/route checks, hand-traced JS), not with new automated tests.
- Do not start a live server in the background for verification — use Flask's test client (the `tests/test_web.py` pattern), matching how prior frontend tasks in this repo were verified in this sandbox.

---

### Task 1: Backend — `search_podcasts()` and the search route

**Files:**
- Modify: `feeds.py:29` (insert `search_podcasts` after `fetch_feed`, before `entry_audio_url`)
- Modify: `web.py:107` (insert the new route after `player_volume`, before `add_feed`)
- Test: `tests/test_feeds.py` (append new tests)
- Test: `tests/test_web.py` (append new tests; add a `feeds_mod` import)

**Interfaces:**
- Consumes: nothing new — uses the existing `requests` dependency and `cfg.user_agent` (already used by every other feed-fetching call).
- Produces (used by Task 2): `GET /api/podcasts/search?q=<term>` → `{"results": [{"title": str, "author": str, "artwork_url": str, "feed_url": str}, ...]}`, with an additional `"error": str` key present only when the upstream call failed.

- [ ] **Step 1: Write the failing tests for `search_podcasts`**

Append to `tests/test_feeds.py`:

```python
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
```

This matches `test_feeds.py`'s existing convention exactly (see its `test_refresh_all_prunes_stale_news`/`test_refresh_all_survives_a_broken_feed`): a local `import feeds as feeds_mod` inside each test function, not a module-level import — the file doesn't import the `feeds` module as a whole at the top, only specific names (`from feeds import add_feed_from_parsed, ...`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_feeds.py -k search_podcasts -v`
Expected: FAIL with `AttributeError: module 'feeds' has no attribute 'search_podcasts'`.

- [ ] **Step 3: Implement `search_podcasts`**

In `feeds.py`, insert immediately after `fetch_feed` (ends at line 29) and before `def entry_audio_url`:

```python
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

```

Note: this deliberately does NOT wrap network/HTTP errors in `FeedError` — `fetch_feed` doesn't either (its `FeedError` is only for the "parsed successfully but the feed body was malformed" case, which has no equivalent for a JSON search response). Callers (the route in Task 1 Step 5, same as `refresh_all`/`add_feed` elsewhere in this codebase) catch broad `Exception` around calls that hit the network.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_feeds.py -v`
Expected: all tests pass, including the two new ones.

- [ ] **Step 5: Write the failing tests for the route**

First, add this import near the top of `tests/test_web.py` (alongside the existing imports):

```python
import feeds as feeds_mod
```

Then append to `tests/test_web.py`:

```python
def test_podcast_search_blank_query_short_circuits(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client
    called = []
    monkeypatch.setattr(feeds_mod, "search_podcasts", lambda *a, **k: called.append(1) or [])
    data = c.get("/api/podcasts/search?q=").get_json()
    assert data == {"results": []}
    assert called == []

def test_podcast_search_returns_results(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client
    fake_results = [{"title": "Mothman Museum Hour", "author": "Jane Doe",
                     "artwork_url": "https://x/art.jpg", "feed_url": "https://x/feed.xml"}]
    monkeypatch.setattr(feeds_mod, "search_podcasts", lambda term, ua: fake_results)
    data = c.get("/api/podcasts/search?q=mothman").get_json()
    assert data == {"results": fake_results}

def test_podcast_search_degrades_gracefully_on_error(no_sonos_client, monkeypatch) -> None:
    c, _ = no_sonos_client

    def boom(term, ua):
        raise RuntimeError("itunes unreachable")

    monkeypatch.setattr(feeds_mod, "search_podcasts", boom)
    resp = c.get("/api/podcasts/search?q=mothman")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == [] and "error" in data
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -k podcast_search -v`
Expected: FAIL with a 404 (route doesn't exist yet).

- [ ] **Step 7: Implement the route**

In `web.py`, insert immediately after `player_volume` (ends at line 107) and before `@app.post("/feeds")` (line 109):

```python
    @app.get("/api/podcasts/search")
    def api_podcast_search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"results": []})
        try:
            results = feeds_mod.search_podcasts(q, cfg.user_agent)
        except Exception:
            logger.exception("podcast search failed for %r", q)
            return jsonify({"results": [], "error": "Podcast search unavailable"})
        return jsonify({"results": results})

```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: all tests pass, including the three new ones.

- [ ] **Step 9: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (backend layers together).

- [ ] **Step 10: Commit**

```bash
git add feeds.py web.py tests/test_feeds.py tests/test_web.py
git commit -m "feat: add podcast-by-name search via iTunes Search API"
```

---

### Task 2: Frontend — search-by-name toggle in the "Add station" form

**Files:**
- Modify: `templates/index.html:176` (CSS addition, before `</style>`)
- Modify: `templates/index.html:227-238` (replace the existing add-feed form block with the toggle + search UI + the same form, URL field now hidden by default)
- Modify: `templates/index.html:582` (new JS state + functions, after the volume section, before the `// ── wire up ──` comment)
- Modify: `templates/index.html:585-624` (extend the delegated click listener)
- Modify: `templates/index.html:626-643` (extend the delegated input listener)
- Modify: `templates/index.html:682-694` (mode-toggle button wiring; extend the `add-feed` submit handler's reset logic)
- Modify: `templates/index.html:712` (call `setAddMode("search")` once on load)

**Interfaces:**
- Consumes: `GET /api/podcasts/search?q=` (Task 1); the existing `esc()`, `$()`, `post()`, `flash()`, `debounce()` helpers (all already in the file, unchanged).
- Produces: nothing consumed by later tasks — this is the last piece of this feature.

- [ ] **Step 1: Add CSS**

In `templates/index.html`, insert immediately before the closing `</style>` tag (currently line 177, right after `.pagination { ... }`):

```css
.pod-art { width: 32px; height: 32px; object-fit: cover; border: 1px solid var(--line); background: var(--bezel); }
```

- [ ] **Step 2: Replace the add-feed form block with the toggle + search UI**

In `templates/index.html`, find this block (currently lines 227-238):

```html
<form class="inline" id="add-feed">
  <input type="url" id="feed-url" placeholder="RSS URL" required>
  <label class="chk"><input type="checkbox" id="feed-news"> News</label>
  <select id="feed-include" aria-label="How much back catalog">
    <option value="latest" selected>Latest episode</option>
    <option value="new_only">New episodes only</option>
    <option value="last_n">Last N episodes</option>
    <option value="all">Entire back catalog</option>
  </select>
  <input type="number" id="feed-count" min="1" value="5" style="display:none;width:80px" aria-label="How many">
  <button type="submit" class="tab">Add station</button>
</form>
```

Replace it with:

```html
<div class="tabs" style="margin-top:14px">
  <button type="button" class="tab small live" id="mode-search" data-add-mode="search">Search</button>
  <button type="button" class="tab small" id="mode-url" data-add-mode="url">Paste URL</button>
</div>
<div id="podcast-search-block">
  <input type="search" id="podcast-search-input" placeholder="Podcast name" style="width:100%;margin-top:8px">
  <div class="svc" id="podcast-search-results"></div>
</div>
<div id="podcast-picked-block" style="display:none">
  <div class="svc-row">
    <img class="pod-art" id="podcast-picked-art" src="" alt="">
    <span class="glow" id="podcast-picked-title"></span>
    <span class="spacer"></span>
    <button type="button" class="tab small" id="podcast-picked-change">Change</button>
  </div>
</div>
<form class="inline" id="add-feed">
  <input type="url" id="feed-url" placeholder="RSS URL" required style="display:none">
  <label class="chk"><input type="checkbox" id="feed-news"> News</label>
  <select id="feed-include" aria-label="How much back catalog">
    <option value="latest" selected>Latest episode</option>
    <option value="new_only">New episodes only</option>
    <option value="last_n">Last N episodes</option>
    <option value="all">Entire back catalog</option>
  </select>
  <input type="number" id="feed-count" min="1" value="5" style="display:none;width:80px" aria-label="How many">
  <button type="submit" class="tab">Add station</button>
</form>
```

Note: an `input[required]` that is hidden via `display: none` (or has a hidden ancestor) is excluded from HTML5 constraint validation entirely per spec — so the still-`required` `#feed-url` won't block form submission while hidden in Search mode. `.tab.live` is an existing class (already used for the play/pause buttons) reused here for the active-mode indicator — no new CSS needed for it.

- [ ] **Step 3: Add JS state and podcast-search functions**

In `templates/index.html`, insert immediately after the volume section's closing `});` (currently line 582, right after the `$("vol").addEventListener("keydown", ...)` block) and before the `// ── wire up ──` comment (line 584):

```js
// ── podcast search (add station) ──────────────────────────────────
let addMode = "search";
let pickedPodcast = null;
let podcastResults = [];

function setAddMode(mode) {
  addMode = mode;
  $("mode-search").classList.toggle("live", mode === "search");
  $("mode-url").classList.toggle("live", mode === "url");
  $("podcast-search-block").style.display = mode === "search" && !pickedPodcast ? "" : "none";
  $("podcast-picked-block").style.display = mode === "search" && pickedPodcast ? "" : "none";
  $("feed-url").style.display = mode === "url" ? "" : "none";
  if (mode === "url") {
    pickedPodcast = null;
    $("feed-url").value = "";
  } else {
    $("podcast-search-input").value = "";
    $("podcast-search-results").innerHTML = "";
  }
}

function podcastResultRow(p, i) {
  return `<div class="svc-row">` +
    `<img class="pod-art" src="${esc(p.artwork_url)}" alt="">` +
    `<span class="glow">${esc(p.title)}</span>` +
    `<span class="m">${esc(p.author)}</span>` +
    `<span class="spacer"></span>` +
    `<button type="button" class="tab small" data-pick-podcast="${i}">Use this</button>` +
    `</div>`;
}

async function runPodcastSearch(term) {
  const results = $("podcast-search-results");
  if (!term) { results.innerHTML = ""; podcastResults = []; return; }
  const data = await (await fetch(`/api/podcasts/search?q=${encodeURIComponent(term)}`)).json();
  if (data.error) flash(data.error);
  podcastResults = data.results;
  results.innerHTML = podcastResults.map(podcastResultRow).join("") || '<div class="empty">NO MATCHES</div>';
}

function pickPodcast(p) {
  pickedPodcast = p;
  $("feed-url").value = p.feed_url;
  $("podcast-picked-art").src = p.artwork_url;
  $("podcast-picked-title").textContent = p.title;
  $("podcast-search-block").style.display = "none";
  $("podcast-picked-block").style.display = "";
}

```

- [ ] **Step 4: Wire up clicks (mode toggle + result picking)**

In `templates/index.html`, the delegated click listener (extended by earlier tasks, currently ending at line 624) currently ends with:

```js
  if (btn.dataset.releaseSelected) {
    const id = Number(btn.dataset.releaseSelected);
    const ids = Array.from(selectedIds[id] || []);
    if (!ids.length) return;
    return void post("/episodes/release", { ids }, btn).then((ok) => {
      if (ok) { selectedIds[id].clear(); selectMode[id] = false; }
      renderEpisodePanel(id);
    });
  }
});
```

Change it to:

```js
  if (btn.dataset.releaseSelected) {
    const id = Number(btn.dataset.releaseSelected);
    const ids = Array.from(selectedIds[id] || []);
    if (!ids.length) return;
    return void post("/episodes/release", { ids }, btn).then((ok) => {
      if (ok) { selectedIds[id].clear(); selectMode[id] = false; }
      renderEpisodePanel(id);
    });
  }
  if (btn.dataset.addMode) return void setAddMode(btn.dataset.addMode);
  if (btn.dataset.pickPodcast) return void pickPodcast(podcastResults[Number(btn.dataset.pickPodcast)]);
  if (btn.id === "podcast-picked-change") {
    pickedPodcast = null;
    $("feed-url").value = "";
    $("podcast-picked-block").style.display = "none";
    $("podcast-search-block").style.display = "";
    return;
  }
});
```

- [ ] **Step 5: Wire up the podcast search input's debounced typing**

In `templates/index.html`, the delegated `input` listener (currently lines 626-643) currently reads:

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

Change it to:

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
  } else if (el.id === "podcast-search-input") {
    debounce(el, () => runPodcastSearch(el.value.trim()));
  }
});
```

- [ ] **Step 6: Reset search/picker state after a successful add, and set the initial mode**

In `templates/index.html`, find the `add-feed` submit handler (currently lines 685-694):

```js
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
```

Change it to:

```js
$("add-feed").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = await post("/feeds", {
    url: $("feed-url").value,
    is_news: $("feed-news").checked,
    include: $("feed-include").value,
    count: $("feed-count").value,
  });
  if (ok) {
    $("feed-url").value = "";
    pickedPodcast = null;
    $("podcast-search-input").value = "";
    $("podcast-search-results").innerHTML = "";
    setAddMode(addMode);
    flash("Station added", true);
  }
});
```

(`setAddMode(addMode)` re-applies whichever mode is currently active — since `pickedPodcast` was just cleared above, this correctly shows the empty search box again if still in Search mode, or leaves the Paste-URL field visible-and-now-empty if in that mode.)

Then, near the bottom of the file, find:

```js
poll();
setInterval(poll, 5000);
```

Change it to:

```js
setAddMode("search");
poll();
setInterval(poll, 5000);
```

- [ ] **Step 7: Verify with Flask's test client**

Run this to confirm the markup renders and the wiring is structurally correct (no live server, no background process):

```bash
.venv/bin/python -c "
from config import Config
from db import Database
from dj import DJ
from web import create_app

cfg = Config.from_env({'DATA_DIR': '/tmp/verify-podcast-search'})
db = Database(cfg.db_path)
db.init()
dj = DJ(db, cfg, lambda: None)
app = create_app(db, dj, cfg)
app.config['TESTING'] = True
client = app.test_client()

resp = client.get('/')
html = resp.get_data(as_text=True)
for marker in [
    'id=\"mode-search\"', 'id=\"mode-url\"', 'id=\"podcast-search-input\"',
    'id=\"podcast-search-results\"', 'id=\"podcast-picked-block\"', 'id=\"podcast-picked-change\"',
    'function setAddMode(', 'function runPodcastSearch(', 'function pickPodcast(',
    'data-add-mode', 'data-pick-podcast', 'setAddMode(\"search\");',
]:
    assert marker in html, f'MISSING: {marker}'
    print('OK:', marker)
"
```

Expected: all `OK:` lines print, no assertion errors.

Also hand-trace: confirm `data-add-mode` (written on the two mode buttons) and `data-pick-podcast` (written per result row, read via `Number(btn.dataset.pickPodcast)` against the `podcastResults` array) are consistent between where they're set and where they're read in the click listener from Step 4.

- [ ] **Step 8: Run the full pytest suite one more time**

Run: `.venv/bin/pytest -q`
Expected: all tests still pass (this task touches only the untested template file — confirms Task 1 remains intact).

- [ ] **Step 9: Commit**

```bash
git add templates/index.html
git commit -m "feat: add podcast-by-name search toggle to the Add station form"
```

---

## Self-Review Notes

- **Spec coverage:** backend search proxy + route with graceful degradation (Task 1), frontend toggle/search/pick UI reusing the existing add-feed submit path unchanged (Task 2), error handling for blank query / no results / upstream failure / feed-url-less entries (Task 1 route + `search_podcasts` filtering, Task 2's `flash()` on `data.error`) — all covered.
- **Placeholder scan:** none found — every step has complete code.
- **Type/signature consistency:** `search_podcasts(term: str, user_agent: str) -> list[dict]` matches between its Task 1 definition, its test calls, and the route's call site. `podcastResultRow(p, i)`, `runPodcastSearch(term)`, `pickPodcast(p)`, `setAddMode(mode)` match between their Task 2 definitions and call sites. The route's response shape (`{"results": [...], "error"?: str}`) matches what `runPodcastSearch` reads (`data.results`, `data.error`).
