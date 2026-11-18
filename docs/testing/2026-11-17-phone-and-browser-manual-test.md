# Phone and browser: manual test plan

Run by the owner with Claude, after deploying `jacquardlabs/backlog`. This plan
covers the three commits that came out of the adversarial review in
`docs/design/specs/2026-11-17-phone-playback-trade-study.md`:

- `1c58d0b` fix: take finished and retired episodes off the phone's queue
- `cc90cd2` feat: play Up Next on this device from the web UI
- `0c75562` fix: close gaps the review found in phone sync and play on this device
- the handoff fix (section H): the last episode played off the speakers leads Up Next; places reach the phone without a known duration; a stopped speaker's stale place is no longer sent

Devices:

- **Pixel**: Pixel 8 Pro running AntennaPod 3.12.3, synced over gpodder, plus Chrome.
- **Laptop**: a browser on the tailnet (`https://mothership.tailb730f2.ts.net`, TS) and on the LAN (`http://192.168.1.222:8005`, LAN).

## Who does what

Every check carries one of these tags:

| Tag | Who | How |
|---|---|---|
| `[adb]` | Claude | `adb exec-out screencap -p`, `adb shell uiautomator dump`, `adb shell dumpsys media_session`, `adb shell input keyevent KEYCODE_MEDIA_*`, and input taps or swipes to open AntennaPod screens |
| `[curl]` | Claude | GET `/api/status`, or GET of the gpodder episode actions, from the Mac. Read-only. |
| `[db]` | Claude, with the owner's OK | Read-only SQLite query inside the container (below) |
| `[hands]` | Owner | Listening, locking the phone, the Sonos app, airplane mode, DevTools, restarting the container |

Steps are done by the owner unless they are marked `[adb]`.

### Probes

```sh
S=https://mothership.tailb730f2.ts.net        # or http://192.168.1.222:8005
# state, deck and Up Next, with each row's banked place
curl -s $S/api/status | jq '{dj_state, transport, np: .now_playing | {episode_id,title,position}, up_next: [.up_next[] | {episode_id,title,is_news,resume}]}'
curl -s $S/api/status | jq '.recently_played[:5]'
# what the server would hand the phone since an epoch (separates "sent" from "applied")
# credentials come from the server's .env; type them at the prompt, never into this file
curl -s -u "$GPODDER_USER" "$S/api/2/episodes/$GPODDER_USER.json?since=<epoch>" | jq '.actions[] | select(.guid=="<guid>")'
```

```sh
# [db] read-only, on mothership, from the deploy dir (owner approves each use)
docker compose exec -T talk-radio python -c "import sqlite3,sys; c=sqlite3.connect('file:/data/radio.db?mode=ro',uri=True); [print(r) for r in c.execute(sys.argv[1])]" \
  "select id,title,status,review,played_at,decided_at,resume_seconds,duration_seconds from episodes where id in (<ids>)"
```

```sh
# [adb] AntennaPod's queue: dump only sees the rows on screen, so scroll and dump again until no new titles appear
adb shell uiautomator dump /sdcard/ap.xml && adb pull /sdcard/ap.xml /tmp/ap.xml
adb exec-out screencap -p > shot.png
adb shell dumpsys media_session | sed -n '/Sessions Stack/,$p'   # Chrome's session: state=3 means playing, 2 paused; plus metadata
```

### Labels used in this plan

The code says "Never play"; the UI says **Mark heard**. Mark heard is on the Review tabs, on Stations ("counts as a listen"), and on Up Next rows.

- Mark heard on Review sets `decided_at`, and so does every Archive.
- The Stations and Up Next Mark heard buttons, and Skip, set `played_at` to now.

Either way, the decision now reaches the phone.

The Archive entry points are:

- Review: *Archive*
- Stations: *Archive* ("won't play unless you add it back")
- the Skip caret on air: *Archive*
- an Up Next row: *Archive*

## Preconditions

