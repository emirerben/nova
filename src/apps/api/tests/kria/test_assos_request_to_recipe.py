"""Assos: does the creator's prompt reach the agents, and do their decisions reach the renderer?

Thin wrapper over the scorecard (`scripts/kria/score_assos.py`, `make kria-replay FIXTURE=assos`),
which replays the recorded request through the real card, worker, research, text-composition,
phone-text-style and device-recipe code with recorded or reconstructed model text.

Checks 1-9 started as the development baseline: RED on the unchanged code on purpose, held as
`xfail(strict=True)` so the first phase that turned one green failed the run until its marker was
removed (the ratchet). All nine are green now and carry no marker.
Check 10 is report-only by owner rule (the product never fails a user for these gaps): it must
run and list its gaps, never fail. Check 11 (no catalog music on the phone) is already green.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[5]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kria_replay" / "assos"


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_assos", REPO / "scripts" / "kria" / "score_assos.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report():
    card = _scorecard()
    patcher = pytest.MonkeyPatch()

    def blocked(*_a, **_k):
        raise card._BASE.OfflineViolation("network access is blocked in the Assos replay")

    patcher.setattr(socket.socket, "connect", blocked)
    patcher.setattr(socket, "create_connection", blocked)
    try:
        yield card.run_offline()
    finally:
        patcher.undo()


def _check(report, check_id):
    return next(c for c in report["checks"] if c["id"] == check_id)


def test_negative_controls_tell_ignoring_from_honouring(report) -> None:
    bad = [c for c in report["negative_controls"] if not c["ok"]]
    assert not bad, bad


def test_no_check_is_inconclusive(report) -> None:
    broken = [
        (c["id"], c["why_it_fails"]) for c in report["checks"] if c["verdict"] == "INCONCLUSIVE"
    ]
    assert not broken, broken


def test_fixtures_are_scrubbed() -> None:
    for name in ("fixture.json", "research.json"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        for needle in ("users/", "gs://", "X-Goog", "Signature=", "@gmail"):
            assert needle not in text, (name, needle)
        for token in text.split('"'):
            assert not token.startswith("http") or any(
                ok in token for ok in ("SCRUBBED", "scrubbed.invalid", "www.w3.org/2000/svg")
            ), (name, token[:80])


@pytest.mark.skipif(
    not os.environ.get("ASSOS_EXPECT_BASELINE"),
    reason="baseline-only: later phases change the card and recipe on purpose",
)
def test_unchanged_code_reproduces_prod(report) -> None:
    drift = [r["name"] for r in report["reproduction"] if not r["reproduced"]]
    assert not drift, drift


@pytest.mark.parametrize(
    "check_id",
    [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        11,
    ],
)
def test_prompt_fidelity_check(report, check_id) -> None:
    result = _check(report, check_id)
    assert result["pass"], result["why_it_fails"]


def test_consumer_gate_is_report_only(report) -> None:
    result = _check(report, 10)
    assert result["report_only"] is True
    assert result["verdict"].startswith("REPORT"), result["why_it_fails"]
    assert isinstance(result["evidence"]["undisclosed"], list)
    json.dumps(result["evidence"])
