from __future__ import annotations

import pytest

from app.pipeline.silence_cut import Removal
from app.pipeline.speech_cut_state import (
    accept_candidate,
    cut_revision,
    make_candidate,
    reproject_timed_records,
    restore_original_timing,
)


def _candidate(source: str = "clip-a") -> dict:
    return make_candidate(
        start_s=4.0,
        end_s=5.0,
        reason="possible abandoned start",
        source="retake_review",
        preview="let me start that again",
        source_fingerprint=source,
        transcript_hash="transcript-a",
    )


def test_candidate_identity_is_source_bound_and_stable() -> None:
    first = _candidate()
    assert first == _candidate()
    assert first["candidate_id"] != _candidate("clip-b")["candidate_id"]
    assert first["coordinate_space"] == "source_v1"


def test_accept_is_in_flight_until_render_publication() -> None:
    candidate = _candidate()
    variant = {"speech_cut_candidates": [candidate]}
    updated, operation = accept_candidate(
        variant,
        candidate_id_value=candidate["candidate_id"],
        expected_revision=cut_revision(variant),
    )

    assert updated["speech_cut_candidates"][0]["status"] == "applying"
    assert (
        updated["speech_cut_in_flight"]["desired_forced_removals"][0]["candidate_id"]
        == candidate["candidate_id"]
    )
    assert "speech_cut_last_receipt" not in updated
    assert operation["time_saved_s"] == 1.0


def test_cut_revision_rejects_stale_actions() -> None:
    candidate = _candidate()
    variant = {"speech_cut_candidates": [candidate]}
    with pytest.raises(ValueError, match="speech_cut_revision_conflict"):
        accept_candidate(
            variant,
            candidate_id_value=candidate["candidate_id"],
            expected_revision="stale",
        )


def test_restore_is_in_flight_and_preserves_applied_state_until_success() -> None:
    variant = {
        "speech_cuts_disabled": False,
        "speech_cut_forced_removals": [{"start_s": 4.0, "end_s": 5.0}],
        "silence_cut": {"removed": [{"start_s": 1.0, "end_s": 2.0}]},
    }
    updated, operation = restore_original_timing(variant, expected_revision=cut_revision(variant))

    assert updated["speech_cuts_disabled"] is False
    assert updated["speech_cut_forced_removals"] == variant["speech_cut_forced_removals"]
    assert updated["speech_cut_in_flight"]["desired_disabled"] is True
    assert operation["restored_s"] == 2.0


def test_restore_receipt_counts_overlapping_forced_range_once() -> None:
    variant = {
        "silence_cut": {"removed": [{"start_s": 1.0, "end_s": 3.0}]},
        "speech_cut_forced_removals": [{"start_s": 2.0, "end_s": 3.0}],
    }

    _, operation = restore_original_timing(variant, expected_revision=cut_revision(variant))

    assert operation["restored_s"] == 2.0


def test_reprojection_uses_source_space_for_existing_cuts_and_point_anchors() -> None:
    old = [Removal(1.0, 2.0, "silence")]
    new = [Removal(1.0, 2.0, "silence"), Removal(4.0, 5.0, "retake_review")]
    records = [
        {"id": "overlay", "start_s": 2.5, "end_s": 4.5},
        {"id": "sfx", "at_s": 2.5},
        {"id": "inside-new-cut", "at_s": 3.0},
    ]

    result = reproject_timed_records(records, old_removals=old, new_removals=new)

    assert result[0]["start_s"] == 2.5
    assert result[0]["end_s"] == 3.5
    assert result[1]["at_s"] == 2.5
    assert [entry["id"] for entry in result] == ["overlay", "sfx"]


def test_reprojection_remaps_nested_typewriter_schedule() -> None:
    result = reproject_timed_records(
        [
            {
                "start_s": 2.0,
                "end_s": 4.0,
                "words": [{"text": "hello", "start_s": 2.0, "end_s": 2.4}],
                "source_params": {"reveal_schedule_s": [2.0, 2.4, 3.0]},
            }
        ],
        old_removals=[Removal(0.5, 1.0, "silence")],
        new_removals=[Removal(1.5, 1.75, "silence")],
    )
    assert result[0]["start_s"] == 2.25
    assert result[0]["words"][0]["start_s"] == 2.25
    assert result[0]["source_params"]["reveal_schedule_s"] == [2.25, 2.65, 3.25]


def test_restore_keeps_interval_end_on_pre_cut_side_of_join() -> None:
    result = reproject_timed_records(
        [
            {"id": "before", "start_s": 0.0, "end_s": 2.0},
            {"id": "after", "start_s": 2.0, "end_s": 4.0},
        ],
        old_removals=[Removal(2.0, 3.0, "silence")],
        new_removals=[],
    )

    assert result == [
        {"id": "before", "start_s": 0.0, "end_s": 2.0},
        {"id": "after", "start_s": 3.0, "end_s": 5.0},
    ]
