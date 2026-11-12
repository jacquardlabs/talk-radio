# UX flows review: re-ranked by the usage log — design

**Date:** 2026-11-11

## Goal

The 2026-10-04 review ranked jobs by assumed frequency and deferred the real
ranking to the usage log (#45). The log now holds 38 days. This review re-ranks
the flows by what was measured, and proposes changes where frequency and
placement disagree.

The program clock, light and dark, Radio Canada, and the existing patterns
(`.flap`, the split pill, `.sw`, `.bulk`, `armed()`, `#flash`) stay. Nothing
here is a reskin. Decided and not reopened: news-first outranks placement, and
preset chips stay on On air (#29). The clock stays; F2 shrinks it on phones
(Decision 5).

## Evidence

- **Log:** `GET /api/usage?days=90` from the live instance, 2026-10-04 to
  2026-11-11. 882 actions came from the dashboard and 671 carried no same-host
  Referer (`web.py:178`), so they did not come from a page.
- **Captures:** 51 Playwright shots of this checkout's templates at 1280×800 and
  390×844, fed one live `/api/status` snapshot and saved payloads.
- **Prototype:** F2 built in a scratch copy of `base.html` and `board.html` and
  captured at 390×844, 768×1024, 820×1180, 1024×768, and 1280×800, with no
  horizontal overflow at any of them. The repo's templates are unchanged. Three
  things in it are mocked: the *Undo* message is injected, the volume sheet is
  not built, and the Review count is a stub field in the status payload.
- **Library at capture:** 84 stations, 50 turned off. 39,619 episodes in the back
  catalog, 523 heard, 11 queued, 139 unplayed — 39 of those on turned-on stations.

File and line citations are for `b26e64f`, the commit before #71.

Limits: one household, so a count under 10 is noise. 27 of the 38 days predate
the redesign (#62); the 7-day column is the current UI. The capture stub answers
`ok` to every POST, so claims about what follows an action cite code or a
Playwright repro. The off-air captures use a payload edited to match what
`dj.status()` returns off air (`dj_state: stopped`, Up next standing,
`dj.py:1617-1626`). Capturing added 6 no-Referer GETs to the log after the
counts were taken.

## What the log says

Dashboard actions, by page. `7d` is since 2026-11-05.

| On air | 38d | 7d | Stations | 38d | 7d | Settings (since #67) | 7d |
|---|---|---|---|---|---|---|---|
| Page loads | 71 | 14 | Page loads | 63 | 20 | Page loads | 10 |
| Play / Pause | 78 | 19 | Episode-list loads | 196 | 32 | Review-list loads | 57 |
| Skip → Never play | 60 | 17 | Notes opened | 26 | 15 | Notes opened | 50 |
| Skip → Back to rotation | 1 | 0 | Back to rotation (1 row) | 14 | 1 | Decisions | 29 |
| Skip → Back to Up next | 0 | 0 | Never play | 6 | 4 | | |
| Reorder | 38 | 12 | Remove station | 5 | 1 | | |
| Drop from Up next | 37 | 25 | Search | 5 | 1 | | |
| Notes opened | 32 | 20 | Send to back catalog | 4 | 0 | | |
| Volume | 16 | 2 | Category rotation switch | 2 | 2 | | |
| Seek (drag) | 10 | 3 | Play next | 1 | 0 | | |
| Refresh all | 9 | 4 | | | | | |
| Play now | 7 | 0 | | | | | |
| Rooms (4 routes) | 18 | 4 | | | | | |
| Wake panel / alarm edits | 7 / 8 | 1 / 2 | | | | | |
| Pin | 5 | 1 | | | | | |
| Wouldn't play / Earlier | 2 / 1 | 0 / 0 | | | | | |
| Off air | 1 | 0 | | | | | |

"Episode-list loads" counts paging, filtering, and re-renders, not sheet opens.

**Never used from the dashboard in 38 days:** Back 15 s, Forward 30 s, the sleep
timer, Back to Up next, Play last, Queue series, Add all to rotation, Add
selected, Mark as news, Turn off, the category select, Add station, the
directory search, Add category, Rename.

**Done with no Referer:** directory search 170, rotation re-cut (`/backfill`,
which has no UI) 130, category assignment 113, episode lists 77, add station 59,
station on/off 52, remove station 28, play order 10, category add/remove/rename
17, other 15. Every station added, turned off, or re-filed in this window was
done outside the pages.

**What the log cannot say:**

- Device width. Phone, tablet, and desktop are one number.
- Drop and Review decisions by kind: the disposition is in the body, and
  `usage_key` only spells out path arguments (`web.py:56-63`).
- Menu opens, so an abandoned menu is invisible.
- Appearance changes since #62: `base.html:487` sends `mode`, `web.py:41` accepts
  `theme`, and the beacon refuses it.

## Flows, ranked

### F1. Skipping what's on takes two taps, and the usual choice is the red one

**Today.** *Skip ▾* opens three choices; *Never play* is last and coloured as
danger (`board.html:203-208`). On a phone the menu is a bottom sheet, so the
second tap lands somewhere else.

**Measured.** 60 of 61 skips chose *Never play*; 17 of 17 in the last 7 days. It
is the second most-used control on the page, behind Play/Pause.

**Proposed.** *Skip* does it in one tap. A caret beside it, the split pill the
power switch uses today, holds *Back to Up next* and *Back to rotation*. The
confirmation carries the way back:

```
[ ⏸ Pause ]  [ Skip | ▾ ]            ● Skipped   [ Undo ]
               └ Back to Up next   keeps its place
                 Back to rotation  plays another day
```

*Undo* puts the episode back on air where it left off (Decision 2). It is
`POST /episodes/review` `keep` (a Never-play row is eligible, `db.py:135-142`),
then `play_now` carrying the position the page held when Skip was pressed. No
new verb and no new rung: Skip acts at rung 1 as before, and Play now is manual
placement, rung 3.

`mark_played` clears `resume_seconds` (`db.py:663`), so the position has to
come back from the page. `play_now` takes it as an optional field and writes it
with `set_resume`; the DJ already seeks a newly current episode to that point
(`dj.py:651-652`). The row is unplayed again by then, so the phone sync sees an
episode in progress, not a heard one with a position (`db.py:568`).

`play_episode` also has to forget it was tracking the episode. Undo usually
lands before a tick has run since the Skip, and `tick()` would read the
episode as still current and overwrite the position instead of seeking to it.

As built, three things follow from reusing what exists:

- The seek lands on the next tick, so the episode plays from 0:00 for up to
  15 s first.
- Whatever started after the Skip goes back to rotation, as with any Play now,
  not back to the head of Up next. News returns there on the next tick.
- `keep` writes `review='keep'`, so Shows counts an undone skip as kept.

**Cost.** `board.html` markup and about 20 lines for Skip and Undo; `base.html`
`#flash` gains a button; `web.py` `play_now` reads one field; `dj.py`
`play_episode` takes the position. Covers #63 for Skip; the row menus' *Mark
heard* can reuse the toast.

### F2. The first screen shows the clock, not what's next

**Today.** At 390×844 the header takes 103px and the dial 300px. The docked
transport covers the bottom 141px, which puts the seek bar (756px) under it at
rest. *Up next* starts at 854px, below the fold. At 1280×800 the first Up next
row starts at 731px and is cut; at 1024×768, 715px. On a tablet held upright
(768 and 820px wide) the transport wraps to four rows, 270px.

**Measured.** The dock's second row (volume, Off air, Sleep) took 17 presses
against 139 for Play/Pause and Skip. Back 15 and Forward 30, half of the first
row, took none. Device split is unknown; PRODUCT.md names the phone and the wall
tablet as the two surfaces.

**Proposed**, built and captured:

```
● On air   Stations   Review 20      ☼      one row, 56px (was 103)
┌──────┐  27 min left
│ dial │  ends at 21:19                      the clock and the numbers it draws
└──────┘
▣ 99% Invisible · 6 Nov 2026        [▢]
100 Objects #23: Coin From the
Ship of Gold
The story of the sinking of the SS …  More
↺ ━━━━━━━●──────────────────────── ↻        seeking sits with the seek bar
Up next   until 05:59        [Refresh all]
≡ 21:19  News  November 11, 2026 – PBS …  ⋯
≡ 22:13  No Such Thing As A Sea Cucum…    ⋯
─────────────────────────────────────────
[ ⏸ Pause ] [ Skip | ▾ ] [ 🔈 30 ] [ ⏻ ]    one row, 73px (was 141)
```

- **Phone.** The dial drops to 136px beside *min left / ends at*; numerals and
  rim pins hide below 760px. Volume opens a full-width slider in a sheet.
- **Off air** is one control: *Now*, *End of episode*, *In 15 / 30 / 60 minutes*.
  Armed, it turns solid and counts down (`25m`). The long-press goes
  (`board.html:831-863`).
- **Wide.** The dial is unchanged. Back 15 and Forward 30 flank the seek bar.
  The transport is one row from 1024px and two rows on an upright tablet, down
  from two and four.

| Viewport | Up next heading | Complete rows on the first screen |
|---|---|---|
| 390×844 | 854px → 505px | 0 → 2 |
| 768×1024 | 825px → 669px | 2 → 4 |
| 820×1180 | 824px → 668px | 4 → 6 |
| 1024×768 | 662px → 572px | 0 → 1 |
| 1280×800 | 678px → 588px | 0 → 2 |

**Cost.** `board.html` (+93 −75 in the prototype) and the phone block of
`base.html` (+19 −4). README screenshots retaken. #29's preset
chips need a home in this layout before it ships.

### F3. Deciding what's in rotation has three homes

**Today.** Rotation is 39 unplayed episodes against a back catalog of 39,619, so choosing
what enters it is the app's second job. It is done in the station sheet (row
menu, *Select* → *Add selected*, *Add all to rotation*), in *Settings*, and by
script. The nav says *Settings*; the page is *Review episodes* and holds no
settings (`base.html:262`, `settings.html:38`).

**Measured.** In its first 7 days Review took 57 list loads and 29 decision
posts, some of them batches. Reading came first: 50 notes opened. Over the same
7 days the
sheet's single-row *Back to rotation* fell from 13 in the prior 31 days to 1. The
sheet's bulk controls went unused for all 38. *Play next*, the one spelled-out
button on every sheet and search row (`stations.html:281`), was pressed once in
38 days.

**Proposed.**

1. Name the page for its job. The nav reads *Review*, with the count of recently
   added episodes awaiting a decision.
2. Show two lines of each episode's notes in Review rows, the way the deck does
   (`.npdesc.clamp`), with the caret for the rest.
3. Make the sheet row's button follow the row. A back-catalog row offers *Add to
   rotation*; *Play next* moves into its menu. Unplayed rows keep *Play next*.
4. Delete the sheet's *Select*, *Add selected*, and *Add all to rotation*
   (`stations.html:510-519`, `712-726`). Replace them with one link, *Review the
   back catalog*, to Review filtered to that show. Review leaves out news and
   turned-off shows (`db.py:952-959`), 54 of 84 stations today, so its
   back-catalog view must list a show asked for by id first. Without that, those
   sheets lose their only bulk path.

```
Review                                              Station sheet, back catalog on
Mastodon – Your Ghost Again        [Keep] [Back    Audio Guide of the Imperfections…
Song Exploder · 11 Nov · 29 min     catalog]       Back catalog · 21 Oct · 30 min
The band Mastodon formed in 2000    [Never play]                [Add to rotation] ⋯
in Atlanta, Georgia. They've won… ⌄
```

**Cost.** `settings.html` row and URL parameters for view and show, so the link
and Back work. `stations.html` row button, toolbar deletion. `db.py`
`_review_filter` admits a named show. The nav count needs
one number in `/api/status`; `review_counts` scans every episode
(`db.py:961-969`), so count `recent` alone and time it on the server first.

### F4. A station opens far from the tile, and has no way out below its first screen

**Today.** The sheet is inserted after the whole shelf (`stations.html:464`). On
a phone, tapping 99% Invisible puts it 836px below the tile and the page jumps.
The first episode is 489px into the sheet, under a 7-line blurb. The sheet is
2,813px long, inline, with *Close* only at its top. Opening one never touches
`location`, so Back leaves the page (`stations.html:604-612`).

**Measured.** The most-used flow on Stations: 196 list loads on 63 page loads.

**Proposed.** Below 760px the sheet is a full-height layer over the page, with the
mini transport still docked under it. Its head (art, title, *Settings*,
*Close*) sticks. The blurb clamps to two lines with *More*. Opening pushes
`#feed-<id>`, which the page already reads, so Back closes it. Wide screens keep
the inline sheet and gain the sticky head.

```
┌──────────────────────────────────────┐
│ ▣ 99% Invisible    [Settings] [Close]│  sticks
├──────────────────────────────────────┤
│ Design is everywhere in our lives,   │
│ perhaps most importantly in… More    │
│ 22 to hear · 815 back catalog · 4 …  │
│ [ Filter this station's episodes   ] │
│ Perfectly Leveraged for…  [Play next]│  first episode near 290px, estimated (was 489)
```

**Cost.** `stations.html` only.

### F5. The library is built outside the dashboard

**Today.** Adding a station is *Add station* → type → *Choose* → options → *Add
station*. With nothing chosen the last button does nothing: the required URL
field is hidden, so the browser blocks the submit and has nowhere to say why
(`stations.html:144`, `153`; Playwright: 0 requests, no message). Turning a
station off or re-filing it is tile → *Settings* → control, one station at a
time. Re-cutting how much of a show is in rotation has no control at all.

**Measured.** Dashboard: 0 stations added, 0 turned off, 0 re-filed. No Referer:
59 added, 52 switched, 113 re-filed, 130 re-cut.

**Proposed.** The owner calls this a gap (Decision 1):

1. Each directory result carries *Add*, with *Latest episode* as the default.
   The options form shows only for *Paste a link*.
2. The sheet's *Settings* gains *In rotation: Latest / Last few / Every / New
   only*, posting to the existing `/feeds/<id>/backfill`.
3. *Select* on the shelves: tap tiles, then *Turn off*, *Turn on*, *Move to*,
   *Remove* from the sticky bar Review already uses (`.bulk`).

**Cost.** `stations.html`; item 3 wants one bulk route rather than N posts.

## Defects found on the way

| # | Defect | Evidence |
|---|---|---|
| D1 | The notes caret is missing on Up next rows with a long show name, 3 of 10 at 390px | `.qmeta` is `nowrap` with `overflow: hidden` and the name never shrinks (`board.html:104`) |
| D2 | *Add station* with nothing chosen is a dead button | F5 |
| D3 | A message covers the phone dock for 4 s | `#flash` sits at `bottom: 24px` (`base.html:208-209`): 750–820px, over a dock at 703–844px |
| D4 | Appearance changes are not counted | `base.html:487`, `web.py:41` |
| D5 | The episode on air reads *Up next*, and offers *Play next*, in its sheet and in search | Its status is `queued` (`stations.html:214-217`) |
| D6 | One transition, two names: *Back to rotation* in the row menu, *Add … to rotation* in the toolbar | `stations.html:236`, `514-517`; the 2026-10-04 decision was *Add to rotation* |
| D7 | A phone menu does not say which episode it acts on; the row is dimmed behind the scrim | `rowMenu`, `epMenu` |
| D8 | The room holding the queue is greyed out, and the reason is only in `aria-label` | `board.html:659-663` |
| D9 | *Earlier* prints times with no day; ten rows span at least three days | `board.html:594-596` |
| D10 | Off air, *Skip* and *Off air* stay live with nothing to act on | `renderPage` disables neither (`board.html:490-503`) |

D1, D3, and D7 are fixed in the F2 prototype.

## Left alone

Search (5), the category rotation switch (4, and #29 and #30 are its planned
consumers), rooms (18), alarms (15), *Earlier* (1), and *Wouldn't play* (2) are
too rare to earn a flow. #64 (focus lost on the Up next poll) and #33 (the `~`
on estimated starts) stand as written; F2 touches the same rows and should not
make #64 worse.

## Recommendation

Five PRs, in order, on top of #71 (below):

1. **F1, D1–D5, D7–D10, and the four measurement gaps.** Small, and the next
   review needs the counts. #71 fixed D6.
2. **F2.**
3. **F3.**
4. **F4.**
5. **F5.**

## Against #71

#71 (*name episodes by where they end up*) merged as `1568895` while this
review was being written, unchanged from the `20b9ba2` read here: 558 tests
pass, and its changed screens hold at 390px with no horizontal overflow.

It renames three things, and this document uses the old names:

| Here | In #71 |
|---|---|
| Back catalog | Archive, Archived |
| Never play | Mark heard |
| Pin, Unpin | Keep in Up next, Stop keeping |

**What it settles.** D6: the archived-to-rotation verb is *Add to rotation* in
the sheet and in Review. The sheet's switch becomes *Unheard / Heard / Archived*
tabs, and the count line leaves the sheet head, so F4's first episode moves
from 489px to 459px. Review gains an *In rotation* tab with its count (49 by
the snapshot's station counts).

**What it changes here.**

- **F1.** The Skip menu grows from 3 choices to 4, and *Mark heard* is still
  last and red. After F1, *Skip* marks heard in one tap and the caret holds
  *Back to Up next*, *Back to rotation*, and *Archive*.
- **F2.** The prototype diff is against `main`. #71 edits the Skip menu and
  `rowMenu`, the two places the prototype also rewrites.
- **F3.** Items 3 and 4 stand: *Play next* is still every row's button, and
  *Select*, *Add selected*, and *Add all to rotation* move to the *Archived* tab.
  Review's tabs go from 4 to 5 and wrap to three rows on a phone, from two.
- **Measurement.** `skip_archive` gets its own key. *Archive* from an Up next
  row is a `drop` disposition in the body, so it joins the unsplit dispositions
  under *What the log cannot say*.

**Left open by #71.**

- *Keep* now means two things. In Review it leaves an episode in rotation, and
  Shows counts those as *kept*. On Up next, *Keep in Up next*, *Stop keeping*,
  and "episodes you kept stay" mean the pin.
- PRODUCT.md still says *Pin is not a rung* and *unpinned* (lines 84 and 90).
- In code, *shelf* now names two things: a category's row of tiles (`.shelf`)
  and a sheet tab (`FEED_SHELVES`, `?shelf=`).

## Decisions (2026-11-11)

1. **Library work outside the dashboard is a gap.** F5 goes ahead in full.
2. **Undo after Skip resumes** where the episode stopped.
3. **Back to Up next stays**, unused or not.
4. **Settings becomes Review.** #71 merged without the rename, so it ships
   with F3.
5. **The 136px dial on phones is accepted.**
