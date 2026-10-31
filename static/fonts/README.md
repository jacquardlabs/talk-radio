# Fonts

Committed rather than linked, so the app looks right on a LAN with no route
to the internet — the same reason the rest of the appliance holds no CDN
references.

Both are the **latin subset** of the Google Fonts release, unmodified, under
the SIL Open Font License 1.1, which permits redistribution alongside this
software. Each is a variable font serving the weight range named in its
`@font-face` rule.

| File | Family | Role | Upstream |
|---|---|---|---|
| `radiocanadabig-normal-var.woff2` | Radio Canada Big | every word on the page | github.com/googlefonts/radiocanadadisplay |
| `radiocanada-normal-var.woff2` | Radio Canada | times, durations, counts | github.com/cbcrc/radiocanadafonts |

Radio Canada's figures are tabular by default, so a column of start times
lines up. Radio Canada Big's `tnum` also widens the space and the colon,
which is why figures take the sibling face instead of the feature.
