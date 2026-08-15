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
    """Tests never touch the network: URL resolution becomes identity.

    audio.py doesn't exist until Task 4 — skip patching until it does.
    """
    try:
        import audio
    except ModuleNotFoundError:
        yield
        return
    monkeypatch.setattr(audio, "resolve_audio_url", lambda url, user_agent: url)
    yield
