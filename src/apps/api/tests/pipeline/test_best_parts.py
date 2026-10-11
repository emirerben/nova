import pytest

from app.pipeline.best_parts import (
    MAX_BEST_PARTS_DURATION_S,
    allocate_best_parts_for_voiceover,
    select_best_parts,
)
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.services.length_planning import build_length_plan


def source(media_id, duration, moments=()):
    return {
        "media_id": media_id,
        "duration_s": duration,
        "analysis": {"best_moments": list(moments)},
    }


def test_uses_highest_energy_moment_with_earliest_tie_and_preserves_source_order():
    result = select_best_parts(
        [
            source(
                "second",
                12,
                [{"start_s": 8, "end_s": 11, "energy": 8}, {"start_s": 2, "end_s": 6, "energy": 8}],
            ),
            source("first", 12, [{"start_s": 4, "end_s": 10, "energy": 9}]),
        ]
    )
    assert [part.media_id for part in result.parts] == ["second", "first"]
    assert result.parts[0].source_start_s == 2
    assert result.parts[0].reason == "best_moment"


def test_falls_back_deterministically_for_missing_or_invalid_analysis():
    result = select_best_parts(
        [source("a", 2), source("b", 2, [{"start_s": 4, "end_s": 5, "energy": 10}])]
    )
    assert [(part.media_id, part.reason, part.source_start_s) for part in result.parts] == [
        ("a", "analysis_fallback", 0.0),
        ("b", "analysis_fallback", 0.0),
    ]


def test_fair_split_redistributes_a_capped_source_capacity():
    result = select_best_parts(
        [source("tiny", 1), source("long-a", 10), source("long-b", 10)], target_duration_s=9
    )
    assert [part.duration_s for part in result.parts] == [1.0, 4.0, 4.0]
    assert result.duration_s == 9


def test_target_longer_than_capacity_is_returned_as_no_reuse_conflict():
    result = allocate_best_parts_for_voiceover([source("a", 2), source("b", 3)], 12)
    assert result.duration_s == 5
    assert [conflict.code for conflict in result.conflicts] == ["insufficient_capacity"]


def test_cap_limits_use_all_duration_to_120_seconds():
    result = select_best_parts([source("a", 100), source("b", 100)])
    assert result.duration_s == MAX_BEST_PARTS_DURATION_S
    assert [part.duration_s for part in result.parts] == [60, 60]


def test_cap_below_every_source_coverage_is_a_typed_conflict_without_dropping():
    result = select_best_parts([source(f"c{index}", 1) for index in range(1201)])
    assert result.duration_s == 0
    assert len(result.parts) == 1201
    assert [conflict.code for conflict in result.conflicts] == ["cap_below_coverage"]


def test_requested_budget_below_coverage_does_not_silently_clip_or_drop():
    result = select_best_parts([source("a", 1), source("b", 1)], target_duration_s=0.1)
    assert result.duration_s == 0
    assert [part.media_id for part in result.parts] == ["a", "b"]
    assert [conflict.code for conflict in result.conflicts] == ["target_below_coverage"]


def test_a_selected_source_shorter_than_100ms_is_a_conflict_even_with_other_footage():
    result = select_best_parts([source("usable", 1), source("tiny", 0.05)])
    assert [part.media_id for part in result.parts] == ["usable"]
    assert [conflict.code for conflict in result.conflicts] == ["source_below_minimum"]


def test_minimum_snapshot_duration_fairly_extends_selected_best_moments():
    result = select_best_parts(
        [
            source("a", 5, [{"start_s": 1, "end_s": 1.2, "energy": 9}]),
            source("b", 5, [{"start_s": 3, "end_s": 3.2, "energy": 8}]),
        ],
        minimum_total_duration_s=3,
    )

    assert result.ok
    assert result.duration_s == 3
    assert [part.duration_s for part in result.parts] == [1.5, 1.5]
    assert [part.reason for part in result.parts] == [
        "best_moment_extended_for_minimum",
        "best_moment_extended_for_minimum",
    ]
    assert result.parts[0].source_start_s <= 1 <= result.parts[0].source_end_s
    assert result.parts[1].source_start_s <= 3 <= result.parts[1].source_end_s
    assert result.receipt()[0]["reason"] == "best_moment_extended_for_minimum"


