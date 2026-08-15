# Per-show playback mode: in-order vs arc-aware random — design

**Date:** 2026-07-06
**Builds on:** `2026-07-05-sonos-talk-radio-design.md` (the rotation engine this modifies) and `2026-07-06-station-categories-design.md` (the per-feed settings pattern this follows). Frontend targets the two-page dashboard (`board.html` / `stations.html` over shared `base.html`) introduced in commits `161b7f1`–`1b14caa`.

## Goal

Not a shuffle mode — normal rotation itself changes. Today the DJ plays every show oldest-unplayed-first, which is right for true serials (Revolutions, History of Philosophy Without Any Gaps) but wrong for the episodic majority: with ~19k back-catalog episodes released, In Our Time will play 2004 episodes for years before reaching anything recent. This makes the within-show pick per-show configurable:

- **`in_order`** — oldest unplayed first (today's behavior, unchanged)
- **`random`** — a random unplayed episode, with an **arc guard**: never start mid-arc — no Part 3 before Part 1

News-first and show selection (random show, no back-to-back repeats, category/enabled/news filters) are untouched. Two modes only: arc-aware random degenerates to plain random for arc-less shows (every episode is its own one-part arc), so a separate "pure random" third mode is redundant.

Grounding (from a live audit of all 55 station feeds, 2026-07-06): `itunes:type` is nearly useless — only Revolutions declares `serial`. Title-numbering heuristics over-trigger (AWS Bites, Good Job Brain!, TRIVIALITY number everything yet shuffle fine) and under-trigger (HOPWAG embeds its numbers as "HoP 442 – …" and scores 0% despite being strictly sequential). Several shows are episodic *with multi-part runs* (Behind the Bastards 61% part-titles, The Rest Is History's 2–5 episode arcs, Philosophize This!, You're Wrong About, Ologies) — hence the arc guard rather than a per-show binary alone.

## Data model (`db.py`)

`feeds.playback_mode TEXT NOT NULL DEFAULT 'in_order' CHECK (playback_mode IN ('in_order','random'))`, added to the schema and via the same idempotent `ALTER TABLE` migration pattern in `init()` as `category_id` and `duration_seconds`. Default `in_order` means deploying is a zero-behavior-change event.

New methods, following existing conventions:

- `toggle_feed_playback(feed_id) -> None` — SQL `CASE` flip between the two modes, same shape as `toggle_feed`
- `new_episodes_for_feed(feed_id) -> list[Row]` — all unplayed episodes, oldest-first; the random draw and the arc guard both work off this one list ("oldest unplayed of the arc" is simply the first list member with a matching key)

## Engine (`dj.py`)

`pick_next()`'s show selection is untouched. The within-show step branches on the chosen feed's `playback_mode`:

- `in_order` → `oldest_new_for_feed()` exactly as today
- `random` → draw one episode at random from `new_episodes_for_feed()`, then apply the arc guard: derive `arc_key(title)` of the draw; if any other unplayed episodes in the same feed share that key, play the oldest unplayed member of the arc instead of the draw

Consequences, by design:

- A partially-played arc (Part 1 heard months ago, Parts 2–3 unplayed) resumes at Part 2 — the guard orders *unplayed* members only, never forces replays.
- Once an arc is started, its remaining parts become *more* likely to be drawn next for that show (any member drawn redirects to the next unplayed part), so arcs tend to finish rather than dangle — an emergent property, no extra machinery.
- `tick()`, queue reconciliation, resume, skip semantics, news insertion: all untouched — they operate on episode status and URIs, not pick order.

### `arc_key(title: str) -> str | None` — pure function

`None` means "no part marker detected — this episode stands alone." Grouping only ever happens between episodes whose keys are non-None and equal, within the same feed, among unplayed episodes — a generic subtitle can never weld unrelated episodes together.

1. **Strip leading episode numbering** (show-level numbering, not arc info): `"437. "`, `"10.91- "`, `"#12: "`, `"Show 66 - "`, `"Ep. 4 –"`
2. **Detect and strip part markers** (detection is what makes the key non-None): `Part 3` / `Part Three` / `Pt. 2` (prefix or suffix), `(2/4)`, `2 of 4`, trailing roman numeral (`Supernova in the East V`)
3. **If a marker was found and a subtitle separator remains** (`:` or `–`), truncate at the first separator — handles The Rest Is History's per-part subtitles: *"The Fall of the Aztecs: Spaniards on the March (Part 1)"* and *"The Fall of the Aztecs: The Great Escape (Part 4)"* both key to `the fall of the aztecs`
4. **Normalize**: lowercase, collapse whitespace, trim punctuation

Real catalog styles that form the test suite: Behind the Bastards' *"Part One: …"* word-number prefixes; Hardcore History's trailing roman numerals; Rest Is History's numbered-with-subtitle parts; Revolutions-style `"10.91- The End"` (number stripped, no marker → `None`).

**Failure characterization:** a *missed* arc means two parts could shuffle — and in random mode across a ~19k-episode pool the halves typically land weeks apart, so misses are mostly invisible; the per-show `in_order` toggle is the escape hatch. A *false* group (e.g. "…World War I" / "…World War II" episodes keying together) merely constrains those episodes to chronological order relative to each other — benign. The heuristic errs cheap in both directions, which is why pick-time title matching beats precomputed arc ids (no schema, no backfill, no reclassification churn).

## API & frontend

- **Route**: the existing `/feeds/<id>/<action>` dispatcher gains a `"playback"` action calling `toggle_feed_playback` — one write path; the rollout script reads current modes from status and toggles only where it differs
- **Status payload**: each station entry gains `"playback_mode"`
- **UI** (`templates/stations.html` only — the board page is unaffected): one more `.tab small` button in the station sheet's `stn-admin` row, next to Pause/Enable, state-labeled **"In order" / "Random"**, click to flip. State-labeled (like the shelf headers' "Rotation on/off") because neither direction is a natural action verb.

