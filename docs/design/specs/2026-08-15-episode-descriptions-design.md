# Episode and show descriptions — design

**Date:** 2026-08-15

## Goal

Up Next tells you a title, a show, a date, and a duration. It does not tell you
what the episode *is*. "482: Reverse G.I. Joe" is not a description, and the
decision the Up Next row exists to support — play this now, or drop it — is the
one decision the row gives you no information for.

Every podcast feed already carries this text. The app throws it away at ingest.

Two new surfaces: an expandable description under any episode row, on both
pages, and the show's own blurb at the top of its station sheet.

Nothing about rotation, ordering, or playback changes.

## Storage (`db.py`)

Two columns, both nullable, both added the way every column since
`duration_seconds` has been added — a `PRAGMA table_info` check in `init()`
followed by a plain `ALTER TABLE ADD COLUMN`:

```python
("description", "description TEXT"),   # episodes
```

```python
if "description" not in feed_cols:
    c.execute("ALTER TABLE feeds ADD COLUMN description TEXT")
```

NULL means "never seen one", empty string would mean "the feed published an
empty description". The distinction is what makes the backfill below
idempotent, so absent descriptions must be stored as NULL and never as `""`.

## Stripping happens at ingest, not at render (`feeds.py`)

Feed descriptions are arbitrary HTML written by strangers. This app has no
authentication and its every endpoint moves a speaker, so an injected
`<script>` in a podcast's show notes would be running with full control of the
station. The templates render through `innerHTML` in 24 places.

So the HTML never reaches the database. `html_to_text()` in `feeds.py` reduces
each description to plain text at the boundary, and everything downstream — DB,
API, template — handles a plain string that `esc()` already renders safely.
This is the boundary-normalization rule the codebase follows elsewhere: a
transform applied at every call site is one the next call site forgets.

Built on stdlib `html.parser.HTMLParser`. No sanitizer dependency, because
nothing is being sanitized — tags are discarded rather than filtered, which is
a smaller problem with no allowlist to get wrong. Behaviour:

- text inside `<script>` and `<style>` is dropped, not emitted
- `<br>` and `</p>` become newlines; runs of blank lines collapse to one
- entities are decoded once (`&amp;` → `&`, `&#8217;` → `'`)
- leading and trailing whitespace stripped; all-whitespace result becomes NULL

Source fields, read with `.get()` like every other field in this module:
episode description from `entry.get("summary")`, falling back to the first
`entry.get("content")` value; show description from `parsed.feed.get("subtitle")`
falling back to `parsed.feed.get("summary")`.

### Truncation

Episode descriptions cap at 2000 characters, show descriptions at 1000, cut at
the last word boundary before the cap with `…` appended.

This is not cosmetic. Some feeds put a full episode transcript in
`<description>`. The library is 23.5k episode rows against an 800K database; a
handful of transcript-publishing feeds at 40KB per episode would add more to
the database than everything now in it, to render four lines in a disclosure
panel.

## Backfill

Two mechanisms have to combine, and each is useless alone.

**Conditional refresh must be bypassed once.** Feeds are re-fetched with
`If-None-Match`/`If-Modified-Since` and a 304 means the body is never parsed.
Every feed that has not published since its last refresh would return 304
forever, so no description would ever be parsed for it. The migration that adds
the columns therefore also clears the headers:

```sql
UPDATE feeds SET etag=NULL, last_modified=NULL
```

NULL already means "never asked" — the documented meaning of these columns — so
the next `refresh_all` fetches all 57 feeds in full. One slow pass, then normal
conditional behaviour resumes on its own.

**`INSERT OR IGNORE` must be bypassed too.** A full re-fetch re-offers every
episode the feed has ever published, and `insert_episodes` ignores rows that
already exist. Left there, the forced refresh would download 23.5k episodes and
update nothing.

`insert_episodes` already solved this once, for durations, and the reasoning
transfers verbatim — episodes ingested before descriptions were parsed have
none, and the feed is the only place one can come from. Add a second backfill
`executemany` beside the existing one, in the same transaction:

