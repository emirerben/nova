"""Every route table's render entry and recipe paths are its pin
(`tests/fixtures/kria_replay/render_entries.json`, KRI-1097 wave 2), and the pinned job runner
renders from that entry.

The route replays grade the value at the table's recipe paths in what its render entry returns,
and the table is the one file a lane owns. The pin keeps that choice in a grader-owned file: a
lane that moves its entry or a path edits the pin in the same commit and the diff is what review
reads, like the card-line golden. The pinned runner must import the entry, call it and use its
value, and a function of the worker module must call the runner, so an echo function no
production path renders from cannot stand in for the entry.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_route", REPO / "scripts" / "kria" / "score_route.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_route_renders_from_its_pinned_entry() -> None:
    from app.kria.route_capabilities import ROUTE_TABLES

    card = _scorecard()
    problems = {str(rid): card.pin_problems(str(rid)) for rid in ROUTE_TABLES}
    assert not any(problems.values()), (
        "a route's RENDER_ENTRY differs from render_entries.json, or its pinned runner does not "
        "render from it; if the move is meant, edit the pin in the same commit",
        {route: why for route, why in problems.items() if why},
    )
