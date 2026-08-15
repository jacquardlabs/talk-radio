# Station categories & rotation filtering — design

**Date:** 2026-07-06
**Builds on:** `2026-07-05-sonos-talk-radio-design.md` (rotation engine this extends) and the LED departure-board template's existing list/toggle conventions (`.svc-row`, `.tab small`, `data-confirm`).

## Goal

With 50 stations now subscribed, the rotation mixes philosophy, AI/tech, comedy, history, and news indiscriminately. This adds a genre classification per station (History, AI & Tech, Comedy & Trivia, etc.) and a per-category on/off switch that controls whether that category's stations participate in automatic rotation — so, for example, Comedy & Trivia can be turned off for a focused afternoon without disabling or deleting those stations.

Scope, confirmed during brainstorming:
- One classification per station (not multi-tag) — every station researched sorted cleanly into exactly one bucket.
- Categories are freely editable (add/rename/remove) — not a fixed built-in list.
- Category state (name, rotation-enabled) lives in its own table, not a text field plus a settings blob — this is a genuine one-to-many relationship with its own per-category attribute.

## Data model (`db.py`)

```sql
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    rotation_enabled INTEGER NOT NULL DEFAULT 1
);
```

`feeds` gains `category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL`. `NULL` means "uncategorized." Uncategorized feeds stay rotation-eligible by default — opt-out, not opt-in, consistent with how `enabled`/`is_news` already default to "on." Deleting a category unassigns its feeds via the FK's `ON DELETE SET NULL` rather than deleting the feeds themselves or blocking the deletion.

No categories are pre-seeded in the schema. This is a personal classification scheme specific to one person's subscriptions, not something that belongs in the app's default install. The 6 categories (History; Philosophy & Literature; AI & Tech; Comedy & Trivia; Business & Law; Science & Curiosity) and the assignment of the current 50 stations to them happens as a one-time data-setup pass against the live server after this ships — the same kind of operational step as the earlier OPML import, not a code feature.

## Backend

New `db.py` methods, following the existing toggle/delete/list conventions:

- `add_category(name) -> int` — raises on duplicate name (UNIQUE constraint); the route catches and returns a friendly error, not a 500
- `list_categories() -> list[Row]` — includes a live station count per category (`LEFT JOIN` + `COUNT`, matching how `counts_by_feed()` already aggregates)
- `rename_category(category_id, name) -> None`
- `toggle_category_rotation(category_id) -> None` — flips `rotation_enabled`, same shape as `toggle_feed`/`toggle_schedule`
- `delete_category(category_id) -> None`
- `set_feed_category(feed_id, category_id: int | None) -> None` — assign, change, or clear (`None`) a feed's category

**Rotation filtering** — `rotation_feeds_with_new()` gains one clause:

```sql
SELECT f.* FROM feeds f
LEFT JOIN categories c ON c.id = f.category_id
WHERE f.enabled=1 AND f.is_news=0
  AND (f.category_id IS NULL OR c.rotation_enabled=1)
  AND EXISTS (SELECT 1 FROM episodes e WHERE e.feed_id=f.id AND e.status='new')
ORDER BY f.id
```

News feeds are already excluded from this query (`is_news=0`), so a category's rotation state is simply irrelevant to news feeds — no special-casing needed.

**New routes**, matching the existing `/feeds/<id>/<action>` and `/schedules/<id>/<action>` dispatch style:

```
POST /categories                    body: {name}          → create
POST /categories/<id>/toggle                               → flip rotation_enabled
POST /categories/<id>/rename        body: {name}           → rename
POST /categories/<id>/delete                                → delete (feeds unassigned, not removed)
POST /feeds/<id>/category           body: {category_id}    → assign/change/clear (null clears)
```

`dj.status()` gains a `"categories"` list (id, name, rotation_enabled, station count) and each station entry in the existing `"stations"` list gains `category_id`. Embedded in the existing status payload rather than a separate fetch-on-demand endpoint — the category list is small and needed on every poll to render both the Categories section and every station's dropdown, unlike the paginated episode-search endpoints, which are deliberately separate due to their size.

## Frontend

**New "Categories" section**, matching the existing eyebrow+list pattern (Stations, Wake schedule, Recently played): each row shows the category name, a live station count, an On/Off rotation toggle (identical mechanics to the existing wake-schedule On/Off toggle), a Rename action, and a Remove action. Rename uses a native `prompt()` dialog pre-filled with the current name — no new inline-edit state machinery, consistent with this app having no modals anywhere else. Remove reuses the existing `data-confirm` pattern already used for station/schedule deletion (e.g. "Remove History? 10 stations will become uncategorized."). A one-line add-category form (text input + button) matches the existing add-feed/add-schedule form style.

**Per-station category dropdown**: each row in the existing Stations list gains a `<select>` (native selects are already globally styled — no new CSS needed) populated with "Uncategorized" plus every category name, pre-selected to the station's current `category_id`. Changing it immediately posts to `POST /feeds/<id>/category`, same fire-and-repoll pattern as every other per-station control.

## Error handling

- Duplicate category name → friendly error, not a 500 (matches `add_feed`'s existing pattern).
- Unknown category or feed id on any route → 404, matching `feed_action`/`schedule_action`.
- `set_feed_category` with a nonexistent `category_id` hits the existing foreign-key constraint (this app already runs with `PRAGMA foreign_keys=ON`) and raises — caught and returned as a friendly error the same way.

## Testing

- `db.py`: category CRUD, station-count aggregation, delete unassigns feeds (not destroys them), `rotation_feeds_with_new` excludes rotation-disabled categories while still including uncategorized feeds.
- `dj.py`: a regression test showing `pick_next()` never selects from a rotation-disabled category while still rotating through others.
- `web.py`: new routes (CRUD + 404s), `status()` payload includes the categories list and per-station `category_id`.
- Frontend: no test harness (existing, accepted project constraint) — manual/hand-traced verification, the same approach used for prior UI work in this app.

## Verification

1. Full pytest suite green.
2. Manual: create a category, assign a few stations to it via the dropdown, toggle its rotation off, confirm those stations' episodes stop appearing in "Up next" / departures while other categories keep rotating; toggle back on and confirm they return; rename a category and confirm the station dropdowns and Categories section both reflect the new name; delete a category with stations assigned and confirm those stations become "Uncategorized" (not deleted) and remain in rotation.
