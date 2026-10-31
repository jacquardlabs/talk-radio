#!/usr/bin/env python3
"""Hold both modes, light and dark, to the contrast the page needs.

Anything meant to be read is --ink or --slate and clears 4.5:1 on the
surfaces it sits on; --tally carries words too (news, Never play), so it
owes the same. Reads the palettes out of base.html rather than keeping a
second copy of them. Run it from the repo root:  python tools/check_contrast.py
"""
from __future__ import annotations

import re
import sys

BASE = "templates/base.html"

# (foreground, background, minimum, what it is)
CHECKS: list[tuple[str, str, float, str]] = [
    ("--ink", "--paper", 4.5, "text on the page"),
    ("--slate", "--paper", 4.5, "secondary text, which is read"),
    ("--ink", "--panel", 4.5, "text in a menu, sheet, or the dock"),
    ("--slate", "--panel", 4.5, "secondary text in a menu or sheet"),
    ("--tally", "--paper", 4.5, "news, and the words of a destructive action"),
    ("--tally", "--panel", 4.5, "Never play in a menu"),
    ("--paper", "--ink", 4.5, "the label on a filled button: Play, a lit chip"),
    ("--paper", "--tally", 4.5, "Sure? on an armed destructive button"),
    ("--slate", "--paper", 3.0, "a switch that is off, a drag handle"),
    ("--arc", "--paper", 3.0, "the rest of the playing episode, on the dial"),
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
    src = open(BASE, encoding="utf-8").read()
    light = re.search(r"^:root \{(.*?)^\}", src, re.S | re.M)
    dark = re.search(r'^:root\[data-mode="dark"\] \{(.*?)^\}', src, re.S | re.M)
    system = re.search(r'^  :root:not\(\[data-mode="light"\]\) \{(.*?)^  \}', src, re.S | re.M)
    # A palette that stops being found is a check that stops running, and a
    # green result is exactly what that looks like.
    if not (light and dark and system):
        sys.exit(f"{BASE}: could not find the light, dark, and system-dark token blocks")
    base = tokens(light.group(1))
    out = {"light": base, "dark": {**base, **tokens(dark.group(1))},
           "dark (system)": {**base, **tokens(system.group(1))}}
    if out["dark"] != out["dark (system)"]:
        sys.exit(f"{BASE}: the system-dark block and [data-mode=dark] disagree")
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
    print("\nboth modes clear the bar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
