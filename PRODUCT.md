# Sonos Talk Radio — product intent

**Status: proposal, not settled fact.** This document was drafted from the repo,
not dictated by the owner. Everything here is up for correction.

Read it with the markers:

- **[code]** — read off the source, anchored to file and line. Change the code
  and this line is wrong.
- **[doc]** — read from prose the owner wrote, anchored by section rather than
  line, since that prose gets rewritten.
- **[inferred]** — a guess at *intent* from what the repo does. Confirm, correct,
  or delete. Nothing marked this way should be quoted back as a commitment.

## Who it is for

**The household listener.** [inferred] Puts talk radio on and leaves it on. Drives
it from a phone or a wall tablet, not a keyboard: the dashboard is an installable
PWA that docks its transport to the bottom of the screen under 760px
[doc, README.md "First run"], and the design brief called for ≥44px tap targets
and no hover-dependent interactions because "this page will often run full-screen
on a wall/table tablet" [docs/original-build-prompt.md, "Web dashboard"].

**The self-hoster.** [inferred] The same person, wearing the other hat. Runs the
container on a home server, curates the station list, and reads the logs when a
feed goes dead. Deployment is a one-liner over SSH to a single host
[doc, README.md "Development"].

That is the whole audience. One household, one Sonos system, one flat LAN — the
speakers must share a broadcast domain with the server because discovery is SSDP
multicast [doc, README.md "What you need"]. There is one database, one selected
speaker — a coordinator, which other rooms can be grouped onto [code,
dj.py:921-964] — and one Up Next; nothing in the schema is per-user. [inferred]

## The rules that govern today

These three hold in code right now. Where they differ from the "non-negotiable"
rules in `docs/original-build-prompt.md`, the code is what ships and the build
prompt is history — see *Superseded* below.

1. **Random show rotation, with a no-repeat guard.** The DJ picks a random
   *show*, then an episode within it, and avoids `last_feed_id` whenever another
   show has unplayed episodes [code, dj.py:37-46]. Only enabled non-news feeds
   whose category has rotation on are candidates [code, db.py:407-418].
2. **Multi-part stories stay in order.** `in_order` shows play their oldest
   unplayed episode; `random` shows draw freely, but if the draw carries an arc
   marker the arc's oldest unplayed member plays instead — never Part 3 before an
   unplayed Part 1 [code, dj.py:49-64]. Rotation's guard uses the loose
   `feeds.arc_key`; the deliberate *Queue series* action uses the stricter
   `series_key`, because acting on a whole group cannot afford the truncation
   that collapses every "SYMHC Classics: …" into one arc [code, dj.py:1140-1143].
3. **News plays first, unconditionally.** Every tick — 15s by default — if fresh
   news exists and the slot after the needle is not already news, the news is
   inserted there, oldest first [code, dj.py:654-666]. News older than
   `NEWS_MAX_AGE_HOURS` is never fresh [code, db.py:420-427]. "Unconditionally"
   is load-bearing: no user action exempts a row from it. See the ladder below.

## Up Next: what outranks what

Eight verbs act on one queue — Play next, Play now, Play last, Pin, Drop,
reorder, Queue series, Refresh all — plus *Defer* on the Skip menu, which also
writes into it [code, dj.py:810-813]. The precedence they resolve by, strongest
first:

1. **The track on air.** Not droppable [code, dj.py:872], not movable
   [code, dj.py:901], preserved by Refresh all [code, dj.py:1101-1106], skipped
   over by Queue series [code, dj.py:1187-1189]. Only Skip and Play now take it
   off.
2. **News-first insertion.** It outranks manual placement. An episode you just
   sent with *Play next*, or dragged to the top, sits behind the headlines within
   a tick. This is one displacement per gap, not a repeated shove: once a news run
   holds the slot the insertion stops [code, dj.py:662], and staged news leaves
   the candidate set [code, db.py:420-427]. Two independent paths encode the same
   order — *Defer* deliberately lands the outgoing episode *behind* the news run
   [code, dj.py:852].
3. **Manual placement.** Play now interrupts [code, dj.py:1052-1061], Play next
   takes `cur_idx+1` [code, dj.py:1039], Play last appends [code, dj.py:1037],
   reorder moves within Up Next only [code, dj.py:901-918], Queue series appends
   the unheard parts in order [code, dj.py:1205-1206], Drop returns a row to the
   pool [code, dj.py:863-882]. Among themselves: last write wins.
4. **Pin is not a rung on this ladder.** It decides one thing — whether *Refresh
   all* may re-roll the row [code, dj.py:1073-1078]. It does not hold a position,
   does not fend off the news insertion, and does not block a Drop. Play
   next/now/last pin automatically, on the reasoning that an episode you chose by
   hand is one you should not lose silently [code, dj.py:1042-1046]; Defer pins
   for the same reason [code, dj.py:861].
