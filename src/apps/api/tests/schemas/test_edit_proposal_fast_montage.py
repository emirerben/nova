import pytest
from pydantic import ValidationError

from app.schemas.edit_proposal import (
    DirectionHypothesis,
    EditProposal,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    MontageAudioPlan,
    MontageCadenceConstraint,
    MontageTextBinding,
    ProposalGuidance,
    StoryBeat,
    media_context_group,
    parse_edit_proposal,
    recognize_image_layout,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
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


def test_numeric_still_hold_round_trips_and_does_not_relax_video_minimum() -> None:
    profile = recognize_mixed_media_timing(
        "Use photos at 0.1 seconds and let the videos hold longer."
    )
    assert profile is not None
    assert profile.image_hold_s == pytest.approx(0.1)
    assert MixedMediaTimingProfile.model_validate(profile.model_dump(mode="json")) == profile

    media = [
        MediaRef(
            lane="asset",
            media_id="photo-1",
            gcs_path="users/u/photo.jpg",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="video-1",
            gcs_path="users/u/video.mp4",
            generation="1",
            kind="video",
            duration_s=3,
        ),
    ]
    profile = profile.model_copy(update={"image_hold_s": 0.1})
    snapshot = _snapshot(
        media=media,
        mixed_media_timing=profile,
        story_beats=[
            StoryBeat(
                beat_id="legacy-envelope",
                topic="Highlights",
                media_ids=["photo-1", "video-1"],
                duration_s=3,
            )
        ],
        fast_cuts=[
            FastMontageCut(
                cut_id="photo",
                media_id="photo-1",
                source_start_s=0,
                source_end_s=0.1,
                output_duration_s=0.1,
                role="hook",
            ),
            FastMontageCut(
                cut_id="video",
                media_id="video-1",
                source_start_s=0,
                source_end_s=2.9,
                output_duration_s=2.9,
                role="payoff",
            ),
        ],
    )
    assert snapshot.fast_cuts[0].output_duration_s == pytest.approx(0.1)


def test_latest_numeric_still_hold_overrides_earlier_conversation_value() -> None:
    profile = recognize_mixed_media_timing(
        "Use photos at 0.1 seconds and let the videos hold longer.\n"
        "Make the images stay 0.2 seconds instead of 0.1."
    )

    assert profile is not None
    assert profile.image_hold_s == pytest.approx(0.2)


def test_mixed_media_timing_preserves_photo_runs_and_ordered_sport_context() -> None:
    profile = recognize_mixed_media_timing(
        "Amongst the videos, add groups of photos that transition in 0.1 seconds. "
        "Group content sequentially by football, basketball, and beach volleyball. "
        "Group by sport and context rather than putting one image between videos."
    )

    assert profile is not None
    assert profile.image_grouping == "runs"
    assert profile.sequence_grouping == "sport_context"
    assert profile.sequence_group_order == [
        "football",
        "basketball",
        "beach_volleyball",
    ]


def test_mixed_media_timing_ignores_total_duration_near_photo_coverage_language() -> None:
    profile = recognize_mixed_media_timing(
        "Use every attached photo and video once. Make the edit 60 seconds. "
        "Keep photos at 0.2 seconds and let video moments breathe longer. "
        "Group football, basketball, and beach volleyball sequentially. "
        "Use every attached photo and video once. Make the edit 60 seconds. "
        "Correction: group photos into runs and group by sport and context."
    )

    assert profile is not None
    assert profile.image_hold_s == pytest.approx(0.2)
    assert profile.image_grouping == "runs"
    assert profile.sequence_grouping == "sport_context"


def test_media_context_group_prefers_specific_subject_over_broader_description() -> None:
    assert (
        media_context_group(
            "",
            "group after playing soccer",
            "friends standing near an outdoor basketball court",
        )
        == "football"
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Don't make the images fill the screen", "supporting_card"),
        ("Fit every photo completely without cropping", "supporting_card"),
        ("Make the photos fill the screen", "fullscreen"),
        (
            "Don't make the images fill the screen. Actually, make the photos fill the screen.",
            "fullscreen",
        ),
        (
            "Render landscape photos exactly like landscape videos, using the normal "
            "full-frame crop with no card, black bars, or blurred background.",
            "fullscreen",
        ),
    ],
)
def test_image_layout_recognizes_latest_explicit_creator_instruction(
    message: str, expected: str
) -> None:
    assert recognize_image_layout(message) == expected


