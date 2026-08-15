# Defer — put the playing episode back in Up Next, not the backlog — design

**Date:** 2026-07-30
**Builds on:** `2026-07-23-up-next-depth-reorder-design.md` (the remove-and-re-add that stands in for a Sonos queue reorder, and `move_in_queue`'s index arithmetic) and the durable Up Next in `f29e987` (the saved order, `pinned`, and the resume seek this leans on entirely).

## Goal

The morning case: a podcast is on, fresh news has landed, and the listener wants
the news now and the podcast afterwards — from where they left off, not from the
top.

Neither existing skip does this. `skip_done` retires the episode. `skip_later`
sends it back to rotation and explicitly throws the position away
(`set_resume(episode["id"], None)` — "replays fresh another day"). Both are
correct for what they are; neither is "hold my place."

Defer is the third disposition. Same prologue, and a tail that preserves each
thing `skip_later` deliberately tears down.

Nothing about rotation, news-first, or the resume seek changes. What changes is
that there is now a way to stop listening to something without deciding you are
done with it.

## The control (`templates/board.html`, `web.py`)

The Skip flap gains a third row, first in the list — the flap already runs
least-destructive to most, and defer is the only one of the three that keeps
everything:

```
DEFER  back to up next
LATER  back in rotation
DONE   never play again
```

It carries `data-player="defer"`, which is the whole frontend change: the flap's
buttons already route through the generic `data-player` handler. `web.py`'s
action map gains one entry beside `skip_later` and `skip_done`:

```python
"defer": dj.defer_current,
```

## Three dispositions, one prologue (`dj.py`)

`_skip(done: bool)` becomes:

```python
def _skip(self, disposition: Literal["later", "done", "defer"]) -> str | None
```

with `skip_later()`, `skip_done()`, and a new `defer_current()` as its three
one-line wrappers.

The prologue is untouched, because it is the part that is subtle and already
right: take one `_match_queue` snapshot, guard that the current track is a
station episode, top up *first* when this is the last track so there is
something to advance to, advance or stop, then remove the outgoing slot. Only
the tail branches.

| disposition | tail |
| --- | --- |
| `done` | `_finish(episode)` |
| `later` | `revert_to_new` + `remove_from_up_next` + `set_resume(None)` + clear `resume_episode_id` |
| `defer` | re-add the staged URI after the news block, `set_resume(episode, cur.position)`, `set_pinned(True)` |

Defer's tail changes no status and touches the saved order not at all. The
episode was `queued` and in `up_next_order` before; it stays both. `_bank_order`
re-banks the running order on the next tick from the Sonos queue itself, so the
new position propagates to the durable list with no separate bookkeeping.

It arrives pinned for the same reason a hand-queued row does: an episode you
were listening to a second ago is one you chose, and losing it to a Refresh all
because you did not also think to pin it is the silent loss the pin exists to
prevent.

The re-add reuses the episode's stored `play_uri` rather than re-resolving it —
the same reasoning as `move_in_queue`. The row is already staged and correct,
and a second resolve would hand back a different expiry token.

## Where it lands

Count the consecutive news episodes starting at `cur_idx + 1`; call it `n`.
News is read the way `tick()` already reads it, `get_feed(ep["feed_id"])["is_news"]`,
not from the episode row. The scan stops at the first slot that is not a matched
news episode — an unmatched slot ends the block too, rather than being skipped
over, so the count never reaches past a gap it cannot identify.

```
offset = max(1, n)
dest   = cur_idx + offset
```

`dest` needs no adjustment after the removal. Taking the outgoing row out first
shifts everything above it down one, so inserting at the original index lands
just after the block — the same behaviour `move_in_queue` documents for moving a
row down. If `dest` lands past the end of the shortened queue, append instead.

`P v NP` playing, Up Next `[News1, News2, EP29]`, so `n = 2`, `offset = 2`:

```
before      P  News1  News2  EP29
remove         News1  News2  EP29
insert@2       News1  News2  P     EP29
```

**The floor is load-bearing.** With no news ahead, `n = 0` and a literal "after
the news block" would place the episode at Up Next position 0 — making it the
next track, so advancing would play it again immediately and defer would be an
elaborate no-op. Flooring the offset at 1 lands it after exactly one item, which
is the weakest placement that still means "not right now."

## The return trip

Defer writes `set_resume(episode["id"], cur.position)` from the snapshot
`_match_queue` already returned, rather than calling `_save_resume()`. That
helper is the right idea and the wrong instrument here: it takes its own second
live read, and `_match_queue`'s docstring is explicit that a second read risks
pairing this snapshot's episode identity with a different track's position.
Defer is holding a good snapshot already.

Everything after that is existing machinery. When the deferred episode comes
back around, `tick()` sees `is_new_current` with a non-null `resume_seconds`,
seeks to `resume_seconds - 10`, and clears it. No new resume path, and the
10-second lead-in the rest of the station uses applies here too.

`resume_episode_id` is left alone. `tick()` rewrites it every tick with whatever
is current, and `start()` restores from `up_next_order()` rather than that key.

## Edge cases

**No speaker, or the current track is not a station episode.** Existing prologue
guards, unchanged.

**Nothing ahead and the rotation pool dry.** The prologue tops up first; if that
finds nothing, the station stops. The tail still runs: `dest` lands past the end
of the shortened queue, so the episode is appended to a stopped queue, which
costs nothing because `start()` clears the Sonos queue and re-stages from the
saved order anyway. What carries across is the resume position and the place in
that saved order, and going back on air resumes it there. No special handling is
needed to get the ordering right on the return trip either: `start()` stages
fresh news *before* the saved order, so the next session opens news-then-podcast
on its own.

**Deferring a news episode.** Allowed. `n = 0` for a news episode at the head of
its own block, so the floor puts it after one item.

**Deferring twice.** The second defer moves it past whatever block is ahead
then. Nothing accumulates.

## Tests

Extending `tests/test_dj_controls.py`, which already covers the two skips:

- lands after a multi-item news block
- falls back to one item when no news is ahead
- banks `resume_seconds` from the snapshot position
- stays `queued` and stays in `up_next_order`
- arrives pinned
- dry queue stops the player and keeps the resume position
- `skip_later` and `skip_done` behaviour is unchanged by the signature change

The last one matters more than it looks: the whole risk of this design is
turning a boolean into an enum on a function two other controls depend on.

## Accepted cost

A deferred episode is pinned, so Refresh all leaves it. Defer often enough
without unpinning and Up Next accumulates rows a refresh cannot clear. The pin
is togglable per row, which is the existing escape hatch, but the accumulation
is real and worth watching before adding anything cleverer.
