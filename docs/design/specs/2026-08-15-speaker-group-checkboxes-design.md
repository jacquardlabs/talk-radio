# Speaker group checkboxes — design

**Date:** 2026-08-15

## Goal

The speaker control is a `<select>` and a *Group all* button: play in one room,
or play in every room. There is nothing between them. Kitchen and Office but not
the bedroom is not expressible, and the only way to leave a room is to group
everything and then walk to the speaker.

Replace the dropdown with a checkbox per speaker. The station keeps playing
where it is playing; the checkboxes decide which other rooms are hearing it.

## What does not change

`speaker_ip` in `kv` still names the one speaker the DJ plays through, and
grouping does not touch it. `find_speaker` still resolves that IP, and
`SonosPlayer._co` still routes every transport call through
`self._device.group.coordinator`, so a grouped station behaves exactly as it
does today.

This separation is what makes the design coherent: *which room is the station*
is one decision, made by the selector, and *which rooms are listening* is
another, made by the checkboxes. Conflating them is what makes the current
control confusing.

`group_all()` is untouched. Its `partymode()` call carries a comment recording
a bug that was already found and fixed once — that it must be called on
`self._device` rather than `self._co` so the user's chosen speaker becomes the
party leader. Nothing here goes near it.

## Reading membership is free

`SonosPlayer.group_members()` returns the group from
`self._device.group.members`.

This costs nothing extra on the 5-second status poll, and that is a measured
property of soco rather than an assumption. `SoCo.group` walks `all_groups`,
which calls `zone_group_state.poll()`, which caches for
`POLLING_CACHE_TIMEOUT = 5` seconds. `_co` already forces exactly this lookup on
every transport call the app makes. Membership therefore rides the existing
`/api/status` payload and needs no new polling and no discovery.

**Members must be sorted by name before returning.** `ZoneGroup.members` is a
`set`, so its iteration order is arbitrary and would differ between polls. Both
pages guard re-renders on a signature computed from the payload; an unsorted
member list would change that signature every five seconds, defeating the guard
and reshuffling the checkbox list under the user's cursor.

```python
def group_members(self) -> list[dict]:
    group = self._device.group
    if group is None:          # slave in a stereo pair
        return []
    coordinator_uid = group.coordinator.uid
    return sorted(
        ({"name": m.player_name, "ip": m.ip_address,
          "is_coordinator": m.uid == coordinator_uid} for m in group.members),
        key=lambda m: m["name"],
    )
```

`group` is documented to return None when the device is a slave in a stereo
pair. Returning `[]` there degrades to the current behaviour rather than raising
into the status poll.

## Changing membership

Two methods on `SonosPlayer`, both taking the target speaker's IP:

```python
def join(self, ip: str) -> None:
    soco.SoCo(ip).join(self._co)

def unjoin(self, ip: str) -> None:
    soco.SoCo(ip).unjoin()
```

`join` targets `self._co` deliberately — the coordinator is what a joiner must
be pointed at, and it is the speaker actually holding the queue.

`unjoin` is safe to call on a speaker that is not grouped; soco documents it as
returning ok in that case. That makes the endpoint idempotent, which matters
because the UI applies toggles optimistically.

The DJ wrappers follow `group_all`'s existing shape exactly — take `self._lock`,
`get_player()`, return `self._NO_SPEAKER` when there is none, and on any
exception log, `self._invalidate_player()`, and return an error string:

```python
def set_group_member(self, ip: str, member: bool) -> str | None
```

## Refusing to drop the coordinator

Unchecking the speaker the station is playing through is rejected, not obeyed.

The coordinator holds the Sonos queue. Removing it would strand playback: the
queue, the needle, and the resume position all live with that device, and the
app's entire model of what is playing is `_match_queue` against it. Honouring
the uncheck would mean transferring the queue to another speaker mid-episode,
which is the single most delicate operation in the codebase and is not worth
building to service a checkbox.

The guard lives in `dj.set_group_member`, not in the template. A UI-only guard
is bypassed by any `curl` against the documented transport API:

```python
if not member and ip == player.coordinator_ip():
    return "That room is holding the queue — switch rooms instead."
```

