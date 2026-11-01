"""Every render route: does each planner-chosen field and each stated ask reach the render, or
the approval card before approval? (KRI-1097 wave 2)

Thin wrapper over the route scorecard (`scripts/kria/score_route.py`,
`make kria-replay FIXTURE=<route>`), one recorded or reconstructed draft per route under
`tests/fixtures/kria_replay/<route>/`. The card is assembled from the pure functions the draft
turn runs; no model, network or database.

Disclosure checks (kind ``route`` / ``disclosed_ok``) must pass. Render checks (kind
``honored``) are the lanes' targets and are red until a lane lands its route replay. Every red
check is listed in `tests/fixtures/kria_replay/lane_targets.json` with its lane; a red check
missing from the list fails, and so does a listed one that turned green (the lane removes it).
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[5]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kria_replay"
ROUTES = (
    "voiceover_montage",
    "talking_head",
    "narrated",
    "lipsync_song",
    "speech_excerpt",
    "slide_post",
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_route", REPO / "scripts" / "kria" / "score_route.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reports():
    card = _scorecard()
    patcher = pytest.MonkeyPatch()

    def blocked(*_a, **_k):
        raise card._BASE.OfflineViolation("network access is blocked in the route replays")

    patcher.setattr(socket.socket, "connect", blocked)
    patcher.setattr(socket, "create_connection", blocked)
    try:
        yield {route: card.run_offline(FIXTURES / route) for route in ROUTES}
    finally:
        patcher.undo()


@pytest.mark.parametrize("route", ROUTES)
def test_negative_controls_tell_ignoring_from_honouring(reports, route) -> None:
    bad = [c for c in reports[route]["negative_controls"] if c["ok"] is not True]
    assert not bad, bad


@pytest.mark.parametrize("route", ROUTES)
def test_no_check_is_inconclusive(reports, route) -> None:
    broken = [
        (c["id"], c["why_it_fails"])
        for c in reports[route]["checks"]
        if c["verdict"] == "INCONCLUSIVE"
    ]
    assert not broken, broken


@pytest.mark.parametrize("route", ROUTES)
def test_fixture_is_scrubbed(route) -> None:
    for path in sorted((FIXTURES / route).glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for needle in ("users/", "gs://", "X-Goog", "Signature=", "@gmail", "creation-threads"):
            assert needle not in text, (path.name, needle)
        for token in text.split('"'):
            assert not token.startswith("http") or "scrubbed.invalid" in token, (
                path.name,
                token[:80],
            )
        stray = {u for u in _UUID.findall(text) if u != "00000000-0000-4000-8000-000000000000"}
        assert not stray, (path.name, sorted(stray)[:3])


@pytest.mark.parametrize("route", ROUTES)
def test_disclosure_checks_pass(reports, route) -> None:
    report = reports[route]
    red = [
        (c["id"], c["why_it_fails"])
        for c in report["checks"]
        if c["kind"] in {"route", "disclosed_ok"}
        and not c["pass"]
        and str(c["id"]) not in report["lane_targets"]
    ]
    assert not red, red


@pytest.mark.parametrize("route", ROUTES)
def test_lane_targets_are_current(reports, route, record_property) -> None:
    report = reports[route]
    record_property("expected_red", ",".join(report["expected_red"]))
    assert not report["unexpected_red"], (
        "red checks not listed in lane_targets.json",
        report["unexpected_red"],
    )
    assert not report["unexpected_green"], (
        "lane targets that now pass: remove them from lane_targets.json",
        report["unexpected_green"],
    )


@pytest.mark.parametrize("route", ROUTES)
def test_consumer_gate_is_report_only(reports, route) -> None:
    gate = next(c for c in reports[route]["checks"] if c["id"] == 10)
    assert gate["report_only"] is True
    assert gate["verdict"].startswith("REPORT"), gate["why_it_fails"]
    assert isinstance(gate["evidence"]["undisclosed"], list)
    json.dumps(gate["evidence"])
