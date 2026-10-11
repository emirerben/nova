import pytest

from app.pipeline.unified_montage import UnifiedClip
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
