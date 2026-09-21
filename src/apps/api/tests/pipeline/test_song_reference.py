from __future__ import annotations

import pytest

from app.pipeline.guided_story import (
    GuidedStoryError,
    _compile_execution_plan_version,
    compile_execution_plan,
    compile_guided_runtime_plan,
    validate_execution_plan,
)
from app.schemas.guided_edit_revision import guided_editor_revision_from_approval
from tests.pipeline.test_guided_story import _guided_snapshot


def _track(
    *, duration_s: float = 30.0, start_s: float = 2.0, artist: str | None = "Artist"
) -> dict:
    return {
        "track_id": "track-reference",
        "title": "Reference Song",
        "artist": artist,
        "catalog_duration_s": duration_s,
        "start_s": start_s,
        "beat_timestamps_s": [2.8, 5.2, 8.4],
    }


def test_v6_compiles_and_replays_external_reference_without_music() -> None:
    guided = _guided_snapshot()
    plan = compile_execution_plan(guided, track=_track())

    assert plan["compiler_version"] == 7
    assert plan["music"] is None
    assert plan["song_reference"] == {
        "schema_version": 1,
        "delivery": "external_platform",
        "track_id": "track-reference",
        "title": "Reference Song",
        "artist": "Artist",
        "start_s": 2.0,
        "end_s": 20.0,
    }
    assert validate_execution_plan(plan, guided) == plan


def test_v6_too_short_track_replays_without_reference() -> None:
    guided = _guided_snapshot()
    plan = compile_execution_plan(guided, track=_track(duration_s=10.0))

    assert plan.get("song_reference") is None
    assert plan["music"] is None
    assert validate_execution_plan(plan, guided) == plan


def test_runtime_none_audio_recalculates_reference_end_after_trim() -> None:
    guided = _guided_snapshot()
    canonical = compile_execution_plan(guided, track=_track())
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    revision["segments"][-1]["duration_s"] = 4.0
    revision["audio"] = {"mode": "none"}
    revision["state_hash"] = ""

    runtime = compile_guided_runtime_plan(canonical, guided, revision)
    assert runtime["music"] is None
    assert runtime["song_reference"]["start_s"] == 2.0
    assert runtime["song_reference"]["end_s"] == pytest.approx(
        runtime["resolved_duration_s"] + 2.0, abs=0.01
    )


def test_runtime_reference_extension_uses_catalog_fence() -> None:
    guided = _guided_snapshot()
    canonical = compile_execution_plan(guided, track=_track(duration_s=30.0))

    def runtime_with_last_duration(duration_s: float):
        revision = guided_editor_revision_from_approval(
            proposal_version=guided["proposal_version"],
            media_digest=guided["media_digest"],
            snapshot=guided["approved_proposal"],
            execution_plan=canonical,
        )
        revision["segments"][-1]["duration_s"] = duration_s
        revision["audio"] = {"mode": "none"}
        revision["state_hash"] = ""
        return compile_guided_runtime_plan(canonical, guided, revision)

    extended = runtime_with_last_duration(16.0)
    assert extended["song_reference"]["end_s"] <= 30.0
    with pytest.raises(GuidedStoryError, match="does not fit"):
        runtime_with_last_duration(30.0)


def test_legacy_versions_replay_and_omit_reference_fields() -> None:
    guided = _guided_snapshot(direction="fast_montage")
    track = {
        **_track(),
        "audio_gcs_path": "music/reference.m4a",
        "generation": "audio-generation",
    }
    for version in (4, 5):
        plan = _compile_execution_plan_version(guided, track=track, compiler_version=version)
        assert plan["music"] is not None
        assert plan.get("song_reference") is None
        assert "song_reference" not in {k for k, v in plan.items() if v is not None}
        assert "song_reference_track_duration_s" not in {
            k for k, v in plan.items() if v is not None
        }
        assert validate_execution_plan(plan, guided) == plan


@pytest.mark.parametrize("version,catalog_duration", [(4, 30), (5, 30), (6, 30), (6, 2)])
def test_reference_mode_retains_real_beat_snapping_and_canonical_replay(version, catalog_duration):
    guided = _guided_snapshot(direction="fast_montage")
    proposal = guided["approved_proposal"]
    proposal["duration_s"] = 3
    proposal["fast_cuts"] = [
        {
            "cut_id": f"cut-{index}",
            "media_id": media_id,
            "source_start_s": start,
            "source_end_s": start + duration,
            "output_duration_s": duration,
            "role": role,
            "beat_align": align,
        }
        for index, (media_id, start, duration, role, align) in enumerate(
            [
                ("coast-video", 2.0, 0.8, "hook", True),
                ("food-photo", 0.0, 0.8, "build", False),
                ("town-photo", 0.0, 0.8, "build", True),
                ("coast-video", 6.0, 0.6, "payoff", False),
            ]
        )
    ]
    track = {
        **_track(duration_s=catalog_duration, start_s=0, artist=None),
        "audio_gcs_path": "music/legacy.m4a",
        "generation": "audio-generation",
        "beat_timestamps_s": [0.79, 2.41],
    }
    plan = _compile_execution_plan_version(guided, track=track, compiler_version=version)
    assert plan["story_timeline"][0]["beat_time_s"] == pytest.approx(0.79)
    assert plan["story_timeline"][2]["beat_time_s"] == pytest.approx(2.41)
    assert validate_execution_plan(plan, guided) == plan
    if version == 6:
        assert plan["music"] is None
        if catalog_duration >= 3:
            assert plan["song_reference"]["artist"] is None
            assert plan["song_reference"]["end_s"] == pytest.approx(3)
        else:
            assert "song_reference" not in plan
            assert "song_reference_track_duration_s" not in plan
    else:
        assert "song_reference" not in plan
        assert "song_reference_track_duration_s" not in plan