5. **Refresh all.** Re-rolls every row that is unpinned and not on air, then lets
   the top-up refill from rotation [code, dj.py:1106-1121].
6. **Rotation top-up, last.** It drains the saved Up Next before it picks
   anything new [code, dj.py:231-246], so a hand-built list is spent before the
   DJ improvises.

**Proposed rule for the next verb.** [inferred] A new queue verb must state its
place on this ladder before it is built, and must not need a new one. If it
cannot be expressed as "acts at rung N", it is a different feature wearing a
queue verb's clothes.

## What is deliberately not built

These are non-goals, not gaps. The README already records them as accepted trades
[doc, README.md "Security"]; stating them as intent is the inferred part.

- **No authentication, and none planned.** Every control is an unauthenticated
  HTTP endpoint, so anyone who can reach the port owns the speakers. That is the
  intended trade for a LAN appliance.
- **No internet exposure.** Do not port-forward it. Remote access is somebody
  else's job — a VPN (Tailscale, WireGuard) in front, not a login page here.
- **No production web server.** Flask's development server is fine for a few LAN
  clients and one speaker, and shipping gunicorn would be scope the appliance
  does not need. [inferred]
- **No multi-tenancy, no accounts, no cloud.** One household, one DB, one Up
  Next. [inferred]
- **No frontend build step.** Three Jinja templates and four static assets; a
  clone runs with `pip install -r requirements.txt` and nothing else.
  [inferred, from the absence of any manifest or bundler in the tree]

## Known problems

**Accepted constraints — permanent, do not file these.**

- No auth, `0.0.0.0` bind, dev server. The three non-goals above.
- **SSDP needs a flat LAN.** A VLAN or guest network between server and speaker
  breaks discovery; `SONOS_IP` is the escape hatch
  [doc, README.md "What you need"].
- **Some CDNs will not stream to Sonos.** Token-guarded URLs and long redirect
  chains fail; `DOWNLOAD_MODE=1` is the answer [doc, README.md "DOWNLOAD_MODE"].
  There is also a third audio path nobody opts into: a resolved URL longer than
  `audio.SONOS_URI_LIMIT` is handed to the speaker as our own `/stream/<id>.mp3`
  and the app fetches the CDN itself [code, dj.py:185-190], so whole episode
  bodies go through the dev server with no configuration and no mention in the
  README.
- **A deep queue dents the unplayed counts.** Ten staged episodes are ten
  episodes out of rotation, reading as `queued` on the stations page until they
  play. Invisible at depth 3, noticeable on small shows at the current default of
  10 [docs/design/specs/2026-07-23-up-next-depth-reorder-design.md, "Accepted
  cost"].
- **News-first outranks your placement.** Rung 2 above. It is the product working,
  not a bug — but it reads as one, which is why it is written down here.

**Open defects live in GitHub Issues.** [inferred — proposed] Issues is enabled on
the repo and currently holds nothing, so there is no home for a known bug or a
deferred review finding, and no way to tell a defect from a decision. The proposal
is: Issues is the tracker of record for open work; this file holds only the
constraints that will never be fixed. Anything that *could* be fixed belongs in
an issue, not here.

## Superseded

`docs/original-build-prompt.md` is a historical artifact — the prompt the app was
built from, not a current spec. Its "Product rules (non-negotiable)" heading is
now misleading in at least three places:

- "Parts of a series … must NOT be forced back-to-back" [build prompt, rule 2] —
  *Queue series* appends a whole story to Up Next in order on one tap
  [code, dj.py:1152-1213].
- "Within a show, always play that show's *oldest unplayed* episode"
  [build prompt, rule 2] — shows can be set to `random`, which draws any unplayed
  episode subject only to the arc guard [code, dj.py:44-45].
- `QUEUE_AHEAD` default 3 [build prompt, "Configuration"] — it is 10
  [code, config.py:42].

Proposed fix: a one-line header on that file marking it historical, with a
pointer here. [inferred]

## Open questions for the owner

1. **Queue series does not pin.** `play_episode` pins [dj.py:1046] and Defer pins
   [dj.py:861], but `queue_arc` never calls `set_pinned` — so *Queue series*
   followed by *Refresh all* silently re-rolls the series you just lined up. Is
   that intended asymmetry, or a gap?
2. **Is news-first meant to outrank a just-placed episode?** The alternative is
   exempting a row placed in the last N seconds from the standing insertion. The
   current behavior is defensible; it is just undocumented for the user.
3. **Are the two personas above right,** and is there really no third — no
   "shares it with a housemate", no "runs it for a second house"?
4. **Does the non-goal list match what you would actually refuse?** These were
   read off a Security section, which records trades, not refusals.
