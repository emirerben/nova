"""Fixture integrity, checker satisfiability, and the East Run baseline pin.

Re-stamp the pin on purpose, after reviewing why a status moved:

    cd src/apps/api && python -m tests.evals.request_following.capture_east_run   # re-reads prod
    # or, to re-pin without re-reading prod, call runner.stamp_baseline(path)
"""

from __future__ import annotations

import unicodedata

import pytest

from .checkers import CHECKERS
from .models import REQUEST_TYPES, FinalPlan
from .runner import (
    FIXTURE_ROOT,
    discover_fixture_paths,
    load_fixture,
    load_footage,
    run_thread,
    score_reference,
)
from .scorer import score

PATHS = discover_fixture_paths()
IDS = [p.stem for p in PATHS]


def test_the_wave_1_fixture_set_is_present():
    """East Run + 12 authored briefs over 4 more footage sets."""
    fixtures = [load_fixture(p) for p in PATHS]
    assert [f.fixture_id for f in fixtures if f.provenance == "prod_capture"] == ["east_run"]
    assert len([f for f in fixtures if f.provenance == "authored"]) == 12
    assert {f.footage for f in fixtures} == {"east_run", "food_day", "trip", "sport", "vlog"}


def test_every_coverage_table_row_has_a_requirement():
    covered = {r.request_type for p in PATHS for r in load_fixture(p).requirements}
    assert covered == set(REQUEST_TYPES), sorted(set(REQUEST_TYPES) - covered)


def test_at_least_one_thread_is_multi_turn_and_east_run_is_fixture_one():
    fixtures = [load_fixture(p) for p in PATHS]
    assert any(len(f.turns) > 1 for f in fixtures if f.provenance == "authored")
    assert len(load_fixture(FIXTURE_ROOT / "threads" / "east_run.json").turns) == 7


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_fixture_is_well_formed(path):
    fixture = load_fixture(path)
    footage = load_footage(fixture.footage)
    clip_ids = {c.clip_id for c in footage.clips}
    for req in fixture.requirements:
        assert req.checker in CHECKERS, f"{req.id}: unknown checker {req.checker}"
    plans: list[FinalPlan] = []
    for turn in fixture.turns:
        if turn.recorded:
            plans.append(turn.recorded.plan_after)
    if fixture.reference:
        plans += [fixture.reference.plan_after]
        if fixture.reference.plan_before:
            plans.append(fixture.reference.plan_before)
    for plan in plans:
        assert {c.clip_id for c in plan.clips} <= clip_ids
    for req in fixture.requirements:
        for key in ("clip_ids", "sequence"):
            assert set(req.params.get(key, [])) <= clip_ids, f"{req.id}: {key} names a missing clip"
        for key in ("labels", "clips"):
            assert set(req.params.get(key, {})) <= clip_ids, f"{req.id}: {key} names a missing clip"


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_fixture_carries_no_storage_paths_or_user_ids(path):
    text = path.read_text(encoding="utf-8")
    assert "users/" not in text and "gcs_path" not in text and "@" not in text


@pytest.mark.parametrize("path", [p for p in PATHS if p.stem != "east_run"], ids=lambda p: p.stem)
def test_authored_reference_meets_every_requirement(path):
    """A checker nobody can pass cannot judge anything."""
    fixture = load_fixture(path)
    result = score_reference(fixture)
    not_met = [(s.requirement_id, s.status, s.reason) for s in result.scores if s.status != "met"]
    assert not not_met, not_met
    assert not any(s.reply_overclaims for s in result.scores)


@pytest.mark.parametrize("path", [p for p in PATHS if p.stem != "east_run"], ids=lambda p: p.stem)
def test_an_untouched_attachment_order_edit_does_not_pass(path):
    """...and it must be discriminating: doing nothing about the brief is not a pass."""
    fixture = load_fixture(path)
    footage = load_footage(fixture.footage)
    naive = FinalPlan(
        clips=[
            {"clip_id": c.clip_id, "start_s": i * 3.0, "end_s": i * 3.0 + 3.0}
            for i, c in enumerate(footage.clips)
        ]
    )
    previous = fixture.reference.plan_before or naive
    scores = score(fixture.requirements, naive, None, footage=footage, previous_plan=previous)
    assert any(s.status != "met" for s in scores), [s.requirement_id for s in scores]


def test_authored_footage_is_attached_out_of_filming_order():
    """Otherwise chronological-order requirements would pass for free."""
    for footage_id in ("food_day", "trip", "sport", "vlog"):
        footage = load_footage(footage_id)
        when = [c.fact("capture_time").value for c in footage.clips]
        assert when != sorted(when), footage_id


def test_exact_title_fixture_keeps_unicode_intact():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "food_exact_title.json")
    literal = fixture.requirements[0].params["literal"]
    assert literal == unicodedata.normalize("NFC", literal)
    assert "ı" in literal and "ö" in literal
    assert fixture.reference.plan_after.title().text == literal


@pytest.mark.parametrize("path", [p for p in PATHS if p.stem != "east_run"], ids=lambda p: p.stem)
def test_authored_threads_are_reported_as_awaiting_recordings(path):
    result = run_thread(load_fixture(path))
    assert result.unrecorded and result.scores == []


# ── East Run: the measured baseline ──────────────────────────────────────────


def test_east_run_baseline_is_pinned():
    """Statuses today, replayed through the real v1 copilot path. When a phase moves one,
    this fails until the pin is re-stamped deliberately (and the movement reviewed)."""
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "east_run.json")
    result = run_thread(fixture)
    assert not result.unrecorded
    assert {s.requirement_id: s.status for s in result.scores} == fixture.baseline


def test_east_run_reproduces_the_kria_185_symptoms():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "east_run.json")
    result = run_thread(fixture)
    by_id = {s.requirement_id: s for s in result.scores}
    # The brief never reached the planner: per-clip labels, route order, reading time missing.
    assert by_id["labels-per-clip"].status == "unmet"
    assert by_id["route-order"].status == "unmet"
    assert by_id["time-to-read"].status == "unmet"
    # "Create the video again" did nothing on the turn it was asked.
    assert by_id["redo-from-prompt"].status == "unmet"
    # The reply for a partially applied brief still says everything else is unchanged.
    assert by_id["route-order"].reply_overclaims and by_id["labels-per-clip"].reply_overclaims
    # What did work is scored as working: the creator's facts and the style rule.
    assert by_id["facts-in-edit"].status == "met" and by_id["no-fraunces"].status == "met"
