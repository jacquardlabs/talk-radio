#!/usr/bin/env python3
"""Hold every theme to the contrast ratios amber already owes.

Amber's own comment in base.html is the standard: --ink-dim may sit at
3.14:1 because it only ever draws a border, a glyph, or a deliberately-off
state, but anything meant to be READ takes --ink-2, and the board's text is
9-12px uppercase — not large text, so 4.5:1 is the bar.

Reads the palettes out of the stylesheets rather than keeping a third copy
of them. Run it from the repo root:  python tools/check_contrast.py
"""
from __future__ import annotations

import re
import sys

BASE = "templates/base.html"
THEMES = "static/themes.css"

# (foreground, background, minimum, what it is)
CHECKS: list[tuple[str, str, float, str]] = [
    ("--ink", "--bg", 4.5, "primary text on the board"),
    ("--ink-2", "--bg", 4.5, "metadata and labels, which are read"),
    ("--ink", "--bezel", 4.5, "a control's label on its face"),
    ("--ink-2", "--bezel", 4.5, "a control's secondary label"),
    ("--on-accent", "--accent", 4.5, "the label on a lit control"),
    ("--red", "--bg", 4.5, "news, danger, and the off-air key"),
    ("--green", "--bg", 4.5, "the healthy-feed marker"),
    ("--ink-dim", "--bg", 3.0, "borders and deliberately-off glyphs"),
]


def parse_hex(value: str) -> tuple[int, int, int] | None:
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", ",").split(",")]
        try:
            return tuple(int(float(p)) for p in parts[:3])  # type: ignore[return-value]
        except ValueError:
            return None
    return None


def luminance(rgb: tuple[int, int, int]) -> float:
    def chan(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def tokens(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)}


def resolve(name: str, decls: dict[str, str], depth: int = 0) -> tuple[int, int, int] | None:
    """Follow one var() alias chain to a literal colour."""
    if depth > 8 or name not in decls:
        return None
    value = decls[name]
    m = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())
    if m:
        return resolve(m.group(1), decls, depth + 1)
    return parse_hex(value)


def palettes() -> dict[str, dict[str, str]]:
    base_src = open(BASE, encoding="utf-8").read()
    m = re.search(r"^:root \{(.*?)^\}", base_src, re.S | re.M)
    if not m:
        sys.exit(f"{BASE}: could not find the :root block")
    amber = tokens(m.group(1))

    out = {"amber": amber}
    themes_src = open(THEMES, encoding="utf-8").read()
    # Only the token block for each theme — the component rules that
    # follow it set colours for one element, not for the palette.
    for tm in re.finditer(r'^\[data-theme="([a-z]+)"\] \{(.*?)^\}', themes_src, re.S | re.M):
        theme = dict(amber)
        theme.update(tokens(tm.group(2)))
        out[tm.group(1)] = theme
    return out


def main() -> int:
    failures: list[str] = []
    for name, decls in palettes().items():
        print(f"\n{name}")
        for fg_name, bg_name, minimum, what in CHECKS:
            fg, bg = resolve(fg_name, decls), resolve(bg_name, decls)
            if fg is None or bg is None:
                failures.append(f"{name}: cannot resolve {fg_name} on {bg_name}")
                print(f"  ??   {fg_name:12s} on {bg_name:10s}  unresolved")
                continue
            r = ratio(fg, bg)
            ok = r >= minimum
            if not ok:
                failures.append(
                    f"{name}: {fg_name} on {bg_name} is {r:.2f}:1, needs {minimum}:1 — {what}")
            print(f"  {'ok ' if ok else 'FAIL'} {fg_name:12s} on {bg_name:10s}"
                  f"  {r:5.2f}:1  (needs {minimum})")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nevery theme clears the bar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
