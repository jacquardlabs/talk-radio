"""Adding and removing rooms from the group hearing the station.

The load-bearing case is the refusal: the Sonos queue, the needle and the
resume position all live on the coordinator, so dropping it would strand
playback. Everything else here is plumbing around that one guard.
"""
import pytest

import sonos_ctl
from config import Config
from db import Database
from dj import DJ
from fake_player import FakeSonosPlayer
from sonos_ctl import SonosPlayer
from web import create_app


# ── fakes for the SoCo surface ───────────────────────────────────────

class _Zone:
    def __init__(self, name: str, ip: str, uid: str) -> None:
        self.player_name = name
        self.ip_address = ip
        self.uid = uid
        self.joined_to = None
        self.unjoined = False

    def join(self, master) -> None:
        self.joined_to = master

    def unjoin(self) -> None:
        self.unjoined = True


class _Group:
    def __init__(self, coordinator, members) -> None:
        self.coordinator = coordinator
        self.members = set(members)      # a real ZoneGroup.members is a set


class _Device(_Zone):
    def __init__(self, name, ip, uid, group=None) -> None:
        super().__init__(name, ip, uid)
        self._group = group

    @property
    def group(self):
        return self._group


def _player_with_group():
    office = _Device("Office", "10.0.0.1", "uid-office")
    kitchen = _Device("Kitchen", "10.0.0.2", "uid-kitchen")
    attic = _Device("Attic", "10.0.0.3", "uid-attic")
    group = _Group(office, [kitchen, attic, office])
    for d in (office, kitchen, attic):
        d._group = group
    return SonosPlayer(office), office, kitchen


# ── group_members ────────────────────────────────────────────────────

def test_group_members_sorted_by_name() -> None:
    # ZoneGroup.members is a set, so without an explicit sort the order varies
    # between calls — which would change the render signature every poll and
    # reshuffle the checkbox list under the user's pointer.
    player, _, _ = _player_with_group()
    assert [m["name"] for m in player.group_members()] == ["Attic", "Kitchen", "Office"]


def test_group_members_is_stable_across_calls() -> None:
    player, _, _ = _player_with_group()
    assert player.group_members() == player.group_members()


def test_exactly_one_member_is_the_coordinator() -> None:
    player, office, _ = _player_with_group()
    members = player.group_members()
    flagged = [m for m in members if m["is_coordinator"]]
    assert len(flagged) == 1
    assert flagged[0]["ip"] == office.ip_address


def test_stereo_pair_slave_has_no_group() -> None:
    # SoCo documents group as None for a slave in a stereo pair. Returning []
    # degrades to "no members known" instead of raising into the status poll.
    player = SonosPlayer(_Device("Slave", "10.0.0.9", "uid-slave", group=None))
    assert player.group_members() == []


def test_coordinator_ip_reads_the_group_not_the_device() -> None:
    kitchen = _Device("Kitchen", "10.0.0.2", "uid-kitchen")
    office = _Device("Office", "10.0.0.1", "uid-office")
    group = _Group(office, [kitchen, office])
    kitchen._group = group
    office._group = group
    # The selected speaker is Kitchen, but Office holds the queue.
    assert SonosPlayer(kitchen).coordinator_ip() == "10.0.0.1"


# ── join / unjoin ────────────────────────────────────────────────────

def test_join_points_at_the_coordinator() -> None:
    player, office, kitchen = _player_with_group()
    newcomer = _Zone("Porch", "10.0.0.4", "uid-porch")
    monkey = {"10.0.0.4": newcomer}
    orig = sonos_ctl.soco.SoCo
    sonos_ctl.soco.SoCo = lambda ip: monkey[ip]
    try:
        player.join("10.0.0.4")
    finally:
        sonos_ctl.soco.SoCo = orig
    assert newcomer.joined_to is office


def test_unjoin_targets_the_named_speaker() -> None:
    player, _, kitchen = _player_with_group()
    orig = sonos_ctl.soco.SoCo
    sonos_ctl.soco.SoCo = lambda ip: kitchen
    try:
        player.unjoin("10.0.0.2")
    finally:
        sonos_ctl.soco.SoCo = orig
    assert kitchen.unjoined is True


# ── the refusal ──────────────────────────────────────────────────────