```sql
UPDATE episodes SET description=?
 WHERE feed_id=? AND guid=? AND description IS NULL
```

The `IS NULL` guard is what makes this safe to run on every refresh forever: it
fills gaps and never overwrites, so a feed that later edits its show notes does
not silently rewrite history, and a second forced refresh is a no-op.

This is why absent descriptions must be NULL rather than `""`. Storing `""`
would make every description-less episode permanently ineligible for backfill.

## The disclosure (`templates/board.html`, `templates/stations.html`)

Each episode row gains a `▾` caret. Clicking it toggles a panel below the row
holding the description, rendered `white-space: pre-wrap` in the content voice
(mixed-case, not LED caps) — this is prose, and the two-voice typography rule
already says prose reads mixed-case. Rows whose episode has no description
render no caret at all, rather than a caret that opens an empty box.

Expanded rows survive the 5-second poll. Both pages re-render from the poll
payload, so an expansion held only in the DOM would collapse under the reader
every five seconds. A module-level `Set` of expanded episode ids is the source
of truth, consulted during render — the same shape as the existing
`lastStationsSig`/`lastCats` guards that keep open panels and focused inputs
alive across polls.

The station sheet renders the show description at the top of the sheet, above
the station's controls.

Descriptions ride the existing `/api/status` payload for Up Next and now-playing,
and the existing station-sheet episode payload for the Stations page. No new
endpoint.

## Edge cases

**Feed publishes no description.** Column stays NULL, no caret, no panel. The
common case for news feeds.

**Description is only markup** (`<p></p>`, a tracking pixel). Stripping yields
whitespace, which is normalized to NULL, so it behaves as "no description"
rather than an empty panel.

**Description is enormous.** Truncated at ingest, so the cap protects the
database and not merely the viewport.

**Feed edits its show notes later.** The `IS NULL` guard means the stored
description is whatever was first seen. Accepted; see below.

**Episode expanded when the poll drops it from Up Next.** The id leaves the
payload, the row disappears, and its entry in the expanded `Set` is inert. The
`Set` is rebuilt from rendered rows rather than accumulating forever.

## Tests

`tests/test_feeds.py`, extending the existing fixture-driven ingest tests:

- entities decoded once, not twice (`&amp;amp;` → `&amp;`)
- `<script>` and `<style>` contents dropped entirely
- `<br>` and `</p>` become newlines; blank-line runs collapse
- markup-only and whitespace-only descriptions become NULL, not `""`
- truncation cuts at a word boundary and appends `…`
- `summary` preferred over `content`; `content` used when `summary` is absent
- show description read from `subtitle`, falling back to `summary`

`tests/test_db.py`:

- migration adds both columns to a database created without them
- migration clears `etag` and `last_modified` on every feed
- backfill UPDATE fills a NULL description on an existing row
- backfill UPDATE does not overwrite a non-NULL description
- inserting an episode that already exists still returns 0 new

The two backfill tests are the ones that matter. The whole design rests on a
re-fetch reaching rows that `INSERT OR IGNORE` skips, and that path is invisible
in any test that only ingests into an empty database.

## Accepted cost

**One slow refresh after deploy.** The forced pass re-fetches all 57 feeds in
full, with a 30-second timeout each. The refresh loop already runs on its own
thread precisely so a slow pass cannot stall the DJ, so the station keeps
playing through it, with descriptions filling in behind. It happens once.

**First description wins.** A show that rewrites its notes keeps the original
text here. The alternative — overwriting on every refresh — would mean 23.5k
UPDATEs on every pass to correct a handful of rows, and would make the refresh
non-idempotent. If stale descriptions ever become a real complaint, the fix is a
per-station "re-read descriptions" action, not a change to the refresh path.

**Database growth.** 23.5k episodes at up to 2000 characters is a bounded
worst case of roughly 47MB against 800K today. Realistically far less, since
most descriptions are a paragraph, but the cap is what makes the worst case
bounded at all.
