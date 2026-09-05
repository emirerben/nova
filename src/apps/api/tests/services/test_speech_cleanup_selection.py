from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.pipeline.silence_cut import build_cut_plan, no_op_plan
from app.services.pipeline_trace import _sanitize_speech_cleanup_detection_payload
from app.services.speech_cleanup_identity import SpeechCleanupAssignment
from app.services.speech_cleanup_selection import (
    build_mixed_gap_receipt,
    select_mixed_gap_mode,
)

ASSIGNED = SpeechCleanupAssignment(
    source_slot=0,
    rollout_fingerprint="a" * 64,
    status="assigned",
)
UNASSIGNED = SpeechCleanupAssignment(
    source_slot=0,
    rollout_fingerprint=None,
    status="identity_cache_unavailable",
)


@pytest.mark.parametrize("policy", ["legacy_auto", "off_v1"])
def test_non_required_contracts_never_enter_mixed_gap(policy: str) -> None:
    selection = select_mixed_gap_mode(
        analysis_policy=policy,
        configured_mode="apply",
        rollout_percent=100,
        assignment=ASSIGNED,
    )
    assert selection.effective_mode == "off"
    assert selection.rollout_bucket is None
    assert selection.source_tag is None


def test_configured_off_ignores_assignment_and_percent() -> None:
    selection = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="off",
        rollout_percent=100,
        assignment=ASSIGNED,
    )
    assert selection.effective_mode == "off"
    assert selection.rollout_bucket is None
    assert selection.source_tag is None


def test_missing_assignment_downgrades_apply_to_shadow() -> None:
    selection = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="apply",
        rollout_percent=100,
        assignment=UNASSIGNED,
    )
    assert selection.effective_mode == "shadow"
    assert selection.rollout_bucket is None
    assert selection.source_tag is None


def test_assigned_apply_bucket_is_stable_and_percentage_gated() -> None:
    first = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="apply",
        rollout_percent=100,
        assignment=ASSIGNED,
    )
    second = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="apply",
        rollout_percent=100,
        assignment=ASSIGNED,
    )
    excluded = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="apply",
        rollout_percent=0,
        assignment=ASSIGNED,
    )
    assert first == second
    assert first.effective_mode == "apply"
    assert first.rollout_bucket is not None
    assert first.source_tag is not None
    assert excluded.rollout_bucket == first.rollout_bucket
    assert excluded.source_tag == first.source_tag
    assert excluded.effective_mode == "shadow"


def test_shadow_always_evaluates_but_never_applies() -> None:
    selection = select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="shadow",
        rollout_percent=100,
        assignment=ASSIGNED,
    )
    assert selection.effective_mode == "shadow"
    assert selection.rollout_bucket is not None
    assert selection.source_tag is not None


def _shadow_selection():
    return select_mixed_gap_mode(
        analysis_policy="required_v1",
        configured_mode="shadow",
        rollout_percent=100,
        assignment=ASSIGNED,
    )


def test_receipt_counts_only_decision_joined_mixed_gap_atoms() -> None:
    words = [
        {"text": "one", "start_s": 0.5, "end_s": 1.0},
        {"text": "two", "start_s": 1.5, "end_s": 2.0},
        {"text": "three", "start_s": 4.0, "end_s": 4.5},
    ]
    silences = [(2.0, 2.4), (2.8, 4.0)]
    candidate = build_cut_plan(
        words,
        silences,
        8.0,
        mixed_gap_enabled=True,
        over_budget_policy="clamp",
    )
    diagnostics = candidate.diagnostics
    assert diagnostics is not None
    acoustic = [
        item for item in diagnostics.atomic_dispositions if item.atom_kind == "filler_acoustic"
    ]
    # One legacy wholly-soundful gap and one decision-backed mixed island.
    assert len(acoustic) == 2
    assert diagnostics.mixed_gap_full_total == 1

    receipt = build_mixed_gap_receipt(
        selection=_shadow_selection(),
        analysis_attempt_id="attempt-a",
        analysis_view="full_clip",
        analysis_policy="required_v1",
        candidate_status="ready",
        silence_detection_status="ok",
        duration_s=8.0,
        words=[SimpleNamespace(start_s=item["start_s"], end_s=item["end_s"]) for item in words],
        silence_spans=silences,
        baseline_plan=no_op_plan(8.0),
        candidate_plan=candidate,
        selected_plan="baseline",
    )
    assert receipt["candidate_plan"]["mixed_gap_full"] == 1
    assert receipt["candidate_plan"]["mixed_gap_dropped"] == 0
    assert _sanitize_speech_cleanup_detection_payload(receipt) == receipt