def test_minimum_snapshot_duration_reports_raw_pool_that_cannot_reach_three_seconds():
    result = select_best_parts(
        [
            source("a", 1, [{"start_s": 0.2, "end_s": 0.4, "energy": 9}]),
            source("b", 1, [{"start_s": 0.3, "end_s": 0.5, "energy": 8}]),
        ],
        minimum_total_duration_s=3,
    )
    assert [conflict.code for conflict in result.conflicts] == ["snapshot_min_duration"]
    assert result.conflicts[0].available_duration_s == 2


def test_tiny_selected_source_remains_a_conflict_even_when_other_footage_can_reach_minimum():
    result = select_best_parts(
        [
            source("usable", 5, [{"start_s": 1, "end_s": 1.2, "energy": 9}]),
            source("tiny", 0.05),
        ],
        minimum_total_duration_s=3,
    )
    assert [conflict.code for conflict in result.conflicts] == ["source_below_minimum"]


def test_explicit_short_target_is_not_silently_inflated_to_snapshot_minimum():
    result = select_best_parts(
        [
            source("a", 5, [{"start_s": 1, "end_s": 1.2, "energy": 9}]),
            source("b", 5, [{"start_s": 3, "end_s": 3.2, "energy": 8}]),
        ],
        target_duration_s=0.3,
        minimum_total_duration_s=3,
    )
    assert result.duration_s == 0.3
    assert all(part.reason == "best_moment" for part in result.parts)


def test_explicit_target_extends_the_selected_best_moment_using_raw_capacity():
    result = select_best_parts(
        [source("a", 60, [{"start_s": 10, "end_s": 10.2, "energy": 9}])],
        target_duration_s=30,
    )
    assert result.ok and result.duration_s == 30
    assert result.parts[0].duration_s == 30
    assert result.parts[0].source_start_s <= 10 <= result.parts[0].source_end_s
    assert result.parts[0].reason == "best_moment_extended_for_target"


def unified_clip(media_id, duration, moments):
    return UnifiedClip(
        media_id=media_id,
        proxy_path=f"{media_id}.mp4",
        generation="1",
        duration_s=duration,
        analysis={"best_moments": moments},
    )


def test_unified_footage_owner_uses_selected_source_windows_and_receipt():
    plan = plan_unified_montage(
        [
            unified_clip("a", 10, [{"start_s": 3, "end_s": 7, "energy": 8}]),
            unified_clip("b", 10, [{"start_s": 4, "end_s": 9, "energy": 9}]),
        ]
    )
    assert [
        (cut.media_id, cut.source_start_s, cut.source_end_s) for cut in plan.snapshot.fast_cuts
    ] == [
        ("a", 3, 7),
        ("b", 4, 9),
    ]
    assert plan.duration_s == 9
    assert plan.record()["best_parts"] == [
        {
            "media_id": "a",
            "source_start_s": 3,
            "source_end_s": 7,
            "score": 8,
            "reason": "best_moment",
        },
        {
            "media_id": "b",
            "source_start_s": 4,
            "source_end_s": 9,
            "score": 9,
            "reason": "best_moment",
        },
    ]


def test_unified_keeps_song_and_proven_creator_duration_paths():
    clips = [unified_clip("a", 10, [{"start_s": 3, "end_s": 7, "energy": 8}])]
    creator = plan_unified_montage(
        clips,
        strategy={"target_duration_s": 6},
        creator_duration_proven=True,
    )
    song = plan_unified_montage(
        clips,
        strategy={"audio_strategy": "user_song"},
        song_duration_s=10,
        song_plan_item_id="song",
        song_generation=1,
    )
    assert "best_parts" not in creator.record()
    assert "best_parts" not in song.record()


def test_unified_extends_analyzed_windows_when_raw_footage_can_make_a_snapshot():
    plan = plan_unified_montage(
        [
            unified_clip("a", 5, [{"start_s": 0, "end_s": 0.2, "energy": 8}]),
            unified_clip("b", 5, [{"start_s": 0, "end_s": 0.2, "energy": 8}]),
        ]
    )
    assert plan.duration_s == 3
    assert [row["reason"] for row in plan.record()["best_parts"]] == [
        "best_moment_extended_for_minimum",
        "best_moment_extended_for_minimum",
    ]


