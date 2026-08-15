# Episode Backlog LED Re-skin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the episode-search and per-station-panel UI (global search, expandable per-station episode panel with local search/pagination/bulk-release, force-play next/now) against the new "LED departure-board" `templates/index.html`, with no behavior changes from the already-approved design.

**Architecture:** Single-file change to `templates/index.html` only — no backend changes (the Task 1-3 backend from the original plan already merged cleanly and is unaffected). Same technique as before: shared `episodeRow()`/`paginationHTML()` renderers reused by both the global search section and the per-station panel; station-list rendering uses a keyed DOM patch (`renderStations()`) so an open panel's live search input survives the 5-second status poll.

**Tech Stack:** Same as before — vanilla JS, no build step, no frontend test harness (verify manually / by hand-tracing).

## Global Constraints

- No behavior changes from `docs/superpowers/specs/2026-07-05-episode-backlog-management-design.md` — this is a pure visual/markup re-skin, per `docs/superpowers/specs/2026-07-06-episode-backlog-led-reskin-design.md`.
- No backend changes — `db.py`/`dj.py`/`web.py` are untouched by this plan.
- No frontend test harness exists in this repo — verify by hand-tracing JS and using Flask's test client for markup/route checks, not new automated tests.
- Reuse existing new-template CSS classes wherever an equivalent already exists (`.svc-row`, `.tab small`, `.glow`, `.m`, `.pri`, `.spacer`) — only add new CSS where the new design has no equivalent.
- Status color mapping (from the re-skin design doc): `new`/`queued` → `.glow` (amber); `played` → `.ok` (green, new CSS rule needed — `.svc-row .pri` and `.svc-row .m` already exist but `.svc-row .ok` doesn't); `skipped` → `.pri` (red, reuse existing); `archived` → `.m` (muted, reuse existing).
- Section placement: "Find an episode" goes directly after `<div id="departures"></div>` and before the "Stations" `<div class="eyebrow">`.
- Do not start a live server in the background for verification — Flask's test client (`tests/test_web.py`'s pattern) is the way to check rendered markup and route responses in this sandbox.

---

### Task 1: Global search section + shared episode-row rendering

**Files:**
- Modify: `templates/index.html:170` (CSS additions, before `</style>`)
- Modify: `templates/index.html:43` (extend the themed input selector to include `type="search"`)
- Modify: `templates/index.html:212` (new "Find an episode" markup, before the Stations `eyebrow` div)
- Modify: `templates/index.html:248` (new JS state, after `let lastStrip = "";`)
- Modify: `templates/index.html:291` (new shared render helpers, after `post()`, before the `// ── render ──` comment)
- Modify: `templates/index.html:439-446` (extend the delegated click listener; add a new delegated `input` listener)

**Interfaces:**
- Consumes: existing `esc()`, `post()`, `flash()`, `$()` helpers (all already in the file, unchanged).
- Produces (used by Task 2): `episodeRow(e, opts)`, `paginationHTML(page, pageSize, total, kind, key)`, `postEpisodeAction(id, action, btn)` — Task 2's per-station panel reuses all three verbatim. `postEpisodeAction` references `expandedFeedId`, which Task 2 introduces — Task 1 must guard that reference (see Step 5) so this task is independently correct before Task 2 lands.

- [ ] **Step 1: CSS additions**

In `templates/index.html`, change line 43 from:

```css
select, input[type="url"], input[type="number"], input[type="time"] {
```

to:

```css
select, input[type="url"], input[type="number"], input[type="time"], input[type="search"] {
```

In `templates/index.html`, insert immediately before the closing `</style>` tag (line 171, right after the `a:focus-visible, button:focus-visible, ...` rule ending at line 170):

```css
.ep-toolbar { display: flex; gap: 8px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
.ep-toolbar input[type="search"] { flex: 1 1 200px; }
.svc-row .ok { color: var(--green); font-size: 10px; letter-spacing: .18em; text-shadow: 0 0 6px rgba(134,224,107,.6); }
.ep-row input[type="checkbox"] { accent-color: var(--amber); width: 16px; height: 16px; }
.pagination { display: flex; gap: 10px; align-items: center; margin-top: 8px; font-size: 11px; letter-spacing: .1em; color: var(--amber-dim); }
```

