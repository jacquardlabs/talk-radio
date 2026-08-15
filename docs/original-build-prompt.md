# Build "Sonos Talk Radio" — a self-hosted podcast DJ for my Sonos system

Build a small self-hosted app that runs on my home Linux server, manages podcast RSS feeds, and continuously programs my Sonos speakers like a personal talk-radio station. I add my science/history/philosophy podcasts plus a couple of news podcasts, press "On air," and it DJs forever.

## Product rules (non-negotiable)

1. **Random show rotation.** For regular (non-news) feeds, the DJ picks a random *show*, not a random episode. Avoid picking the same show twice in a row whenever another show has unplayed episodes.
2. **Multi-part episodes stay in order.** Within a show, always play that show's *oldest unplayed* episode. Combined with rule 1, a 3-part series plays in order (1 → 2 → 3) but the parts are naturally interleaved with other shows — they must NOT be forced back-to-back.
3. **News always plays first.** Feeds can be flagged as "news." Any unplayed episode from a news feed is always inserted at the very front of "up next" (right after the currently playing track), oldest first. News episodes older than `NEWS_MAX_AGE_HOURS` (default 24) are auto-marked skipped so I never hear Tuesday's headlines on Thursday. Net effect: when I hit play in the morning, the news plays first.

## Tech stack

