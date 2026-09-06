"""One-off repair for titles ingested before entry_title() existed (feeds.py):
feeds.title and episodes.title were stored straight from feedparser, so a
feed that double-encodes ('&amp;ndash;' -> feedparser's single decode ->
'&ndash;') left the literal entity in the row instead of the character it
names. Re-runs the same decode/strip over what's already stored.

Dry-run by default — prints every row that would change. Pass --apply to
write. Safe to re-run: rows already clean are no-ops.

    python tools/fix_stale_titles.py /path/to/radio.db
    python tools/fix_stale_titles.py /path/to/radio.db --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    """Mirrors feeds.py's _TextExtractor. Kept standalone so this script
    runs with only the stdlib — no need to have the app importable on
    whatever host holds the database."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _clean(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw or "")
        parser.close()
    except Exception:
        return raw
    text = re.sub(r"\s+", " ", parser.text()).strip()
    return text or raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run report only)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in ("feeds", "episodes"):
            rows = conn.execute(f"SELECT id, title FROM {table}").fetchall()
            changes = [(r["id"], r["title"], _clean(r["title"])) for r in rows]
            changes = [(i, old, new) for i, old, new in changes if old != new]
            print(f"{table}: {len(changes)} of {len(rows)} rows change")
            for i, old, new in changes:
                print(f"  [{i}] {old!r} -> {new!r}")
            if args.apply and changes:
                conn.executemany(
                    f"UPDATE {table} SET title=? WHERE id=?",
                    [(new, i) for i, _old, new in changes])
        if args.apply:
            conn.commit()
            print("applied")
        else:
            print("dry run — no changes written; pass --apply to write")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
