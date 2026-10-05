# UX review: On air and Stations — design

**Date:** 2026-10-04

## Goal

Re-evaluate both pages by the jobs people do on them, not by how they look.
The trigger was the Pretty Heady Stuff station sheet: 164 episodes, 162 of them
labelled ARCHIVED, each row ragged and carrying two 44px buttons.

The LED two-voice identity (caps machine chrome, mixed-case content) and the
eight themes stay. Nothing here is a reskin; layout changes reuse existing
patterns (`.qrow`, `.flap`, `armed()`).

Evidence: full-page captures of the live instance (58 stations, 6 categories,
11 in Up Next) at 1280px and 390px, taken read-only on 2026-10-04.

## Jobs, by frequency

| Job | Who | How often (assumed) | Where it lives today | Measured by |
|---|---|---|---|---|
| Play / pause / volume / skip | listener | many times a day | Board transport | `POST /player/*`, `/player/volume`, `/player/seek` |
| See what's on and what's next | listener | daily | Board deck + Up Next | `GET /` |
| Hear a specific episode or show | listener | daily–weekly | Stations: global search, or tile → sheet → page through episodes | `GET /api/episodes/search`, `GET /api/feeds/<id>/episodes`, `POST /episodes/<id>/play_*`, `queue_arc` |
| Shape the mix (categories on/off) | listener | weekly | Board *In rotation* **and** every shelf header on Stations | `POST /categories/<id>/toggle`, split by page |
| Choose rooms | listener | weekly | Board top row, above the deck | `POST /api/speaker*`, `GET /api/speakers`, `POST /player/group_all` |
| "What was that I just heard?" | listener | weekly | Board basement, *Earlier* one-liner | `UI earlier` |
| Alarm, sleep timer | listener | set once / nightly | Board basement; power caret | `POST /schedules*`, `POST /player/sleep` |
| Add a station | self-hoster | monthly | Stations, bottom | `POST /feeds` (with `ok`), `GET /api/podcasts/search` |
| Per-station settings (news, order, pause, remove, back catalog) | self-hoster | rarely | Top of every station sheet, above the episodes | `POST /feeds/<id>/*` |
| Categories: add, rename, remove | self-hoster | twice a year | Every shelf header (*Edit ▾*) + permanent form at page bottom | `POST /categories*` |
| Diagnose a dead feed | self-hoster | rarely | Board basement (*Wouldn't play*), sheet counts | `UI trouble` |

Personas come from PRODUCT.md, which marks both `[inferred]`; the frequency
column is assumed, so the ranking below is provisional. The usage log
(`GET /api/usage?days=N`) counts every key in the last column; re-rank once it
holds a few weeks of data. The findings below are the places where frequency
and placement disagree.

## Findings, ranked

### F1. One concept, four names; one name, two concepts

`archived` is a single status (`db.py:50`): an episode outside the rotation pool.
Adding a station with *Latest episode* archives everything but the newest
(`feeds.py:366-369`). The UI calls it four things:

- **ARCHIVED** on each row
- **in the vault** in the sheet counts
- **Release** *out of the vault* to un-archive one (`db.py:692`)
- **Add back catalog** to un-archive all (`db.py:635`)

Meanwhile **Held** means both paused playback (deck, `board.html:142`) and a
paused station (tile badge and sheet counts, `stations.html:158,179`), whose own
button reads *Pause* / *Enable*.

Cost: a freshly added show reads as 162 dead episodes, and the first fix is
finding out what "archived" means. This is the page that prompted this review.

The board writes the same statuses under its own verbs: Skip ▾ / Drop ▾ *Done*
marks an episode `played` (`dj.py:945`, `dj.py:1035` via `_finish`), *Later*
returns it to `new` (`dj.py:947`). `skipped` is never a user's choice: stale news
(`db.py:432`) and dead links (`db.py:507`) set it.

Proposal: one listener-facing name per status, used on both pages.

| Status | Label | Verbs that produce it |
|---|---|---|
| `new` | (none; the default) | Skip/Drop *Later*, *Add to rotation* |
| `queued` | Up next | Play next / now / last, Queue series |
| `played` | Heard | playing through; Skip/Drop *Done* |
| `skipped` | Missed | stale news, dead link (system only) |
| `archived` | Back catalog | add station with *Latest* / *Last N* / *New only* |
| station `enabled=0` | Off | *Turn off* / *Turn on* |

*Release* becomes *Add to rotation*; *Add back catalog* becomes *Add all to
rotation*; "vault" goes. "Held" stays for paused playback only. A station
that is *Off* and a category with rotation off both mean "the DJ won't pick
from here"; the station one also silences news (`db.py:424`), so the two keep
separate words.

### F2. The episode list costs ~200px per row on a phone

Each `ep-row` is a wrapping flexbox (`base.html:425`). Where the date and buttons
break depends on title length, so no two rows share a shape. On a phone the
Pretty Heady Stuff page of 25 rows is 5,386px; on desktop, ~90px per row. The
status label repeats on every row and carries nothing when 162 of 164 match.
Paging is Prev / Next across 7 pages, newest first.

Proposal: rows on the Up Next grid. Title (2-line clamp), meta line below
(status only when it isn't the default, date, length), *Play next* and a `⋯`
flap in a right column. Group the list into the three sections the sheet
already counts (`stations.html:176-177`): **Unplayed** (`new` + `queued`),
**Back catalog** (`archived`), **Heard** (`played` + `skipped`). The label moves
from each row to the section head, and the episodes the DJ can actually pick
sit at the top.

The episodes endpoint pages newest-first across all statuses
(`db.py:656-668`), so page 2 would split a section. It needs to return each
section at its own page, in one request so opening a sheet stays one call.

```
UNPLAYED · 2
Jessica den Outer and Shauna Doll dig into the        [Play next] [⋯]
growing certainty that Nature has Rights
2026-10-01 · 62 MIN  ▾
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
BACK CATALOG · 162                               [Add all to rotation]
Matthew Huber wants to incite a class realignment     [Play next] [⋯]
to fight global heating & the tyranny of capital
2026-08-10 · 58 MIN  ▾
```

### F3. The sheet leads with admin

Opening a station shows the blurb, counts, then six controls (category, Mark
news, Pause, Random, Add back catalog, Remove station) before the first episode.
On a phone that header is a full screen. The daily job (pick an episode) sits
behind the rarest one.

Proposal: episodes first. Admin folds behind one *Settings ▾* in the sheet head,
using the existing `.flap`. Remove station keeps its `armed()` two-press.

### F4. Four search boxes, four scopes

Stations has *Search all stations by episode or show title*, *Filter stations by
name*, *Filter this station's episodes*, and *Podcast name* (the Apple
directory). Each finds a different thing; none says which.

Proposal: one box at the top of Stations. Typing shows matching **stations**
(tiles), then matching **episodes** (rows), then, when the library has few or
no hits, **Add from the directory** (the existing iTunes search). The per-station
filter stays inside the sheet, where its scope is obvious.

### F5. Shelves hide stations off the right edge

Shelves scroll horizontally. At 1280px, AI & Tech shows 9 of 14 and History 9
of 14; at 390px, 3 of each. Desktop Chrome with always-on scrollbars draws a
thin bar under each shelf; with overlay scrollbars (macOS trackpad default,
every phone and tablet) nothing says there are more.

Proposal: wrap shelves into a grid (58 tiles is not a lot), or keep the scroll
and show "+5" as the last visible tile. Wrapping is simpler and makes the
station filter less necessary.

### F6. No path from an episode to its show

Up Next rows, the deck, and *Earlier* name a show but don't link to it. "More of
this" means switching pages, finding the shelf, and opening the tile.

Proposal: the show name in the deck, Up Next meta, and *Earlier* opens
`/stations#feed-<id>`, which opens that sheet.

### F7. Up Next rows carry four controls each

Grip, pin, *Play now*, *Drop ▾* on every row × 11, dimmed to .35 until hover
on desktop, fully lit on touch. On a phone the controls drop to their own line,
roughly doubling each row. At rest on desktop the one pinned row of 11 is
nearly indistinguishable from the rest: its lit pin sits inside a `.qact` at
opacity .35. Pin only matters to *Refresh all*, and
hand-picked rows already pin themselves (`dj.py:1205`).

Proposal: grip and `⋯` per row; *Play now*, *Pin*, *Drop later*, *Drop for good*
move into the flap. Pinned rows keep a visible pin glyph in the meta line so the
state stays readable.

### F8. Rooms sit above what's playing

The *Output* row (rooms, Scan, Group all) is the first thing on the board, two
rows deep on a phone. Room changes happen weekly.

Proposal: one chip on the deck eyebrow, e.g. *Office + 2 ▾*, opening the
current checkbox list in a flap. Scan and Group all move inside it.

### F9. Rotation switches live on both pages

Category on/off appears as *In rotation* tiles on the board and as a *Rotation
on* button in each shelf header. The board copy is the listener's mix control.
The shelf copy duplicates it at full weight beside *Edit ▾*.

Proposal: keep it on the board. On Stations, show off-categories as dimmed
shelves (already done, `base.html:598`) with no button.

### F10. Paste URL accepts only RSS and shows the raw exception

Pasting a Pocket Casts page returned `could not add feed: 403 Client Error:
Forbidden for url: …` (`web.py:221`). The directory search would have found the
show; the error doesn't say so.

Proposal, cheapest first: (a) recognise non-feed URLs (Pocket Casts, Apple,
Spotify show pages) and reply "That's a show page, not a feed — search for it by
name instead"; (b) resolve Apple links through the iTunes lookup API to their
`feedUrl`.

### F11. Rare admin keeps permanent space

*Add category* is a permanent form at the bottom of Stations, and every shelf
carries *Edit ▾*. Categories change twice a year.

Proposal: move *Add station* and *Add category* behind one *Manage* control at
the top of Stations (or option B's Manage page).

## Information architecture options

### A. Keep two pages; fix in place

F1–F11 applied to the current pages. Stations stays the library and the admin
surface, with admin folded behind *Settings ▾* / *Manage*.

```
ON AIR                                  STATIONS
[deck · Office+2 ▾]                     [ one search box            ] [Manage ▾]
[transport]                             AI & TECH (grid, wraps)
UP NEXT  (grip · title · ⋯)             ▢ ▢ ▢ ▢ ▢ ▢ ▢
IN ROTATION                             ▢ ▢ ▢ ▢ ▢ ▢ ▢
basement                                ┌ sheet: episodes first, Settings ▾ ┐
```

Cost: medium. Touches `board.html`, `stations.html`, `base.html`; README
screenshots retaken. Server changes: per-section pages on the episodes
endpoint (F2), a show-page check in `web.py` add-feed (F10a), and Apple link
resolution in `feeds.add_feed` (F10b). Linking from a show name to its sheet
(F6) needs a `#feed-<id>` anchor that opens that sheet.

### B. Three pages by job: Listen / Library / Manage

Listen is today's board minus the basement admin. Library is shelves + sheets +
one search, with no admin. Manage holds add station, categories, per-station
settings as a table (58 rows: news, order, on/off, category, remove), alarms,
and *Wouldn't play*.

```
LISTEN            LIBRARY                    MANAGE
deck              [ search ]                 ADD STATION  [search | URL]
transport         shelves (grid)             STATIONS  table: name · cat · news · order · on · ✕
up next           sheet: episodes only       CATEGORIES  rename · remove · add
in rotation                                  ALARMS
                                             WOULDN'T PLAY
```

Cost: high. A third template and nav tab, sheet admin rewritten as a table,
tests and screenshots for all three. Gain: the listener never sees a red
*Remove station*, and per-station settings become comparable across stations,
which a sheet-at-a-time can't do.

### Recommendation

A, in three PRs ordered by what hurts most:

1. Vocabulary + episode rows + sheet order (F1, F2, F3). Fixes the page that
   started this.
2. One search + wrapped shelves + show links (F4, F5, F6).
3. Board: Up Next row menu, rooms chip, rotation de-dup, add-feed errors
   (F7–F10). Then F11.

B stays open if per-station settings keep growing; A's *Settings ▾* is the
seam it would grow from.

## Decisions (2026-10-04)

0. **Frequencies:** measure rather than guess. A usage log records each
   action by key and page; the jobs table maps jobs to keys.
1. **Vocabulary:** *Back catalog* / *Add to rotation* / *Heard* / *Missed* /
   *Off*, as in F1.
2. **Pin:** folds into the Up Next row flap; pinned rows show a pin in the
   meta line (F7).
3. **Shelves:** wrap into a grid (F5).
4. **IA:** option A, two pages, in the three PRs above.