- Python 3.12, single process: a Flask web thread + one background DJ loop thread.
- SQLite for all state (enable WAL; open a connection per operation — two threads share the DB).
- [SoCo](https://github.com/SoCo/SoCo) for Sonos control, `feedparser` + `requests` for feeds.
- Deployable via Docker (`docker-compose.yml` with `network_mode: host` — Sonos discovery needs multicast/SSDP, which doesn't work on the default bridge network) and also runnable in a plain venv.

## Data model

- `feeds`: id, url, title, image_url, is_news (bool), enabled (bool), added_at.
- `episodes`: id, feed_id, guid, title, audio_url, published_at, status, resume_seconds (nullable), local_path (nullable). `UNIQUE(feed_id, guid)`; ingest with INSERT OR IGNORE so refreshes are idempotent.
- `kv`: simple key/value table for runtime state (selected `speaker_ip`, `last_feed_id` picked, `dj_state` = playing/stopped).
- `schedules`: id, time ("HH:MM"), days (Mon–Sun set), enabled, last_fired_date.
- Episode status lifecycle: `new` → `queued` (staged on the Sonos queue) → `played`. Two out-of-band states: `skipped` (stale news, failed loads) and `archived` (predates the subscription window chosen at add time — see Catalog scope). Only `new` episodes are eligible for picking.

## The DJ engine (the important part)

Background loop: refresh all feeds every `REFRESH_MINUTES` (default 30), and run a reconcile `tick()` plus a wake-schedule check every `TICK_SECONDS` (default 15). Isolate per-feed exceptions during refresh so one broken feed can't kill the loop.

**start() ("On air"):** clear the Sonos queue → enqueue all pending fresh news oldest-first → fill with random picks until the queue holds `QUEUE_AHEAD + 1` tracks (default 3 ahead) → `play_from_queue(0)` → set `dj_state = playing`. Mark each staged episode `queued`.

**pick_next():** choose a random enabled non-news feed that has unplayed episodes, excluding `last_feed_id` when any alternative exists; return that feed's oldest `new` episode; remember the feed as `last_feed_id`.

**tick() reconciliation, in order:**
1. Read the coordinator's current track + queue. Match Sonos queue items to `queued` episodes by normalized URI (fall back to comparing the URL path only, ignoring query strings — some hosts append changing tokens).
2. Mark every episode *before* the current queue position that is still `queued` as `played` (delete its downloaded file if any). Only auto-mark items in `queued` status — an episode explicitly skipped-for-later has already been reset to `new` and removed from the queue, and must not be re-flagged.
3. **Hijack detection:** if the queue no longer contains our staged items (e.g. someone started Spotify), stand down gracefully — revert `queued` episodes to `new`, set `dj_state = stopped`, and don't fight the user for the speaker.
4. **News insertion:** if fresh unplayed news exists and the *next* queued track isn't already news, insert the news episodes immediately after the current position, oldest first.
5. Top up: append random picks until `QUEUE_AHEAD` tracks remain ahead of the current position.
6. If transport state is STOPPED but we're `playing` and tracks remain, resume with `play_from_queue(current_position)` — this makes it survive reaching the end of the queue between ticks.

**Controls:**
- Play (start or resume) and Pause — the dashboard's main button is a single play/pause toggle driven by the current transport state.
- **Seek:** restart episode (seek to 0:00), back 15 s, forward 30 s. SoCo's `seek()` takes an `H:MM:SS` timestamp; compute relative jumps from `get_current_track_info()`'s `position`/`duration` strings and clamp to track bounds. (In download mode, the Range support on `/media/` is exactly what makes Sonos seeking work on local files.)
- **Two skips:** *Skip — play later* returns the current episode to `new` so it re-enters rotation another day (advance to the next track — topping up first if it's the last one queued — then remove the skipped item from the Sonos queue and reset its status); *Skip — done* marks it `played` immediately and advances. A plain "next" from the native Sonos app defaults to counting the episode as played (via reconciliation) and must keep working — that's why the queue is kept topped up ahead rather than pushed one track at a time.
- Stop = "Off air" (stop playback, revert `queued` → `new`).

**Catalog scope (Sonarr-style, chosen at add time):** the add-station form has an *Include* picker instead of a raw number:
- **New episodes only** — everything existing is `archived`; the show plays nothing until its next episode drops.
- **Latest episode** (default) — the newest episode is playable now, everything older is `archived`.
- **Last N episodes** — reveals a count field; the newest N become playable.
- **Entire back catalog** — every episode is playable; combined with the oldest-first rule, the show starts at episode 1 and works forward over time.

After add, every refresh makes newly published episodes playable regardless of mode. Because the DJ picks *shows* uniformly at random, a 900-episode archive gets no more airtime than a weekly show — catalog scope only decides where a show starts and how long its well lasts. Each station row shows its archived count and offers an **Add back catalog** action that releases the archive into rotation (`archived` → `new`).

## Wake schedule ("start playing at")

Built-in alarm-clock starts so the station turns itself on:

- Schedules are phone-style alarms: a time plus day-of-week chips, each with an on/off toggle — e.g. one at 08:00 Mon–Fri and another at 10:00 Sat–Sun. Support any number of them.
- The background loop checks them every tick against **local time**. Containers default to UTC, so the compose file must pass `TZ` — a wrong-timezone alarm is the classic self-hosted bug; call it out in the README.
- Firing guard: fire when a schedule's time has passed today, it hasn't fired yet today (persist last-fired per schedule), and we're within a grace window (default 10 min) — a reboot at 8:03 still catches the 8:00 start, but a server that was down all morning doesn't blast podcasts at 3 pm.
- **What firing does:** refresh feeds first, so a news episode published 20 minutes earlier is definitely ingested. Then: if the station is already actively playing, do nothing beyond the standing news-insertion rule (no jarring interruption). If it's paused or stopped — including paused mid-episode — run a fresh `start()` with **news first, always**, then the interrupted episode (see resume memory), then normal random rotation.
- **Resume memory:** keep a nullable `resume_seconds` on episodes; update it every tick for the currently playing episode (costs nothing and survives power cuts) and on pause/off-air. When an episode with `resume_seconds` next begins playing, seek once to that position minus ~10 s of context, then clear it (also cleared when marked `played`). Morning flow: headlines → drop back into last night's episode right where you left off → rotation.

## Audio delivery

- **Default (stream):** resolve each enclosure URL by following redirect chains (HEAD, falling back to ranged GET) to a final direct URL, then enqueue that URL on Sonos with DIDL metadata (episode title + show name, protocol info inferred from file extension; fall back to plain `add_uri_to_queue` if metadata construction fails).
- **`DOWNLOAD_MODE=1` (fallback):** download episodes to `MEDIA_DIR` and serve them from Flask at `/media/<filename>` with HTTP Range support (`send_from_directory(..., conditional=True)` handles this). Needed because some podcast CDNs (token-guarded or long redirect chains) won't play directly on Sonos. Delete the local file once the episode is marked played. Auto-detect `BASE_URL` if unset by opening a UDP socket toward the speaker's IP and reading the local address (that's the server's LAN IP as the speaker sees it).
- **Failure handling:** if resolving, downloading, or enqueueing an episode fails, mark it `skipped`, log the error, and immediately pick a replacement so the queue stays full — one dead CDN link must never stall the station. No retries or prefetching for now.

## SoCo gotchas (learned the hard way — encode these)

- Always control the **group coordinator** (`speaker.group.coordinator`), not whatever device was discovered, so grouped speakers behave.
- `get_current_track_info()['playlist_position']` is a **1-based string**; `play_from_queue()` is **0-based**; `add_to_queue`'s position argument is 1-based where **0 means append**. Convert carefully.
- Queue items expose their URI at `item.resources[0].uri`.
- Speaker selection order: saved IP in `kv` → `SONOS_IP` env → match `SONOS_SPEAKER` name against `soco.discover()` → first discovered. Discovery can return nothing (wrong network); the app must boot fine anyway and let the user pick a speaker later.
- Expose `partymode()` as a "Group all speakers" button.

## Web dashboard (single page, `templates/index.html`)

One self-contained page (inline CSS/JS, system fonts, no build step) that polls `GET /api/status` every ~5s.

Sections: **Now playing** (title, show, elapsed/total time, tuner-style progress bar — tappable to seek — and a full transport row: On air · restart ⏮ · −15 s · play/pause toggle · +30 s · Skip—play later · Skip—done · Off air), **speaker picker** with a Scan button + Group all, **Up next** (numbered, news items tagged), **Stations** (each feed with unplayed/played/archived counts and actions: mark/unmark news, pause/enable, add back catalog, remove), **Add station** form (RSS URL, "news" checkbox, and the *Include* picker from the Catalog-scope section), **Wake schedule** (alarm-style rows: time, day chips, on/off toggle, delete, add-alarm control, and a "Next start: …" readout), **Recently played**, flash messages for errors.

Design direction: a hi-fi **receiver faceplate** — warm near-black background (#171310 territory), amber dial accent (~#f0a43a), red reserved for the pulsing ON AIR lamp and news tags (~#d4553f), monospace for timestamps/counters, and a tuner-strip progress bar with a glowing needle as the signature element. Restrained motion; respect `prefers-reduced-motion`. Don't let it look like a generic admin template.

This page will often run full-screen on a wall/table tablet or a spare monitor as the household's podcast remote, so treat it as a touch control surface: large tap targets (≥44 px) on the transport row, now-playing state readable from across the room, no hover-dependent interactions, and instant visual feedback on every button press (the status poll is only every ~5 s).

## HTTP routes

- `GET /` dashboard · `GET /api/status` JSON for polling · `GET /api/speakers` (discovery scan) · `POST /api/speaker` (select by IP)
- `POST /player/<action>` where action ∈ play | pause | restart | back_15 | fwd_30 | skip_later | skip_done | stop | group_all
- `POST /player/seek` with `{"seconds": <absolute position>}` — backs the tappable progress bar
- `POST /feeds` (url, is_news, include ∈ new_only | latest | last_n [+ count] | all) · `POST /feeds/<id>/<action>` where action ∈ delete | toggle | news | unarchive
- `POST /schedules` (time, days) · `POST /schedules/<id>/<action>` where action ∈ toggle | delete — and include the next upcoming start in `/api/status` for the dashboard readout
- `GET /media/<filename>` (Range-capable local media, only meaningful in download mode)

## Configuration (env vars, with defaults)

`DATA_DIR` (./data) · `DB_PATH` ($DATA_DIR/radio.db) · `MEDIA_DIR` ($DATA_DIR/media) · `HOST` (0.0.0.0) · `PORT` (8080) · `SONOS_SPEAKER` (name, optional) · `SONOS_IP` (optional) · `TICK_SECONDS` (15) · `REFRESH_MINUTES` (30) · `QUEUE_AHEAD` (3) · `NEWS_MAX_AGE_HOURS` (24) · `DOWNLOAD_MODE` (0) · `BASE_URL` (auto-detected if empty) · `TZ` (timezone the wake schedule fires in, e.g. `America/Chicago`) · a custom `USER_AGENT` for feed/audio requests.

## Suggested layout

`config.py` · `db.py` · `feeds.py` (fetch/ingest/refresh) · `sonos_ctl.py` (discovery, coordinator, enqueue with DIDL) · `dj.py` (start/stop/tick/pick logic) · `web.py` (Flask app) · `main.py` (init DB, start background thread, run Flask with `use_reloader=False`) · `templates/index.html`.

## Deliverables

1. The working app, plus `requirements.txt` (flask, soco, feedparser, requests).
2. `Dockerfile` (python:3.12-slim) and `docker-compose.yml` (host networking, `./data:/data` volume, `TZ` passed through, restart unless-stopped).
3. `README.md`: what it is, the three DJ rules in plain language, quickstart (compose + venv), first-run steps (pick speaker → add feeds, flagging the news ones → set wake times → On air), the config table, DOWNLOAD_MODE explanation, how the wake schedule works and why `TZ` matters in Docker (the transport API — e.g. `curl -X POST http://server:8080/player/play` — remains available for external automation), and troubleshooting (discovery fails → host networking or set SONOS_IP; an episode won't play → try DOWNLOAD_MODE=1).
4. Tests/smoke checks that run without a Sonos on the network: DB init, feed ingest from a fixture RSS string (verify all four catalog-scope modes + oldest-first ordering + news pruning + unarchive), pick_next's no-repeat behavior, wake-schedule matching (day/time selection, once-per-day guard, grace window), and Flask test-client hits on `/` and `/api/status`. Sonos-dependent paths must degrade gracefully when discovery returns nothing.

## Acceptance criteria

- Adding a feed flagged "news" causes its fresh episodes to appear at the top of Up Next and play before everything else; news older than the cutoff never plays.
- A show's episodes always play oldest-unplayed-first; parts of a series are never out of order, and the same show never plays twice in a row while another show has unplayed episodes.
- Skipping from the native Sonos app just works (queue stays topped up), and starting Spotify/anything else on the speaker makes the app stand down instead of fighting for the queue.
- Catalog scope behaves: "New episodes only" plays nothing until the next episode drops; "Entire back catalog" starts a show at its first episode; "Add back catalog" releases archived episodes into rotation; an episode that fails to load is skipped and replaced without stalling playback.
- Wake schedule: with alarms at 8:00 Mon–Fri and 10:00 Sat–Sun, the station comes on air at those local times with fresh news first — even if it was paused mid-episode the night before — and the interrupted episode resumes where it left off immediately after the news.
- The transport works as a remote from a tablet: play/pause toggles correctly, −15 s / +30 s / restart seek within the current episode, *Skip — play later* puts the episode back into future rotation, and *Skip — done* ensures it never plays again.
- The app boots with no Sonos reachable and the dashboard still loads.
