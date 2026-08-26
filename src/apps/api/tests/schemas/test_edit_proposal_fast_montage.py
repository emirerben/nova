import pytest
from pydantic import ValidationError

from app.schemas.edit_proposal import (
    DirectionHypothesis,
    EditProposal,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    ProposalGuidance,
    StoryBeat,
    parse_edit_proposal,
)


def _snapshot(**overrides) -> EditProposalSnapshot:
    values = {
        "direction": "fast_montage",
        "pace": "fast",
        "duration_s": 3,
        "title": "Corfu",
        "media": [
            MediaRef(
                lane="clip",
                media_id="clip-1",
                gcs_path="users/u/corfu.mp4",
                generation="1",
                kind="video",
                duration_s=3,
            )
        ],
        "story_beats": [
            StoryBeat(
                beat_id="legacy-envelope",
                topic="Highlights",
                media_ids=["clip-1"],
                duration_s=3,
            )
        ],
    }
    values.update(overrides)
    return EditProposalSnapshot(**values)


def test_legacy_fast_montage_without_cut_contract_stays_readable() -> None:
    snapshot = _snapshot()

    assert snapshot.fast_cuts is None


def test_new_fast_montage_cut_contract_is_source_aware_and_hook_first() -> None:
    snapshot = _snapshot(
        fast_cuts=[
            FastMontageCut(
                cut_id="cut-1",
                media_id="clip-1",
                source_start_s=0,
                source_end_s=1,
                output_duration_s=1.0,
                role="hook",
            ),
            FastMontageCut(
                cut_id="cut-2",
                media_id="clip-1",
                source_start_s=1,
                source_end_s=2,
                output_duration_s=1.0,
                role="build",
                beat_align=True,
            ),
            FastMontageCut(
                cut_id="cut-3",
                media_id="clip-1",
                source_start_s=2,
                source_end_s=3,
                output_duration_s=1.0,
                role="payoff",
            ),
        ]
    )

    assert snapshot.fast_cuts[0].transition == "none"
    assert snapshot.fast_cuts[1].beat_align is True


@pytest.mark.parametrize(
    "cut,match",
    [
        (
            FastMontageCut(
                cut_id="cut-1",
                media_id="clip-1",
                source_start_s=0,
                source_end_s=1,
                output_duration_s=1,
                role="build",
            ),
            "open with a hook",
        ),
        (
            FastMontageCut(
                cut_id="cut-1",
                media_id="unknown",
                source_start_s=0,
                source_end_s=1,
                output_duration_s=1,
                role="hook",
            ),
            "missing media",
        ),
    ],
)
def test_fast_montage_snapshot_rejects_invalid_cut_contract(
    cut: FastMontageCut, match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        _snapshot(fast_cuts=[cut])


def test_fast_montage_output_must_match_source_window() -> None:
    with pytest.raises(ValidationError, match="must match"):
        FastMontageCut(
            cut_id="cut-1",
            media_id="clip-1",
            source_start_s=0,
            source_end_s=0.6,
            output_duration_s=0.8,
            role="hook",
        )


def test_fast_montage_cut_total_must_match_proposal_duration() -> None:
    with pytest.raises(ValidationError, match="must match the proposal duration"):
        _snapshot(
            duration_s=3,
            fast_cuts=[
                FastMontageCut(
                    cut_id="cut-1",
                    media_id="clip-1",
                    source_start_s=0,
                    source_end_s=1,
                    output_duration_s=1,
                    role="hook",
                )
            ],
        )


def test_legacy_fast_montage_cannot_use_expanded_video_hold() -> None:
    with pytest.raises(ValidationError, match="above 1.2s require"):
        _snapshot(
            fast_cuts=[
                FastMontageCut(
                    cut_id="cut-1",
                    media_id="clip-1",
                    source_start_s=0,
                    source_end_s=3,
                    output_duration_s=3,
                    role="hook",
                )
            ]
        )


def test_mixed_media_timing_rejects_overlapping_video_windows() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        _snapshot(
            duration_s=3,
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            ),
            fast_cuts=[
                FastMontageCut(
                    cut_id="cut-1",
                    media_id="clip-1",
                    source_start_s=0,
                    source_end_s=1.5,
                    output_duration_s=1.5,
                    role="hook",
                ),
                FastMontageCut(
                    cut_id="cut-2",
                    media_id="clip-1",
                    source_start_s=1,
                    source_end_s=2.5,
                    output_duration_s=1.5,
                    role="payoff",
                ),
            ],
        )


def test_direction_guidance_is_additive_to_legacy_proposal_envelope() -> None:
    legacy = EditProposal(
        proposal_version=1,
        generation_attempt_id="attempt-1",
        status="briefing",
    )
    assert legacy.guidance is None
    assert parse_edit_proposal(legacy.model_dump(mode="json")) == legacy

    guided = legacy.model_copy(
        update={
            "guidance": ProposalGuidance(
                state="awaiting_direction_confirmation",
                provenance="ai_inferred",
                hypothesis=DirectionHypothesis(
                    direction="fast_montage",
                    pace="fast",
                    duration_s=12,
                    text_density="minimal",
                    audio_role="music_led",
                    rationale="The strongest uploaded moments are visual and varied.",
                    buildability_warnings=[],
                ),
                fingerprint="a" * 64,
            )
        }
    )

    reparsed = parse_edit_proposal(guided.model_dump(mode="json"))
    assert reparsed is not None
    assert reparsed.status == "briefing"
    assert reparsed.guidance is not None
    assert reparsed.guidance.state == "awaiting_direction_confirmation"