def test_fast_montage_snapshot_rejects_subminimum_video_cut() -> None:
    with pytest.raises(ValidationError, match="video cuts must be at least 0.4s"):
        _snapshot(
            fast_cuts=[
                FastMontageCut(
                    cut_id="video-cut",
                    media_id="clip-1",
                    source_start_s=0,
                    source_end_s=0.3,
                    output_duration_s=0.3,
                    role="hook",
                )
            ]
        )


def test_mixed_media_snapshot_accepts_complete_very_short_video() -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.2,
        video_hold="longer",
        boundary_style="cut",
    )
    snapshot = _snapshot(
        duration_s=3,
        media=[
            MediaRef(
                lane="clip",
                media_id="short-video",
                gcs_path="users/u/short.mp4",
                generation="1",
                kind="video",
                duration_s=0.3,
            ),
            MediaRef(
                lane="asset",
                media_id="photo",
                gcs_path="users/u/photo.jpg",
                generation="1",
                kind="image",
            ),
            MediaRef(
                lane="clip",
                media_id="long-video",
                gcs_path="users/u/long.mp4",
                generation="1",
                kind="video",
                duration_s=3,
            ),
        ],
        mixed_media_timing=profile,
        story_beats=[
            StoryBeat(
                beat_id="legacy-envelope",
                topic="Highlights",
                media_ids=["short-video", "photo", "long-video"],
                duration_s=3,
            )
        ],
        fast_cuts=[
            FastMontageCut(
                cut_id="short",
                media_id="short-video",
                source_start_s=0,
                source_end_s=0.3,
                output_duration_s=0.3,
                role="hook",
            ),
            FastMontageCut(
                cut_id="photo",
                media_id="photo",
                source_start_s=0,
                source_end_s=0.2,
                output_duration_s=0.2,
                role="build",
            ),
            FastMontageCut(
                cut_id="long",
                media_id="long-video",
                source_start_s=0,
                source_end_s=2.5,
                output_duration_s=2.5,
                role="payoff",
            ),
        ],
    )

    assert [cut.media_id for cut in snapshot.fast_cuts or []] == [
        "short-video",
        "photo",
        "long-video",
    ]


def test_mixed_media_snapshot_requires_three_photos_when_available() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id=f"photo-{index}",
            gcs_path=f"users/u/photo-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(3)
    ] + [
        MediaRef(
            lane="clip",
            media_id="video-1",
            gcs_path="users/u/video.mp4",
            generation="1",
            kind="video",
            duration_s=3,
        )
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.1,
        video_hold="longer",
        boundary_style="cut",
    )
    with pytest.raises(ValidationError, match="up to three distinct photos"):
        _snapshot(
            media=media,
            mixed_media_timing=profile,
            story_beats=[
                StoryBeat(
                    beat_id="legacy-envelope",
                    topic="Highlights",
                    media_ids=["photo-0", "photo-1", "photo-2", "video-1"],
                    duration_s=3,
                )
            ],
            fast_cuts=[
                FastMontageCut(
                    cut_id="photo",
                    media_id="photo-0",
                    source_start_s=0,
                    source_end_s=0.1,
                    output_duration_s=0.1,
                    role="hook",
                ),
                FastMontageCut(
                    cut_id="video",
                    media_id="video-1",
                    source_start_s=0,
                    source_end_s=2.9,
                    output_duration_s=2.9,
                    role="payoff",
                ),
            ],
        )


