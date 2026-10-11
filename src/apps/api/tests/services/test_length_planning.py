import pytest

from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.services.length_planning import build_length_plan, read_length_plan


def _clip(media_id: str, *, kind: str = "video", duration_s: float = 5) -> UnifiedClip:
    return UnifiedClip(
        media_id=media_id,
        proxy_path=f"{media_id}.mp4",
        generation="1",
        duration_s=duration_s,
        kind=kind,
    )


def test_image_source_uses_synthetic_capacity_and_preferred_window() -> None:
    plan = build_length_plan(
        {"edit_format": "montage"},
        {"clip_assignments": [{"media_id": "still", "kind": "image", "duration_s": None}]},
        creator_target_s=1.2,
    )

    assert plan["sources"] == [
        {
            "media_id": "still",
            "generation": None,
            "storage_generation": None,
            "gcs_path": None,
            "kind": "image",
            "duration_s": 3.0,
        }
    ]
    assert plan["capacity_raw_s"] == 3.0
    assert plan["windows"][0]["source_start_s"] == 0.0
    assert plan["windows"][0]["source_end_s"] == 1.2


def test_image_only_plan_uses_synthetic_hold_to_meet_snapshot_minimum() -> None:
    plan = build_length_plan(
        {"edit_format": "montage"},
        {"clip_assignments": [{"media_id": "still", "kind": "image", "duration_s": None}]},
    )

    assert plan["conflicts"] == []
    assert plan["windows"][0]["source_end_s"] == 3.0


def test_approved_image_hold_ignores_zero_runtime_duration() -> None:
    envelope = {
        "version": 1,
        "sources": [
            {
                "media_id": "still",
                "generation": "1",
                "storage_generation": "1",
                "gcs_path": "still.mp4",
                "kind": "image",
                "duration_s": 3.0,
            }
        ],
        "windows": [{"media_id": "still", "source_start_s": 0, "source_end_s": 0.65, "score": 0}],
        "conflicts": [],
        "window_eligible": True,
    }

    assert (
        read_length_plan(envelope, [_clip("still", kind="image", duration_s=0)])[0].duration_s
        == 0.65
    )


def test_bad_selected_source_is_a_conflict_even_when_another_source_is_usable() -> None:
    plan = build_length_plan(
        {"edit_format": "montage"},
        {
            "clip_assignments": [
                {"media_id": "usable", "duration_s": 5},
                {"media_id": "missing-duration", "duration_s": None},
            ]
        },
        selected_ids=["usable", "missing-duration"],
    )

    assert [conflict["code"] for conflict in plan["conflicts"]] == ["source_below_minimum"]


@pytest.mark.parametrize("duration", [float("nan"), None])
def test_reader_rejects_non_finite_or_missing_stored_duration(duration: float | None) -> None:
    envelope = {
        "version": 1,
        "sources": [
            {"media_id": "a", "storage_generation": "1", "kind": "video", "duration_s": duration}
        ],
        "windows": [],
        "conflicts": [],
    }
    with pytest.raises(ValueError, match="duration"):
        read_length_plan(envelope, [_clip("a")])


def test_source_capacity_inside_creator_tolerance_uses_exact_available_windows() -> None:
    plan = build_length_plan(
        {"edit_format": "montage", "audio_strategy": "original_audio"},
        {"clip_assignments": [{"media_id": "a", "duration_s": 9.5}]},
        creator_target_s=10.0,
    )
    assert plan["target_duration_s"] == 10.0
    assert plan["owner"] == "creator"
    assert plan["conflicts"] == []
    assert plan["windows"][0]["source_end_s"] == 9.5


def test_snapshot_minimum_inside_creator_tolerance_preserves_requested_target() -> None:
    plan = build_length_plan(
        {"edit_format": "montage", "audio_strategy": "original_audio"},
        {"clip_assignments": [{"media_id": "a", "duration_s": 10.0}]},
        creator_target_s=2.9,
    )
    assert plan["target_duration_s"] == 2.9
    assert plan["conflicts"] == []
    assert plan["windows"][0]["source_end_s"] == 3.0


