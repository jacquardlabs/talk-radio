"""The palette and its fonts, enforced.

The look is data, not code, so nothing else in the suite touches it: a mode
that fails contrast or a font file that was never committed would ship
green. These tests are the gate.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import check_contrast  # noqa: E402


@pytest.fixture(scope="module")
def base_html() -> str:
    with open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def palettes():
    """check_contrast reads repo-relative paths, as it does on the CLI."""
    prev = os.getcwd()
    os.chdir(ROOT)
    try:
        yield check_contrast.palettes()
    finally:
        os.chdir(prev)


def test_both_modes_clear_the_contrast_bar(palettes):
    """Anything meant to be read clears 4.5:1 on the surface behind it, in
    light and in dark, whether dark came from the system or the button."""
    assert set(palettes) == {"light", "dark", "dark (system)"}
    failures = []
    for name, decls in palettes.items():
        for fg, bg, minimum, what in check_contrast.CHECKS:
            a = check_contrast.resolve(fg, decls)
            b = check_contrast.resolve(bg, decls)
            assert a is not None and b is not None, f"{name}: cannot resolve {fg} on {bg}"
            r = check_contrast.ratio(a, b)
            if r < minimum:
                failures.append(f"{name}: {fg} on {bg} is {r:.2f}:1, needs {minimum}:1 ({what})")
    assert not failures, "\n".join(failures)


def test_dark_restates_the_colour_scheme(base_html):
    """color-scheme cannot be a var(); without it a dark page keeps light
    chrome on the alarm's time input and the scrollbars."""
    for sel in (r':root\[data-mode="dark"\] \{', r':root:not\(\[data-mode="light"\]\) \{'):
        block = re.search(sel + r"(.*?)\}", base_html, re.S)
        assert block and "color-scheme: dark" in block.group(1), sel


def test_every_font_the_page_names_is_committed(base_html):
    """`make deploy` ships `git archive HEAD`, so a font that was downloaded
    but never added falls back to a system face on the server only."""
    refs = re.findall(r'url\("(/static/[^"]+\.woff2)"\)', base_html)
    assert refs, "no @font-face sources found in base.html"
    missing = [r for r in refs if not os.path.exists(os.path.join(ROOT, r.lstrip("/")))]
    assert not missing, f"referenced but not committed: {missing}"


def test_no_font_is_committed_unused(base_html):
    """Deleting beats adding: an orphan face is bytes in every clone."""
    refs = {os.path.basename(r) for r in re.findall(r'url\("(/static/[^"]+\.woff2)"\)', base_html)}
    on_disk = {f for f in os.listdir(os.path.join(ROOT, "static", "fonts")) if f.endswith(".woff2")}
    assert on_disk - refs == set(), f"committed but unreferenced: {sorted(on_disk - refs)}"
