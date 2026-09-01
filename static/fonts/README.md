# Theme fonts

Committed rather than linked, so the board looks right on a LAN with no route
to the internet — the same reason the rest of the appliance holds no CDN
references. Only the active theme's faces are fetched by the browser;
`@font-face` is lazy.

All ten families are under the SIL Open Font License 1.1, which permits
redistribution alongside this software. Each is the **latin subset** of the
Google Fonts release, unmodified; a `-var` file is the variable font serving
the whole weight range named in its `@font-face` rule.

| File | Family | Theme | Upstream |
|---|---|---|---|
| `oswald-normal-var.woff2` | Oswald | Solari | github.com/googlefonts/OswaldFont |
| `vt323-normal-400.woff2` | VT323 | Ceefax | github.com/phoikoi/VT323 |
| `archivo-normal-var.woff2` | Archivo | J-Card | github.com/Omnibus-Type/Archivo |
| `plexsans-normal-var.woff2` | IBM Plex Sans | Rams | github.com/IBM/plex |
| `plexmono-normal-300.woff2` | IBM Plex Mono | Rams (clock) | github.com/IBM/plex |
| `bodoni-normal-var.woff2`, `bodoni-italic-400.woff2` | Bodoni Moda | Quiet Storm | github.com/indestructible-type/Bodoni |
| `publicsans-normal-var.woff2` | Public Sans | Quiet Storm | github.com/uswds/public-sans |
| `barlow-normal-*.woff2` | Barlow | Console | github.com/jpt/barlow |
| `barlowsc-normal-*.woff2` | Barlow Semi Condensed | Console | github.com/jpt/barlow |
| `jost-normal-var.woff2` | Jost | Longwave | github.com/indestructible-type/Jost |

Amber, the default, uses the system monospace stack and downloads nothing.

Barlow ships as one file per weight because it has no variable release.
