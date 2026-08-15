from config import Config


def test_defaults() -> None:
    cfg = Config.from_env({})
    assert cfg.data_dir == "./data"
    assert cfg.db_path == "./data/radio.db"
    assert cfg.media_dir == "./data/media"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8080
    assert cfg.sonos_speaker is None
    assert cfg.sonos_ip is None
    assert cfg.tick_seconds == 15
    assert cfg.refresh_minutes == 30
    assert cfg.queue_ahead == 10
    assert cfg.news_max_age_hours == 24
    assert cfg.download_mode is False
    assert cfg.base_url is None
    assert cfg.user_agent == "SonosTalkRadio/1.0"
    assert cfg.grace_minutes == 10


def test_overrides_and_derived_paths() -> None:
    cfg = Config.from_env({"DATA_DIR": "/x", "PORT": "9090", "DOWNLOAD_MODE": "1"})
    assert cfg.db_path == "/x/radio.db"
    assert cfg.media_dir == "/x/media"
    assert cfg.port == 9090
    assert cfg.download_mode is True


def test_explicit_db_path_beats_derived() -> None:
    cfg = Config.from_env({"DATA_DIR": "/x", "DB_PATH": "/elsewhere/r.db"})
    assert cfg.db_path == "/elsewhere/r.db"
