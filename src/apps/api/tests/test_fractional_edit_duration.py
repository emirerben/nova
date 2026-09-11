"""Fractional timing survives creator, planner, persistence, and renderer boundaries."""

import json

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents.edit_guide import EditGuideRevision
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
)
from app.pipeline.guided_story import (
    GuidedStoryError,
    compile_execution_plan,
    validate_proposal_timing,
)
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    recognize_total_duration_s,
)
from app.services.edit_direction_planner import (
    clamp_fast_montage_target_duration_s,
    deterministic_fast_cuts,
    deterministic_guided_beats,
)
from app.tasks.edit_proposal_build import adapt_target_duration_s


def _media() -> list[MediaRef]:
    return [
        MediaRef(
            media_id=f"clip-{index}",
            lane="clip",
            kind="video",
            gcs_path=f"users/test/clip-{index}.mp4",
            generation="1",
            duration_s=duration,
        )
        for index, duration in enumerate([4.068333, 4.9, 3.733333])
    ]


def _input(target: int | float = 12) -> EditProposalAgentInput:
    return EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=target,
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in _media()],
        media=[EditProposalMedia.model_validate(ref.model_dump()) for ref in _media()],
    )


def _response() -> dict:
    return {
        "title": "Summer in Madrid",
        "duration_s": 12.7,
        "story_beats": [
            {
                "topic": topic,
                "thought": topic,
                "media_ids": [media_id],
                "layout": "fullscreen",
                "duration_s": duration,
            }
            for topic, media_id, duration in [
                ("Royal Palace of Madrid", "clip-1", 4.9),
                ("Retiro Park", "clip-2", 3.73),
                ("Fountain", "clip-0", 4.07),
            ]
        ],
    }


def _compile(snapshot: EditProposalSnapshot) -> dict:
    return compile_execution_plan(
        {
            "proposal_version": 1,
            "media_digest": canonical_media_digest(snapshot.media),
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": [
                {
                    key: getattr(ref, key)
                    for key in ("lane", "media_id", "gcs_path", "generation", "kind")
                }
                for ref in snapshot.media
            ],
        },
        track=None,
    )


def test_provider_fractional_duration_is_valid_on_first_parse() -> None:
    output = EditProposalAgent(None).parse(json.dumps(_response()), _input())
    assert output.duration_s == 12.7
    assert [beat.thought for beat in output.story_beats] == [
        "Royal Palace of Madrid",
        "Retiro Park",
        "Fountain",
    ]


def test_fractional_duration_roundtrips_from_brief_through_compiled_timeline() -> None:
    brief = ProposalBrief(duration_s=12.7)
    assert _input(brief.duration_s).target_duration_s == 12.7
    output = EditProposalAgentOutput.model_validate(_response())
    snapshot = EditProposalSnapshot(
        title=output.title,
        duration_s=brief.duration_s,
        # A full-footage target uses hard cuts; crossfades consume overlap.
        pace="fast",
        media=_media(),
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in _media()],
        story_beats=[
            StoryBeat(beat_id=f"beat-{index}", **beat.model_dump())
            for index, beat in enumerate(output.story_beats)
        ],
    )
    restored = EditProposalSnapshot.model_validate_json(snapshot.model_dump_json())
    plan = _compile(restored)
    assert plan["approved_duration_s"] == 12.7
    assert plan["resolved_duration_s"] == pytest.approx(12.7, abs=1 / 30)
    assert plan["selected_media_ids"] == ["clip-1", "clip-2", "clip-0"]
    assert [moment["media_id"] for moment in plan["story_timeline"]] == plan["selected_media_ids"]


def test_fractional_creator_and_revision_targets_are_not_rounded() -> None:
    assert CreativeStrategy(target_duration_s=12.7).target_duration_s == 12.7
    revision = EditGuideRevision.model_validate(
        {
            **_response(),
            "direction": "guided_story",
            "pace": "balanced",
            "story_beats": [
                {**beat, "beat_id": f"beat-{index}"}
                for index, beat in enumerate(_response()["story_beats"])
            ],
        }
    )
    assert revision.duration_s == 12.7


