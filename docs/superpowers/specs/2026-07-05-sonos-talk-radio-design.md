# Sonos Talk Radio — design

**Date:** 2026-07-05
**Requirements:** `sonos-talk-radio-claude-code-prompt.md` (repo root) is the authoritative requirements document — product rules, data model, DJ algorithm, routes, config, deliverables, and acceptance criteria. This design records the decisions layered on top of it; where the two documents overlap, the prompt wins.

## Goal

Self-hosted app on a home Linux server that manages podcast RSS feeds and continuously programs Sonos speakers like a personal talk-radio station: random show rotation, multi-part episodes in order, news always first.

## Architecture

Single Python 3.12 process, two threads:

- **Flask web thread** — dashboard + JSON/control API (`web.py`).
- **DJ loop thread** — daemon started by `main.py`; refreshes feeds every `REFRESH_MINUTES`, runs `tick()` + wake-schedule check every `TICK_SECONDS`. Per-feed exceptions isolated so one broken feed can't kill the loop.

Modules per the prompt's suggested layout: `config.py`, `db.py`, `feeds.py`, `sonos_ctl.py`, `dj.py`, `web.py`, `main.py`, `templates/index.html`.

## Decision: SonosPlayer seam (chosen over module functions or no seam)

`sonos_ctl.py` exposes:

- `SonosPlayer` — class wrapping the **group coordinator**: queue ops (`queue_uris`, `add_to_queue` with DIDL metadata + plain-URI fallback, `remove_from_queue`, `clear_queue`), transport (`play_from_queue`, `pause`, `stop`, `seek`, `next`), `current()` track info (normalized: 0-based position as int, position/duration in seconds), `transport_state()`, `group_all()`.
- `find_speaker()` — factory implementing the selection order: saved IP in `kv` → `SONOS_IP` → `SONOS_SPEAKER` name match against `soco.discover()` → first discovered → `None` (app boots fine, user picks later).

`dj.py` holds a `DJ` class that takes any `SonosPlayer`-shaped object. Tests inject a `FakeSonosPlayer` with an in-memory queue and transport state, so reconciliation, hijack detection, news insertion, and both skips are testable with no Sonos on the network. All 1-based/0-based SoCo index conversions live inside `SonosPlayer`; `dj.py` thinks in 0-based ints only.

## Decision: status lifecycle enforced in db.py

All episode status transitions (`new → queued → played`, plus `skipped` and `archived`) go through named functions in `db.py` — no raw status UPDATEs elsewhere. SQLite in WAL mode, one connection per operation (two threads share the file safely).

## DJ engine

Implemented exactly as specified in the prompt: ordered `tick()` (URI-match → mark played → hijack stand-down → news insertion → top-up → resume-if-stopped), `pick_next()` excluding `last_feed_id` when alternatives exist, `start()` news-first, *Skip — play later* vs *Skip — done*, wake schedules with grace window and per-schedule `last_fired_date`, `resume_seconds` updated every tick and re-seeked (minus ~10 s) on next play. URI matching falls back to path-only comparison for CDNs that append changing query tokens.

## Audio delivery

Stream mode default: resolve enclosure redirect chains (HEAD, falling back to ranged GET) to a final URL, enqueue with DIDL metadata. `DOWNLOAD_MODE=1`: download to `MEDIA_DIR`, serve via Flask `/media/<filename>` with `conditional=True` Range support, delete after played, auto-detect `BASE_URL` via UDP-socket trick toward the speaker IP. Any resolve/download/enqueue failure → mark `skipped`, log, immediately pick a replacement. No retries or prefetching.

## Testing (pytest, no Sonos required)

- DB init; fixture-RSS ingest covering all 4 catalog-scope modes, oldest-first ordering, news pruning at `NEWS_MAX_AGE_HOURS`, unarchive.
- `pick_next` no-repeat behavior.
- Wake-schedule matching: day/time selection, once-per-day guard, grace window.
- Flask test client: `GET /` and `GET /api/status`.
- Via `FakeSonosPlayer` (beyond the prompt's minimum, deliberately — `tick()` is the highest-risk code): reconciliation marks played, hijack stand-down reverts `queued → new` and stops, news inserts after current position when next track isn't news, skip-later returns episode to `new` and removes it from the queue, skip-done marks played.
- Sonos-dependent paths degrade gracefully when discovery returns nothing.

## Tooling & delivery

- Dev via `uv` (venv, installs); ship `requirements.txt` (flask, soco, feedparser, requests) per the prompt. `pytest` is dev-only, not in requirements.txt.
- `Dockerfile` (python:3.12-slim), `docker-compose.yml` (host networking for SSDP, `./data:/data`, `TZ` passthrough, restart unless-stopped).
- README per the prompt's outline, including the TZ-in-Docker warning and troubleshooting table.
- Git repo initialized with this design as the first commit.

## Verification

1. Full pytest suite green.
2. Live against the real speaker on this network: discovery scan, speaker pick, add a real feed (each catalog scope), flag a news feed, On air (news first), queue top-up, seek/±15/30 s, both skips, native-app skip reconciliation, Off air, hijack stand-down (start something else on the speaker).

---

## Revision History

Signed off via viva review — 1 round, 10 sections, 0 revised. 2026-07-05
