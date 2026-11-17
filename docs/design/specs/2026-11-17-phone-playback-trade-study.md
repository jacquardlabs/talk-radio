# Phone and laptop playback: trade study

Status: no decision. Two untested go/no-gos and three owner answers stand
between this and a recommendation.

## Inventory

- Server: 8,124 lines of app code, 7,726 lines of tests, 1 Sonos coordinator,
  1 Up Next. Reachable at `http://192.168.1.222:8005` on the LAN and at
  `https://mothership.tailb730f2.ts.net` on the tailnet (`tailscale serve`).
- Phone: Pixel 8 Pro, Android 17, AntennaPod 3.12.3 synced over gpodder
  (`gpodder.py`, 430 lines; `tests/test_gpodder.py`, 508 lines).
- Tailnet members: mothership, the Mac, the Pixel.

## Goal

One listener hears the same Up Next on Sonos, on the phone (including offline),
and on a laptop over VPN, without AntennaPod.

## What is wrong today (measured 2026-11-17)

AntennaPod's queue held 21 episodes. 16 were already finished on the server.

1. **Never play never reaches the phone (11 of 16).** Never play sets
   `played_at` to the publish date (`db.py:157`); the phone asks for episodes
   played since its last sync (`db.py:603`).
2. **Duration mismatch (5 of 16).** The server reports played as position =
   its own duration; the phone's copy of the file is 2–6 minutes longer and
   AntennaPod reads the report as a pause. Cause of the length difference
   unconfirmed.
3. **Two queues.** gpodder carries play positions only. Up Next order,
   news-first, Archive, and Never play have no representation on the phone.

## Constraint found in the code

Up Next on the server is the Sonos queue plus a banked copy.

- `tick()` returns unless the station is on air and a speaker answers
  (`dj.py:574-578`): off air, nothing tops up, inserts news, or retires.
- Drop needs the speaker's queue (`dj.py:1014-1017`). Play next, Play now, and
  Queue series start Sonos when off air (`dj.py:1244-1245`, `dj.py:1440-1441`).
- `apply_remote_action` discards reports for the episode on air
  (`dj.py:1125-1133`).

So a playback target other than Sonos is a **second player behind the DJ's one
seam** (`get_player`, `dj.py:131-134`): a server-side virtual queue with the
18-method surface `tests/fake_player.py` already implements in 142 lines, fed
position and track-ended reports by whichever device is playing. The DJ's
1,780 lines run against it unchanged. Offline, the device plays a snapshot and
replays its events on reconnect — a sync protocol of our own, smaller than
gpodder's because both ends share one model.

This piece is required by options W, S, and B below. Only A avoids it.

## Options

- **W — web switch.** Output switch in the existing web app: Sonos | This
  device. The browser plays `/stream/<id>`, saves episodes through a service
  worker, and queues offline actions.
- **S — native shell.** W's pages inside a small Android app that owns
  playback and downloads natively. Fallback if W's go/no-go fails.
- **B — native client.** A full Android app (own UI, player, downloads) against
  the server's API.
- **A — phone is the host.** The DJ, database, feeds, and Sonos control move
  into an Android app, which also serves the web UI.

## Comparison

V = verified this session. D = from vendor docs, untested here. U = untested.

| | W | S | B | A |
|---|---|---|---|---|
| Server code kept | All (V) | All (V) | All (V) | None: rewrite in Kotlin; tests do not port (V) |
| New code | Virtual player, browser player, service worker | W + Kotlin player and downloader | Virtual player + whole Android app | Whole system |
| Laptop playback over VPN | Yes (V: HTTPS address exists) | Via W's pages | Needs W's browser player as well | Needs W's browser player, served from the phone |
| Sync | Own outbox | Own outbox | Own outbox | None |
| Next episode starts, screen off | U — go/no-go | D: foreground media service | D | D |
| Unattended restock before leaving home | No: downloads need the app open or a tap (D) | Yes (D) | Yes (D) | Yes (D) |
| Away, internet, no tailnet | Saved episodes only | Saved episodes only | Saved episodes only | Full function |
| Needs Tailscale as the phone's active VPN | Always, at home too: service workers need the HTTPS address (D) | Away only: can use the LAN address | Away only | Never |
| Android Auto | No | Possible | Yes | Yes |
| Sonos, wall tablet, wake alarm with the phone away, off, or rebooted | Unaffected (V) | Unaffected | Unaffected | Stop; overnight survival U — go/no-go |
| Sonos unofficial local API risk | Same | Same | Same | Same |

## Findings

1. **A conflicts with the laptop requirement.** With the phone as host, the
   laptop plays only while the phone is awake, reachable, and serving, and the
   browser player still has to be built. A's unique gains are no sync and no
   server.
2. **W, S, and B share their server work.** The virtual player and the
   reconnect protocol are common; the options differ only in the client. W's
   client is also the laptop's client under every option.
3. **W → S → B is a ladder.** Each step keeps the one before. A reuses nothing.
4. **Each side has one untested go/no-go.** W: the next episode starting with
   the screen off on the Pixel's Chrome. A: a foreground service surviving a
   night and firing a wake alarm. Neither is settled by documentation.
5. **Earlier claims corrected.** "Days" for the output switch was wrong: every
   Up Next verb talks to the Sonos queue, so the switch is the virtual player,
   not a flag. And W does not remove sync; it replaces gpodder's with a smaller
   one.

## Open questions for the owner

1. Is NordVPN normally the active VPN on the phone? Android allows one at a
   time. If yes, W cannot load its offline-capable address even at home.
2. How do episodes get onto the phone today — AntennaPod auto-download? If
   unattended restocking matters, W alone falls short and S is the floor.
3. Does phone listening happen through Android Auto?

## Proposed next step

Half-day spike on the Pixel, no production code: play 2 short episodes through
`/stream` from the HTTPS address with the screen off; save 1, go to airplane
mode, play and seek it. Pass → build the virtual player, then W. Fail → build
the virtual player, then S.
