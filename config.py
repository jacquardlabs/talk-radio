"""All configuration enters here, from env vars, with defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Config:
    data_dir: str
    db_path: str
    media_dir: str
    host: str
    port: int
    sonos_speaker: str | None
    sonos_ip: str | None
    tick_seconds: int
    refresh_minutes: int
    queue_ahead: int
    news_max_age_hours: int
    download_mode: bool
    base_url: str | None
    user_agent: str
    grace_minutes: int
    warm_minutes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        e: Mapping[str, str] = os.environ if env is None else env
        data_dir = e.get("DATA_DIR", "./data")
        return cls(
            data_dir=data_dir,
            db_path=e.get("DB_PATH", os.path.join(data_dir, "radio.db")),
            media_dir=e.get("MEDIA_DIR", os.path.join(data_dir, "media")),
            host=e.get("HOST", "0.0.0.0"),
            port=int(e.get("PORT", "8080")),
            sonos_speaker=e.get("SONOS_SPEAKER") or None,
            sonos_ip=e.get("SONOS_IP") or None,
            tick_seconds=int(e.get("TICK_SECONDS", "15")),
            refresh_minutes=int(e.get("REFRESH_MINUTES", "30")),
            queue_ahead=int(e.get("QUEUE_AHEAD", "10")),
            news_max_age_hours=int(e.get("NEWS_MAX_AGE_HOURS", "24")),
            download_mode=e.get("DOWNLOAD_MODE", "0") == "1",
            base_url=e.get("BASE_URL") or None,
            user_agent=e.get("USER_AGENT", "SonosTalkRadio/1.0"),
            grace_minutes=int(e.get("GRACE_MINUTES", "10")),
            warm_minutes=int(e.get("WARM_MINUTES", "5")),
        )
