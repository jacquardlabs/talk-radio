# Phone and laptop playback: trade study

Status: recommendation — the server stays the brain; the phone gets option S,
the laptop gets W's browser player. Owner's answers are recorded below.

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

## Owner's answers

1. **Tailscale is the VPN used to reach the server.** The HTTPS address is
   reachable from the phone; the one-VPN limit does not block it.
2. **AntennaPod downloads automatically.** Unattended restocking is a
   requirement. W cannot meet it.
3. **Listening happens through Android Auto.** W cannot meet it.
4. **Android Auto does not connect while Tailscale is on.** In the car the
   phone has no route to the server.

Consequence of 4: the car is the offline case, not a special one. The app
plays saved episodes, records progress and decisions in the outbox, and
replays them when the server is next reachable. Two requirements follow for
S: it never needs the server to start or continue playback, and at home it
reaches the server at the LAN address so restocking and replay do not depend
on Tailscale being switched back on. Excluding Android Auto from Tailscale
(app split tunneling) may lift the limit; unconfirmed on this phone, and the
design does not rely on it.

### Reaching the server without Tailscale (proposed by the owner)

Exposing a sync endpoint to the internet lets the phone sync on cellular, in
the car included. It reopens the "No internet exposure" non-goal
(`PRODUCT.md`), so it carries conditions:

- **Expose the sync API only.** Every other route is an unauthenticated
  control over the speakers, and stays LAN- and tailnet-only.
- **Token auth and TLS on what is exposed**, the same narrow exception
  `gpodder.py` already makes for `/api/2/*`.
- **Audio does not go through it.** A native app downloads from the podcast
  hosts directly, so the exposed surface is small JSON.

Two ways to expose it: a router port-forward (needs a stable name for the home
address and a TLS-terminating proxy on mothership), or Tailscale Funnel
(public HTTPS on the existing `ts.net` name, no router change; ports 443,
8443, and 10000 only; relayed, with bandwidth limits). Not chosen yet.

## Recommendation

- **Phone: S.** Answers 2 and 3 rule out W on the phone, so its untested
  screen-off question no longer needs a spike: a native player answers it. S
  keeps one set of screens for phone, laptop, and tablet; B would rebuild them
  in Kotlin for no requirement on this list.
- **Laptop: W's browser player**, which S's pages contain anyway.
- **Not A.** Finding 1 stands, and its overnight go/no-go is still untested.

S's native side, all standard Android components (D): a Media3 player and
`MediaLibraryService` (lock screen, headset, Android Auto), a downloader that
restocks the head of Up Next on Wi-Fi, a store for saved episodes, and the
outbox that replays offline events.

Untested for S:

- **Android Auto with a sideloaded app.** Media apps need *Unknown sources*
  enabled in Android Auto's developer settings; reports say Android Auto
  updates can reset it. Not confirmed on this phone or car.
- **Offline screens.** The pages come from the server. What the app shows with
  no connection — a native list of saved episodes, or cached pages — is a
  design decision, not yet made.
- **Toolchain.** A first Kotlin/Gradle project in a Python repo, built locally.

## Build order

1. **Virtual player + output switch + browser player.** Server and web only.
   Ships laptop playback, and phone playback with the app open. AntennaPod
   stays.
2. **Android shell.** Native player, auto-download, outbox, Android Auto.
3. **Delete `gpodder.py` and its tests; retire AntennaPod.**
