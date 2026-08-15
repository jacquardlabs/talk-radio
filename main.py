"""Entry point: init DB, start the DJ and feed-refresh threads, run Flask."""
from __future__ import annotations

import logging
import os
import threading

import feeds
import sonos_ctl
from config import Config
from db import Database
from dj import DJ
from web import create_app

logger = logging.getLogger(__name__)


def dj_loop(cfg: Config, dj_instance: DJ, stop_event: threading.Event) -> None:
    """Reconcile the Sonos queue and check wake schedules, every
    TICK_SECONDS. One broken iteration never kills it.

    Deliberately does no fetching: feed refreshes are serial network I/O, a
    30s timeout per feed and minutes across a large library, and this loop
    has to hold its beat through them. An alarm that comes due while the
    library is being fetched must still fire on its own tick — sharing a
    thread with the refresh made it wait for the fetching to finish."""
    while not stop_event.is_set():
        try:
            # Ahead of check_schedules: warming is what makes the fire that
            # follows it, minutes later, quick.
            dj_instance.warm_next_start()
            dj_instance.check_schedules()
            dj_instance.tick()
        except Exception:
            logger.exception("dj loop iteration failed")
        stop_event.wait(cfg.tick_seconds)


def refresh_loop(db: Database, cfg: Config, stop_event: threading.Event) -> None:
    """Pull every feed, then sleep REFRESH_MINUTES. Runs on its own thread so
    the slow part of the system can't stall the part that has to be punctual.
    Refreshes first and waits after, so a restart picks up whatever published
    while the server was down. One broken pass never kills it."""
    while not stop_event.is_set():
        try:
            feeds.refresh_all(db, cfg)
        except Exception:
            logger.exception("feed refresh failed")
        stop_event.wait(cfg.refresh_minutes * 60)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.media_dir, exist_ok=True)
    db = Database(cfg.db_path)
    db.init()
    dj_instance = DJ(db, cfg, sonos_ctl.make_player_provider(db, cfg))
    stop_event = threading.Event()
    threading.Thread(target=dj_loop, args=(cfg, dj_instance, stop_event),
                     daemon=True, name="dj-loop").start()
    threading.Thread(target=refresh_loop, args=(db, cfg, stop_event),
                     daemon=True, name="feed-refresh").start()
    app = create_app(db, dj_instance, cfg)
    app.run(host=cfg.host, port=cfg.port, use_reloader=False)


if __name__ == "__main__":
    main()