def test_mixed_media_plan_caps_allocation_without_losing_late_video_coordinates() -> None:
    plan = build_length_plan(
        {
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "cut",
            },
        },
        {
            "clip_assignments": [
                {
                    "media_id": "video",
                    "generation": "1",
                    "duration_s": 10,
                    "analysis": {"best_moments": [{"start_s": 7, "end_s": 10, "energy": 9}]},
                },
                {"media_id": "still", "generation": "1", "kind": "image", "duration_s": None},
            ]
        },
        creator_target_s=3.65,
    )

    assert plan["conflicts"] == []
    assert plan["sources"][0]["duration_s"] == 10
    assert plan["capacity_raw_s"] == 3.65
    windows = [
        (window["media_id"], window["source_start_s"], window["source_end_s"])
        for window in plan["windows"]
    ]
    assert windows == [
        ("video", 7.0, 10.0),
        ("still", 0.0, 0.65),
    ]
    compiled = plan_unified_montage(
        [_clip("video", duration_s=10), _clip("still", kind="image", duration_s=0)],
        strategy={
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "cut",
            }
        },
        approved_length_plan=plan,
    )
    compiled_windows = [
        (cut.media_id, cut.source_start_s, cut.source_end_s) for cut in compiled.snapshot.fast_cuts
    ]
    assert compiled_windows == windows
    over_budget = build_length_plan(
        {
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "cut",
            },
        },
        {
            "clip_assignments": [
                {"media_id": "video", "duration_s": 10},
                {"media_id": "still", "kind": "image", "duration_s": None},
            ]
        },
        creator_target_s=8,
    )
    assert over_budget["capacity_raw_s"] == 3.65
    assert "insufficient_capacity" in [conflict["code"] for conflict in over_budget["conflicts"]]


def test_single_hero_plan_preserves_policy_order_and_blocks_over_budget_or_source_count() -> None:
    strategy = {"edit_format": "single_hero", "audio_strategy": "original_audio"}
    snapshot = {
        "clip_assignments": [
            {
                "media_id": "hero",
                "generation": "1",
                "duration_s": 20,
                "analysis": {
                    "hook_score": 9,
                    "best_moments": [{"start_s": 1, "end_s": 1.2, "energy": 9}],
                },
            },
            {
                "media_id": "support",
                "generation": "1",
                "duration_s": 10,
                "analysis": {"hook_score": 4},
            },
        ]
    }

    plan = build_length_plan(strategy, snapshot, creator_target_s=20)

    assert plan["conflicts"] == []
    assert plan["max_source_count"] == 4
    assert [window["media_id"] for window in plan["windows"]] == ["hero", "support"]
    assert [window["source_end_s"] - window["source_start_s"] for window in plan["windows"]] == [
        14.0,
        6.0,
    ]
    compiled = plan_unified_montage(
        [_clip("hero", duration_s=20), _clip("support", duration_s=10)],
        strategy=strategy,
        approved_length_plan=plan,
    )
    assert compiled.duration_s == 20

    tolerance_floor = build_length_plan(strategy, snapshot, creator_target_s=3)
    assert tolerance_floor["target_duration_s"] == 3
    assert tolerance_floor["conflicts"] == []
    assert tolerance_floor["capacity_raw_s"] == 3.1
    floor_durations = [
        window["source_end_s"] - window["source_start_s"] for window in tolerance_floor["windows"]
    ]
    assert floor_durations == [3.0, 0.1]

    over_budget = build_length_plan(strategy, snapshot, creator_target_s=40)
    assert "insufficient_capacity" in [conflict["code"] for conflict in over_budget["conflicts"]]
    constrained_hero = build_length_plan(
        strategy,
        {
            "clip_assignments": [
                {"media_id": "hero", "duration_s": 5, "hook_score": 9},
                {"media_id": "support", "duration_s": 20, "hook_score": 4},
            ]
        },
        creator_target_s=20,
    )
    assert constrained_hero["capacity_raw_s"] == pytest.approx(8.333)
    assert "insufficient_capacity" in [
        conflict["code"] for conflict in constrained_hero["conflicts"]
    ]
    too_many = build_length_plan(
        strategy,
        {
            "clip_assignments": [
                {"media_id": f"clip-{index}", "duration_s": 10, "hook_score": index}
                for index in range(5)
            ]
        },
    )
    assert "source_count_limit" in [conflict["code"] for conflict in too_many["conflicts"]]


