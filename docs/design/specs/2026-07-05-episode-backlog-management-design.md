# Episode backlog management — design

**Date:** 2026-07-05
**Builds on:** `2026-07-05-sonos-talk-radio-design.md` (the DJ engine, DB schema, and Flask/dashboard patterns this design extends).

## Goal

Today the dashboard only shows per-station aggregate counts (new/played/archived) and one bulk "Add back catalog" button that releases *every* archived episode for a feed at once. There's no way to see individual episodes or force a specific one to play. This adds:

1. A way to browse a station's full episode list and release specific archived episodes (individually or via multi-select), instead of only all-at-once.
2. A way to force-play a specific episode — "play next" (after the current track) or "play now" (interrupt immediately) — regardless of the episode's current status.
3. A global search across all stations' episodes by title or show name, plus a per-station local search — for "find that mothman episode" without knowing which show it's on.

Everything here is additive to the existing rotation/news/wake-schedule behavior in `dj.py` — none of that changes.

## Data layer (`db.py`)

New read methods, all following the existing `episodes_for_feed` ordering (`published_at DESC, id DESC`), fixed page size 25:

- `count_episodes_for_feed(feed_id, q=None) -> int`
- `episodes_for_feed_page(feed_id, page, page_size, q=None) -> list[Row]` — optional `LIKE` filter on episode title
- `count_search_episodes(q) -> int`
- `search_episodes(q, page, page_size) -> list[Row]` — joins `episodes` to `feeds`; matches episode title **or** feed title, so searching a show name (not just a topic word) also works; includes `feed_title` in the result rows

New write methods:

- `release_episode(episode_id) -> bool` — sets `status='new'` only if currently `archived`; no-op otherwise (mirrors the guard already implicit in `unarchive_feed`, scoped to one row)
- `release_episodes(episode_ids: list[int]) -> int` — bulk version for multi-select; returns count actually released

No schema changes — the existing `episodes` table already has everything needed (`title`, `status`, `published_at`, `feed_id`).

## API (`web.py`)

- `GET /api/feeds/<id>/episodes?page=&q=` — one feed's episodes, paginated + searchable
- `GET /api/episodes/search?q=&page=` — global search across all feeds
- `POST /episodes/<id>/release` — unarchive one episode
- `POST /episodes/release` (body `{ids: [...]}`) — bulk unarchive
- `POST /episodes/<id>/play_next` — queue right after the current track
- `POST /episodes/<id>/play_now` — interrupt and play immediately

The existing `POST /feeds/<id>/unarchive` (release-all) is unchanged and stays available alongside the new granular controls.

All new mutation routes follow the existing `result()`/`call_player()` conventions in `web.py` (JSON `{ok, error}`, dead-player exceptions turned into a graceful error response rather than a 500).

## DJ engine (`dj.py`)

One new public method:

```
def play_episode(self, episode_id: int, mode: Literal["next", "now"]) -> str | None
```

Behavior, under the existing lock:

1. Look up the episode; return an error string if it doesn't exist.
2. If `dj_state != "playing"`, call `self.start()` first — reuses the existing on-air bootstrap (news → resume → rotation top-up) unchanged. This is how off-air force-play "just works": the station comes on air normally, and the chosen episode is inserted immediately after.
3. Re-run `_match_queue()` to find the current queue index (`cur_idx`).
4. `_enqueue(player, episode, index0=cur_idx + 1)` — stages the chosen episode right after the current track. Works regardless of the episode's prior status (new, archived, played, or skipped); `mark_queued` doesn't care what status it was.
5. **mode="next"**: done. The episode plays after the current one finishes, like the existing news-insertion pattern.
6. **mode="now"**: if a track was already playing at the old `cur_idx`, call the existing `_skip(done=False)` path (same as the "Skip · later" button) — it reverts that episode to `new` and clears its resume position ("replays fresh another day"), then advances playback to `cur_idx + 1`, which is now the chosen episode. If nothing was playing (e.g. queue was empty), just `player.play_from_queue(insert_index)` directly.
7. `kv_set("last_feed_id", str(episode["feed_id"]))` — so the automatic rotation's no-repeat-show rule accounts for the manual pick too, and won't immediately replay the same show right after.

