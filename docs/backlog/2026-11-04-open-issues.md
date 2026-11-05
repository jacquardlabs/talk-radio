# Open issue review, 2026-11-04

28 open issues checked against `4dcb86f`, after the redesign (#62) and #65, #67, #68, and #69.

| Verdict | Count | Issues |
|---|---|---|
| Close | 6 | #43, #18, #15, #14, #13, #7 |
| Rewrite, then keep | 8 | #63, #36, #34, #33, #30, #29, #24, #9 |
| Keep as written | 14 | #66, #64, #31, #25, #23, #22, #21, #20, #19, #17, #11, #8, #6, #5 |

Evidence types: **browser** is a Playwright run of the current templates against a real `/api/status` snapshot, **live** is a read-only request to 192.168.1.222:8005, and **code** is a read of the source.

## Close

| # | Issue | Evidence |
|---|---|---|
| 43 | Row menus open under the phone dock | **Browser**, 390×844, last row above the dock: the menu is a fixed bottom sheet at z-index 40, the dock is 20, and all 4 items are topmost and on screen. Fixed by #62 (`templates/base.html:181-184`). |
| 18 | Up Next has no precedence rule | **Code**: `PRODUCT.md:58-92` states the ladder, added in #26. The verb-subtraction audit was never done, but the issue calls itself a design conversation rather than a defect. Some `dj.py` line citations in that section have drifted. |
| 15 | Deck disclosure label differs from `discToggle` | **Code**: the hand-rolled caret is gone. The deck now has a visible More/Less button with `aria-expanded` and `aria-controls` (`board.html:191`, `452-453`). |
| 14 | `.qact` rests at .35 opacity | **Browser**: effective opacity of the row menu button and description caret is 1.0 with no hover. `tools/check_contrast.py` passes both modes. |
| 13 | 3 duplicated prose blocks | **Code**: 1 shared rule (`templates/base.html:220`) with per-context margins. |
| 7 | Arc rule split across 2 near-duplicate keys | Not fixed: this is a disagreement with the finding, and no module owns the rule as a whole. **Code**: the 2 keys differ on purpose, per docstrings (`feeds.py:149-183`) and tests (`tests/test_arc_key.py:59-84`), and share `_strip_part_marker`. A new module adds a file and changes no behavior. |

## Rewrite, then keep

| # | Issue | What changed | Rewrite to |
|---|---|---|---|
| 63 | Undo after Never play | Settings → Never play → Back to rotation now reverses a row or Skip Never play (#67; `db.py:131`, `mark_played(never=True)`), except for news and turned-off shows. *Remove station* is already armed rather than one-tap (`stations.html:350`, `:671`). | In-place Undo toast as a shortcut. It is no longer the only way back. Decide whether undoing Skip should also resume the episode. |
| 36 | Manifest ignores the chosen theme | The 8 themes became light and dark. The manifest is fixed to light `#eef0f3`, so a dark-mode install still gets a light cold-start color (dark paper is `#11151d`). | 2 manifests, swapped from the head script that sets the mode. The issue's open question stands: whether a browser re-reads the manifest after install. |
| 33 | Program guide | **Browser**: wall-clock starts shipped in #62 (`board.html:528-539`), and the guide toggle is moot. `schedule()` does not advance past an unknown-duration row, so the next row prints the same start with no `~` (`board.html:376-379`). | Only the `~` marker on rows after an unknown duration. |
| 34 | Taste-weighted rotation | `db.review_shows()` (#67) now counts heard, kept, back catalog, and Never play per show. `pick_next` is still `random.choice` (`dj.py:42`). | Weight from `review_shows` data. Correct the premise: Back to rotation records nothing (`db.py:825-829`), so only Never play and back-catalog sends count as skips unless that write is added. |
| 30 | Dayparting | The board has no rotation chips; category rotation lives on Stations (`stations.html:399-405`). `db.rotation_candidates()` is now `rotation_feeds_with_new` (`db.py:605`). | Same design with the status on the Stations shelf and the current function name. |
| 29 | Channel presets | The On air "rotation block" the chips were to sit above no longer exists. | Decide where presets live: the clock page gets a new control, or Stations gets it and loses the one-tap dial. Blocks #30's auto-arm. |
| 24 | No schema versioning | Worse: 12 `ALTER TABLE`s, a `media_key` backfill, and a kv one-shot flag (`stale_news_dropped`) in `db.py:208-306`. The "six guards" count and line numbers are stale. | Update the inventory. The constraint list still holds. |
| 9 | `dj.py` size | 1,741 lines, but now 1.6× `db.py` (1,106), not 3.4×. | Update the title and numbers. |

## Keep as written

| # | Issue | Current state |
|---|---|---|
| 66 | gpodder first sync can drop recent listens | **Code**: `db.py:559` is still oldest-first with `LIMIT 5000`, and the response timestamp is `_now()` (`gpodder.py:401`), so cut rows are never re-sent. **Live**: 475 played and 42,523 archived, so not triggered yet. A bulk Never play of about 4,500 back-catalog rows would trigger it for the next new phone. |
| 64 | Focus lost on Up Next poll | **Browser**: focus on a row's menu button moved to `BODY` after one poll. `board.html:536` rewrites the rows unconditionally. |
| 17 | News outranks a just-placed episode | **Code**: `dj.py:670-682` unchanged. `PRODUCT.md:145` calls it intended and `:178` still lists it as an open question; #60 leans further toward news-first. `README.md:130` still has no caveat. Needs your decision (below). |
| 31 | DJ speaks (TTS) | No TTS code. The news insertion it would reuse still exists. |
| 25 | DNS rebinding past the fetch guard | **Code**: `audio.py:95-143` still resolves twice with no pinning. |
| 23 | `status()` holds no lock | **Code**: `dj.py:1535` takes no lock, and calls `_match_queue` and `_invalidate_player` (`:1642`, `:1673`). Docstring at `:149` still claims otherwise. gpodder's episode GET now calls it too (`gpodder.py:390`). |
| 22 | Lock held across network staging | **Code**: `start()` staging at `dj.py:553`, `tick()` through `_top_up` at `:684`. `queue_arc` added a third hand-carved escape (`:1424`). Sequence before #23. |
| 21 | Speaker IP unvalidated | **Code**: `web.py:225-231` stores any non-empty string. `/api/speaker/group` passes one on unchecked (`:238-241`). |
| 20 | No security headers | **Live**: `GET /` returns none of the 4. The only `after_request` hook is usage counting (`web.py:150`). `nosniff` is set on audio routes only. |
| 19 | Container runs as root | **Code**: no `USER` in `Dockerfile`, no `cap_drop` in `docker-compose.yml`. |
| 11 | `create_app` factory size | Tripwire, not task. `web.py` has 44 routes plus 10 in the gpodder blueprint, so `create_app` is 578 lines. The blueprint is now the pattern to copy. |
| 8 | `sonos_ctl.py` reads the DB | **Code**: `sonos_ctl.py:203`, `:230`, `:241` unchanged. No second backend is planned, so low priority. |
| 6 | Up Next order in kv JSON | **Code**: `db.py:747-776` unchanged. Easier after #24. |
| 5 | `sqlite3.Row` as domain model | **Code**: `db.py` still returns rows. About 300 string subscripts across 5 modules, up from 221 with `gpodder.py` added; the grep includes some plain dicts. |

## Suggested order

1. #66, #64: small, verified defects.
2. #21, #20: boundary fixes with a contained change. Check both pages render under the CSP.
3. #17: a decision, then 1 README sentence or a grace flag.
4. #22 before #23, per the sequencing in both issues.

## Decisions

- #17: news-first outranks placement. Documented in `README.md`.
- #29: preset chips stay on On air.