def test_cadence_cut_duration_must_align_to_thirty_fps_frames() -> None:
    assert (
        MontageCadenceConstraint(
            source_media_ids=["clip-1", "clip-2"], cut_duration_s=0.4
        ).cut_duration_s
        == 0.4
    )

    with pytest.raises(ValidationError, match="align to 30fps frames"):
        MontageCadenceConstraint(source_media_ids=["clip-1", "clip-2"], cut_duration_s=0.41)


def test_cadence_recognizer_ignores_non_frame_aligned_timing() -> None:
    assert recognize_round_robin_cadence("alternate every 0.41 seconds") is None
    assert recognize_round_robin_cadence("alternate every 0.4 seconds") == 0.4


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


def test_explicit_cadence_allows_exact_cuts_above_legacy_montage_limit() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"users/u/{media_id}.mp4",
            generation="1",
            kind="video",
            duration_s=3,
        )
        for media_id in ("clip-1", "clip-2")
    ]
    cadence = MontageCadenceConstraint(source_media_ids=["clip-1", "clip-2"], cut_duration_s=1.5)

    snapshot = _snapshot(
        media=media,
        montage_cadence=cadence,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index + 1}",
                media_id=media_id,
                source_start_s=0,
                source_end_s=1.5,
                output_duration_s=1.5,
                role="hook" if index == 0 else "payoff",
            )
            for index, media_id in enumerate(cadence.source_media_ids)
        ],
    )

    assert [cut.output_duration_s for cut in snapshot.fast_cuts or []] == [1.5, 1.5]


def _cadence_snapshot_payload(*, reuse_policy: str = "no_repeat") -> dict:
    media = [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"users/u/{media_id}.mp4",
            generation="1",
            kind="video",
            duration_s=4,
        )
        for media_id in ("clip-1", "clip-2")
    ]
    return {
        "direction": "fast_montage",
        "pace": "fast",
        "duration_s": 4,
        "title": "Alternating matches",
        "media": media,
        "story_beats": [
            StoryBeat(
                beat_id="legacy-envelope",
                topic="Highlights",
                media_ids=["clip-1", "clip-2"],
                duration_s=4,
            )
        ],
        "montage_cadence": MontageCadenceConstraint(
            source_media_ids=["clip-1", "clip-2"],
            cut_duration_s=1,
            reuse_policy=reuse_policy,
        ),
        "fast_cuts": [
            FastMontageCut(
                cut_id=f"cut-{index + 1}",
                media_id=("clip-1", "clip-2")[index % 2],
                source_start_s=float(index // 2),
                source_end_s=float(index // 2 + 1),
                output_duration_s=1,
                role="hook" if index == 0 else "payoff" if index == 3 else "build",
            )
            for index in range(4)
        ],
    }


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("wrong_direction", "only valid for fast montage"),
        ("unknown_source", "references unknown media"),
        ("missing_cuts", "requires fast cuts"),
        ("partial_cycle", "complete source cycles"),
        ("wrong_order", "preserve round-robin source order"),
        ("wrong_duration", "preserve the exact cadence duration"),
        ("overlapping_no_repeat", "must not overlap"),
    ],
)
def test_cadence_snapshot_rejects_invalid_contracts(case: str, match: str) -> None:
    payload = _cadence_snapshot_payload()
    cuts = list(payload["fast_cuts"])
    if case == "wrong_direction":
        payload["direction"] = "guided_story"
        payload["fast_cuts"] = None
    elif case == "unknown_source":
        payload["montage_cadence"] = MontageCadenceConstraint(
            source_media_ids=["clip-1", "missing"], cut_duration_s=1
        )
    elif case == "missing_cuts":
        payload["fast_cuts"] = None
    elif case == "partial_cycle":
        payload["duration_s"] = 3
        payload["fast_cuts"] = cuts[:3]
    elif case == "wrong_order":
        payload["fast_cuts"] = [cuts[0], cuts[2], cuts[1], cuts[3]]
    elif case == "wrong_duration":
        payload["montage_cadence"] = MontageCadenceConstraint(
            source_media_ids=["clip-1", "clip-2"], cut_duration_s=2
        )
    elif case == "overlapping_no_repeat":
        payload["fast_cuts"] = [
            cuts[0],
            cuts[1],
            cuts[2].model_copy(update={"source_start_s": 0, "source_end_s": 1}),
            cuts[3],
        ]

    with pytest.raises(ValidationError, match=match):
        EditProposalSnapshot.model_validate(payload)


