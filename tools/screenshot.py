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
import base64
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

# (id, label under the panel) — ids match THEMES in templates/base.html.
THEMES: list[tuple[str, str]] = [
    ("amber", "Amber — dot-matrix VFD (default)"),
    ("solari", "Solari — split-flap departures board"),
    ("ceefax", "Ceefax — teletext, page 100"),
    ("jcard", "J-Card — cassette inlay"),
    ("rams", "Rams — Braun hi-fi, 1962"),
    ("storm", "Quiet Storm — late-night FM"),
    ("console", "Console — mixing desk"),
    ("longwave", "Longwave — shortwave dial"),
]


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
    page.wait_for_timeout(1500)  # Solari's flaps animate on the first title
    return page


def jpeg(page: Page, path: Path, **opts) -> None:
    page.screenshot(path=str(path), type="jpeg", quality=85, **opts)
    print(f"wrote {path.relative_to(ROOT)}")


def shoot_pages(browser: Browser, base: str) -> None:
    # 792 ends on the divider under the fifth Up Next row, not through the sixth.
    jpeg(open_page(browser, f"{base}/", 1400, 792), OUT / "on-air.jpg")
    jpeg(open_page(browser, f"{base}/", 390, 844, scale=2), OUT / "on-air-narrow.jpg")
    jpeg(open_page(browser, f"{base}/stations", 1400, 807,
                   storage={"artModeV2": "color"}), OUT / "stations.jpg")


def deck_clip(page: Page) -> dict[str, float]:
    """The deck down through the transport, with a little margin that stops
    short of the header (Ceefax paints it as a band)."""
    top = page.locator(".deck").bounding_box()
    bottom = page.locator(".transport").bounding_box()
    header = page.locator("header").bounding_box()
    pad = 12
    y = max(top["y"] - pad, header["y"] + header["height"] + 1)
    return {"x": top["x"] - pad, "y": y, "width": top["width"] + 2 * pad,
            "height": bottom["y"] + bottom["height"] + pad - y}


def theme_panel(browser: Browser, base: str, theme: str, label: str) -> str:
    page = open_page(browser, f"{base}/", 1100, 700, scale=2, storage={"theme": theme})
    png = base64.b64encode(page.screenshot(clip=deck_clip(page))).decode()
    page.close()
    return (f'<figure><figcaption>{label}</figcaption>'
            f'<img src="data:image/png;base64,{png}"></figure>')


def shoot_themes(browser: Browser, base: str) -> None:
    panels = [theme_panel(browser, base, theme, label) for theme, label in THEMES]
    sheet = browser.new_page(viewport={"width": 1178, "height": 400})
    sheet.set_content(
        "<style>body{margin:0;padding:6px 16px 16px;background:#ecebe6;"
        "font:15px/1.3 Helvetica,Arial,sans-serif;color:#222}"
        "main{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}"
        "figure{margin:0}figcaption{padding:8px 0 6px}"
        "img{display:block;width:100%;border:1px solid #999}</style>"
        f"<main>{''.join(panels)}</main>", wait_until="load")
    jpeg(sheet, OUT / "themes.jpg", full_page=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("status", help="/api/status URL of a live instance, or a saved JSON file")
    args = parser.parse_args()
    status = stage(load_status(args.status))
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    with serve(stub_app(status)) as base, sync_playwright() as pw:
        browser = pw.chromium.launch()
        shoot_pages(browser, base)
        shoot_themes(browser, base)
        browser.close()


if __name__ == "__main__":
    main()