def test_unified_keeps_typed_too_short_error_when_raw_footage_cannot_make_snapshot():
    with pytest.raises(ValueError, match="too short.*snapshot_min_duration"):
        plan_unified_montage(
            [
                unified_clip("a", 1, [{"start_s": 0, "end_s": 0.2, "energy": 8}]),
                unified_clip("b", 1, [{"start_s": 0, "end_s": 0.2, "energy": 8}]),
            ]
        )


def test_unified_uses_an_approved_length_plan_without_reselecting_windows():
    clips = [
        unified_clip("a", 5, [{"start_s": 1, "end_s": 1.2, "energy": 9}]),
        unified_clip("b", 5, [{"start_s": 3, "end_s": 3.2, "energy": 8}]),
    ]
    envelope = build_length_plan(
        {"edit_format": "montage", "audio_strategy": "licensed_music"},
        {
            "clip_assignments": [
                {
                    "media_id": clip.media_id,
                    "generation": clip.generation,
                    "duration_s": clip.duration_s,
                    "analysis": clip.analysis,
                }
                for clip in clips
            ]
        },
    )
    plan = plan_unified_montage(clips, approved_length_plan=envelope)
    assert [(cut.source_start_s, cut.source_end_s) for cut in plan.snapshot.fast_cuts] == [
        (0.35, 1.85),
        (2.35, 3.85),
    ]


def test_unified_keeps_approved_window_pool_out_of_legacy_allocation():
    clips = [unified_clip(f"clip-{index}", 5, []) for index in range(80)]
    duration_s = 1.5
    envelope = {
        "version": 1,
        "sources": [
            {
                "media_id": clip.media_id,
                "generation": "1",
                "storage_generation": "1",
                "gcs_path": clip.proxy_path,
                "kind": "video",
                "duration_s": 5.0,
            }
            for clip in clips
        ],
        "windows": [
            {
                "media_id": clip.media_id,
                "source_start_s": 0.0,
                "source_end_s": duration_s,
                "score": 0.0,
            }
            for clip in clips
        ],
        "conflicts": [],
        "window_eligible": True,
    }

    plan = plan_unified_montage(clips, approved_length_plan=envelope)

    assert plan.duration_s == pytest.approx(120)
    assert all(cut.output_duration_s == duration_s for cut in plan.snapshot.fast_cuts)


def test_unified_uses_shared_photo_hold_bounds_with_mixed_media_profile():
    image = UnifiedClip(
        media_id="still",
        proxy_path="still.jpg",
        generation="1",
        duration_s=0,
        kind="image",
    )
    plan = plan_unified_montage(
        [unified_clip("video", 5, []), image],
        strategy={
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "crossfade",
            }
        },
    )

    still_cut = next(cut for cut in plan.snapshot.fast_cuts if cut.media_id == "still")
    assert 0.5 <= still_cut.output_duration_s <= 0.8


def test_unified_best_parts_preserves_creator_order_pins_and_label_hold():
    plan = plan_unified_montage(
        [
            unified_clip("a", 8, [{"start_s": 1, "end_s": 2, "energy": 9}]),
            unified_clip("b", 8, [{"start_s": 4, "end_s": 6, "energy": 8}]),
        ],
        BriefView(wants_per_clip_text=True, clip_literals={"a": "A readable label"}),
        creator_order=[1, 0],
        strategy={
            "pinned_texts": [{"text": "Pinned", "corner": "top_left", "start_s": 0, "end_s": 1}]
        },
    )
    assert plan.clip_ids == ["b", "a"]
    cuts = {cut.media_id: cut for cut in plan.snapshot.fast_cuts}
    assert cuts["a"].output_duration_s >= 1.7
    assert cuts["a"].source_start_s <= 1 <= cuts["a"].source_end_s
    assert plan.snapshot.pinned_texts[0].text == "Pinned"
    assert [row["media_id"] for row in plan.record()["best_parts"]] == ["b", "a"]
