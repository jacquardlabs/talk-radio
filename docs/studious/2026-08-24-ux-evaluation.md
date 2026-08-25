# UX evaluation — 2026-08-24

Method: drove the live board at 192.168.1.222:8005 in Chrome at 1568px and 390px
(phone PWA width), read all three templates. Findings ordered by severity.
Design language (amber LED, two-voice type, flap menus, armed confirms) is
strong and consistent — nothing below argues for changing it.

## Defects

### 1. Mobile docked transport hides the power button behind horizontal scroll
`base.html` @760px sets `.transport { flex-wrap: nowrap; overflow-x: auto }`.
At 390px the volume meter is clipped mid-control and the power/stop button is
entirely off-screen; the only affordance is a thin scrollbar. On the phone —
the primary surface per the build brief — "go off air" is effectively
undiscoverable, and the volume drag can trigger horizontal pan of the bar.
Fix: let the transport wrap to two rows (transport buttons / vol+power), or
shrink −15/+30 to icon width and cap the vol meter harder so all five controls
fit one 390px row.

### 2. Queue titles crushed to ~12 characters on mobile
`.qrow` keeps all five grid columns at phone width; pin + Play now + Drop
(~180px) and the disc/caret column leave the title cell so narrow that every
row reads "The Korean W…", "2. The Bronz…", while the meta wraps to four
lines. The list is unscannable — the title is the point of the row.
Fix: below ~600px stack the row — title/meta full width, actions on a second
line — or fold Play now into the Drop flap (one "⋯" flap per row: Play now /
Pin / Later / Done).

### 3. Episode-row disclosure caret orphans onto its own line
In `.svc-row.ep-row` (station sheet + global search) the `▾` disc button is
emitted after `.ep-actions`, so flex-wrap drops it onto a lone line under the
row, floating between rows — ambiguous ownership, easy to miss, and on mobile
it reads as stray debris between episodes. Screenshots show it on both
desktop and mobile. Fix: render the caret immediately before/after the title
span (like Up Next does with its dedicated grid cell), or make the title
itself the toggle and drop the caret.

### 4. Small dim text breaks the codebase's own contrast rule
The `--amber-dim` comment in base.html states the rule: 3.14:1 is fine for
borders/large text, small uppercase labels need `--amber-prose` (7:1). The
rule is applied to `.rots .st`, `.spk-empty`, `.grp-head .cnt` — but not to
`.qmeta` (9.5px), `.tnm` tile names (9.5px), `.np-meta`, `.times`, `.qtitle
.news`… all still `--amber-dim` at 9–12px. On the wall tablet at arm's length
this is the metadata you actually read (show name, date, duration).
Fix: sweep small-text uses of `--amber-dim` to `--amber-prose`; keep
`--amber-dim` for borders, glyphs, and true de-emphasis.

### 5. Tooltip-only affordances on a touch-first product
The brief bans hover-dependent interaction, but `title=` is still the only
channel for: "Tap to seek" (the track), pin behavior, Refresh-all semantics,
power = "stop and revert the queue". None of it exists on touch. Fix: the
track gets a visible cue once (or none — but see #6); pin/refresh semantics
already have armed/aria states, so just drop the pretense; power could take a
two-step arm like Refresh all, which also guards the accidental tap.

### 6. Seek track is a 14px target with no drag
Volume got pointer-capture drag; the track is click-only and 14px tall
against the ≥44px target rule. Seeking to a story you half-heard is a real
use ("back up two minutes") and currently takes pixel-precision tapping.
Fix: extend the hit area (padding + `background-clip`), add the same
pointerdown/move/up drag the volume has, with the mark following the finger.

## Improvements

### 7. Episode rows repeat three spelled-out buttons per row
Every row in the sheet/search renders ADD TO UP NEXT · PLAY NEXT · PLAY NOW
(+ RELEASE/QUEUE SERIES) — 60–100 uppercase buttons per page. It's visually
loud, pushes rows to wrap, and buries the occasional actions. One primary
(Play next, arguably the most-used) + a per-row flap matching the Drop ▾
pattern would halve the chrome and make rows one line again.

### 8. Episode browser rows omit duration
Up Next shows "· 57 MIN"; the station sheet and global search show only the
date. Duration drives "can I fit this in" — it's in the DB already.

### 9. Mobile header spends ~120px before content
Brand, nav, and clock wrap to three rows at 390px. The clock earns its place
on the wall tablet, not the phone. Hide the clock (or fold it into the brand
line) below ~500px and the deck rises a full row.

### 10. Category admin chrome repeats at full weight per shelf
Each shelf head shows ROTATION ON · RENAME · REMOVE, with destructive REMOVE
styled at equal size next to the everyday rotation toggle (mobile wraps them
to their own row per category). Rotation is daily; rename/remove are
twice-a-year. Fold rename/remove behind the existing armed pattern in the
open sheet, or into a small "⋯" flap on the shelf head.

### 11. Pin is nearly invisible on mobile
`.tab.pin` sits at `opacity .55` + `--amber-dim` even where hover can't
raise it (`hover: none` only unhides `.qact`, the pin stays dimmed). Unpinned
state reads as an empty button. Raise unpinned opacity on touch devices, or
give the unpinned glyph an outline treatment.

### 12. No transport off the board page
On the phone, pausing from the Stations page means navigating back. A slim
docked mini-transport (play/pause + now-playing marquee) on Stations would
match the PWA remote-control posture. Optional — scope call.

### 13. Small a11y sweeps
- Esc doesn't close the Skip/Drop flaps; add a keydown handler alongside the
  outside-click close.
- The Drop/Skip flaps aren't `role="menu"`, and arrow keys don't move within
  them (tab order works, so this is polish).
- `aria-live` on `#flash` exists via `role="alert"` — good; the armed
  "Sure?" swap is not announced (`aria-live="polite"` on the button label
  would cover it).

## What's working (keep)

Armed inline confirms; the Skip/Drop verb triad with consequence subtitles
("Later — back in rotation"); honest relative starts only while playing;
pinned-survives-refresh; interpolated 1s ticker; drag reorder with keyboard
fallback and focus restoration; signature-guarded repaints preserving open
dropdowns/search focus; the amber duotone art mode; "Wouldn't play" surfacing
silent Sonos failures; board-title-as-on-air-lamp.