The protected speaker is the **group coordinator**, read live via a
`coordinator_ip()` helper returning `self._co.ip_address` — not `speaker_ip`
from `kv`. Usually they are the same device: joining points the new speaker at
`self._co`, so the selected room stays coordinator. They diverge when the group
was formed from the Sonos app, which can leave the selected room a slave of some
other coordinator. The queue lives with the coordinator in both cases, so the
coordinator is what must be protected, and the message avoids claiming it is the
room the user selected.

The checkbox for that room renders checked and `disabled`, so the refusal is
communicated before it is triggered rather than as an error afterwards. The
`is_coordinator` flag already in the members payload is what the template keys
off, so the UI and the guard read the same fact from the same source.

## The control (`templates/board.html`, `web.py`)

`<select id="speakers">` becomes a checkbox list in the same slot.

Two sources feed it, and keeping them separate is the point:

- **Scan** still runs SSDP discovery via `/api/speakers`, on demand only. It is
  a 5-second blocking network sweep and must never move onto the poll.
- **Membership** comes from `status().speaker.members` every 5 seconds, so the
  boxes track reality — including changes made from the Sonos app — without
  scanning.

A speaker known only from membership and not from the last scan still renders;
the list is the union, keyed by IP. This is the normal case on a fresh page
load, where nothing has been scanned yet but the group is already known.

One new endpoint:

```
POST /api/speaker/group   {"ip": "...", "member": true|false}
```

routed through the existing `call_player` helper, which already turns a
dead-speaker exception into a graceful error instead of a 500 and invalidates
the cached player.

Toggles apply optimistically and revert on a non-ok response, with the message
surfaced through `flash()`.

## Edge cases

**Speaker powers off while grouped.** It leaves `group.members`, so its box
clears on the next poll. Acting on a dead speaker raises, which `call_player`
converts to "Speaker unreachable" and invalidates the cached player.

**Group changed from the Sonos app.** Picked up on the next poll; the checkbox
list is a view of group state, not a local model of it.

**Unchecking a speaker that is already out of the group.** `unjoin` returns ok.
No-op.

**Stereo pair slave.** `group` is None, `group_members()` returns `[]`, the list
shows only what Scan found. No crash in the status path.

**Scan finds nothing.** Existing behaviour and existing message ("No speakers
found — same network? host networking?"), unchanged.

**Group all pressed while boxes are checked.** `partymode()` groups everything;
the next poll shows every box checked. Consistent by construction.

## Tests

`tests/fake_player.py` grows `members`, `join`, and `unjoin` over the existing
`grouped` flag, keeping the fake's surface equal to `SonosPlayer`'s.

`tests/test_sonos_ctl.py`:

- `group_members` sorts by name and is stable across repeated calls
- the coordinator is flagged, and exactly one member is
- `group is None` (stereo-pair slave) returns `[]` rather than raising

`tests/test_dj_controls.py`:

- `set_group_member(ip, True)` joins to the coordinator, not to `self._device`
- unchecking a non-coordinator unjoins it
- unchecking the coordinator is refused, and no Sonos call is made
- the refusal keys on the live coordinator, not on `speaker_ip`: with the
  selected room a slave of another coordinator, unchecking the *coordinator* is
  refused and unchecking the *selected room* is allowed
- no speaker returns `_NO_SPEAKER` without raising
- a raising player invalidates the cached player, matching `group_all`

`tests/test_web.py`:

- `/api/speaker/group` rejects a missing or empty ip
- membership appears in the `/api/status` payload
- a dead speaker returns "Speaker unreachable", not a 500

The refusal test is the load-bearing one. It is the only thing standing between
a checkbox and a stranded queue, and it has to hold at the DJ layer where the
HTTP API cannot route around it.

## Accepted cost

**Grouping is not persisted.** If the speakers are regrouped from the Sonos app
or lose power, the app does not restore the previous set — it reports whatever
the group is now. Persisting an intended group would mean reconciling it on
every tick and fighting the Sonos app for authority over the same state. The
speaker is the source of truth; the checkboxes are a view.

**Moving rooms is still a two-step.** Playing in the bedroom instead of the
office means selecting the bedroom, then adjusting boxes. Making the coordinator
checkbox drag-to-move would collapse the two, and requires the queue transfer
this design explicitly declines to build.