@pytest.fixture
def dj_with_group(db: Database, cfg: Config):
    player = FakeSonosPlayer()
    player.members = [
        {"name": "Office", "ip": "10.0.0.1", "is_coordinator": True},
        {"name": "Kitchen", "ip": "10.0.0.2", "is_coordinator": False},
    ]
    return DJ(db, cfg, lambda: player), player


def test_unchecking_the_coordinator_is_refused(dj_with_group) -> None:
    dj, player = dj_with_group
    err = dj.set_group_member("10.0.0.1", False)
    assert err is not None
    assert "queue" in err
    # and crucially, nothing was asked of the speaker
    assert player.unjoined == []
    assert len(player.members) == 2


def test_unchecking_a_non_coordinator_unjoins_it(dj_with_group) -> None:
    dj, player = dj_with_group
    assert dj.set_group_member("10.0.0.2", False) is None
    assert player.unjoined == ["10.0.0.2"]


def test_checking_a_room_joins_it(dj_with_group) -> None:
    dj, player = dj_with_group
    assert dj.set_group_member("10.0.0.7", True) is None
    assert player.joined == ["10.0.0.7"]


def test_refusal_keys_on_the_coordinator_not_the_selected_room(dj_with_group) -> None:
    """The two diverge when a group is formed from the Sonos app, which can
    leave the selected room a slave. The queue is on the coordinator, so that
    is what gets protected — and the selected room becomes removable."""
    dj, player = dj_with_group
    player.members = [
        {"name": "Office", "ip": "10.0.0.1", "is_coordinator": False},
        {"name": "Kitchen", "ip": "10.0.0.2", "is_coordinator": True},
    ]
    assert dj.set_group_member("10.0.0.2", False) is not None   # coordinator
    assert dj.set_group_member("10.0.0.1", False) is None       # selected room


def test_no_speaker_reports_cleanly(db: Database, cfg: Config) -> None:
    dj = DJ(db, cfg, lambda: None)
    assert dj.set_group_member("10.0.0.2", True) == dj._NO_SPEAKER


def test_missing_ip_is_rejected(dj_with_group) -> None:
    dj, _ = dj_with_group
    assert dj.set_group_member("", True) == "missing ip"


def test_a_raising_player_invalidates_the_cache(db: Database, cfg: Config) -> None:
    # Matches group_all's contract: a cached-but-dead player must be dropped
    # so the next call rediscovers rather than reusing a corpse.
    player = FakeSonosPlayer()
    player.members = [{"name": "Office", "ip": "10.0.0.1", "is_coordinator": True}]

    def boom(ip):
        raise OSError("speaker gone")
    player.join = boom

    invalidated = []
    provider = lambda: player
    provider.invalidate = lambda: invalidated.append(1)
    dj = DJ(db, cfg, provider)

    assert dj.set_group_member("10.0.0.9", True) == "Speaker unreachable"
    assert invalidated == [1]


# ── HTTP surface ─────────────────────────────────────────────────────

@pytest.fixture
def client(db: Database, cfg: Config):
    player = FakeSonosPlayer()
    player.members = [
        {"name": "Office", "ip": "10.0.0.1", "is_coordinator": True},
        {"name": "Kitchen", "ip": "10.0.0.2", "is_coordinator": False},
    ]
    app = create_app(db, DJ(db, cfg, lambda: player), cfg)
    app.config["TESTING"] = True
    return app.test_client(), player


def test_endpoint_rejects_missing_ip(client) -> None:
    c, _ = client
    assert c.post("/api/speaker/group", json={"member": True}).get_json()["ok"] is False


def test_endpoint_refuses_to_drop_the_coordinator(client) -> None:
    c, player = client
    body = c.post("/api/speaker/group",
                  json={"ip": "10.0.0.1", "member": False}).get_json()
    assert body["ok"] is False
    assert player.unjoined == []


def test_endpoint_adds_a_room(client) -> None:
    c, player = client
    body = c.post("/api/speaker/group",
                  json={"ip": "10.0.0.5", "member": True}).get_json()
    assert body["ok"] is True
    assert player.joined == ["10.0.0.5"]


def test_members_ride_the_status_payload(client) -> None:
    c, _ = client
    speaker = c.get("/api/status").get_json()["speaker"]
    assert [m["name"] for m in speaker["members"]] == ["Kitchen", "Office"]
    assert speaker["members"][1]["is_coordinator"] is True