1. Before deploying, record the baseline with AntennaPod's queue on screen: titles and count. `[adb]` Scroll and dump. Save the resource IDs of the queue rows for later dumps.
2. For each queue title, record the server's state. `[db]` Query by title. You should find 21 entries: 16 finished on the server (11 Mark heard via Review, 5 heard with a longer copy on the phone) and 5 still unplayed.
3. Owner deploys: `make deploy DEPLOY_HOST=<you>@192.168.1.222 DEPLOY_PORT=8005`. The Makefile's default port is 8080, and that only affects its post-deploy curl check.
4. Hard-reload every browser tab that is used (laptop TS, laptop LAN, Pixel Chrome). On air → *Rooms* must offer **Play on this device**. That proves the new page is loaded.
5. `[curl]` `dj_state` is reported, and Up Next rows carry `resume`. That proves the new server is running.
6. `[db]` The `decided_at` column exists. It is NULL on every row decided before the deploy, because there is no backfill.
7. Turn on USB or wireless debugging on the Pixel. `[hands]` Then Claude confirms `adb devices` and that the dump, screencap and `dumpsys media_session` commands work.
8. Note the wake schedules that exist now. Only the one added in B13 is removed afterwards.
9. Agree which shows to use for areas A and H. They must be non-news (news never syncs). Record 4–6 episode ids, ideally from shows whose phone copy is longer, plus one with `duration_seconds` NULL for H5.

### P1. Clearing the baseline (decision needed before running)

Deploying alone will not clear the 16 stale entries. The phone syncs incrementally, from its last `since`, and none of the 16 is newer than that:

- The 11 have `decided_at` NULL, and their `played_at` is the publish date.
- The 5 were heard before the last sync.

Pick one of these paths:

- **Path 1, targeted. Recommended.** Off air, for each of the 11, go to Review → *Marked heard* → *Add to rotation*, then *In rotation* → *Mark heard*. That dates `decided_at` now. For each of the 5, go to Stations → Heard shelf → *Mark unheard*, then *Mark heard*. That sets `played_at` now.
  - Trade-off: those 5 then count as dismissed (`review='never'`), so they leave Recently played.
  - Then run *Synchronize now*.
- **Path 2, broad.** AntennaPod: Settings → Synchronization → **Force full synchronization**.
  - Back up `data/radio.db` first. `[hands]`
  - The phone's first-sync branch uploads every episode it holds as played. Read in AntennaPod's `SyncService` at 3.12.3, the branch where `lastSync == 0`. The server will mark any of those it still has unplayed as heard.
  - It then asks with `since=0`, so the server sends every played row. Unverified: the reset of `since` behind the button is not in `SyncService`. Check it with the server log or the request. They come 5000 per answer, split on whole seconds, so press *Synchronize now* until the count stops changing.
  - Unverified: whether AntennaPod applies an action dated at publish time over its own state. The conflict rule is in `EpisodeActionFilter`, which has not been read. Record what happens.

**Expect:** about 5 entries left, the ones that are really unplayed. Any of the 16 still present is a finding, so record its server row. `[adb]` dump, `[db]` row.

## A. AntennaPod sync

Sync on demand with AntennaPod → Settings → Synchronization → *Synchronize now* (`[adb]` taps, or owner). Where an AntennaPod check is marked `[adb]`, it means a dump of the queue plus a screenshot of the episode's screen (played tick and position).

**A1. Mark heard on Review reaches the phone**
1. Pick episode X: in the phone's queue and downloaded there, `new` on the server. Add it to the phone's queue if needed. `[hands]`
2. Review → *In rotation* (or *Recently added*) → X → *Mark heard*.
3. `[curl]` The episode actions for `since` = 1 minute ago include X with `position` = 604800.
4. *Synchronize now*.
- Expect: X is marked played in AntennaPod and has left its queue. On the server it is not in Recently played. `[adb]` `[curl]`

