# A Deeper, Hand-Orderable Up Next — Implementation Plan

**Design:** `docs/design/specs/2026-07-23-up-next-depth-reorder-design.md`

**Goal:** Take Up Next from three read-only rows to ten that can be reordered by hand and filled from the stations page.

**Architecture:** `QUEUE_AHEAD` moves 3 → 10, and `_top_up` splits into claim → stage-concurrently → enqueue so depth costs one request of latency instead of ten. A new `move_in_queue` reorders by remove-and-re-add on the stored `play_uri`. `play_episode` gains a `"last"` mode that appends. The board grows a pointer-drag (and keyboard) reorder on Up Next rows.

**Tech Stack:** Python 3.12, Flask, SQLite, pytest, vanilla JS — no build step, no frontend test harness.

## Global Constraints

- Follow existing conventions exactly: type hints throughout, `{"ok", "error"}` JSON via `result()`, mutation routes through `call_player` so a dead speaker degrades instead of 500ing.
- The Sonos queue stays the single source of truth. No new schema, no new columns — nothing about the arrangement persists across off-air.
- News keeps riding first. `tick()`'s insertion at `cur_idx + 1` is untouched.
- No frontend test harness — Task 4 is verified with the Flask test client for markup plus hand-tracing. Do not start a live server in the background.
- Never probe the DJ lock with `/player/volume`: for a grouped speaker that reads the members' average and SetGroupVolume rescales with integer rounding, so posting the value back ratchets the house down. Use `/player/group_all`.

---

### Task 1: Depth — claim, stage concurrently, enqueue

**Files:** `config.py` (default), `dj.py` (`_top_up` and two new helpers), `tests/test_dj.py`, `tests/test_config.py`

**Interfaces produced:** `_claim_batch(count) -> list[Row]`, `_stage_batch(episodes, player) -> tuple[list[tuple], list[Row]]`

- [ ] **Step 1: failing tests** — a batch of N claims N *distinct* episodes; a claimed episode is `queued` with a null `play_uri` and matches no queue slot; staging runs concurrently (assert via a stubbed `_stage_uri` that records overlap, never wall-clock); a partially-failing batch marks only the failures skipped; a wholly-failing batch reverts every episode to `new` and stops.
- [ ] **Step 2:** `QUEUE_AHEAD` default `"3"` → `"10"`; update `test_config`.
- [ ] **Step 3:** add `_claim_batch` — calls `pick_next`, then `mark_queued(id, None)` immediately so the next pick can't return the same episode.
- [ ] **Step 4:** add `_stage_batch` — `ThreadPoolExecutor(min(_STAGE_WORKERS, len(episodes)))` over the existing `_stage_uri`, returning staged and failed in pick order.
- [ ] **Step 5:** rewrite `_top_up` as claim → stage → enqueue. Replace `_MAX_CONSECUTIVE_ENQUEUE_FAILURES` with the batch rule: a wholly-failed batch reverts to `new` (systemic outage — don't burn ten episodes) and stops; partial failures are marked skipped as before.

### Task 2: Reorder

**Files:** `dj.py` (`move_in_queue`), `web.py` (route), `tests/test_dj_controls.py`, `tests/test_web.py`

- [ ] **Step 1: failing tests** — move up, move down, to first, to last, onto itself (no-op), clamped out-of-range, the current track (refused), an unqueued episode (refused); `play_uri` unchanged and no re-stage; arrangement survives a following `tick()` top-up while tick-inserted news still lands ahead of it.
- [ ] **Step 2:** `move_in_queue(episode_id, to_position)` — resolve `from_idx` among slots after the needle, map `to_position` through the matched-slot list to `to_idx`, then `remove_from_queue(from_idx)` + `add_to_queue(play_uri, title, show, to_idx)`.
- [ ] **Step 3:** `POST /queue/reorder` through `call_player`, validating `episode_id` and `to_position` as ints.

### Task 3: Add to Up Next

**Files:** `dj.py` (`play_episode`), `web.py` (route), `templates/stations.html`, `tests/test_dj_controls.py`, `tests/test_web.py`

- [ ] **Step 1: failing tests** — `mode="last"` appends to the end; starts playback when off air; moves rather than copies an episode already queued (the existing duplicate sweep); updates `last_feed_id`.
- [ ] **Step 2:** widen the mode literal to `["next", "now", "last"]`; `insert_at = None` for `"last"`.
- [ ] **Step 3:** `POST /episodes/<id>/play_last`; add an **Add to Up Next** button to the stations episode row.

### Task 4: Frontend reorder

**Files:** `templates/board.html`

- [ ] **Step 1:** render a grip handle per Up Next row — a real `<button>` carrying its position, so it is focusable and labelled.
- [ ] **Step 2:** pointer-events drag (`pointerdown`/`pointermove`/`pointerup` + `setPointerCapture`), not HTML5 DnD, which never fires on touch.
- [ ] **Step 3:** suppress the `#departures` re-render while a drag is live, following the `volDrag` precedent — the 5s poll rewrites that container wholesale and would tear the row out from under the pointer.
- [ ] **Step 4:** ArrowUp/ArrowDown on the focused handle moves the row — the cheap path to keyboard and assistive-tech support.
- [ ] **Step 5:** reorder optimistically, `POST /queue/reorder`, let the next poll reconcile.

## Verification

1. `pytest` green
2. Deploy; confirm ten rows, and measure `start()`'s lock hold with concurrent `/player/group_all` requests — expect seconds, not tens of seconds
3. Drag on desktop and phone; arrangement holds across two polls and one tick top-up
4. Add an episode from the stations page; confirm it lands last
5. Reorder using only the keyboard