Note: `.svc-row .pri` (red) and `.svc-row .m` (muted) already exist (lines 137, 139) — only the green `.ok` variant is missing and added here. `.ep-panel` (the per-station panel container) is added in Task 2, since Task 1 doesn't create panels yet.

- [ ] **Step 2: Add the "Find an episode" section markup**

In `templates/index.html`, insert immediately after `<div id="departures"></div>` (line 212) and before `<div class="eyebrow"><span>Stations</span></div>` (line 214):

```html
<div class="eyebrow"><span>Find an episode</span></div>
<input type="search" id="global-search-input" placeholder="Search all stations by episode or show title" style="width:100%">
<div class="svc" id="global-search-results"></div>
<div id="global-search-pagination"></div>

```

- [ ] **Step 3: Add JS state**

In `templates/index.html`, immediately after `let lastStrip = "";` (line 248) and before `function esc(s) {` (line 250), insert:

```js
const globalSearch = { q: "", page: 1 };
const debounceTimers = new WeakMap();
function debounce(el, fn, ms = 300) {
  clearTimeout(debounceTimers.get(el));
  debounceTimers.set(el, setTimeout(fn, ms));
}
```

- [ ] **Step 4: Add shared render helpers**

In `templates/index.html`, immediately after the closing brace of `post()` (line 291, `}`) and before the `// ── render ─────` comment (line 293), insert:

```js
// ── shared episode rendering (station panel + global search) ────────
function episodeRow(e, opts) {
  opts = opts || {};
  const statusClass = { played: "ok", skipped: "pri", archived: "m" }[e.status] || "glow";
  const checkbox = opts.checkbox
    ? `<input type="checkbox" data-episode-check="${e.id}"${opts.selected ? " checked" : ""}>`
    : "";
  return `<div class="svc-row ep-row">` +
    checkbox +
    `<span class="${statusClass}">${esc(e.status)}</span>` +
    (opts.showBadge ? `<span class="m">${esc(e.show)}</span>` : "") +
    `<span class="glow">${esc(e.title)}</span>` +
    `<span class="m">${esc((e.published_at || "").slice(0, 10))}</span>` +
    `<span class="spacer"></span>` +
    `<button type="button" class="tab small" data-episode="${e.id}/play_next" title="Play after current">Play next</button>` +
    `<button type="button" class="tab small" data-episode="${e.id}/play_now" title="Interrupt and play now">Play now</button>` +
    (e.status === "archived"
      ? `<button type="button" class="tab small" data-episode="${e.id}/release">Release</button>` : "") +
    `</div>`;
}

function paginationHTML(page, pageSize, total, kind, key) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return `<div class="pagination">` +
    `<button type="button" class="tab small" data-page="${kind}:${key}:${page - 1}" ${page <= 1 ? "disabled" : ""}>Prev</button>` +
    `<span>Page ${page} of ${pages} (${total})</span>` +
    `<button type="button" class="tab small" data-page="${kind}:${key}:${page + 1}" ${page >= pages ? "disabled" : ""}>Next</button>` +
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
    || '<div class="empty">No matching episodes</div>';
  pager.innerHTML = data.total ? paginationHTML(data.page, data.page_size, data.total, "global", "q") : "";
}
```

Note: `renderEpisodePanel` and `expandedFeedId` are defined in Task 2 — `postEpisodeAction` guards with `typeof expandedFeedId !== "undefined"` so this task's code works and is independently testable before Task 2 lands. Task 2 removes this guard once both pieces coexist (see Task 2 Step 3).

- [ ] **Step 5: Wire up the global search input and its pagination clicks**

In `templates/index.html`, the existing delegated click listener (lines 439-446) currently ends with:

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

(Task 2 Step 6 extends this same `input` listener with the per-station search case, and the `data-page` branch with the `"station"` kind.)

