#!/usr/bin/env python3
"""Parse a TASKS.md backlog into build-task dicts. Stdlib-only (mirrors admin.py).

Used by scripts/queue.sh `sync`: this module ONLY parses the markdown into
{title, body, priority} records (so it's unit-testable); queue.sh does the dedup
+ mint against the admin API. Reading this file's parsing rules:

- An item is a top-level unchecked list line: `- [ ] ...` (also `* [ ]`).
- `- [x]` (checked) items are skipped — the manual "stop considering this" lever.
- Title vs body split on the FIRST ` :: ` (or `::`). No `::` → whole line is title.
- A leading `(p<N>)` in the title sets priority (lower = sooner); default 100.
- Blank lines, headings (`#`), and prose are ignored.

Usage: python3 scripts/queue_sync.py TASKS.md   # prints one JSON object per line
"""

from __future__ import annotations

import json
import re
import sys

_ITEM_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<rest>.+?)\s*$")
_PRIORITY_RE = re.compile(r"^\((?:p|P)(?P<n>\d{1,4})\)\s*(?P<rest>.+)$")


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def parse_tasks_md(text: str) -> list[dict]:
    """Return [{title, body, priority}] for each UNCHECKED `- [ ]` item."""
    # Strip HTML comment blocks so commented-out example items aren't minted.
    text = _COMMENT_RE.sub("", text)
    tasks: list[dict] = []
    for line in text.splitlines():
        m = _ITEM_RE.match(line)
        if not m or m.group("mark").strip().lower() == "x":
            continue  # not an item, or already checked off
        rest = m.group("rest").strip()

        priority = 100
        pm = _PRIORITY_RE.match(rest)
        if pm:
            priority = int(pm.group("n"))
            rest = pm.group("rest").strip()

        if "::" in rest:
            title, body = rest.split("::", 1)
            title, body = title.strip(), body.strip()
        else:
            title, body = rest, ""
        if not title:
            continue
        tasks.append({"title": title, "body": body, "priority": priority})
    return tasks


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: queue_sync.py <TASKS.md>", file=sys.stderr)
        return 2
    try:
        text = open(argv[1], encoding="utf-8").read()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for task in parse_tasks_md(text):
        print(json.dumps(task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