def test_receipt_caps_keep_exact_totals_and_fit_persistence_limit() -> None:
    decisions = [
        SimpleNamespace(
            window_start_s=index * 0.1,
            window_end_s=index * 0.1 + 0.09,
            island_start_s=index * 0.1 + 0.02,
            island_end_s=index * 0.1 + 0.07,
            left_silence_s=0.02,
            right_silence_s=0.02,
            detection="eligible" if index % 2 == 0 else "rejected",
            reason="bilateral_silence" if index % 2 == 0 else "island_too_short",
        )
        for index in range(80)
    ]
    dispositions = [
        SimpleNamespace(
            atom_start_s=index * 0.1 + 0.02,
            atom_end_s=index * 0.1 + 0.07,
            group_start_s=index * 0.1 + 0.02,
            group_end_s=index * 0.1 + 0.07,
            atom_kind="filler_acoustic",
            priority="filler",
            disposition="selected_full",
        )
        for index in range(80)
    ]
    diagnostics = SimpleNamespace(
        lexical_candidates=[
            SimpleNamespace(start_s=index * 0.1, end_s=index * 0.1 + 0.05) for index in range(40)
        ],
        lexical_candidates_omitted=0,
        acoustic_decisions=decisions,
        acoustic_decisions_total=80,
        acoustic_eligible_total=40,
        atomic_dispositions=dispositions,
        atomic_dispositions_total=80,
        mixed_gap_full_total=40,
        mixed_gap_partial_total=0,
        mixed_gap_dropped_total=0,
    )
    removals = [
        SimpleNamespace(start_s=index * 0.1, end_s=index * 0.1 + 0.05) for index in range(150)
    ]
    candidate = SimpleNamespace(
        removed=removals,
        time_saved_s=7.5,
        clamped=True,
        bailout_reason=None,
        diagnostics=diagnostics,
    )
    words = [
        SimpleNamespace(start_s=index * 0.05, end_s=index * 0.05 + 0.02) for index in range(200)
    ]
    silences = [(index * 0.05, index * 0.05 + 0.02) for index in range(200)]

    receipt = build_mixed_gap_receipt(
        selection=_shadow_selection(),
        analysis_attempt_id="attempt-b",
        analysis_view="full_clip",
        analysis_policy="required_v1",
        candidate_status="ready",
        silence_detection_status="ok",
        duration_s=20.0,
        words=words,
        silence_spans=silences,
        baseline_plan=candidate,
        candidate_plan=candidate,
        selected_plan="baseline",
    )

    inputs = receipt["inputs"]
    scan = receipt["mixed_gap_scan"]
    allocator = receipt["allocator"]
    assert len(inputs["asr_word_spans_ms"]) + inputs["asr_word_spans_omitted"] == 200
    assert len(inputs["silence_spans_ms"]) + inputs["silence_spans_omitted"] == 200
    assert len(inputs["lexical_candidate_spans_ms"]) + inputs["lexical_candidates_omitted"] == 40
    assert len(scan["records"]) + scan["records_omitted"] == 80
    assert scan["eligible_total"] == 40
    assert len(allocator["atomic_dispositions"]) + allocator["atomic_dispositions_omitted"] == 80
    for plan_key in ("baseline_plan", "candidate_plan"):
        plan = receipt[plan_key]
        assert len(plan["removed_spans_ms"]) + plan["removed_spans_omitted"] == 150
    assert len(json.dumps(receipt).encode()) <= 16 * 1024
    assert _sanitize_speech_cleanup_detection_payload(receipt) == receipt


def test_displayed_mixed_island_keeps_disposition_beyond_general_atomic_cap() -> None:
    decision = SimpleNamespace(
        window_start_s=8.0,
        window_end_s=10.0,
        island_start_s=9.0,
        island_end_s=9.5,
        left_silence_s=1.0,
        right_silence_s=0.5,
        detection="eligible",
        reason="bilateral_silence",
    )
    earlier = [
        SimpleNamespace(
            atom_start_s=index * 0.1,
            atom_end_s=index * 0.1 + 0.05,
            group_start_s=index * 0.1,
            group_end_s=index * 0.1 + 0.05,
            atom_kind="filler_lexical",
            priority="filler",
            disposition="selected_full",
        )
        for index in range(64)
    ]
    diagnostics = SimpleNamespace(
        lexical_candidates=[],
        lexical_candidates_omitted=0,
        acoustic_decisions=[decision],
        acoustic_decisions_total=1,
        acoustic_eligible_total=1,
        mixed_gap_decision_dispositions=((9.0, 9.5, "selected_full"),),
        atomic_dispositions=earlier,
        atomic_dispositions_total=65,
        mixed_gap_full_total=1,
        mixed_gap_partial_total=0,
        mixed_gap_dropped_total=0,
    )
    candidate = SimpleNamespace(
        removed=[],
        time_saved_s=0.0,
        clamped=False,
        bailout_reason=None,
        diagnostics=diagnostics,
    )

    receipt = build_mixed_gap_receipt(
        selection=_shadow_selection(),
        analysis_attempt_id="attempt-c",
        analysis_view="full_clip",
        analysis_policy="required_v1",
        candidate_status="ready",
        silence_detection_status="ok",
        duration_s=10.0,
        words=[],
        silence_spans=[],
        baseline_plan=no_op_plan(10.0),
        candidate_plan=candidate,
        selected_plan="baseline",
    )

    assert receipt["mixed_gap_scan"]["records"][0]["plan_disposition"] == "selected_full"
    assert receipt["candidate_plan"]["mixed_gap_full"] == 1
