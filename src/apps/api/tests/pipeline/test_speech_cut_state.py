from __future__ import annotations

import math

import pytest

from app.pipeline.silence_cut import Removal
from app.pipeline.speech_cut_state import (
    RenderedCut,
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


# ---------------------------------------------------------------------------
# Reprojection on a cloud render's frame grid (RenderedCut)
#
# Old render: removals (2.04, 2.52) and (5.01, 5.645) on a 10 s clip at
# 30 fps. Its keep segments snap to frames [0, 61), [76, 150), [169, 300):
# 2.04 s = 61.2 -> 61, 2.52 s = 75.6 -> 76, 5.01 s = 150.3 -> 150 and
# 5.645 s = 169.35 -> 169, which is BEFORE the removal's end. Output: span 1
# is 0-2.0333 s (61 frames), span 2 2.0333-4.5 s (74), span 3 from 4.5 s.
# ---------------------------------------------------------------------------

OLD_REMOVALS = [Removal(2.04, 2.52, "silence"), Removal(5.01, 5.645, "silence")]
OLD_SUMMARY = {
    "removed": [
        {"start_s": 2.04, "end_s": 2.52, "reason": "silence"},
        {"start_s": 5.01, "end_s": 5.645, "reason": "silence"},
    ],
    "original_duration_s": 10.0,
    "frame_grid": {"fps": 30, "frames": [[0, 61], [76, 150], [169, 300]]},
}
# Accepting (7.015, 7.4) on top: 7.015 s = 210.45 -> 210, 7.4 s = 222. The new
# render's span 3 is 4.5-5.8667 s (41 frames), span 4 from 5.8667 s.
NEW_REMOVALS = [*OLD_REMOVALS, Removal(7.015, 7.4, "retake_review")]
NEW_SUMMARY = {
    "removed": [
        *OLD_SUMMARY["removed"],
        {"start_s": 7.015, "end_s": 7.4, "reason": "retake_review"},
    ],
    "original_duration_s": 10.0,
    "frame_grid": {"fps": 30, "frames": [[0, 61], [76, 150], [169, 210], [222, 300]]},
}


def test_rendered_cut_reads_the_persisted_frames() -> None:
    render = RenderedCut.from_summary(OLD_SUMMARY)

    assert render is not None
    assert render.fps == 30
    # The last span runs open past the clip end.
    assert render.spans == ((0.0, 61 / 30), (76 / 30, 150 / 30), (169 / 30, math.inf))


def test_rendered_cut_keeps_a_trailing_cut_unplayed() -> None:
    render = RenderedCut.from_summary(
        {"original_duration_s": 10.0, "frame_grid": {"fps": 30, "frames": [[0, 240]]}}
    )

    assert render is not None
    assert render.spans == ((0.0, 8.0), (10.0, math.inf))


@pytest.mark.parametrize(
    "summary",
    [
        None,
        {"removed": [{"start_s": 1.0, "end_s": 2.0}]},
        {"frame_grid": {"fps": 0, "frames": [[0, 30]]}},
        {"frame_grid": {"fps": 30, "frames": []}},
        {"frame_grid": {"fps": 30, "frames": [[30, 30]]}},
        {"frame_grid": {"fps": 30, "frames": [[0, 40], [30, 60]]}},
        {"frame_grid": {"fps": 30, "frames": [["a", 30]]}},
        {"frame_grid": {"fps": 30}},
        # A stored row is data: overflowing, non-integer, negative or
        # non-finite values keep the removal mapping instead of raising.
        ["not", "a", "summary"],
        {"frame_grid": {"fps": float("inf"), "frames": [[0, 30]]}},
        {"frame_grid": {"fps": 30.0, "frames": [[0, 30]]}},
        {"frame_grid": {"fps": True, "frames": [[0, 30]]}},
        {"frame_grid": {"fps": 30, "frames": [[0, 10**400]]}},
        {"frame_grid": {"fps": 30, "frames": [[-300, -30]]}},
        {"frame_grid": {"fps": 30, "frames": [[0, 30.5]]}},
        {"frame_grid": {"fps": 30, "frames": [[0, 30]]}, "original_duration_s": "abc"},
        {"frame_grid": {"fps": 30, "frames": [[0, 30]]}, "original_duration_s": float("inf")},
    ],
)
def test_summary_without_a_usable_grid_keeps_the_removal_mapping(summary) -> None:
    assert RenderedCut.from_summary(summary) is None


def test_same_render_reprojects_every_lane_onto_itself() -> None:
    render = RenderedCut.from_summary(OLD_SUMMARY)
    records = [
        {"id": "ends-at-cut", "start_s": 0.5, "end_s": 2.033},
        {"id": "between-cuts", "start_s": 2.033, "end_s": 4.5},
        {"id": "sfx-at-cut", "at_s": 4.5},
        {"id": "to-the-end", "start_s": 4.5, "end_s": 8.867},
    ]

    result = reproject_timed_records(
        records,
        old_removals=OLD_REMOVALS,
        new_removals=OLD_REMOVALS,
        old_render=render,
        new_render=render,
    )

    assert result == records


def test_restore_returns_lanes_to_the_source_the_old_render_played() -> None:
    records = [
        {"id": "span-2", "at_s": 3.0},
        {"id": "span-3", "at_s": 5.0},
        {"id": "between-cuts", "start_s": 2.033, "end_s": 4.5},
        {"id": "sfx-at-cut", "at_s": 4.5},
    ]

    result = reproject_timed_records(
        records,
        old_removals=OLD_REMOVALS,
        new_removals=[],
        old_render=RenderedCut.from_summary(OLD_SUMMARY),
    )

    # 3.0 s is 0.9667 s into span 2, which starts at frame 76 (2.5333 s);
    # 5.0 s is 0.5 s into span 3 (frame 169, 5.6333 s). The cut points land
    # on the span edges: a start after its cut, an end before it.
    assert result == [
        {"id": "span-2", "at_s": 3.5},
        {"id": "span-3", "at_s": 6.133},
        {"id": "between-cuts", "start_s": 2.533, "end_s": 5.0},
        {"id": "sfx-at-cut", "at_s": 5.633},
    ]
    # The removal arithmetic puts 5.0 s at 6.115 s: 18 ms off the audio.
    raw = reproject_timed_records(records[1:2], old_removals=OLD_REMOVALS, new_removals=[])
    assert raw[0]["at_s"] == 6.115


def test_accepted_cut_reprojects_lanes_onto_the_new_frames() -> None:
    records = [
        {"id": "before-cuts", "at_s": 1.0},
        # Old cut 2 maps to frame 169 (5.6333 s), inside the removal's float
        # range (5.01, 5.645); the new render plays it, so the anchor stays.
        {"id": "sfx-at-cut", "at_s": 4.5},
        {"id": "after-new-cut", "at_s": 7.0},
        {"id": "inside-new-cut", "at_s": 6.0},
        {"id": "across-new-cut", "start_s": 5.5, "end_s": 6.5},
        {"id": "into-new-cut", "start_s": 5.0, "end_s": 6.0},
    ]

    result = reproject_timed_records(
        records,
        old_removals=OLD_REMOVALS,
        new_removals=NEW_REMOVALS,
        old_render=RenderedCut.from_summary(OLD_SUMMARY),
        new_render=RenderedCut.from_summary(NEW_SUMMARY),
    )

    # 7.0 s plays source frame 244 (8.1333 s); the new render plays it 22
    # frames into span 4: frame 176 + 22 = 198, 6.6 s. 6.0 s plays 7.1333 s,
    # well inside the new cut's frames [210, 222). The record across the cut
    # (source 6.6333-7.6333 s) keeps both sides: 165 and 176 + 7 frames.
    assert result == [
        {"id": "before-cuts", "at_s": 1.0},
        {"id": "sfx-at-cut", "at_s": 4.5},
        {"id": "after-new-cut", "at_s": 6.6},
        {"id": "across-new-cut", "start_s": 5.5, "end_s": 6.1},
        {"id": "into-new-cut", "start_s": 5.0, "end_s": 5.867},
    ]
    raw = reproject_timed_records(
        records[2:3], old_removals=OLD_REMOVALS, new_removals=NEW_REMOVALS
    )
    assert raw[0]["at_s"] == 6.615


def test_prior_without_a_grid_keeps_both_sides_on_the_removals() -> None:
    # The old render cut but recorded no frames (made before frame_grid was
    # persisted). Mapping only the new side through its frames would move
    # lanes the new cut never touches (3.0 s to 2.98 s); both sides on the
    # removals cancel, as before frame_grid existed.
    records = [
        {"id": "lane", "start_s": 3.0, "end_s": 3.5},
        {"id": "sfx", "at_s": 4.0},
        {"id": "sfx-at-cut", "at_s": 2.04},
    ]

    result = reproject_timed_records(
        records,
        old_removals=OLD_REMOVALS,
        new_removals=NEW_REMOVALS,
        new_render=RenderedCut.from_summary(NEW_SUMMARY),
    )

    assert result == records


def test_anchor_just_inside_a_new_cut_stays_at_the_cut() -> None:
    # An uncut prior: source time is output time. 2.04 s is 0.2 frame past the
    # new render's first span end (61/30 s), so it stays at that cut; 2.06 s
    # is deeper in the cut and does not play.
    result = reproject_timed_records(
        [{"id": "just-into-cut", "at_s": 2.04}, {"id": "deep", "at_s": 2.06}],
        old_removals=[],
        new_removals=NEW_REMOVALS,
        new_render=RenderedCut.from_summary(NEW_SUMMARY),
    )

    assert result == [{"id": "just-into-cut", "at_s": 2.033}]


def test_leading_trim_keeps_only_anchors_at_the_first_frame() -> None:
    render = RenderedCut.from_summary(
        {"original_duration_s": 10.0, "frame_grid": {"fps": 30, "frames": [[15, 300]]}}
    )

    assert render is not None
    assert render.anchor_to_output(0.49) == 0.0
    assert render.anchor_to_output(0.2) is None


def test_render_path_remaps_nested_words_and_reveal_schedule() -> None:
    # On the new render's frames the word ends at the first cut (61/30 s) and
    # the reveal at 3.0 s plays at 2.5 s; the removals would give 2.04 s and
    # 2.52 s. 2.4 s falls inside the cut and is dropped from the schedule.
    result = reproject_timed_records(
        [
            {
                "start_s": 2.0,
                "end_s": 4.0,
                "words": [{"text": "hi", "start_s": 2.0, "end_s": 2.4}],
                "source_params": {"reveal_schedule_s": [2.0, 2.4, 3.0]},
            }
        ],
        old_removals=[],
        new_removals=NEW_REMOVALS,
        new_render=RenderedCut.from_summary(NEW_SUMMARY),
    )

    assert result[0]["end_s"] == 3.5
    assert result[0]["words"] == [{"text": "hi", "start_s": 2.0, "end_s": 2.033}]
    assert result[0]["source_params"]["reveal_schedule_s"] == [2.0, 2.5]


def test_trailing_cut_drops_lanes_on_the_unplayed_tail() -> None:
    render = RenderedCut.from_summary(
        {"original_duration_s": 10.0, "frame_grid": {"fps": 30, "frames": [[0, 240]]}}
    )

    result = reproject_timed_records(
        [
            {"id": "clamped", "start_s": 7.5, "end_s": 9.5},
            {"id": "cut", "start_s": 9.0, "end_s": 9.5},
            {"id": "cut-sfx", "at_s": 9.0},
            {"id": "past-the-end", "at_s": 10.5},
        ],
        old_removals=[],
        new_removals=[Removal(8.0, 10.0, "silence")],
        new_render=render,
    )

    # Past the clip end time runs on one to one, as under the removals.
    assert result == [
        {"id": "clamped", "start_s": 7.5, "end_s": 8.0},
        {"id": "past-the-end", "at_s": 8.5},
    ]
