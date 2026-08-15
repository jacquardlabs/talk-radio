# Sonos Talk Radio

A self-hosted podcast DJ for your Sonos system. Add your science/history/
philosophy shows plus a couple of news feeds, press **On air**, and it
programs the speakers like a personal talk-radio station — forever.

## The three DJ rules

1. **Random show rotation.** The DJ picks a random *show*, not a random
   episode, and won't play the same show twice in a row while another show
   has unplayed episodes. Assign shows to a category on the *Stations* page
   and switch a whole category's rotation off (e.g. Comedy & Trivia) without
   pausing or removing its stations — via the *In rotation* row under the
   On air transport controls, or the category header on *Stations* — manual force-play ("Play next"
   / "Play now") always works regardless of a category's rotation setting.
   An episode already queued when you switch a category off still plays out;
   only future automatic picks are affected.
2. **Multi-part episodes stay in order.** Shows set to *In order* always play
   their oldest unplayed episode. Shows set to *Random* draw any unplayed
   episode instead — but an arc guard still applies: when a draw belongs to a
   multi-part story ("Part 3", "(2/4)", a trailing "II"), the story's oldest
   unplayed part plays first, so a series still arrives 1 → 2 → 3. New shows
   are classified automatically on add (declared serials and heavily numbered
   feeds start *In order*); flip any show from its card in Stations.
3. **News always plays first.** Feeds flagged "news" jump to the front of
   Up Next, oldest first. News older than 24 h (configurable) is dropped —
   you'll never hear Tuesday's headlines on Thursday.

## Quickstart — Docker (recommended)

    git clone <this repo> && cd talk-radio
    TZ=America/Chicago docker compose up -d --build

Open http://<server>:8080.

## Quickstart — plain venv

    python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python main.py

## First run

The dashboard is two pages: **On air** (now-playing deck, transport, Up Next,
wake alarms, recently played) and **Stations** (the station library —
categories, episodes, back catalog).

On-air controls: one **Play/Pause**, −15/+30 seek, **Skip ▾** (choose *Later*
— back in rotation — or *Done* — never again), volume, and a power switch to
go off air. Up Next rows act too: *Play now* interrupts immediately, *Drop*
sends that episode back into rotation for another day.

The dashboard is an installable PWA — on a phone, use *Add to Home Screen*
and it opens standalone like a remote control.

1. **Pick a speaker** — on the *On air* page, hit *Scan*, choose one (grouped
   rooms follow the coordinator automatically; *Group all* groups every speaker).
2. **Add stations** — on the *Stations* page, search by podcast name (default)
   or toggle to *Paste URL*
   for feeds a directory search won't find. Check *news* for news feeds. Pick how
   much back catalog to include:
   - *New episodes only* — nothing until the next episode drops
   - *Latest episode* (default) — start from the newest
   - *Last N episodes* — the newest N
   - *Entire back catalog* — starts the show at episode 1
   You can release the archive later with *Add back catalog*.
3. **Set wake times** — alarm-style rows (time + day chips), e.g. 08:00
   Mon–Fri and 10:00 Sat–Sun.
4. Press **ON AIR**.
5. **Manage episodes** — the *Stations* page is a record crate: category
   shelves of artwork tiles (each shelf header carries the category's
   rotation toggle; badges show unplayed counts; the *Amber/Color* switch
   picks the artwork treatment). Tap a tile to open its station sheet with
   the station's controls and episode browser. Release archived episodes one
   at a time (*Release* button)
   or in batches (*Select* for checkboxes, then *Release selected (N)*). On
   any episode, *Play next* queues it after the current track and *Play now*
   interrupts playback immediately — both turn the station on air if off, and
   work on any episode regardless of status. Search across all stations with
   *Find an episode* by title or show name; each station's panel has a local
   search too.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | SQLite + downloaded media live here |
| `DB_PATH` | `$DATA_DIR/radio.db` | SQLite file |
| `MEDIA_DIR` | `$DATA_DIR/media` | download-mode storage |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | web bind |
| `SONOS_SPEAKER` | — | speaker name to prefer at discovery |
| `SONOS_IP` | — | skip discovery, use this IP |
| `TICK_SECONDS` | `15` | reconcile loop interval |
| `REFRESH_MINUTES` | `30` | feed refresh interval |
| `QUEUE_AHEAD` | `10` | tracks kept queued ahead of the needle |
| `NEWS_MAX_AGE_HOURS` | `24` | news older than this is skipped |
| `DOWNLOAD_MODE` | `0` | `1` = download & serve locally (see below) |
| `BASE_URL` | auto | how the speaker reaches this server |
| `GRACE_MINUTES` | `10` | wake-alarm catch-up window after downtime |
| `WARM_MINUTES` | `5` | how far ahead of an alarm its URLs are resolved |
| `TZ` | — | timezone wake alarms fire in (**set this in Docker**) |
| `USER_AGENT` | `SonosTalkRadio/1.0` | for feed/audio requests |

## DOWNLOAD_MODE

By default episodes stream straight from the podcast CDN. Some CDNs
(token-guarded URLs, long redirect chains) won't play on Sonos. Set
`DOWNLOAD_MODE=1` and the app downloads each episode and serves it from
`/media/` with HTTP Range support (that's what makes Sonos seeking work on
local files), deleting it once played. `BASE_URL` is auto-detected from the
server's LAN address as the speaker sees it; set it explicitly if detection
guesses wrong.

## Wake schedule (and why TZ matters)

Alarms fire on **local time inside the container** — and containers default
to UTC. Pass `TZ` in compose or your 8:00 alarm fires at 8:00 UTC.
`WARM_MINUTES` before the alarm the app resolves the URLs it is about to
need, so the wait at 8:00 is a couple of seconds rather than half a minute
of redirect chains; nothing is downloaded or queued by that pass. Firing
refreshes feeds first, so headlines published 20 minutes earlier are
included; if the station was paused mid-episode the night before, morning
flow is: fresh news → the interrupted episode right where you left off →
normal rotation. If you were **already listening** when the alarm came due,
it gathers the speakers and leaves playback exactly where it is — the
headlines queue up as the next track instead of restarting what you're in
the middle of. A `GRACE_MINUTES` window means a reboot at 8:03 still
catches the 8:00 start, but a server down all morning stays quiet at 3 pm.

The transport API is plain HTTP, so external automation works too:

    curl -X POST http://server:8080/player/play

Actions: `play` `pause` `restart` `back_15` `fwd_30` `skip_later`
`skip_done` `stop` `group_all`.

## Troubleshooting

- **Discovery finds nothing** — the container must share the LAN
  (`network_mode: host`), or set `SONOS_IP` and skip discovery.
- **An episode won't play / instantly skips** — that CDN doesn't stream to
  Sonos; try `DOWNLOAD_MODE=1`.
- **Alarms fire at odd hours** — `TZ` isn't set in the container.
- **Someone started Spotify** — the DJ notices its queue is gone and stands
  down; press ON AIR to take back over.