- [ ] **Step 6: Verify with Flask's test client**

Run this to confirm the markup renders and the search API responds correctly (no live server, no background process):

```bash
.venv/bin/python -c "
from config import Config
from db import Database
from dj import DJ
from web import create_app

cfg = Config.from_env({'DATA_DIR': '/tmp/verify-reskin-task1'})
db = Database(cfg.db_path)
db.init()
dj = DJ(db, cfg, lambda: None)
app = create_app(db, dj, cfg)
app.config['TESTING'] = True
client = app.test_client()

resp = client.get('/')
html = resp.get_data(as_text=True)
for marker in ['id=\"global-search-input\"', 'id=\"global-search-results\"', 'id=\"global-search-pagination\"']:
    assert marker in html, f'MISSING: {marker}'
    print('OK:', marker)

fid = db.add_feed('https://x/rss', 'Mothman Museum Hour', None, False)
db.insert_episode(fid, 'g1', 'The Mothman Sighting', 'https://cdn/x/1.mp3', '2026-01-01T00:00:00Z')
r = client.get('/api/episodes/search?q=mothman')
data = r.get_json()
assert data['total'] == 1 and data['episodes'][0]['show'] == 'Mothman Museum Hour'
print('OK: search API')
"
```

Expected: three `OK:` lines, no assertion errors.

- [ ] **Step 7: Run the full pytest suite as a sanity check**

Run: `.venv/bin/pytest -q`
Expected: all tests pass (this task touches only the untested template file — this confirms nothing else broke).

- [ ] **Step 8: Commit**

```bash
git add templates/index.html
git commit -m "feat: re-skin global episode search onto the LED departure-board design"
```

---

### Task 2: Per-station episode panel with keyed rendering and bulk release

**Files:**
- Modify: `templates/index.html` (station-list rendering, new panel functions, event wiring — all within the file Task 1 already modified)

**Interfaces:**
- Consumes: `episodeRow`, `paginationHTML`, `postEpisodeAction` (Task 1).
- Produces: `expandedFeedId`, `renderEpisodePanel(feedId, panelEl?)`, `episodeState(feedId)`, `renderStations(stations)` — replaces the inline stations-map rendering currently inside `render()`.

- [ ] **Step 1: Add per-station state**

In `templates/index.html`, immediately after the `debounce()` function added in Task 1 Step 3, insert:

```js
let expandedFeedId = null;
const stationSearch = {};   // feedId -> { q, page }
const selectMode = {};      // feedId -> bool
const selectedIds = {};     // feedId -> Set<number>

function episodeState(feedId) {
  return stationSearch[feedId] || (stationSearch[feedId] = { q: "", page: 1 });
}
```

- [ ] **Step 2: Add CSS for the panel container**

In `templates/index.html`, add one more rule alongside the CSS block Task 1 added in its Step 1 (anywhere in that block is fine):

```css
.ep-panel { padding: 10px 0 14px 0; }
```

- [ ] **Step 3: Add the station-row and panel renderers**

In `templates/index.html`, immediately after the `paginationHTML()` function added in Task 1 Step 4 (and before `postEpisodeAction`), insert:

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

async function renderEpisodePanel(feedId, panelEl) {
  panelEl = panelEl || $("stations").querySelector(`[data-episodes-panel="${feedId}"]`);
  if (!panelEl) return;
  const st = episodeState(feedId);
  const data = await (await fetch(
    `/api/feeds/${feedId}/episodes?page=${st.page}&q=${encodeURIComponent(st.q)}`
  )).json();
  const sel = !!selectMode[feedId];
  const selected = selectedIds[feedId] || (selectedIds[feedId] = new Set());
  panelEl.innerHTML =
    `<div class="ep-toolbar">` +
    `<input type="search" placeholder="Filter this station's episodes" value="${esc(st.q)}" data-station-search="${feedId}">` +
    `<button type="button" class="tab small" data-select-toggle="${feedId}">${sel ? "Done selecting" : "Select"}</button>` +
    (sel ? `<button type="button" class="tab small" data-release-selected="${feedId}">Release selected (${selected.size})</button>` : "") +
    `</div>` +
    (data.episodes.map((e) => episodeRow(e, {
      checkbox: sel && e.status === "archived", selected: selected.has(e.id),
    })).join("") || '<div class="empty">No episodes match</div>') +
    paginationHTML(st.page, data.page_size, data.total, "station", feedId);
}

