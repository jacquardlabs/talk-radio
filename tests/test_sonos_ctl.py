import sonos_ctl
from config import Config
from db import Database
from sonos_ctl import find_speaker, hms_to_seconds, make_player_provider, seconds_to_hms


def test_hms_to_seconds() -> None:
    assert hms_to_seconds("0:12:34") == 754
    assert hms_to_seconds("1:00:05") == 3605
    assert hms_to_seconds("12:34") == 754
    assert hms_to_seconds("") == 0
    assert hms_to_seconds(None) == 0
    assert hms_to_seconds("NOT_IMPLEMENTED") == 0


def test_seconds_to_hms_roundtrip() -> None:
    assert seconds_to_hms(754) == "0:12:34"
    assert seconds_to_hms(3605) == "1:00:05"
    assert seconds_to_hms(-5) == "0:00:00"
    assert hms_to_seconds(seconds_to_hms(9999)) == 9999


def test_find_speaker_none_when_discovery_empty(db: Database, monkeypatch) -> None:
    cfg = Config.from_env({})
    monkeypatch.setattr(sonos_ctl.soco, "discover", lambda timeout=5: None)
    assert find_speaker(db, cfg) is None


def test_provider_caches_failed_discovery(db: Database, monkeypatch) -> None:
    cfg = Config.from_env({})
    calls: list[int] = []

    def fake_discover(timeout=5):
        calls.append(1)
        return None

    monkeypatch.setattr(sonos_ctl.soco, "discover", fake_discover)
    provider = make_player_provider(db, cfg, retry_seconds=3600)
    assert provider() is None
    assert provider() is None  # within retry window: no second discovery
    assert len(calls) == 1


class _FakeZone:
    def __init__(self, name: str, ip: str) -> None:
        self.player_name = name
        self.ip_address = ip


def test_provider_invalidate_forces_rediscovery(db: Database, monkeypatch) -> None:
    """A cached player that dies (speaker powered off, DHCP-changed IP) must
    be discoverable again — invalidate() must both drop the cache AND bypass
    the failed-discovery retry throttle, since this is a fresh failure, not
    a repeat of an already-throttled one. find_speaker itself is stubbed out
    here so this test isolates the provider's caching/invalidation logic
    from find_speaker's own ip-vs-discovery branching (covered separately)."""
    cfg = Config.from_env({})
    calls: list[int] = []

    class FakePlayer:
        ip = "10.0.0.50"
        name = "Kitchen"

    def fake_find_speaker(db_arg, cfg_arg):
        calls.append(1)
        return FakePlayer()

    monkeypatch.setattr(sonos_ctl, "find_speaker", fake_find_speaker)
    provider = make_player_provider(db, cfg, retry_seconds=3600)

    first = provider()
    assert first is not None
    assert len(calls) == 1

    again = provider()
    assert again is first  # cached — no rediscovery
    assert len(calls) == 1

    assert hasattr(provider, "invalidate")
    provider.invalidate()

    rediscovered = provider()
    assert rediscovered is not None
    assert len(calls) == 2  # invalidate forced a fresh lookup, not throttled


def test_find_speaker_explicit_ip_unreachable_does_not_fall_back(
        db: Database, monkeypatch) -> None:
    """An explicit saved speaker_ip is a user choice, not a suggestion. If
    it's momentarily unreachable, find_speaker must return None (retry
    later) rather than silently reassigning playback to a different,
    reachable speaker found via discovery."""
    cfg = Config.from_env({})
    db.kv_set("speaker_ip", "10.0.0.5")

    def fake_soco(ip):
        raise OSError("connection refused")

    monkeypatch.setattr(sonos_ctl.soco, "SoCo", fake_soco)
    monkeypatch.setattr(sonos_ctl.soco, "discover",
                        lambda timeout=5: {_FakeZone("Other Room", "10.0.0.9")})
    assert find_speaker(db, cfg) is None