No changes to `tick()` — it already reconciles the queue purely from episode `status` + `play_uri` matching, which manual and automatic queuing both produce identically.

## Frontend (`templates/index.html`)

**Global search** — a new "Find an episode" section above Stations. Debounced (~300 ms) search input, results list, Prev/Next pagination. Each result row shows show name, title, status badge, and action buttons. Entirely independent of the 5-second status poll — it only fetches on typing/pagination/action clicks.

**Per-station panel** — each station row gets an "Episodes ▸" toggle. Clicking it expands a panel nested under that row (accordion style, one open at a time) with:
- a local search input scoped to that feed
- the same paginated episode-row rendering as global search, minus the show badge
- a "Select" toggle that adds checkboxes to *archived* rows and a "Release selected (N)" button. Selection persists across pagination within a station's panel (accepted during final review as more useful than page-scoping, with low risk since only archived rows are selectable and releasing one not currently visible is harmless)

Both panels share one `episodeRow(episode, {showBadge})` renderer, which embeds the same button set inline: **Play next**, **Play now**, and **Release** (shown only when `status === "archived"`) — implemented as one function rather than a separate `episodeActions()` helper, since splitting it out added no reuse benefit.

**Required rendering change**: today `render()` fully replaces `#stations`' innerHTML from `status.stations` on every 5-second poll. If an open episode panel's search `<input>` lived inside that replaced subtree, the poll would destroy/recreate it every 5 s, killing focus and in-progress typing. So station-list rendering changes from full replace to a **keyed patch**: each station `<li>` is keyed by feed id. The poll updates only that row's count/badge text and button states in place, and adds/removes rows for added/deleted feeds — it never tears down a station's open episode-panel subtree. Up Next, Recently Played, and Schedules keep their current simple full-replace rendering; they have no persistent interactive sub-state to protect.

## Error handling

- Unknown episode id on any new route → `404` with `{"ok": false, "error": "no such episode"}`, matching the existing `feed_action`/`schedule_action` 404 pattern.
- `play_next`/`play_now` go through the same `call_player()` wrapper as other transport actions — a dead/unreachable speaker degrades to a JSON error, not a 500, and invalidates the cached player exactly like today's transport routes.
- `release`/`release` (bulk) on a non-archived episode is a silent no-op per episode (not an error) — bulk selection may legitimately include a mix, and the UI already only shows checkboxes on archived rows, so this is a defensive guard, not an expected path.

## Testing

- `db.py`: pagination correctness (page boundaries, `q` filtering by title, search matching feed title vs episode title), `release_episode` guard (no-op on non-archived), `release_episodes` bulk count.
- `dj.py` (via `FakeSonosPlayer`, matching the existing `tick()`-focused test style): `play_episode` mode="next" inserts without disturbing current playback; mode="now" recycles the interrupted episode to `new` with cleared resume (asserting identical behavior to `skip_later`) and jumps to the chosen episode; off-air force-play calls `start()` first and still lands on the chosen episode; `last_feed_id` is updated after a manual pick.
- `web.py` (Flask test client): new routes return correct JSON shapes, 404 on unknown episode/feed ids, pagination query params respected.
- No frontend test harness exists in this repo today (dashboard is untested vanilla JS) — this design doesn't add one; the keyed-patch rendering change should be verified manually (expand a station, type in its search box, confirm it doesn't lose focus across a poll tick) per this repo's `verify` skill before merging.

## Verification

1. Full pytest suite green.
2. Manual, live against the dashboard: expand a station with an archived backlog, release one episode individually, then a few via select-mode; search globally for a term present in one show's episode titles and confirm the right show/episode surfaces; force "play next" and confirm it plays after the current track; force "play now" on a different episode and confirm the interrupted one reappears in the `new` pool later; force-play while off air and confirm it comes on air with the chosen episode; confirm typing in an expanded station's local search box survives multiple 5-second poll ticks without losing focus.
