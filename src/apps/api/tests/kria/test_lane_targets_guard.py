"""`tests/fixtures/kria_replay/lane_targets.json` only moves toward green from its foundation
copy, `lane_targets.foundation.json` (KRI-1097 wave 2).

A lane owns its route table and its replay module, and edits lane_targets.json when it lands.
Without a fixed point it could also drop a must_grade entry (hide a red), add a target (relabel
a regression as expected) or remove a target whose check is still red. The scorecard's
`target_drift` refuses all three: must_grade only grows, targets only shrink, and each removed
target must be VERIFIED by the grader now. Lanes never edit the foundation copy.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[5]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kria_replay"


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_route", REPO / "scripts" / "kria" / "score_route.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verified(card, fixture: str, monkeypatch) -> set[str]:
    def blocked(*_a, **_k):
        raise card._BASE.OfflineViolation("network access is blocked in the route replays")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    report = card.run_offline(FIXTURES / fixture)
    return {str(c["id"]) for c in report["checks"] if c["verdict"] == "VERIFIED"}


def _targets(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixtures() -> list[str]:
    """Every fixture either file names. Read at collection so each fixture is its own
    test: three of them replay a whole route (about 15 s each on a CI runner), and as one
    test they sat at 45 s against the suite's 60 s timeout."""
    return sorted(
        {
            name
            for data in (_targets("lane_targets.json"), _targets("lane_targets.foundation.json"))
            for part in ("must_grade", "targets")
            for name in data.get(part) or {}
        }
    )


def test_the_guard_reads_the_files_the_scorecard_reads() -> None:
    card = _scorecard()
    assert card.LANE_TARGETS_FOUNDATION.is_file(), "the foundation copy is missing"
    assert card.LANE_TARGETS == FIXTURES / "lane_targets.json"
    assert card.LANE_TARGETS_FOUNDATION == FIXTURES / "lane_targets.foundation.json"
    assert _fixtures(), "no fixture is named in lane_targets.json or its foundation copy"


@pytest.mark.parametrize("fixture", _fixtures())
def test_lane_targets_only_move_toward_green(fixture: str, monkeypatch) -> None:
    card = _scorecard()
    current = _targets("lane_targets.json")
    foundation = _targets("lane_targets.foundation.json")
    was = (foundation.get("targets") or {}).get(fixture) or {}
    now = (current.get("targets") or {}).get(fixture) or {}
    removed = set(was) - set(now)
    verified = _verified(card, fixture, monkeypatch) if removed else set()
    drift = card.target_drift(fixture, verified, current, foundation)
    assert not drift, (
        "lane_targets.json moved off its foundation copy other than toward green",
        {fixture: drift},
    )
