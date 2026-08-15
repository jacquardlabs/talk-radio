# A deeper, hand-orderable Up Next — design

**Date:** 2026-07-23
**Builds on:** `2026-07-05-sonos-talk-radio-design.md` (the DJ engine and queue reconciliation this extends) and `2026-07-06-episode-backlog-led-reskin-design.md` (the board/stations two-page dashboard this adds controls to). Assumes the queue-needle and wake work on `fix/needle-index-during-sonos-transition` — in particular `current_index()` and the duplicate sweep in `play_episode`.

## Goal

Up Next is three rows deep and read-only. Three changes make it a queue you can actually work with:

- **Ten deep** instead of three, so the morning's plan is visible rather than implied
- **Reorderable by hand**, by dragging rows on phone or desktop
- **Fillable from the backlog**, so any episode on the stations page can be sent to the end of Up Next

The rotation engine, news-first, resume, and skip semantics are untouched. What changes is how much of the queue is staged, and who gets to arrange it.

## Queue depth (`config.py`, `dj.py`)

`QUEUE_AHEAD` default moves `3` → `10`.

Depth is expensive today because `_top_up` interleaves picking and staging: pick one episode, resolve its URL over the network, enqueue it, repeat. Picking is DB-only and fast; resolving is `audio.resolve_audio_url` — an HTTP HEAD with a 20s timeout and a ranged-GET fallback — and it is the whole cost. Measured 2026-07-23 on the live library: a `start()` staging ~5 episodes held the DJ lock ~14s, so ~2.8s per episode. At depth 10 that is ~30s of frozen transport controls at every start and every 8am wake.

So `_top_up` splits into three phases:

1. **Claim** the N episodes needed, sequentially. Each pick calls `pick_next` and immediately claims the result with the existing `mark_queued(id, play_uri=None)`.
2. **Stage** all N concurrently through a bounded `ThreadPoolExecutor` calling the existing `_stage_uri`.
3. **Enqueue** them onto Sonos in pick order, recording the real URI with `mark_queued(id, uri, local_path)`.

The claim is what makes batching safe. `pick_next` only considers `status='new'`, so picking ten before enqueueing any would otherwise return the same episode repeatedly — a feed's oldest unplayed episode does not change until something marks it. Claiming flips it to `queued` at pick time, which removes it from later picks with no new exclusion parameter threaded through `pick_next`.

A claimed-but-unstaged episode is `queued` with a null `play_uri`. `_match_queue`'s `match()` already skips rows with a falsy `play_uri`, so the transient state is invisible to reconciliation rather than corrupting it. Recovery needs nothing new: `revert_all_queued()` on the next `start()` or `stop_off_air()` clears any claim orphaned by a crash, and if every claim were left unstaged, tick's hijack detection would see no matched slots and revert.

Staging concurrency is bounded (`_STAGE_WORKERS = 8`) rather than unbounded — in download mode `_stage_uri` writes whole episodes to disk, and ten simultaneous downloads is a different proposition from ten HEAD requests.

`_MAX_CONSECUTIVE_ENQUEUE_FAILURES` loses its meaning once staging is concurrent, because failures arrive together rather than in sequence. It becomes a batch rule: if every episode in a batch fails to stage, stop for this tick rather than picking another batch. The intent is unchanged — a systemic outage must not drain the catalog in one tick.

This also cuts the lock-hold time that already exists at depth 3, which is the residual flagged after the wake work: `start()` stages under `self._lock`, and that lock serializes every transport control the dashboard can send.

**Accepted cost:** ten staged episodes are ten episodes out of rotation. They read as `queued`, so they leave the unplayed counts on the stations page until played or reverted. At depth 3 this is invisible; at 10 it is noticeable on small shows.

## Reorder (`dj.py`, `web.py`)

SoCo exposes no reorder for the playback queue — `reorder_sonos_playlist` operates on saved playlists, not `Q:0`. A move is therefore `remove_from_queue` followed by `add_to_queue` at the target index, reusing the episode's stored `play_uri`. No re-resolution, no network, no `mark_queued`: the row is already correct.

New method:

```
move_in_queue(episode_id: int, to_position: int) -> str | None
```

`to_position` is an index **within Up Next**, zero-based, not a raw Sonos queue index. The frontend never has to reason about finished tracks still sitting at the head of the queue — tick marks them played but never removes them, so the raw indices drift from what the user sees. Internally the method resolves both endpoints against the same `_match_queue` snapshot:

- `from_idx` — the absolute index of the episode among slots after the needle. Absent → `"That episode isn't in Up Next"`, matching `drop_from_queue`'s wording.
- `to_idx` — `[i for i in range(cur_idx+1, len(matches)) if matches[i] is not None][to_position]`, with `to_position` clamped to the list.

Then `remove_from_queue(from_idx)` and `add_to_queue(..., index0=to_idx)`. `insert_at = to_idx` is correct in both directions: moving down, removing the earlier slot shifts the target down one, so inserting at the original `to_idx` lands the row just after it; moving up, nothing below shifts and the row lands at `to_idx` directly. Moving an episode onto its own position is a no-op that returns `None`.