function renderStations(stations) {
  const container = $("stations");
  if (!stations.length) {
    container.innerHTML = '<div class="empty">NO STATIONS — ADD AN RSS URL BELOW</div>';
    return;
  }
  const seen = new Set();
  let prevEl = null;
  for (const f of stations) {
    seen.add(String(f.id));
    let row = container.querySelector(`[data-station="${f.id}"]`);
    if (!row) {
      row = document.createElement("div");
      row.className = "svc-row";
      row.dataset.station = f.id;
    }
    row.innerHTML = stationRowHTML(f);
    if (prevEl) prevEl.after(row); else container.prepend(row);
    prevEl = row;

    let panel = container.querySelector(`[data-episodes-panel="${f.id}"]`);
    if (expandedFeedId === f.id) {
      if (!panel) {
        panel = document.createElement("div");
        panel.className = "ep-panel";
        panel.dataset.episodesPanel = f.id;
        row.after(panel);
        renderEpisodePanel(f.id, panel);
      } else {
        row.after(panel);  // reposition only -- never rebuild while open, or a live search input loses focus/state
      }
      prevEl = panel;
    } else if (panel) {
      panel.remove();
    }
  }
  for (const el of Array.from(container.querySelectorAll("[data-station]"))) {
    if (!seen.has(el.dataset.station)) el.remove();
  }
  for (const el of Array.from(container.querySelectorAll("[data-episodes-panel]"))) {
    if (!seen.has(el.dataset.episodesPanel)) el.remove();
  }
}
```

- [ ] **Step 4: Remove the `typeof` guard added in Task 1**

In `templates/index.html`, in `postEpisodeAction` (added in Task 1 Step 4), change:

```js
    if (typeof expandedFeedId !== "undefined" && expandedFeedId != null) renderEpisodePanel(expandedFeedId);
```

to:

```js
    if (expandedFeedId != null) renderEpisodePanel(expandedFeedId);
```

- [ ] **Step 5: Switch `render()` to use the keyed station renderer**

In `templates/index.html`, find this block inside `render()`:

```js
  // stations
  $("stations").innerHTML = s.stations.map((f) => {
    const c = f.counts;
    const unplayed = c.new + c.queued;
    return `<div class="svc-row">` +
      (f.is_news ? '<span class="pri">NEWS</span>' : "") +
      `<span class="glow">${esc(f.title)}</span>` +
      (f.enabled ? "" : '<span class="m">[HELD]</span>') +
      `<span class="m">${unplayed} unplayed &middot; ${c.played} played &middot; ${c.archived} archived</span>` +
      `<span class="spacer"></span>` +
      `<button type="button" class="tab small" data-feed="${f.id}/news">${f.is_news ? "Not news" : "Mark news"}</button>` +
      `<button type="button" class="tab small" data-feed="${f.id}/toggle">${f.enabled ? "Pause" : "Enable"}</button>` +
      (c.archived ? `<button type="button" class="tab small" data-feed="${f.id}/unarchive">Add back catalog</button>` : "") +
      `<button type="button" class="tab small red" data-feed="${f.id}/delete" data-confirm="Remove ${esc(f.title)}?">Remove</button>` +
      `</div>`;
  }).join("") || '<div class="empty">NO STATIONS — ADD AN RSS URL BELOW</div>';
```

Replace it with:

```js
  // stations
  renderStations(s.stations);
