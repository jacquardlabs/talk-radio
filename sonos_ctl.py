"""Everything SoCo. All index-base conversions live here: callers think in
0-based ints; SoCo's mixed 0-/1-based APIs never leak out."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import soco
from soco.data_structures import DidlItem, DidlResource

from audio import guess_mime
from config import Config
from db import Database

logger = logging.getLogger(__name__)


def hms_to_seconds(value: str | None) -> int:
    if not value or ":" not in value:
        return 0
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


def seconds_to_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


@dataclass
class TrackInfo:
    uri: str
    queue_index: int  # 0-based; -1 when Sonos reports no playlist position
    position: int     # seconds
    duration: int     # seconds
    title: str


class SonosPlayer:
    """Wraps one SoCo device; every call goes through the group coordinator
    so grouped speakers behave."""

    def __init__(self, device: soco.SoCo) -> None:
        self._device = device

    @property
    def _co(self) -> soco.SoCo:
        return self._device.group.coordinator

    @property
    def ip(self) -> str:
        return self._device.ip_address

    @property
    def name(self) -> str:
        return self._device.player_name

    def transport_state(self) -> str:
        return self._co.get_current_transport_info()["current_transport_state"]

    def current(self) -> TrackInfo | None:
        info = self._co.get_current_track_info()
        uri = info.get("uri") or ""
        if not uri:
            return None
        # playlist_position is a 1-based STRING; '0'/'' means not from queue
        pos1 = int(info.get("playlist_position") or 0)
        return TrackInfo(
            uri=uri,
            queue_index=pos1 - 1,
            position=hms_to_seconds(info.get("position")),
            duration=hms_to_seconds(info.get("duration")),
            title=info.get("title", ""),
        )

    def queue_uris(self) -> list[str]:
        # queue items expose their URI at item.resources[0].uri
        return [item.resources[0].uri
                for item in self._co.get_queue(max_items=500)
                if item.resources]

    def queue_length(self) -> int:
        return len(self.queue_uris())

    def add_to_queue(self, uri: str, title: str, show: str, index0: int | None = None) -> None:
        # SoCo add_to_queue position is 1-based where 0 means append
        pos1 = 0 if index0 is None else index0 + 1
        try:
            res = [DidlResource(uri=uri, protocol_info=f"http-get:*:{guess_mime(uri)}:*")]
            item = DidlItem(title=title, parent_id="-1", item_id="-1",
                            creator=show, resources=res)
            self._co.add_to_queue(item, position=pos1)
        except Exception:
            logger.warning("DIDL enqueue failed for %s; using add_uri_to_queue", uri)
            self._co.add_uri_to_queue(uri, position=pos1)

    def remove_from_queue(self, index0: int) -> None:
        self._co.remove_from_queue(index0)  # SoCo's remove_from_queue is 0-based

    def clear_queue(self) -> None:
        self._co.clear_queue()

    def play_from_queue(self, index0: int) -> None:
        self._co.play_from_queue(index0)  # 0-based

    def play(self) -> None:
        self._co.play()

    def pause(self) -> None:
        self._co.pause()

    def stop(self) -> None:
        self._co.stop()

    def next(self) -> None:
        self._co.next()

    def seek_seconds(self, seconds: int) -> None:
        self._co.seek(seconds_to_hms(seconds))

    def get_volume(self) -> int:
        # group volume, so grouped rooms read/adjust together
        return int(self._device.group.volume)

    def set_volume(self, value: int) -> None:
        self._device.group.volume = max(0, min(100, int(value)))

    def group_all(self) -> None:
        # Deliberately call on self._device, not self._co: partymode() makes
        # whichever device it's called on become the party coordinator. Using
        # self._device ensures the speaker the user selected becomes the party
        # leader, not whatever speaker currently coordinates its group.
        self._device.partymode()

    def coordinator_ip(self) -> str | None:
        """Whoever currently holds the queue. Usually the selected speaker —
        joining points a new member at self._co, so the selection stays
        coordinator — but a group formed from the Sonos app can leave the
        selection a slave of some other room, and it is the coordinator that
        must be protected either way."""
        try:
            return self._co.ip_address
        except Exception:
            return None

    def group_members(self) -> list[dict]:
        """Everyone hearing this station, sorted by name.

        Free on the status poll: SoCo.group walks all_groups, which polls
        zone-group state behind a 5-second cache (POLLING_CACHE_TIMEOUT) —
        the same lookup _co already forces on every transport call.

        Sorted because ZoneGroup.members is a set, and an arbitrary order
        would change the render signature on every poll, defeating the guard
        that keeps the page from repainting and reshuffling under the cursor.
        """
        group = self._device.group
        if group is None:      # a slave in a stereo pair has no group of its own
            return []
        coordinator_uid = group.coordinator.uid
        return sorted(
            ({"name": m.player_name, "ip": m.ip_address,
              "is_coordinator": m.uid == coordinator_uid}
             for m in group.members),
            key=lambda m: m["name"],
        )

    def join(self, ip: str) -> None:
        """Point another speaker at this station's coordinator — the device
        actually holding the queue, not necessarily the selected one."""
        soco.SoCo(ip).join(self._co)

    def unjoin(self, ip: str) -> None:
        """Drop a speaker out of the group. Safe on one that is not grouped,
        which is what lets the endpoint be idempotent under an optimistic UI."""
        soco.SoCo(ip).unjoin()


def discover_speakers(timeout: int = 5) -> list[dict[str, str]]:
    zones = soco.discover(timeout=timeout) or set()
    return sorted(
        ({"name": z.player_name, "ip": z.ip_address} for z in zones),
        key=lambda s: s["name"],
    )


def find_speaker(db: Database, cfg: Config) -> SonosPlayer | None:
    """Selection order: saved IP in kv -> SONOS_IP -> SONOS_SPEAKER name
    match -> first discovered -> None (the app must boot fine anyway).

    An explicit saved/configured IP is a user choice, not a suggestion: if
    it's momentarily unreachable (speaker rebooting, DHCP blip) we return
    None and let the caller retry later, rather than falling through to
    discovery and silently reassigning playback to a different room."""
    ip = db.kv_get("speaker_ip") or cfg.sonos_ip
    if ip:
        try:
            device = soco.SoCo(ip)
            device.player_name  # probe reachability
            return SonosPlayer(device)
        except Exception:
            logger.warning("saved speaker %s unreachable; will retry, not "
                            "falling back to another speaker", ip)
            return None
    zones = soco.discover(timeout=5) or set()
    if cfg.sonos_speaker:
        for zone in zones:
            if zone.player_name.lower() == cfg.sonos_speaker.lower():
                return SonosPlayer(zone)
    first = next(iter(zones), None)
    return SonosPlayer(first) if first else None


def make_player_provider(db: Database, cfg: Config,
                         retry_seconds: int = 30) -> Callable[[], SonosPlayer | None]:
    """Caches the found player so the 5-second status poll doesn't re-run
    SSDP discovery; failed lookups are retried at most every retry_seconds,
    except when the wanted IP changes (user picked a new speaker)."""
    state: dict = {"ip": None, "player": None, "wanted": None, "last_try": 0.0}

    def provider() -> SonosPlayer | None:
        wanted = db.kv_get("speaker_ip") or cfg.sonos_ip
        if state["player"] is not None and (wanted is None or state["ip"] == wanted):
            return state["player"]
        if wanted == state["wanted"] and time.monotonic() - state["last_try"] < retry_seconds:
            return None
        state["wanted"] = wanted
        state["last_try"] = time.monotonic()
        player = find_speaker(db, cfg)
        if player is not None:
            state["ip"] = player.ip
            state["player"] = player
            db.kv_set("speaker_ip", player.ip)
        else:
            state["ip"] = None
            state["player"] = None
        return player

    def invalidate() -> None:
        """Force the next provider() call to re-run discovery instead of
        returning the stale cached player. Called when a caller's Sonos
        operation on the cached player just failed (speaker powered off,
        DHCP-changed IP) — without this the same dead player would be
        returned forever with no path back to a working speaker.

        Resets "wanted" to a sentinel (rather than just clearing the cached
        player) so the very next call also bypasses the failed-discovery
        retry throttle below — this is a fresh failure, not a repeat of an
        already-throttled one, so it should retry immediately."""
        state["player"] = None
        state["ip"] = None
        state["wanted"] = object()

    provider.invalidate = invalidate
    return provider
