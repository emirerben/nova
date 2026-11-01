"""Every route's approval-card lines, both languages, with their typed codes, are the recorded
golden (`tests/fixtures/kria_replay/golden_lines.json`, KRI-1097 wave 2).

A line that changes wording or meaning (a shared sentence rewritten to say a value "is applied
exactly as planned") or its code changes the golden: re-record it with
``scripts/kria/score_route.py --write-golden-lines`` and the diff is what review reads.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "kria_replay" / "golden_lines.json"


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_route", REPO / "scripts" / "kria" / "score_route.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_route_line_is_the_recorded_golden() -> None:
    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))["routes"]
    current = _scorecard().golden_lines()
    changed = _scorecard()._diff_paths(recorded, current)
    assert not changed, (
        "route card lines changed; if meant, re-record with "
        "scripts/kria/score_route.py --write-golden-lines and review the golden diff",
        changed[:10],
    )
