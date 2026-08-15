# Defer — Implementation Plan

**Design:** `docs/design/specs/2026-07-30-defer-current-episode-design.md`

**Goal:** Give the playing episode a way back into Up Next that keeps its place, so the news can go on now and the podcast resumes where it stopped.

**Architecture:** `_skip`'s `done: bool` widens to a three-way `disposition`. All three share the existing prologue — top up before advancing when this is the last track, advance or stop, remove the outgoing slot — and differ only in the tail. Defer's tail re-adds the stored `play_uri` after the news block (offset floored at one item), banks `cur.position` from the snapshot already in hand, and pins the row. No new schema, no new resume path.

**Tech Stack:** Python 3.12, Flask, SQLite, pytest, vanilla JS — no build step, no frontend test harness.

## Global Constraints

- Follow existing conventions exactly: type hints throughout, `{"ok", "error"}` JSON via `result()`, mutation routes through `call_player` so a dead speaker degrades instead of 500ing.
- The prologue stays single-sourced. Widening `_skip` is the whole point of the approach — do not copy the advance/remove sequence into a fourth place.
- No new columns. Defer is `status`, `resume_seconds`, `pinned`, and `up_next_order` used as they already are.
- News keeps riding first. `tick()`'s insertion at `cur_idx + 1` is untouched, and defer reads news the way tick does — `get_feed(ep["feed_id"])["is_news"]`, not off the episode row.
- Take one `_match_queue` snapshot and compute everything from it. A second live read can pair this snapshot's episode identity with a different track's position — the reason defer banks resume itself instead of calling `_save_resume()`.
- No frontend test harness — Task 3 is verified with the Flask test client for markup plus hand-tracing. Do not start a live server in the background.
- Never probe the DJ lock with `/player/volume`: for a grouped speaker that reads the members' average and SetGroupVolume rescales with integer rounding, so posting the value back ratchets the house down. Use `/player/group_all`.

---

### Task 1: Three dispositions on one prologue

**Files:** `dj.py` (`_skip`, `skip_later`, `skip_done`, `defer_current`, one new helper), `tests/test_dj_controls.py`

**Interfaces produced:** `defer_current() -> str | None`, `_skip(disposition: Literal["later", "done", "defer"]) -> str | None`, `_news_run(matches, cur_idx) -> int`

- [ ] **Step 1: failing tests** — defer lands after a two-item news block; with no news ahead it lands after exactly one item; it banks `resume_seconds` from the snapshot position; the episode stays `queued` and stays in `up_next_order`; it arrives pinned; `play_uri` is unchanged and nothing re-stages; a dry queue stops the player and keeps `resume_seconds`; the "current track isn't a station episode" guard still fires. Plus the regression that matters most: `skip_later` and `skip_done` keep their existing behaviour through the new signature, including `skip_later` still nulling `resume_seconds` and dropping the row from `up_next_order`.
- [ ] **Step 2:** widen `_skip(done: bool)` → `_skip(disposition)`; `skip_later`/`skip_done` become `_skip("later")`/`_skip("done")`. Behaviour-neutral — the suite stays green on this step alone, which is what proves the enum change is safe before any new behaviour rides on it.
- [ ] **Step 3:** add `_news_run(matches, cur_idx)` — count consecutive matched news episodes from `cur_idx + 1`, stopping at the first slot that is not one. An unmatched slot ends the run rather than being skipped over.
- [ ] **Step 4:** the defer tail — `set_resume(episode["id"], cur.position)` from the snapshot, re-add `episode["play_uri"]` at `cur_idx + max(1, _news_run(...))` (append when that lands past the shortened queue), `set_pinned(episode["id"], True)`. Leave status and the saved order alone; `_bank_order` picks the new position up on the next tick.
- [ ] **Step 5:** `defer_current()` wrapper beside the other two.

### Task 2: The route

**Files:** `web.py` (action map), `tests/test_web.py`

- [ ] **Step 1: failing tests** — `POST /player/defer` reaches `dj.defer_current`; an unknown action still 404s (`web.py:106`); a dead speaker degrades through `call_player` rather than 500ing.
- [ ] **Step 2:** one entry beside `skip_later` and `skip_done`: `"defer": dj.defer_current`.

### Task 3: The flap row

**Files:** `templates/board.html`, `tests/test_web.py`

- [ ] **Step 1: failing test** — the board markup carries a button with `data-player="defer"`, placed before `skip_later`.
- [ ] **Step 2:** add the row first in the flap, matching the existing label-plus-`<i>` shape: `Defer <i>back to up next</i>`. No new class — it is the non-destructive one, so it takes the default styling and `danger` stays on Done alone.
- [ ] **Step 3:** check the flap at three rows on phone width; it is `min-width: 230px` and absolutely positioned, so confirm it neither clips nor pushes the transport row.

## Verification

1. `pytest` green
2. Deploy; with fresh news available, hit Defer mid-episode — news plays, and the deferred row sits directly after the news block, pinned
3. Let it come back around: confirm it resumes ~10s before the banked position rather than from 0:00
4. Defer with no news queued — lands after exactly one item, never replays itself immediately
5. Refresh all with a deferred row present — it survives
6. Flap reads cleanly at three rows on phone width