**A2. Mark heard on Stations and on an Up Next row reach the phone**
1. Stations → episode Y (in the phone's queue) → *Mark heard*.
2. On air page, off air → Up Next row Z → *Mark heard*.
3. *Synchronize now*.
- Expect: Y and Z are played and out of the phone's queue. Z has left Up Next. `[adb]` `[curl]`

**A3. Archive reaches the phone as played**
1. Archive one episode in the phone's queue from Review, one from Stations, and, on air, one with Skip caret → *Archive*.
2. *Synchronize now*.
- Expect: all three are played and out of AntennaPod's queue. On the server they are `archived` with `decided_at` set. `[adb]` `[db]`

**A4. Heard on Sonos, with a longer copy on the phone**
1. Use episode W from a show whose phone copy runs longer, downloaded on the phone. On air, let W finish on the speakers, or seek to its last minute and let it end. `[hands]`
2. `[curl]` W is in `recently_played`.
3. *Synchronize now*.
- Expect: W is played and out of the queue, not shown as paused near the end. This was the old duration-mismatch symptom. `[adb]`

**A5. Heard on the phone**
1. In AntennaPod, play episode V (also in server Up Next, off air) to its last 30s and let it end. `[hands]`
2. *Synchronize now*.
- Expect:
  - V is `played` and in `recently_played`, and gone from `up_next`. `[curl]`
  - V stays out of `up_next` across later polls and an off-air → on-air cycle. A remote finish now leaves the saved order explicitly.

**A6. Resume, phone to server**
1. In AntennaPod, play episode U (in Up Next, off air) to about 12:00, then pause. `[hands]`
2. *Synchronize now*.
- Expect:
  - `up_next[U].resume` ≈ 720. `[curl]`
  - On the laptop, Play on this device with U at the head starts at about 11:50. `[hands]`

**A7. Resume, server to phone**
1. Laptop, Play on this device: play T to about 20:00, then pause. `[hands]`
2. `[curl]` `up_next[T].resume` ≈ 1200.
3. *Synchronize now*.
- Expect: AntennaPod shows T at about 20:00. AntennaPod may rewind a few seconds on resume, which is its own setting. `[adb]`

**A8. Released back to rotation after an archive**
1. Take one of the A3 episodes. Review → *Archived* → *Add to rotation*.
2. *Synchronize now*.
- Expect: `new` on the server. It stays **played** on the phone. Expected, not a bug: gpodder can't un-play it. `[db]` `[adb]`

**A9. Skip on device reaches the phone**
1. This is covered by B7. After B7's Skip, run *Synchronize now*.
- Expect: the skipped episode is played and out of AntennaPod's queue, if it was there. `[adb]`

**A10. Finish on the phone while Sonos is playing the same episode**
1. On air with episode R playing on the speakers, finish R in AntennaPod and run *Synchronize now*. `[hands]`
- Expect: ignored. R keeps playing on the speakers and stays `queued`. Expected, not a bug: the track on air leaves only by Skip or Play now. `[curl]`

**A11. A trip: several Up Next episodes finished on the phone, one sync**
1. Tailscale off. In AntennaPod, finish 2–3 episodes that are in server Up Next (let each end, or seek to its last 30s). `[hands]`
2. Run once with the station off air, and once with the speakers playing something else.
3. Tailscale on, *Synchronize now*.
- Expect: every one is gone from the web UI's Up Next, and listed in Recently played (heard, not dismissed). `[curl]` In the DB each is `played` with `review` NULL. `[db]` On air, the speakers' queue no longer holds them, and the track the speakers are playing is untouched.

## H. Handoff between the web player and AntennaPod

The rule under test: off air, whatever was played last on the phone or in a browser leads Up Next, pinned, at its place. On air, the speakers' list stands and only the place is kept. AntennaPod never jumps to an episode by itself; you open it, and it resumes at the synced place.

Run each case with the station **off air**. "Sync" means AntennaPod → *Synchronize now*. For each case record the place on both sides (`[curl]` `up_next[0].resume`, `[adb]` AntennaPod's episode screen) and the difference in seconds.

**H1. AntennaPod → web, mid-episode**
1. In AntennaPod, play episode P (any show, not at the head of Up Next) to about 15:00, pause. `[hands]`
2. Sync.
3. `[curl]` `up_next[0]` is P, `resume` ≈ 900, pinned.
4. Laptop: Play on this device.
- Expect: P starts at about 14:50 (the 10s lead-in). Note any offset from inserted ads (H7).

**H2. Web → AntennaPod, mid-episode**
1. Laptop, Play on this device: play P on to about 25:00, pause.
2. Sync; open P in AntennaPod.
- Expect: AntennaPod shows P at about 25:00 and resumes there. `[adb]`

**H3. Round trip twice**
1. Repeat H1 then H2 once more, ending at about 35:00 on the phone.
- Expect: each side starts where the other stopped, every time; no episode is marked played on the way.

**H4. Phone not yet synced**
1. Play P in AntennaPod to about 40:00 and pause, but do **not** sync.
2. Laptop: Play on this device.
- Expect: the browser starts at the last *synced* place (H3's), not 40:00. Expected, not a bug: the server only knows what the phone has sent.
3. Let the browser play on to about 42:00 and pause. Now sync the phone (its 40:00 was reached earlier).
- Expect: the place stays at 42:00 and P stays at the head (`[curl]`). A late phone sync never overwrites a newer listen. Then in AntennaPod the episode should resume at about 42:00 after a second sync. `[adb]`

**H5. Episode with no known duration**
1. With the H-episode whose `duration_seconds` is NULL: play it in the browser to about 5:00, pause, Sync.
- Expect: AntennaPod resumes it at about 5:00. (Before this fix the place was never sent.)

**H6. Near the end**
1. In AntennaPod, play P to about 3 minutes before its end, pause, Sync.
2. Laptop: Play on this device; let it end.
- Expect: the browser plays the last minutes and ends; P is `played`, out of Up Next, and after the next Sync it is played and out of AntennaPod's queue.
3. Variant: in AntennaPod, stop another episode 45s from its end and Sync.
- Expect: the server reads it as finished (a phone place within 60s of the end counts as heard), so it never reaches the browser. Expected, not a bug.

**H7. Ad offset**
1. Pick a show with dynamically inserted ads. In AntennaPod, stop at a recognisable sentence about 20 minutes in. Sync, Play on this device.
- Expect: the browser starts within a minute or two of that sentence. Record the gap; this is the size of the "pick up where I left off" error no code can remove.

**H7b. Car case: phone syncs late**
1. Tailscale off. In AntennaPod, play Q to 10:00, pause.
2. Laptop: Play on this device on a different episode R for a couple of minutes, pause.
3. Tailscale on (or home Wi-Fi), Sync.
- Expect: R stays at the head (its listen is newer); Q's place (10:00) is saved and Q sits behind it. `[curl]`

**H8. Two phone episodes in one sync**
1. In AntennaPod, play A to 5:00, then B to 7:00, pause. Sync once.
- Expect: `up_next[0]` is B (the later one), then A; both carry their places.

**H9. Handoff while the speakers are on air**
1. Sonos playing episode S. In AntennaPod play a different episode Q to 10:00, Sync.
- Expect: Up Next on the speakers is unchanged; Q's place is saved (`[db]` `resume_seconds`). Go off air: Q is not at the head (only off-air listens lead). Expected, not a bug.

**H10. Speakers → phone**
1. On air, let S play to about 8:00, then Off air. Sync.
- Expect: AntennaPod resumes S at about 8:00 when opened. `[adb]` And no stale place is sent after: `[curl]` the gpodder actions since the Off air carry S once, at the banked place.

## B. Play on this device: laptop browser (TS URL; repeat B1, B2, B5 and B11 on LAN)

**B1. Switch to the device from on air**
1. On air, Sonos playing episode H at a known place.
2. *Rooms* → *Play on this device*.
- Expect:
  - Sonos stops and `dj_state` = `stopped`. `[curl]`
  - The laptop plays H from about 10s before the place Sonos reached. If fresh news is waiting, the news plays first and H follows.
  - Rooms reads "This device", and H is not in the Up Next list on the page.

**B2. Switch to the device while off air**
1. Off air, with fresh news unplayed.
2. *Play on this device*.
- Expect: news plays first (`up_next[0].is_news` is true before the press), then the saved order. With no fresh news, the head of the saved Up Next plays. `[curl]` `[hands]`

**B3. Resume position and heartbeat**
1. Play, and note the clock. `[curl]` every 15s.
- Expect: `up_next[<playing id>].resume` advances in steps of about 15s.
2. Pause at T.
- Expect: `resume` ≈ T within a second or two.
3. Reload the tab, then *Play on this device*.
- Expect: it starts at about T−10.

**B4. Seek**
1. Drag the seek bar.
2. Press back 15.
3. Press forward 30.
4. Pause.
- Expect: the audio follows each step, and `resume` matches the last place after the pause. `[curl]`

**B5. Auto-advance**
1. Seek to the last 20s of the playing episode, and let it end.
- Expect:
  - The next Up Next entry starts on its own, from its own `resume` −10s.
  - The finished one is in `recently_played` and gone from `up_next`. `[curl]`

**B6. An empty Up Next draws from rotation**
1. Decide with the owner first. This changes the saved list, and the dropped rows go back to rotation in no particular order. Nothing here restores the order. Record `up_next` ids. `[curl]`
2. Off air, drop rows with *Back to rotation* until one remains. This also exercises the off-air drop fix.
3. *Play on this device* (or let the last one start).
- Expect: as the last entry starts, one rotation pick is appended. `[curl]` shows one row behind it.
4. Seek near the end and let it run out.
- Expect: the pick plays, and another is appended behind it.

**B7. Skip, and Undo**
1. While playing S at P, press **Skip**.
- Expect:
  - The flash reads "Skipped and marked heard", and the next entry starts.
  - S is absent from both `up_next` and `recently_played` (a dismissal). `[curl]` In the DB it is `played` with `review='never'`. `[db]`
2. Press **Undo** in the flash.
- Expect: S plays again from about P. S is at the head of the saved order: the first row of `up_next` that isn't news. `[curl]`
3. Skip again and leave it, for A9.

**B8. Play now on a row**
1. Up Next row further down → *Play now*.
- Expect: it plays here and `dj_state` stays `stopped`. The speakers do not wake. `[curl]`

**B9. Off-air row actions while playing here**
1. On rows of Up Next, including the pick that B6 appended, use *Back to rotation*, *Archive* and *Mark heard* on one each.
- Expect: each row leaves Up Next, with no "That episode isn't in Up Next" error. The archived one has `decided_at` set. `[curl]` `[db]`
2. Off air, go to Review → *In rotation*. Pick an episode that is in Up Next, preferably the one the stopped speaker sits on, and choose *Archive*.
- Expect: it moves, with nothing held back. The flash shows no "held" count, and the episode leaves `up_next`. `[curl]`

**B10. Deck details**
1. Move the volume slider.
- Expect: the browser's level changes, and the Sonos volume does not. `[hands]`
- Expect: Sleep and the Skip caret are disabled. Expected, not a bug.

**B11. Switch back to Sonos, keeping the place**
1. Playing H at T, press *Rooms* → *Play on the speakers*.
- Expect:
  - The browser stops.
  - Sonos goes on air with H from about T−10.
  - `dj_state` = `playing`. `[curl]` `[hands]`
2. Variant: after B8's Play now, switch back.
- Expect: Sonos resumes the **head** of the saved list, not the Play-now episode. Expected, not a bug: PRODUCT.md, Up Next ladder.

**B12. The speakers take over: Play elsewhere**
1. Laptop playing here at T. On the Pixel's browser, On air → **Play**.
- Expect:
  - Within about 5s the laptop pauses.
  - The flash reads "The speakers took over", with *Play here*.
  - Sonos plays from somewhere between T−25 and T−10. `[hands]` `[curl]` The speakers start from the last 15s heartbeat minus the 10s rewind. The browser's final report arrives once the episode is on air, and is ignored.
2. Press *Play here*.
- Expect: Sonos goes off air and the laptop resumes.

**B13. The speakers take over: wake alarm**
1. Add a wake schedule for 2–3 minutes from now, today.
2. Laptop playing here, and move the place well past where Sonos last stopped.
3. Wait for the alarm.
- Expect:
  - The laptop pauses with the same flash.
  - The speakers resume from the **laptop's** place (last heartbeat −10s, so up to 25s back), not the old Sonos place. That is the `_save_resume` fix.
4. Delete only that schedule.

**B14. Sonos app playing while off air**
1. Off air, start the speaker from the Sonos app and let it play for about 2 minutes, to place Q. `[hands]`
2. While it is still playing, use the web UI's off-air menu → *Go off air* → *Now*.
- Expect: `resume` for that episode ≈ Q. `[curl]` `[db]` A speaker that is really playing banks its place again (`_save_resume` with `_transport_live`). Stopping it from the Sonos app instead banks nothing, because the server never sees that stop.

## C. Play on this device: Pixel Chrome, screen locked

Use the TS URL. Repeat C1 on the LAN URL as well.

First, rerun B1, B3, B5, B6 (if done), B7 and B11 on Pixel Chrome with the screen on, using the same expectations. Then run the locked-screen cases below.

**C1. Lock-screen controls**
1. Pixel Chrome → *Play on this device*. Lock the phone. `[hands]`
- Expect: the lock screen shows the title, the show and the artwork. `[adb]` screencap, and `dumpsys media_session` shows Chrome's session with metadata and state=3.
2. On the lock screen, press pause, play, back, forward, and drag the scrubber. `[hands]`
- Expect: each one acts. `[adb]` After a pause, `dumpsys` shows state=2, and `[curl]` `resume` matches.
3. `[adb]` Send `input keyevent KEYCODE_MEDIA_PAUSE`, then `KEYCODE_MEDIA_PLAY`.
- Expect: the same results as the taps.
- Over the LAN URL (plain http), record whether the lock-screen controls appear at all. Unverified whether Chrome offers Media Session outside a secure context.

**C2. Next on the lock screen is Skip**
1. Press *next* on the lock screen (or send `KEYCODE_MEDIA_NEXT` with `[adb]`).
- Expect: the episode is marked heard, the next one plays, and the lock screen updates. Expected, not a bug: next is Skip, so it is marked heard. Undo is only in the page.

**C3. Heartbeat while locked**
1. Leave the phone locked and playing for 3+ minutes. `[curl]` every 15s.
- Expect: `resume` keeps advancing about every 15s. Record it if the reports stop while the screen is off.

**C4. Auto-advance while locked**
1. Before locking, seek to the last 30s.
2. Lock and wait.
- Expect: the next episode starts without unlocking, and the lock-screen metadata changes. `[adb]` `[hands]`

**C5. The speakers take over while locked**
1. Phone locked and playing. On the laptop, press **Play**.
- Expect:
  - The phone goes silent within about 5s. Record how long it took, because the timers in a locked tab may be throttled.
  - The lock-screen session clears.
  - Sonos plays from the phone's place: its last heartbeat −10s, so up to 25s back. `[adb]` `[curl]` `[hands]`

**C6. Another app takes audio focus**
1. Phone playing here. Play a short clip in another app. `[hands]`
- Expect: Chrome pauses, and `resume` is banked at that point. `[curl]`
2. Return and press Play.
- Expect: it resumes there.

**C7. Volume**
1. Use the hardware volume buttons.
- Expect: they work. The page's slider may have no effect on Android. Expected, not a bug.

## D. Two devices at once

Nothing in the code arbitrates between two browsers. Only going on air makes a device yield. For D1–D3, record what happens, then decide whether it needs a rule.

**D1. Both browsers on Play on this device**
1. Laptop playing here.
2. Phone browser → *Play on this device*.
- Code predicts:
  - Both play the head of the list, and neither pauses.
  - Positions are last-writer-wins. Each reports every 15s, so `resume` flips between the two.
  - A finish or Skip on one drops the episode from the other's next list only after a poll. `[curl]`

**D2. Laptop browser plus AntennaPod on different episodes**
1. Laptop plays A here. AntennaPod plays B, which is next in the server's Up Next, to its end. Then *Synchronize now*.
- Expect:
  - B leaves `up_next`. `[curl]`
  - When A ends, the laptop plays the entry after B, not B.

**D3. Laptop browser plus AntennaPod on the same episode**
1. Laptop plays A here. AntennaPod plays A to a different place, pauses and syncs.
- Code predicts: AntennaPod's place lands, and the laptop's next 15s report overwrites it. Last writer wins. `[curl]`
2. Pause the laptop first, then sync the phone, then close the laptop tab.
- Expect: the phone's place stands. A paused tab that is closing reports nothing, and that is the `pagehide` fix. `[curl]`

**D4. Speakers on, one browser yields, and the other page only watches**
1. Phone browser playing here. Laptop on the Stations page → episode → *Play next*.
- Expect:
  - The speakers go on air: `play_episode` calls `start()` whenever it is off air, in every mode.
  - The phone browser pauses with "The speakers took over". Expected, not a bug. `[curl]` `[hands]`

## E. Failure cases

**E1. Stream error**
1. Laptop playing here. In DevTools, open Network → request blocking, block `*/stream/*`, and seek far ahead past the buffer. `[hands]`
- Expect: the flash reads "This episode wouldn't play here. Press Play to try again, or Skip to move on".
2. Unblock, then press **Play**.
- Expect: the stream reloads at the place it reached and plays.
3. Repeat, but press **Skip** instead.
- Expect: the next episode plays.

**E2. Network drop**
1. Laptop: Wi-Fi off for 60s mid-episode, then on. `[hands]`
- Expect:
  - The audio plays out its buffer, then stalls or errors.
  - The reports fail silently.
  - After reconnecting, Play resumes. At most about 15s of banked place is lost. `[curl]`
2. Pixel on the TS URL: Wi-Fi off, so it moves to cellular over Tailscale. `[hands]`
- Record whether playback continues.
3. Pixel on the LAN URL, off Wi-Fi.
- Expect: it fails. Expected, not a bug: the LAN URL works only at home.

**E3. Server restart mid-episode**
1. Laptop playing here.
2. Owner restarts the container: `docker compose restart talk-radio` on mothership. `[hands]`
- Expect:
  - The audio plays out its buffer, then stalls or errors.
  - "Server unreachable" may flash.
  - After restart, `dj_state` is still `stopped` (it is kept in the DB) and Up Next is intact. `[curl]`
  - Play reloads from the place. `resume` is at most about 15s behind.
- Record whether the page stays in device mode or needs *Play on this device* again.

**E4. Tab closed while playing**
1. Close the tab mid-episode at T.
- Expect: `resume` ≈ T, sent by the keepalive report on `pagehide`. `[curl]`
2. Reopen the tab.
- Expect: the page is not in device mode. Choose *Play on this device* again. It starts at about T−10.

**E5. Leaving the On air page**
1. While playing here, follow the link to Stations or Review.
- Expect: playback stops with the place saved. Expected, not a bug: README, Play on this device.

## Expected, not a bug

From the three commits:

- An archive or Mark heard reaches the phone as played. *Add to rotation* or *Mark unheard* later can't un-play it there.
- Episodes left out by rotation depth or skipped titles are never sent to the phone, and neither are prunes of the back catalog.
- A phone copy whose length AntennaPod hasn't read yet (duration 0, usually not downloaded) won't be marked played by a sync.
- Decisions made before this deploy have no `decided_at` and don't reach the phone by incremental sync. See P1.
- A bulk decision of more than 5000 rows reaches the phone over several syncs.
- News never reaches the phone.
- Remote reports (phone or browser) for the episode on air on Sonos are ignored.
- Play on this device:
  - It plays only while the On air page is open: there is no offline mode, and the tab must stay open.
  - It resumes 10s before the banked place.
  - A lost report costs up to 15s of place.
  - Sleep and the Skip caret are disabled.
  - A phone may ignore the volume slider.
  - Lock-screen *next* is Skip.
  - The speakers always win, and alarms always ring on the speakers.
  - After *Play now* on a row, going back to the speakers resumes the head of the saved list.

## Covered by unit tests only

These are not practical to stage by hand:

- A finish reported in the instant between going on air and the first tick naming the track (`_at_needle`).
- More than 5000 rows decided in one second, and the phone's resume timestamp after a cut-short answer. Path 2 of P1 may exercise this for real.

## Results

Record pass, fail or observed per case, with a screenshot or probe output for every fail.

| Case | Result | Notes |
|---|---|---|
| P1 | pass | Re-decided 18 entries (12 Mark heard, 5 heard, 1 archive); after one sync AntennaPod's queue went 21 → 4, all 4 unplayed on the server. |
| H10 (speakers → phone) | pass | Sonos paused at 49:37; AntennaPod showed 49:37, same 1:38:45 length. |
| B1 (speakers → browser) | pass | Speakers off air; browser started at 49:29 (10s lead-in); progress every ~15s; no console errors. Headless Chromium against the live server. |
| H2 (browser → phone) | pass | AntennaPod 49:41 = server's place. The headless close lost the last ~8s report; recheck with a real tab. |
| H1 (phone → browser) | pass | Phone listen synced at 53:30; browser opened that episode first at 53:20. |
| H3 (round trip) | pass | Phone resumed at 53:27 after the browser's 53:29 (AntennaPod's own rewind). Watch: on the first phone play it landed ~3 min past the synced place; not reproduced. |
| H7b (late phone sync) | pass | Browser 1:00:09 newer than unsynced phone 53:40; server kept 1:00:09 and the phone took it on the next sync. |
| B (speakers take over) | pass | Browser paused itself within one poll; Sonos resumed at 1:00:02 (lead-in plus one report that landed after the switch). |
| B (ended → advance) | pass | Episode ended in the browser, left Up Next, went to Recently played, next item (news) started; after a sync it left AntennaPod's queue. |
| A1–A10 | | |
| B1–B14 | | |
| C1–C7 | | |
| D1–D4 | | |
| E1–E5 | | |