@pytest.mark.parametrize(
    "creator_request",
    [
        "Make it 12.7 seconds",
        "Make a 12.7-second edit",
        "I want a 12.7-second video",
        "Use a total of 12.7 seconds",
    ],
)
def test_explicit_fractional_duration_request_is_preserved(creator_request: str) -> None:
    assert recognize_total_duration_s(creator_request) == 12.7


def test_cut_duration_is_not_confused_with_output_duration() -> None:
    assert recognize_total_duration_s("Alternate the clips every 0.7 seconds") is None


def test_footage_clamps_preserve_fractional_seconds() -> None:
    assert adapt_target_duration_s(24, 6.768333) == pytest.approx(6.768333)
    assert adapt_target_duration_s(12.7, 40) == 12.7
    target = clamp_fast_montage_target_duration_s(_media(), 12.7)
    assert target == pytest.approx(12.7)
    cuts = deterministic_fast_cuts(_media(), target)
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(12.7, abs=1 / 30)


def test_guided_fallback_all_media_compiles_in_story_order() -> None:
    snapshot = EditProposalSnapshot(
        title="Fallback",
        duration_s=12,
        media=_media(),
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in _media()],
        story_beats=deterministic_guided_beats(_media(), 12),
    )
    validate_proposal_timing(snapshot)


@pytest.mark.parametrize("scope", ["all", "selected"])
def test_reordered_story_still_requires_exact_media_coverage(scope: str) -> None:
    snapshot = EditProposalSnapshot(
        title="Madrid",
        duration_s=6,
        media=_media(),
        media_scope=scope,
        selected_media_ids=[ref.media_id for ref in _media()],
        story_beats=deterministic_guided_beats(_media(), 6),
    )
    plan = _compile(snapshot)
    assert plan["selected_media_ids"] == ["clip-1", "clip-0", "clip-2"]
    # Reordering is legal, omitting an approved source remains invalid.
    snapshot.story_beats.pop()
    with pytest.raises((GuidedStoryError, ValidationError)):
        _compile(snapshot)


def test_fractional_capacity_never_budgets_an_unavailable_frame() -> None:
    media = [ref.model_copy(update={"duration_s": 3.05}) for ref in _media()[:2]]
    target = clamp_fast_montage_target_duration_s(media, 6.1)
    cuts = deterministic_fast_cuts(media, target)
    assert target == pytest.approx(182 / 30, abs=0.000001)
    assert sum(cut.output_duration_s for cut in cuts) <= target + 0.001


@pytest.mark.parametrize("duration", [2.99, 60.01, float("inf"), float("-inf"), float("nan")])
def test_fractional_duration_keeps_finite_range_validation(duration: float) -> None:
    with pytest.raises(ValidationError):
        ProposalBrief(duration_s=duration)
    with pytest.raises(ValidationError):
        EditProposalAgentOutput.model_validate({**_response(), "duration_s": duration})


def test_integer_duration_keeps_existing_serialization() -> None:
    # Existing approval fingerprints must not change from 12 to 12.0.
    assert json.loads(ProposalBrief(duration_s=12).model_dump_json())["duration_s"] == 12
    assert '"duration_s":12,' in ProposalBrief(duration_s=12).model_dump_json()


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ProposalBrief, "duration_s"),
        (EditProposalAgentOutput, "duration_s"),
        (CreativeStrategy, "target_duration_s"),
        (EditGuideRevision, "duration_s"),
    ],
)
def test_provider_schema_exposes_bounded_fractional_number(model: type, field: str) -> None:
    schema = model.model_json_schema()["properties"][field]
    assert schema["type"] == "number"
    assert schema["minimum"] == 3
    assert schema["maximum"] == 60


def test_fractional_source_precision_allows_subframe_beat_sum_drift() -> None:
    response = _response()
    for beat, duration in zip(response["story_beats"], [4.9, 3.733333, 4.068333], strict=True):
        beat["duration_s"] = duration
    output = EditProposalAgent(None).parse(json.dumps(response), _input(12.7))
    assert output.duration_s == 12.7
    response["story_beats"][0]["duration_s"] += 1 / 30
    with pytest.raises(SchemaError, match="beat durations do not fit"):
        EditProposalAgent(None).parse(json.dumps(response), _input(12.7))
