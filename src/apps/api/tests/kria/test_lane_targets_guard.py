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


def test_lane_targets_only_move_toward_green(monkeypatch) -> None:
    card = _scorecard()
    assert card.LANE_TARGETS_FOUNDATION.is_file(), "the foundation copy is missing"
    current = json.loads(card.LANE_TARGETS.read_text(encoding="utf-8"))
    foundation = json.loads(card.LANE_TARGETS_FOUNDATION.read_text(encoding="utf-8"))
    fixtures = {
        name
        for data in (current, foundation)
        for part in ("must_grade", "targets")
        for name in data.get(part) or {}
    }
    drift = {}
    for fixture in sorted(fixtures):
        was = (foundation.get("targets") or {}).get(fixture) or {}
        now = (current.get("targets") or {}).get(fixture) or {}
        removed = set(was) - set(now)
        verified = _verified(card, fixture, monkeypatch) if removed else set()
        drift[fixture] = card.target_drift(fixture, verified, current, foundation)
    assert not any(drift.values()), (
        "lane_targets.json moved off its foundation copy other than toward green",
        {fixture: why for fixture, why in drift.items() if why},
    )
