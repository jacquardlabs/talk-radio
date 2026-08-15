# Add a podcast by name search — design

**Date:** 2026-07-06
**Builds on:** `2026-07-05-sonos-talk-radio-design.md` (feed-add flow this extends) and the LED departure-board template's existing CSS/JS conventions (`.tab small`, `.svc-row`, `.m`, `input[type="search"]`, debounced search — already established by the episode-search feature).

## Goal

Adding a station today requires knowing and pasting its RSS feed URL. This adds a second way in: search by podcast name, pick a result, and the feed URL is filled in automatically. The direct-URL path stays available (toggle, not a replacement) for feeds a public directory won't have — self-hosted, private, or obscure ones.

## Backend

**New function** in `feeds.py`, alongside `fetch_feed`:

```python
def search_podcasts(term: str, user_agent: str) -> list[dict]:
```

Calls `GET https://itunes.apple.com/search?media=podcast&entity=podcast&limit=15&term=<term>` with an 8-second timeout — shorter than `fetch_feed`'s 30s, since this backs an interactive search-as-you-type UI rather than a one-time feed add. No API key or signup required (Apple's Search API is open). Normalizes each result to `{"title": ..., "author": ..., "artwork_url": ..., "feed_url": ...}` from iTunes' `collectionName`/`artistName`/`artworkUrl100`/`feedUrl`. Entries with no `feedUrl` (some iTunes podcast entries lack one) are dropped — never surface a pick that can't be added. Network/timeout failures propagate as whatever `requests` raises — deliberately not wrapped in `FeedError`, since that exception is specifically for `fetch_feed`'s "parsed successfully but the feed body was malformed" case, which has no equivalent for a JSON search response.

**New route** in `web.py`:

```python
@app.get("/api/podcasts/search")
def api_podcast_search():
```

Takes `?q=`. Blank `q` → `{"results": []}` immediately, no request to iTunes (mirrors the existing episode-search route's blank-query short-circuit). Any exception from `search_podcasts` (caught broadly, matching how `add_feed`'s route already handles `fetch_feed` failures) → `{"results": [], "error": "..."}` with `200` (graceful degradation — a slow/unreachable directory is not a server error, consistent with how this app treats every other flaky-network condition).

No schema changes — this doesn't touch the `feeds`/`episodes` tables at all; it only helps populate the URL that the existing `add_feed` flow already accepts.

## Frontend

The existing `<form class="inline" id="add-feed">` gains a mode toggle at the top: **Search** (new default) vs **Paste URL** (today's existing behavior), styled as two `.tab small` buttons.

**Search mode**: a debounced (300ms, matching the episode-search convention) `<input type="search">` for the podcast name. Results render as `.svc-row`s — artwork thumbnail, title (`.glow`), author (`.m`), and a "Use this" `.tab small` button. Nothing typed yet, or no matches → the existing `.empty` placeholder style.

**Selecting a result**: replaces the search input + results list with a compact confirmation strip (artwork + title + a "Change" button back to search), and sets the *existing* `#feed-url` input's value to the picked feed's URL — that field becomes hidden rather than removed. The News checkbox, back-catalog dropdown, and the `$("add-feed")` submit handler are **completely untouched**: they don't know or care whether the URL came from typing or from search.

**Paste URL mode**: unchanged from today — the plain `<input type="url" id="feed-url">` shown directly.

Switching modes clears whichever state belongs to the mode being left (a picked search result doesn't linger if you switch to typing a URL, and vice versa).

Podcast artwork images load directly from Apple's CDN via `<img src>` — no proxying needed, `<img>` tags aren't subject to CORS the way `fetch()`/XHR calls are (which is exactly why the *search* call itself needs the server-side proxy, but the resulting artwork URLs don't).

## Error handling

- Blank query: no API call, empty results, no error.
- iTunes unreachable/timeout: route returns `{"results": [], "error": "..."}` (200, not 500); frontend flashes a message and "Paste URL" stays available as a fallback.
- No results for a real query: `.empty` placeholder, not an error state.
- Results missing a feed URL: filtered out server-side.
- A selected feed that later fails to fetch/parse on submit: falls through to `add_feed`'s existing error handling in `web.py` (unchanged) — by that point it's just a normal add-station attempt with the URL sourced from search instead of typed.

## Testing

No mocking library in this repo (`requirements-dev.txt` is just `pytest`); existing tests hand-roll small fakes or monkeypatch the module-level function they depend on (e.g. `test_feeds.py` monkeypatches `feeds_mod.fetch_feed`).

- `feeds.py`: `search_podcasts` — monkeypatch `requests.get` with a small fake response object (`.raise_for_status()`, `.json()` returning canned iTunes-shaped JSON); verify field normalization and that feed-url-less entries are dropped.
- `web.py`: the new route — monkeypatch `feeds_mod.search_podcasts` directly (same pattern already used for `fetch_feed`); test the blank-query short-circuit, normal results passthrough, and the graceful-degradation error shape.
- No frontend test harness (existing, accepted project constraint) — verified manually the same way the episode-search UI was: Flask test-client checks for markup/route shape, hand-traced JS wiring.

## Verification

1. Full pytest suite green.
2. Manual: search for a well-known podcast by name, confirm results show artwork/title/author and picking one fills the URL and lets News/back-catalog options still be set before submitting; confirm "Paste URL" toggle still works exactly as before; confirm a nonsense search term shows the empty-results placeholder, not an error.
