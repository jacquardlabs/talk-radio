# Episode backlog UI — LED departure-board re-skin

**Date:** 2026-07-06
**Builds on:** `2026-07-05-episode-backlog-management-design.md` (behavior — unchanged by this doc) and main's `8c3c687` "LED departure-board dashboard" redesign of `templates/index.html` (visual system this re-skin targets).

## Goal

The episode-backlog-management branch built its episode-search and per-station-panel UI against the dashboard's old muted-panel template. While that work was in review, main replaced the entire template with a new "LED departure-board" design (amber-on-black, monospace, glowing text, `.svc-row`/`.tab` conventions, computed departure times, volume control). The old template no longer exists, so this UI needs to be rebuilt against the new one.

**No behavior changes.** Every requirement from the original spec — global search, per-station expandable panel with local search/pagination, multi-select archived-episode release, force-play next/now on any episode — carries over exactly as approved. This document only maps that behavior onto the new template's markup, CSS, and visual language.

## Markup mapping

| Old (removed template) | New (LED departure-board template) |
|---|---|
| `<ul id="stations"><li class="row">` | `<div class="svc" id="stations"><div class="svc-row">` |
| Plain `<button>` | `<button type="button" class="tab small">` |
| `.tag` / `.tag off` (status badges) | `.status` with modifier classes — see below |
| `.muted` (dim text) | `.m` |
| `.grow` (flex-fill spacer) | `<span class="spacer"></span>` (explicit element, same visual effect) |
| `mono` class on numeric/date text | dropped — the whole page is already monospace |

The per-station episode panel becomes a `.svc-row`-styled block rather than a `<li>`; the global search section keeps its own `<div>`/`<section>`-equivalent container using the new template's existing section pattern (`<div class="eyebrow">` label + content div, as used for Stations/Wake schedule/Recently played).

## Status color-coding

The new template already has a color language for status: `.ok` (green, "ON TIME") and `.pri` (red, "PRIORITY") in the departures list. Episode status badges adopt it instead of the old uniform muted style, applied directly (not via a `.status` wrapper class) inside a `.svc-row`-scoped element:

- `new` / `queued` → `.glow` (amber — active/upcoming, matches the page's live-state accent)
- `played` → `.ok` (green, new `.svc-row .ok` rule — handled/done)
- `skipped` → `.pri` (red, reuses existing `.svc-row .pri` — flagged as an exception worth noticing)
- `archived` → `.m` (muted, reuses existing `.svc-row .m` — inactive backlog)

## Section placement

"Find an episode" (global search) sits directly after the departures board (`<div id="departures">`) and before the "Stations" section — the same relative position it held in the old template (between the queue/departures list and the stations list).

## Per-station control

The existing per-station button row (Mark news / Pause / Add back catalog / Remove, all `.tab small`) gains one more: "Episodes", toggling the expandable panel. Same button styling as its siblings, same toggle behavior as before (label flips to "Hide episodes" when open).

## What does NOT change

- All JS state (`expandedFeedId`, `stationSearch`, `selectMode`, `selectedIds`, `globalSearch`, `debounce`)
- `renderStations()`'s keyed-DOM-patch algorithm (station `<div>` keyed by feed id, panel `<div>` never destroyed while open, only repositioned — same correctness property, same technique, just `<div>` instead of `<li>`)
- `episodeRow()` / `paginationHTML()` / `postEpisodeAction()` function shapes and logic
- Event delegation wiring (`data-episode`, `data-page`, `data-episodes-toggle`, `data-select-toggle`, `data-release-selected`, `data-episode-check`, `data-station-search`)
- All backend routes and contracts (Tasks 1-3 of the original plan — untouched by this doc)

## Testing

No frontend test harness exists in this repo (unchanged constraint from the original spec). Verification is manual/hand-traced, same approach used throughout the original implementation: Flask test-client checks for markup presence and route behavior, hand-tracing of `data-*` attribute consistency between production and consumption sites, and the same keyed-patch correctness trace (station poll must never destroy an open panel's live search input).

## Verification

1. Full pytest suite green (no backend changes expected, so this should be unaffected).
2. Manual: expand a station, confirm the panel renders with the new visual style and matches surrounding sections; type in its local search box and confirm it survives multiple 5-second poll ticks without losing focus; confirm status color-coding renders correctly across new/queued/played/skipped/archived rows; confirm global search results, pagination, force-play (next/now), single release, and multi-select release all work end-to-end against the new markup.
