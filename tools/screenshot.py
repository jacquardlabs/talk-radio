# /// script
# requires-python = ">=3.12"
# dependencies = ["flask>=3.0", "playwright>=1.45"]
# ///
"""Recapture the README screenshots in docs/screenshots/ from this checkout.

The pages are served from the working tree's templates/ and static/, so the
shots show the code you're about to commit, not whatever the server runs.
Only the data is real: one /api/status snapshot, taken from a live instance
or a saved file, answers every poll. Page loads against the live box are
harmless; driving the transport for a screenshot is not — so the snapshot is
staged instead (playing, mid-episode, no sleep timer) and nothing is clicked.

    uv run tools/screenshot.py http://server:8080/api/status
    uv run tools/screenshot.py status.json

First run on a new machine:  uv run --with playwright playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, jsonify, render_template
from playwright.sync_api import Browser, Page, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"

def load_status(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=10) as resp:
            return json.load(resp)
    return json.loads(Path(source).read_text())


def stage(status: dict) -> dict:
    """On air, 40% through the episode, no sleep timer armed."""
    now = status.get("now_playing")
    if now is None:
        raise SystemExit("snapshot has nothing on air — take it while an episode is queued")
    return {
        **status,
        "transport": "PLAYING",
        "sleep": None,
        "now_playing": {**now, "position": int(now["duration"] * 0.4)},
    }


def stub_app(status: dict) -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"),
                static_folder=str(ROOT / "static"))
    app.add_url_rule("/", "index", lambda: render_template("board.html"))
    app.add_url_rule("/stations", "stations", lambda: render_template("stations.html"))
    app.add_url_rule("/api/status", "status", lambda: jsonify(status))
    return app


@contextmanager
def serve(app: Flask) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def open_page(browser: Browser, url: str, width: int, height: int,
              scale: int = 1, storage: dict[str, str] | None = None) -> Page:
    """A loaded page with artwork, fonts, and the first poll's render settled."""
    page = browser.new_page(viewport={"width": width, "height": height},
                            device_scale_factor=scale)
    seed = json.dumps(storage or {})
    page.add_init_script(
        f"for (const [k, v] of Object.entries({seed})) localStorage.setItem(k, v);")
    page.goto(url, wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(1500)  # the first poll renders the page
    return page


def jpeg(page: Page, path: Path, **opts) -> None:
    page.screenshot(path=str(path), type="jpeg", quality=85, **opts)
    print(f"wrote {path.relative_to(ROOT)}")


def shoot_pages(browser: Browser, base: str) -> None:
    # 792 ends on the divider under the fourth Up Next row, not through the fifth.
    jpeg(open_page(browser, f"{base}/", 1400, 792, storage={"mode": "light"}), OUT / "on-air.jpg")
    jpeg(open_page(browser, f"{base}/", 1400, 792, storage={"mode": "dark"}), OUT / "on-air-dark.jpg")
    jpeg(open_page(browser, f"{base}/", 390, 844, scale=2, storage={"mode": "light"}), OUT / "on-air-narrow.jpg")
    jpeg(open_page(browser, f"{base}/stations", 1400, 807, storage={"mode": "light"}), OUT / "stations.jpg")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("status", help="/api/status URL of a live instance, or a saved JSON file")
    args = parser.parse_args()
    status = stage(load_status(args.status))
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    with serve(stub_app(status)) as base, sync_playwright() as pw:
        browser = pw.chromium.launch()
        shoot_pages(browser, base)
        browser.close()


if __name__ == "__main__":
    main()