@pytest.mark.parametrize("case", ["mixed", "voiceover", "licensed_song", "unreachable_clock"])
def test_kri492_bounded_windows_and_single_hero_audio_clocks(case: str) -> None:
    if case == "mixed":
        strategy = {
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "cut",
            },
        }
        plan = build_length_plan(
            strategy,
            {
                "clip_assignments": [
                    {
                        "media_id": "video",
                        "generation": "1",
                        "duration_s": 10,
                        "analysis": {"best_moments": [{"start_s": 0, "end_s": 10}]},
                    },
                    {"media_id": "still", "generation": "1", "kind": "image"},
                ]
            },
            creator_target_s=3.8,
        )
        assert plan["conflicts"] == []
        durations = [
            window["source_end_s"] - window["source_start_s"] for window in plan["windows"]
        ]
        assert durations == [3.0, 0.8]
        compiled = plan_unified_montage(
            [_clip("video", duration_s=10), _clip("still", kind="image", duration_s=0)],
            strategy=strategy,
            approved_length_plan=plan,
        )
        assert compiled.duration_s == 3.8
        return

    strategy = {"edit_format": "single_hero", "audio_strategy": "voiceover"}
    if case == "licensed_song":
        strategy = {
            "edit_format": "single_hero",
            "audio_strategy": "licensed_music",
            "selected_music_section": {"start_s": 0, "end_s": 20},
        }
    snapshot = {
        "clip_assignments": [
            {"media_id": "hero", "duration_s": 20, "hook_score": 9},
            {"media_id": "support", "duration_s": 20, "hook_score": 5},
        ]
    }
    if case != "licensed_song":
        strategy["voiceover_duration_s"] = 20
    if case == "unreachable_clock":
        snapshot["clip_assignments"][0]["duration_s"] = 5

    plan = build_length_plan(strategy, snapshot)

    if case == "unreachable_clock":
        assert "insufficient_capacity" in [conflict["code"] for conflict in plan["conflicts"]]
    else:
        assert plan["conflicts"] == []
        durations = [
            window["source_end_s"] - window["source_start_s"] for window in plan["windows"]
        ]
        assert durations == [14.0, 6.0]


# KRI-595 (prod 2026-10-11, job 0c9f0a82): a 30 fps analysis proxy rounds its duration up to
# whole frames (5.0 s) while the device original is 4.968333 s. The plan stores the proxy's
# duration; the worker measures the original. The reader demanded a 1 ms match and failed
# every such render with "length plan source duration changed".
PROXY_S = 5.0
ORIGINAL_S = 4.968333333333334


def _proxy_envelope(*, window_end_s: float, duration_s: float = PROXY_S) -> dict:
    return {
        "version": 1,
        "sources": [
            {
                "media_id": "a",
                "storage_generation": "1",
                "gcs_path": "a.mp4",
                "kind": "video",
                "duration_s": duration_s,
            }
        ],
        "windows": [
            {"media_id": "a", "source_start_s": 0.0, "source_end_s": window_end_s, "score": 5.0}
        ],
        "conflicts": [],
        "window_eligible": True,
    }


def test_kri595_proxy_frame_rounding_does_not_invalidate_the_approved_plan() -> None:
    parts = read_length_plan(_proxy_envelope(window_end_s=3.0), [_clip("a", duration_s=ORIGINAL_S)])

    assert parts is not None
    assert (parts[0].source_start_s, parts[0].source_end_s) == (0.0, 3.0)


def test_kri595_window_to_the_proxy_end_is_clamped_to_the_real_clip() -> None:
    parts = read_length_plan(
        _proxy_envelope(window_end_s=PROXY_S), [_clip("a", duration_s=ORIGINAL_S)]
    )

    assert parts is not None
    assert parts[0].source_end_s <= ORIGINAL_S
    assert parts[0].source_end_s > ORIGINAL_S - 0.002


@pytest.mark.parametrize("actual_s", [4.8, 5.2])
def test_kri595_a_genuinely_different_source_is_still_rejected(actual_s: float) -> None:
    with pytest.raises(ValueError, match="length plan source duration changed"):
        read_length_plan(_proxy_envelope(window_end_s=3.0), [_clip("a", duration_s=actual_s)])


def test_kri595_a_window_the_real_clip_cannot_hold_is_rejected() -> None:
    envelope = _proxy_envelope(window_end_s=PROXY_S)
    envelope["windows"][0]["source_start_s"] = 4.95

    with pytest.raises(ValueError, match="window exceeds source"):
        read_length_plan(envelope, [_clip("a", duration_s=ORIGINAL_S)])


def test_kri595_the_approved_proxy_plan_reaches_a_montage_snapshot() -> None:
    """End to end through the planner: the incident's plan + the worker's original clip."""
    compiled = plan_unified_montage(
        [_clip("a", duration_s=ORIGINAL_S)],
        strategy={},
        approved_length_plan=_proxy_envelope(window_end_s=PROXY_S),
    )

    cuts = compiled.snapshot.fast_cuts
    assert [cut.media_id for cut in cuts] == ["a"]
    assert cuts[0].source_end_s <= ORIGINAL_S + 0.001
