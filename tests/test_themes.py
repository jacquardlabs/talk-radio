"""The theme contract, enforced.

The look is data, not code, so nothing else in the suite touches it: a
palette that fails contrast, a font file that was never committed, or a
theme the picker offers and the stylesheet does not define would all ship
green. These tests are the gate the README promises.
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
def themes_css() -> str:
    with open(os.path.join(ROOT, "static", "themes.css"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def palettes(monkeypatch_cwd) -> dict:
    return check_contrast.palettes()


@pytest.fixture(scope="module")
def monkeypatch_cwd():
    """check_contrast reads repo-relative paths, as it does on the CLI."""
    prev = os.getcwd()
    os.chdir(ROOT)
    yield
    os.chdir(prev)


def test_every_theme_clears_the_contrast_bar(palettes):
    """Amber's own rule, applied to all eight: --ink-dim may sit at 3:1
    because it only draws borders and off states, but anything meant to be
    read clears 4.5:1 on the surface behind it."""
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


def test_picker_and_stylesheet_agree(base_html, themes_css):
    """A theme the picker offers but the stylesheet does not define renders
    as amber under a different name; one defined but not offered is dead."""
    offered = set(re.findall(r'^\s*\["([a-z]+)", "', base_html, re.M))
    defined = set(re.findall(r'^:root\[data-theme="([a-z]+)"\] \{', themes_css, re.M))
    assert offered, "the picker's THEMES list was not found in base.html"
    # Amber is the :root block in base.html, so it is offered but not in themes.css.
    assert offered - {"amber"} == defined, (
        f"offered but undefined: {sorted(offered - {'amber'} - defined)}; "
        f"defined but not offered: {sorted(defined - offered)}")


def test_every_font_the_stylesheet_names_is_committed(themes_css):
    """`make deploy` ships `git archive HEAD`, so a font that was downloaded
    but never added falls back to a system face on the server only."""
    refs = re.findall(r'url\("(/static/[^"]+\.woff2)"\)', themes_css)
    assert refs, "no @font-face sources found in themes.css"
    missing = [r for r in refs if not os.path.exists(os.path.join(ROOT, r.lstrip("/")))]
    assert not missing, f"referenced but not committed: {missing}"


def test_no_font_is_committed_unused(themes_css):
    """Deleting beats adding: an orphan face is bytes in every clone."""
    refs = {os.path.basename(r) for r in re.findall(r'url\("(/static/[^"]+\.woff2)"\)', themes_css)}
    on_disk = {f for f in os.listdir(os.path.join(ROOT, "static", "fonts")) if f.endswith(".woff2")}
    assert on_disk - refs == set(), f"committed but unreferenced: {sorted(on_disk - refs)}"


def test_themes_do_not_shrink_the_seek_target(themes_css):
    """The seek bar is drag-to-seek and was specified at >=44px. A theme may
    move .track's padding to change the band it draws; it may not set a
    height, which would shrink the box the finger actually hits."""
    offenders = []
    for m in re.finditer(r'\[data-theme="([a-z]+)"\] \.track \{([^}]*)\}', themes_css):
        if re.search(r"(?<!line-)height\s*:", m.group(2)):
            offenders.append(m.group(1))
    assert not offenders, f"themes setting a height on .track: {offenders}"


def test_light_themes_declare_their_colour_scheme(themes_css):
    """color-scheme cannot be a var(), so each theme restates it; without it
    a light board keeps dark chrome on the alarm's time input."""
    for m in re.finditer(r'^:root\[data-theme="([a-z]+)"\] \{(.*?)^\}', themes_css, re.S | re.M):
        assert "color-scheme:" in m.group(2), f"{m.group(1)} declares no color-scheme"