The current track and anything before it are not movable — that is what Skip is for.

Route: `POST /queue/reorder` with `{"episode_id": int, "to_position": int}`, through the existing `call_player` wrapper so a dead speaker degrades to `{"ok": false, "error": ...}` rather than a 500.

## Add to Up Next (`dj.py`, `web.py`, `templates/stations.html`)

`play_episode`'s mode literal gains `"last"`: instead of inserting at `cur_idx + 1`, append to the end of the queue (`_enqueue(player, episode, None)`). Everything else in that method applies unchanged — the on-air bootstrap through `start()`, the `last_feed_id` update so rotation does not immediately repeat the show, and the duplicate sweep, so adding an episode already in Up Next moves it to the end rather than queueing a second copy.

Route `POST /episodes/<id>/play_last`, named for consistency with its `play_next` / `play_now` siblings even though the button reads **Add to Up Next**.

Stations page: a third `.tab small` button in the episode row's action group, beside Play next and Play now.

## Frontend (`templates/board.html`)

Rows in `#departures` get a grip handle and become draggable via **pointer events** (`pointerdown` / `pointermove` / `pointerup` with `setPointerCapture`), not HTML5 drag-and-drop. HTML5 DnD does not fire on touch, and the board is used as a phone PWA where the transport docks to the bottom under 760px.

The drag follows the pattern `volDrag` already establishes for the volume slider:

- A module-level `dragState` holds the episode id, the grabbed row, and the current target position.
- While a drag is live, `renderPage` skips rewriting `#departures`. The 5s poll rewrites that container wholesale, and a re-render mid-drag would tear the row out from under the pointer. The volume slider already guards this way (`if (!volDrag) setVol(...)`), and the category shelf guards with a signature check — this is the same idea.
- On drop, the row is reordered optimistically in the DOM, `POST /queue/reorder` fires, and the next poll reconciles against server truth.

Rows are keyboard-reorderable too: the handle is a `<button>` carrying the row's position, with ArrowUp/ArrowDown moving it. This is what makes the feature usable without a pointer, and it is a much cheaper path to correctness than emulating drag semantics for assistive tech.

Visual treatment follows the existing amber LED language and the board's two-voice typography: the grip is machine chrome, the dragged row lifts using the established `.glow` treatment, and the drop position is shown by moving the row itself rather than drawing a separate insertion marker — the row list is short enough that the arrangement reads directly.

## What does NOT change

- Rotation, `pick_next`, the arc guard, and per-show playback modes
- News-first: tick still inserts fresh news at `cur_idx + 1`, ahead of any hand-made arrangement. Top-ups still append below it. The relative order of everything else survives both.
- Resume, skip, drop, and the wake schedule path
- Persistence: an arrangement lives on the Sonos queue for the current session only. Going off air, the 8am wake, or a hijack rebuilds the queue and the order is gone. No new schema, no new column, no interaction with news-first or resume to specify.

## Testing

Engine, against `FakeSonosPlayer` (no Sonos required):

- `_top_up` claims before staging: a batch of N yields N distinct episodes, never a repeat, and the queue order matches pick order
- A claimed episode with a null `play_uri` matches no queue slot, and `revert_all_queued()` clears an orphaned claim
- A batch where every stage fails marks those episodes skipped and does not pick another batch
- Staging runs concurrently — assert via a stub `_stage_uri` recording overlap, not by wall-clock timing
- `move_in_queue`: up, down, to first, to last, onto itself, clamped out-of-range, an episode that is the current track, and one not queued at all
- A move preserves `play_uri` and does not re-stage
- Order survives a following `tick()` top-up; news inserted by tick still lands ahead of the arrangement
- `play_episode(mode="last")`: appends, starts playback when off air, and moves rather than copies an episode already queued

Web: `/queue/reorder` argument validation (missing/non-integer fields), 404 on an unknown episode, and graceful degradation when the speaker is unreachable.

## Verification

1. `pytest` green
2. Deploy, confirm Up Next renders ten rows and `start()` no longer holds the lock for tens of seconds — measure with concurrent requests against a lock-taking endpoint, but **not** `/player/volume`: for a grouped speaker that reads the members' average and Sonos's SetGroupVolume rescales with integer rounding, so repeatedly posting the value back ratchets the whole house down. Use `/player/group_all`, which takes the same lock and is idempotent once grouped.
3. Drag a row on desktop and on the phone; confirm the arrangement holds across at least two 5s polls and one tick top-up
4. Add an episode from the stations page and confirm it lands at the end
5. Reorder with the keyboard alone

## Risks

- **Drag interaction is the bulk of the work.** Depth and Add-to-Up-Next are contained; the pointer drag plus poll suppression is where the risk sits. If it proves fiddly, ↑/↓ buttons deliver the same capability and the drag can follow.
- **Claim-then-stage widens a window** in which an episode is `queued` but not on the Sonos queue. It is inside `_top_up` under the lock, and `status()` — which does not take the lock — can observe it. The consequence is a station's unplayed count being low by a few for the duration of a staging batch. Acceptable; the alternative is threading exclusions through `pick_next`.
- **Ten-deep rotation drain** on small shows, as noted above.
