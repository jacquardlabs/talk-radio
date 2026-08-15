import ipaddress

import pytest

from config import Config
from db import Database


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    d.init()
    return d


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.from_env({"DATA_DIR": str(tmp_path)})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the network: URL resolution becomes identity, and
    name resolution answers with a fixed public address.

    The DNS stub is what lets a test name a feed `https://x/rss` without a
    resolver: the fetch guard refuses a name it cannot resolve, so without
    this every placeholder host would read as an unsafe URL. A test that is
    *about* resolution overrides this with its own getaddrinfo.

    audio.py doesn't exist until Task 4 — skip patching until it does.
    """
    try:
        import audio
    except ModuleNotFoundError:
        yield
        return
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, user_agent: url)
    def resolve(host, port, *a, **kw):
        try:  # an address literal resolves to itself, as it really does
            addr = str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            addr = "93.184.216.34"
        family = (audio.socket.AF_INET6 if ":" in addr
                  else audio.socket.AF_INET)
        return [(family, audio.socket.SOCK_STREAM, 6, "", (addr, port))]

    monkeypatch.setattr(audio.socket, "getaddrinfo", resolve)
    yield
