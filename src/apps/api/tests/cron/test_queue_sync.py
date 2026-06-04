"""Unit tests for the TASKS.md parser (scripts/queue_sync.py).

The parser is the only non-trivial logic in the intake path (queue.sh does the
HTTP via admin.py), so we lock its rules: unchecked-only, title/body split, the
priority prefix, and that prose/headings/checked items are ignored.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "queue_sync.py").exists():
            return parent
    raise RuntimeError("repo root not found")


_spec = importlib.util.spec_from_file_location(
    "queue_sync", _repo_root() / "scripts" / "queue_sync.py"
)
queue_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(queue_sync)
parse = queue_sync.parse_tasks_md


def test_parses_unchecked_items_only() -> None:
    md = """\
# Backlog
Some prose that is not a task.

- [ ] First task
- [x] Already done, must be ignored
* [ ] Second task with an asterisk bullet
  - [ ] indented still counts
"""
    titles = [t["title"] for t in parse(md)]
    assert titles == ["First task", "Second task with an asterisk bullet", "indented still counts"]


def test_title_body_split_on_double_colon() -> None:
    [task] = parse("- [ ] Add a retry :: wrap upload in tenacity retry(3) + a test")
    assert task["title"] == "Add a retry"
    assert task["body"] == "wrap upload in tenacity retry(3) + a test"
    assert task["priority"] == 100


def test_no_body_when_no_double_colon() -> None:
    [task] = parse("- [ ] Just a title")
    assert task["title"] == "Just a title"
    assert task["body"] == ""


def test_priority_prefix() -> None:
    [task] = parse("- [ ] (p10) Urgent thing :: do it")
    assert task["priority"] == 10
    assert task["title"] == "Urgent thing"
    assert task["body"] == "do it"


def test_checked_and_prose_ignored() -> None:
    assert parse("- [x] done\nnot a task\n## heading\n") == []


def test_html_comment_items_ignored() -> None:
    # Commented-out example items (the TASKS.md template) must not be minted.
    md = """\
- [ ] Real task
<!--
     Example:
     - [ ] (p50) Commented example :: should be ignored
-->
- [ ] Another real task
"""
    assert [t["title"] for t in parse(md)] == ["Real task", "Another real task"]