## Rollout & new-feed defaults

Post-deploy operational step, same pattern as the category rollout: everything defaults `in_order` (deploy changes nothing) → I classify all 55 shows from the audit data + title samples → user reviews the proposed list → a script applies it via the toggle route. Starting `in_order` proposal (final list reviewed before applying): Revolutions, History of Philosophy Without Any Gaps, Fall of Civilizations, Empire, Hardcore History, possibly The Ancients.

New feeds auto-classify on add: declared `itunes:type == serial`, or ≥50% of ingested titles carrying a leading number or part marker (the audit's threshold, computed with the same patterns `arc_key` uses) → `in_order`, else `random`. Classification errors land on the safe side — `in_order` is today's behavior, and the per-station toggle fixes any miss.

## Testing

- `arc_key()`: pure-function tests against the real title styles above (word-number prefixes, trailing romans, subtitled parts, plain titles → `None`, leading-number-only → `None`).
- `pick_next()`: seeded-random tests — random mode varies its picks; the arc guard redirects a mid-arc draw to the arc's oldest unplayed member; partially-played arcs resume at the right part; `in_order` shows behave byte-for-byte as today; existing no-repeat-show/news/category tests pass unchanged.
- `db.py`: migration idempotence, toggle flip, `new_episodes_for_feed` ordering and status filter.
- `web.py`: `playback` action on the feed dispatcher, `playback_mode` in the status payload, 404 on unknown feed.
- Frontend: no test harness (accepted constraint) — Flask test-client markup checks + hand-traced wiring, as before.

## Verification

1. Full pytest suite green.
2. Manual: flip a big episodic show (In Our Time) to Random and confirm consecutive picks from it are non-chronological; confirm a serial left In order still plays sequentially; flip Behind the Bastards to Random and confirm a "Part Two" never precedes its unplayed "Part One" (seeded engine tests cover this deterministically; live spot-check for sanity); confirm the toggle button reflects and flips the mode from the station sheet.