def test_cadence_snapshot_allows_overlap_only_after_explicit_reuse_opt_in() -> None:
    payload = _cadence_snapshot_payload(reuse_policy="allow_repeat")
    cuts = list(payload["fast_cuts"])
    payload["fast_cuts"] = [
        cuts[0],
        cuts[1],
        cuts[2].model_copy(update={"source_start_s": 0, "source_end_s": 1}),
        cuts[3].model_copy(update={"source_start_s": 0, "source_end_s": 1}),
    ]

    snapshot = EditProposalSnapshot.model_validate(payload)

    assert snapshot.montage_cadence is not None
    assert snapshot.montage_cadence.reuse_policy == "allow_repeat"


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


def test_montage_contract_accepts_ai_authored_source_order_and_bindings() -> None:
    snapshot = _snapshot(
        duration_s=4,
        media=[
            MediaRef(
                lane="clip",
                media_id="clip-1",
                gcs_path="users/u/a.mp4",
                generation="1",
                kind="video",
                duration_s=4,
            ),
            MediaRef(
                lane="clip",
                media_id="clip-2",
                gcs_path="users/u/b.mp4",
                generation="1",
                kind="video",
                duration_s=4,
            ),
        ],
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id=("clip-1" if index % 2 == 0 else "clip-2"),
                source_start_s=(index // 2),
                source_end_s=(index // 2) + 1,
                output_duration_s=1,
                role="hook" if index == 0 else "payoff" if index == 3 else "build",
            )
            for index in range(4)
        ],
        montage_text_bindings=[
            MontageTextBinding(media_id="clip-1", text="The same release"),
            MontageTextBinding(media_id="clip-2", text="A different night"),
        ],
        montage_audio=MontageAudioPlan(
            preserve_source_audio=True,
            preview_source_beds=True,
            source_media_ids=["clip-1", "clip-2"],
        ),
    )

    assert [cut.media_id for cut in snapshot.fast_cuts] == [
        "clip-1",
        "clip-2",
        "clip-1",
        "clip-2",
    ]
    assert len(snapshot.montage_text_bindings) == 2


def test_montage_contract_does_not_require_round_robin_or_fixed_cadence() -> None:
    snapshot = _snapshot(
        duration_s=4,
        media=[
            MediaRef(
                lane="clip",
                media_id=f"clip-{index}",
                gcs_path=f"users/u/{index}.mp4",
                generation="1",
                kind="video",
                duration_s=4,
            )
            for index in range(1, 4)
        ],
        fast_cuts=[
            FastMontageCut(
                cut_id="cut-1",
                media_id="clip-1",
                source_start_s=0,
                source_end_s=0.8,
                output_duration_s=0.8,
                role="hook",
            ),
            FastMontageCut(
                cut_id="cut-2",
                media_id="clip-2",
                source_start_s=0,
                source_end_s=1.2,
                output_duration_s=1.2,
                role="build",
            ),
            FastMontageCut(
                cut_id="cut-3",
                media_id="clip-3",
                source_start_s=0,
                source_end_s=0.8,
                output_duration_s=0.8,
                role="build",
            ),
            FastMontageCut(
                cut_id="cut-4",
                media_id="clip-1",
                source_start_s=0.8,
                source_end_s=2.0,
                output_duration_s=1.2,
                role="payoff",
            ),
        ],
    )

    assert [cut.media_id for cut in snapshot.fast_cuts] == ["clip-1", "clip-2", "clip-3", "clip-1"]


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