```

- [ ] **Step 6: Wire up expand/collapse, select-mode, checkboxes, and bulk release**

In `templates/index.html`, in the delegated click listener extended in Task 1 Step 5, insert new branches right before the final `});` of that listener:

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

Also extend the `data-page` branch (added in Task 1 Step 5) to handle the `"station"` kind. Change:

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

Add a `change` listener (for the archived-episode checkboxes) right after the `input` listener added in Task 1 Step 5:

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

Extend the `input` listener added in Task 1 Step 5 to handle per-station search boxes. Change:

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

- [ ] **Step 7: Verify with Flask's test client and hand-tracing**

Run:

```bash
.venv/bin/python -c "
from config import Config
from db import Database
from dj import DJ
from web import create_app

cfg = Config.from_env({'DATA_DIR': '/tmp/verify-reskin-task2'})
db = Database(cfg.db_path)
db.init()
dj = DJ(db, cfg, lambda: None)
app = create_app(db, dj, cfg)
app.config['TESTING'] = True
client = app.test_client()

resp = client.get('/')
html = resp.get_data(as_text=True)
for marker in [
    'function renderStations(', 'function renderEpisodePanel(', 'function stationRowHTML(',
    'let expandedFeedId = null;', 'data-episodes-toggle', 'data-station-search',
    'data-select-toggle', 'data-release-selected', 'data-episode-check',
    'renderStations(s.stations);',
    'if (expandedFeedId != null) renderEpisodePanel(expandedFeedId);',
]:
    assert marker in html, f'MISSING: {marker}'
    print('OK:', marker)
assert 'typeof expandedFeedId' not in html, 'Task 1 guard should be removed'
print('OK: guard removed')

fid = db.add_feed('https://x/rss', 'X Show', None, False)
db.insert_episode(fid, 'g1', 'Ep 1', 'https://cdn/x/1.mp3', '2026-01-01T00:00:00Z', status='archived')
r = client.get(f'/api/feeds/{fid}/episodes')
data = r.get_json()
assert data['total'] == 1 and data['episodes'][0]['status'] == 'archived'
print('OK: per-station episodes API')

eid = data['episodes'][0]['id']
rel = client.post(f'/episodes/{eid}/release')
assert rel.get_json()['ok'] is True
print('OK: release route')
"
```

Expected: all `OK:` lines print, no assertion errors.

Also hand-trace: confirm every `data-*` attribute written by `stationRowHTML`/`renderEpisodePanel`/`episodeRow` (`data-episodes-toggle`, `data-station-search`, `data-select-toggle`, `data-release-selected`, `data-episode-check`, `data-episodes-panel`, `data-station`) has a matching `.dataset.*` read in the click/change/input listeners — and confirm `renderStations()` never calls `.innerHTML =` on an already-open panel element (only `.after()` to reposition it), since that's the property this task exists to deliver.

- [ ] **Step 8: Run the full pytest suite one more time**

Run: `.venv/bin/pytest -q`
Expected: all tests still pass (this task touches only the untested template file; this confirms Task 1 and the rest of the merged branch remain intact).

- [ ] **Step 9: Commit**

```bash
git add templates/index.html
git commit -m "feat: re-skin per-station episode panel onto the LED departure-board design"
```

---

## Self-Review Notes

- **Spec coverage:** global search section + shared renderers (Task 1), per-station panel + keyed DOM patch + bulk select/release (Task 2), status color-coding via `.glow`/`.ok`/`.pri`/`.m` (Task 1 CSS + `episodeRow`'s `statusClass` map), section placement after departures/before stations (Task 1 Step 2), themed `input[type="search"]` styling (Task 1 Step 1) — all covered. No backend work needed since Tasks 1-3 of the original plan already merged cleanly.
- **Placeholder scan:** none found — every step has complete code.
- **Type/signature consistency:** `episodeRow(e, opts)`, `paginationHTML(page, pageSize, total, kind, key)`, `postEpisodeAction(id, action, btn)` match between Task 1's definitions and Task 2's reuse. `renderEpisodePanel(feedId, panelEl)` and `episodeState(feedId)` match between Task 2's definition and its own call sites. `renderStations(stations)` signature matches its call site in `render()` (Step 5).
