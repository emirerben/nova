import json

import pytest

from app.agents._runtime import SchemaError, TerminalError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
)
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    MontageCadenceConstraint,
    StoryBeat,
    media_context_group,
)
from app.services import edit_direction_planner


class FailingAgent:
    def __init__(self, _client) -> None:  # noqa: ANN001
        pass

    def run(self, _input, ctx=None):  # noqa: ANN001, ARG002
        raise TerminalError("provider returned invalid fast-cut arithmetic")


def test_round_robin_capacity_and_fallback_match_production_lengths() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id="match-a",
            gcs_path="users/test/match-a.mp4",
            generation="1",
            kind="video",
            duration_s=6.633,
            analysis={
                "best_moments": [
                    {"start_s": 2.0, "end_s": 6.0, "energy": 9.0},
                ]
            },
        ),
        MediaRef(
            lane="asset",
            media_id="match-b",
            gcs_path="users/test/match-b.mp4",
            generation="1",
            kind="video",
            duration_s=26.433,
            analysis={
                "best_moments": [
                    {"start_s": 10.0, "end_s": 16.0, "energy": 8.0},
                ]
            },
        ),
    ]
    cadence = MontageCadenceConstraint(
        source_media_ids=["match-a", "match-b"],
        cut_duration_s=1,
    )

    assert edit_direction_planner.round_robin_capacity_s(media, cadence) == 12

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 12, montage_cadence=cadence)

    assert len(cuts) == 12
    assert [cut.media_id for cut in cuts] == ["match-a", "match-b"] * 6
    assert all(cut.output_duration_s == 1 for cut in cuts)
    assert cuts[0].source_start_s == 2
    assert cuts[1].source_start_s == 10
    for media_id in cadence.source_media_ids:
        windows = sorted(
            (cut.source_start_s, cut.source_end_s) for cut in cuts if cut.media_id == media_id
        )
        assert all(
            current[0] >= previous[1]
            for previous, current in zip(windows, windows[1:], strict=False)
        )


def test_round_robin_offset_best_moment_cannot_fragment_valid_capacity() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"users/test/{media_id}.mp4",
            generation="1",
            kind="video",
            duration_s=2,
            analysis={"best_moments": [{"start_s": 0.5, "end_s": 1.5, "energy": 9}]},
        )
        for media_id in ("match-a", "match-b")
    ]
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 4, montage_cadence=cadence)

    assert [cut.media_id for cut in cuts] == ["match-a", "match-b"] * 2
    for media_id in cadence.source_media_ids:
        assert [
            (cut.source_start_s, cut.source_end_s) for cut in cuts if cut.media_id == media_id
        ] == [(0, 1), (1, 2)]


def test_round_robin_reuses_ranked_windows_only_after_explicit_opt_in() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"users/test/{media_id}.mp4",
            generation="1",
            kind="video",
            duration_s=1,
            analysis={"best_moments": [{"start_s": 0, "end_s": 1, "energy": 9}]},
        )
        for media_id in ("match-a", "match-b")
    ]
    no_repeat = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    with pytest.raises(ValueError, match="non-repeating source capacity"):
        edit_direction_planner.deterministic_fast_cuts(media, 4, montage_cadence=no_repeat)

    allow_repeat = no_repeat.model_copy(update={"reuse_policy": "allow_repeat"})
    cuts = edit_direction_planner.deterministic_fast_cuts(media, 4, montage_cadence=allow_repeat)
    assert [cut.media_id for cut in cuts] == ["match-a", "match-b"] * 2
    assert [(cut.source_start_s, cut.source_end_s) for cut in cuts] == [(0, 1)] * 4


def test_fast_montage_snapshot_uses_server_requested_duration(monkeypatch) -> None:
    source = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the trip story",
        pace="balanced",
        duration_s=8,
        title="Corfu",
        media=[
            MediaRef(
                lane="clip",
                media_id="media-1",
                gcs_path="users/test/media-1.mp4",
                generation="1",
                kind="video",
                duration_s=10,
            )
        ],
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Corfu",
                media_ids=["media-1"],
                duration_s=8,
            )
        ],
    )
    cuts = [
        FastMontageCut(
            cut_id=f"cut-{index + 1}",
            media_id="media-1",
            source_start_s=index,
            source_end_s=index + 1,
            output_duration_s=1,
            role="hook" if index == 0 else "payoff" if index == 2 else "build",
        )
        for index in range(3)
    ]

    class FakeAgent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, _input, ctx=None) -> EditProposalAgentOutput:  # noqa: ANN001, ARG002
            return EditProposalAgentOutput(
                title="Fast Corfu",
                duration_s=4,
                story_beats=[],
                fast_cuts=cuts,
            )

    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FakeAgent)

    snapshot = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal="Move through the strongest moments",
        pace="fast",
        duration_s=3,
    )

    assert snapshot.duration_s == 3
    assert sum(cut.output_duration_s for cut in snapshot.fast_cuts or []) == pytest.approx(3)


def test_fast_montage_uses_analyzed_deterministic_fallback_on_terminal_schema(
    monkeypatch,
) -> None:
    source = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the trip story",
        pace="balanced",
        duration_s=4,
        title="Creator title",
        media=[
            MediaRef(
                lane="clip",
                media_id="strong",
                gcs_path="users/test/strong.mp4",
                generation="1",
                kind="video",
                duration_s=8,
                analysis={
                    "best_moments": [
                        {"start_s": 2, "end_s": 4, "energy": 9},
                    ]
                },
            ),
            MediaRef(
                lane="clip",
                media_id="support",
                gcs_path="users/test/support.mp4",
                generation="1",
                kind="video",
                duration_s=8,
                analysis={
                    "best_moments": [
                        {"start_s": 4, "end_s": 6, "energy": 5},
                    ]
                },
            ),
        ],
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Corfu",
                media_ids=["strong", "support"],
                duration_s=4,
            )
        ],
    )

    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)

    snapshot = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal="Move through the strongest moments",
        pace="fast",
        duration_s=4,
        job_id="job-1",
    )

    cuts = snapshot.fast_cuts or []
    assert snapshot.duration_s == 4
    assert snapshot.title == "Creator title"
    assert len(cuts) == 4
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(4)
    assert [cut.media_id for cut in cuts] == ["strong", "support", "strong", "support"]
    assert cuts[0].role == "hook"
    assert cuts[-1].role == "payoff"
    assert all(cut.transition == "none" for cut in cuts)
    assert cuts[0].source_start_s == 0
    assert cuts[0].source_end_s == 1.2
    strong_windows = sorted(
        (cut.source_start_s, cut.source_end_s) for cut in cuts if cut.media_id == "strong"
    )
    assert all(
        current[0] >= previous[1] for previous, current in zip(strong_windows, strong_windows[1:])
    )


def test_mixed_media_fallback_prefers_quick_photos_and_longer_videos() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id="photo",
            gcs_path="users/test/photo.jpg",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="video",
            gcs_path="users/test/video.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        ),
    ]
    cuts = edit_direction_planner.deterministic_fast_cuts(
        media,
        4,
        MixedMediaTimingProfile(image_hold="very_fast", video_hold="longer", boundary_style="cut"),
    )
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(4)
    assert any(cut.media_id == "photo" and 0.5 <= cut.output_duration_s <= 0.8 for cut in cuts)
    assert any(cut.media_id == "video" and cut.output_duration_s >= 1.5 for cut in cuts)
    assert all(cut.transition == "none" for cut in cuts)


def test_mixed_media_fallback_preserves_numeric_still_cadence_and_cfr_windows() -> None:
    media = [
        *[
            MediaRef(
                lane="asset",
                media_id=f"photo-{index}",
                gcs_path=f"users/test/photo-{index}.jpg",
                generation="1",
                kind="image",
            )
            for index in range(4)
        ],
        MediaRef(
            lane="clip",
            media_id="video-a",
            gcs_path="users/test/video-a.mp4",
            generation="1",
            kind="video",
            duration_s=2.067,
        ),
        MediaRef(
            lane="clip",
            media_id="video-b",
            gcs_path="users/test/video-b.mp4",
            generation="1",
            kind="video",
            duration_s=2.833,
        ),
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.1,
        video_hold="longer",
        boundary_style="cut",
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 3, profile)

    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(3)
    assert all(
        cut.output_duration_s * 30 == pytest.approx(round(cut.output_duration_s * 30))
        for cut in cuts
    )
    assert all(
        (cut.source_end_s - cut.source_start_s) * 30
        == pytest.approx(round((cut.source_end_s - cut.source_start_s) * 30))
        for cut in cuts
    )
    assert any(
        cut.media_id.startswith("photo-") and cut.output_duration_s == pytest.approx(0.1)
        for cut in cuts
    )


def test_mixed_media_fallback_keeps_multiple_photos_when_videos_rank_higher() -> None:
    media = [
        *[
            MediaRef(
                lane="asset",
                media_id=f"photo-{index}",
                gcs_path=f"users/test/photo-{index}.jpg",
                generation="1",
                kind="image",
            )
            for index in range(23)
        ],
        *[
            MediaRef(
                lane="clip",
                media_id=f"video-{index}",
                gcs_path=f"users/test/video-{index}.mp4",
                generation="1",
                kind="video",
                duration_s=30,
                analysis={"best_moments": [{"start_s": 0, "end_s": 30, "energy": 10}]},
            )
            for index in range(8)
        ],
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.1,
        video_hold="longer",
        boundary_style="cut",
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 30, profile)

    selected_photos = {cut.media_id for cut in cuts if cut.media_id.startswith("photo-")}
    assert len(selected_photos) >= 3
    assert all(
        cut.output_duration_s == pytest.approx(0.1)
        for cut in cuts
        if cut.media_id.startswith("photo-")
    )
    assert all(cut.output_duration_s >= 0.4 for cut in cuts if cut.media_id.startswith("video-"))


def test_mixed_media_fallback_caps_source_floor_to_low_target_capacity() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id=f"photo-{index}",
            gcs_path=f"users/test/photo-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(7)
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 3, profile)

    assert len({cut.media_id for cut in cuts}) == 6
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(3)
    assert all(0.5 <= cut.output_duration_s <= 0.8 for cut in cuts)


def test_mixed_media_fallback_source_floor_accounts_for_video_hold() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id=f"photo-{index}",
            gcs_path=f"users/test/photo-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(7)
    ] + [
        MediaRef(
            lane="clip",
            media_id="video",
            gcs_path="users/test/video.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        )
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 3, profile)

    assert len({cut.media_id for cut in cuts}) == 4
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(3)
    assert any(
        cut.media_id == "video" and cut.output_duration_s == pytest.approx(1.5) for cut in cuts
    )


def test_mixed_media_target_is_clamped_to_image_and_video_capacity() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id="photo",
            gcs_path="users/test/photo.jpg",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="video",
            gcs_path="users/test/video.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        ),
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    # One photo can separate only two windows from the same video, so the
    # schedulable capacity is 3s + 0.8s + 3s, not the raw 8.8s source sum.
    assert edit_direction_planner.clamp_fast_montage_target_duration_s(media, 60, profile) == 6
    # No profile means the legacy 3–60s target contract remains unchanged.
    assert edit_direction_planner.clamp_fast_montage_target_duration_s(media, 24) == 24


def test_one_video_one_photo_fallback_succeeds_at_adjacency_aware_clamp(monkeypatch) -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id="photo",
            gcs_path="users/test/photo.jpg",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="video",
            gcs_path="users/test/video.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        ),
    ]
    source = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the story",
        pace="balanced",
        duration_s=60,
        title="Creator title",
        media=media,
        story_beats=[
            StoryBeat(beat_id="beat-1", topic="Story", media_ids=["photo"], duration_s=12)
        ],
    )
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)

    planned = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal="Move through the strongest moments",
        pace="fast",
        duration_s=60,
        mixed_media_timing=profile,
    )

    assert planned.duration_s == 6
    assert sum(cut.output_duration_s for cut in planned.fast_cuts or []) == pytest.approx(6)


def test_mixed_media_target_rejects_capacity_below_agent_minimum() -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id="photo",
            gcs_path="users/test/photo.jpg",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="video",
            gcs_path="users/test/video.mp4",
            generation="1",
            kind="video",
            duration_s=1.0,
        ),
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    with pytest.raises(ValueError, match="less than the minimum 3s"):
        edit_direction_planner.clamp_fast_montage_target_duration_s(media, 60, profile)


def test_mixed_media_fallback_uses_clamped_target(monkeypatch) -> None:
    media = [
        MediaRef(
            lane="asset",
            media_id="photo",
            gcs_path="users/test/photo.jpg",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="video-1",
            gcs_path="users/test/video-1.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        ),
        MediaRef(
            lane="clip",
            media_id="video-2",
            gcs_path="users/test/video-2.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        ),
    ]
    source = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the story",
        pace="balanced",
        duration_s=60,
        title="Creator title",
        media=media,
        story_beats=[
            StoryBeat(beat_id="beat-1", topic="Story", media_ids=["photo"], duration_s=12)
        ],
    )
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)

    planned = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal="Move through the strongest moments",
        pace="fast",
        duration_s=60,
        mixed_media_timing=profile,
    )

    assert planned.duration_s == 16
    assert sum(cut.output_duration_s for cut in planned.fast_cuts or []) == pytest.approx(16)


def test_real_large_mixed_media_shape_selects_a_timed_subset_without_overlap() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id=f"video-{index}",
            gcs_path=f"users/test/video-{index}.mp4",
            generation="1",
            kind="video",
            duration_s=8.0,
            analysis={"best_moments": [{"energy": 10 - (index % 5)}]},
        )
        for index in range(45)
    ] + [
        MediaRef(
            lane="asset",
            media_id=f"photo-{index}",
            gcs_path=f"users/test/photo-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(58)
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast", video_hold="longer", boundary_style="cut"
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 24, profile)

    assert len(cuts) < len(media)
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(24, abs=0.001)
    by_id = {ref.media_id: ref for ref in media}
    assert {by_id[cut.media_id].kind for cut in cuts} == {"image", "video"}
    windows: dict[str, list[tuple[float, float]]] = {}
    for cut in cuts:
        ref = by_id[cut.media_id]
        assert cut.transition == "none"
        if ref.kind == "image":
            assert 0.5 <= cut.output_duration_s <= 0.8
        else:
            assert 1.5 <= cut.output_duration_s <= 3.0
            assert cut.source_end_s <= float(ref.duration_s)
            windows.setdefault(ref.media_id, []).append((cut.source_start_s, cut.source_end_s))
    for source_windows in windows.values():
        ordered = sorted(source_windows)
        assert all(
            current[0] >= previous[1]
            for previous, current in zip(ordered, ordered[1:], strict=False)
        )


def test_guided_replan_terminal_failure_builds_compiler_valid_mixed_media_fallback(
    monkeypatch,
) -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id=f"clip-{index}",
            gcs_path=f"users/test/clip-{index}.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        )
        for index in range(45)
    ]
    media.extend(
        MediaRef(
            lane="asset",
            media_id=f"asset-{index}",
            gcs_path=f"users/test/asset-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(58)
    )
    source = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the story",
        pace="balanced",
        duration_s=24,
        title="Creator-approved title",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Opening",
                media_ids=[media[0].media_id],
                duration_s=12,
            ),
            StoryBeat(
                beat_id="beat-2",
                topic="Closing",
                media_ids=[media[-1].media_id],
                duration_s=12,
            ),
        ],
    )
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)

    planned = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="guided_story",
        goal="Use the strongest photos and clips",
        pace="balanced",
        duration_s=24,
    )

    assert planned.title == "Creator-approved title"
    assert len(planned.media) == 103
    selected = {media_id for beat in planned.story_beats for media_id in beat.media_ids}
    assert selected <= {ref.media_id for ref in planned.media}
    assert {ref.kind for ref in planned.media if ref.media_id in selected} == {"image", "video"}


def test_text_explainer_replan_terminal_failure_does_not_invent_copy(monkeypatch) -> None:
    source = _mixed_duration_source(14)
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)

    with pytest.raises(TerminalError):
        edit_direction_planner.plan_direction_snapshot(
            source,
            direction="text_explainer",
            goal="Explain the visible details",
            pace="balanced",
            duration_s=14,
        )


def _mixed_duration_source(duration_s: int) -> EditProposalSnapshot:
    media = [
        MediaRef(
            lane="clip",
            media_id="short",
            gcs_path="users/test/short.mp4",
            generation="1",
            kind="video",
            duration_s=0.4,
            analysis={"best_moments": [{"start_s": 0, "end_s": 0.4, "energy": 10}]},
        ),
        *[
            MediaRef(
                lane="clip",
                media_id=f"long-{index}",
                gcs_path=f"users/test/long-{index}.mp4",
                generation="1",
                kind="video",
                duration_s=10,
                analysis={"best_moments": [{"start_s": 1, "end_s": 4, "energy": 9 - index}]},
            )
            for index in range(4)
        ],
    ]
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the trip story",
        pace="balanced",
        duration_s=duration_s,
        title="Creator title",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Corfu",
                media_ids=[ref.media_id for ref in media[:4]],
                duration_s=min(12, duration_s),
            )
        ],
    )


def test_fast_montage_fallback_omits_unneeded_short_source_without_shortening_long_cuts(
    monkeypatch,
) -> None:
    duration_s = 14
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)
    source = _mixed_duration_source(duration_s)

    snapshot = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal="Move through the strongest moments",
        pace="fast",
        duration_s=duration_s,
    )

    cuts = snapshot.fast_cuts or []
    short_cuts = [cut for cut in cuts if cut.media_id == "short"]
    long_cuts = [cut for cut in cuts if cut.media_id != "short"]
    assert len(cuts) <= 80
    assert short_cuts == []
    assert len({cut.media_id for cut in long_cuts}) >= 4
    assert all(0.8 <= cut.output_duration_s <= 1.2 for cut in long_cuts)
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(duration_s)
    by_id = {ref.media_id: ref for ref in source.media}
    assert all(cut.source_end_s <= float(by_id[cut.media_id].duration_s or 0) for cut in cuts)


def test_fast_montage_fallback_fails_when_non_overlapping_capacity_is_insufficient(
    monkeypatch,
) -> None:
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)

    with pytest.raises(ValueError, match="distinct source windows safely"):
        edit_direction_planner.plan_direction_snapshot(
            _mixed_duration_source(60),
            direction="fast_montage",
            goal="Move through the strongest moments",
            pace="fast",
            duration_s=60,
        )


def test_fast_montage_fallback_respects_subsecond_source_specific_capacity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", FailingAgent)
    source = _mixed_duration_source(14)
    source.media[0] = source.media[0].model_copy(update={"duration_s": 0.8})
    source.media[1] = source.media[1].model_copy(update={"duration_s": 0.9})

    snapshot = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal="Move through the strongest moments",
        pace="fast",
        duration_s=14,
    )

    cuts = snapshot.fast_cuts or []
    by_id = {ref.media_id: ref for ref in source.media}
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(14)
    assert not [cut for cut in cuts if cut.media_id == "short"]
    assert len([cut for cut in cuts if cut.media_id == "long-0"]) == 1
    assert all(cut.source_start_s >= 0 for cut in cuts)
    assert all(
        cut.source_end_s <= float(by_id[cut.media_id].duration_s or 0) + 0.001 for cut in cuts
    )


def test_fast_montage_fallback_never_repeats_adjacent_or_overlaps_video_windows() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id="long",
            gcs_path="users/test/long.mp4",
            generation="1",
            kind="video",
            duration_s=10,
        ),
        *[
            MediaRef(
                lane="clip",
                media_id=f"short-{index}",
                gcs_path=f"users/test/short-{index}.mp4",
                generation="1",
                kind="video",
                duration_s=0.8 + index * 0.04,
            )
            for index in range(6)
        ],
    ]

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 10)

    assert all(left.media_id != right.media_id for left, right in zip(cuts, cuts[1:]))
    windows_by_id: dict[str, list[tuple[float, float]]] = {}
    for cut in cuts:
        windows_by_id.setdefault(cut.media_id, []).append((cut.source_start_s, cut.source_end_s))
    for windows in windows_by_id.values():
        windows.sort()
        assert all(current[0] >= previous[1] for previous, current in zip(windows, windows[1:]))


def test_mixed_media_fallback_uses_every_source_once_when_they_fit() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id=f"video-{index}",
            gcs_path=f"users/test/video-{index}.mp4",
            generation="1",
            kind="video",
            duration_s=0.3 if index == 0 else 3,
            analysis={"best_moments": [{"start_s": 0, "end_s": 3, "energy": 10 - index / 10}]},
        )
        for index in range(35)
    ] + [
        MediaRef(
            lane="asset",
            media_id=f"photo-{index}",
            gcs_path=f"users/test/photo-{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(23)
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.2,
        video_hold="longer",
        boundary_style="cut",
        image_grouping="runs",
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(
        media,
        60,
        mixed_media_timing=profile,
    )

    assert len(cuts) == len(media)
    assert {cut.media_id for cut in cuts} == {ref.media_id for ref in media}
    assert len({cut.media_id for cut in cuts}) == len(cuts)
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(60)
    assert [cut.output_duration_s for cut in cuts if cut.media_id.startswith("photo-")] == [
        0.2
    ] * 23
    kinds = {ref.media_id: ref.kind for ref in media}
    photo_runs = [
        len(group)
        for group in "".join("i" if kinds[cut.media_id] == "image" else "v" for cut in cuts).split(
            "v"
        )
        if group
    ]
    assert photo_runs
    assert min(photo_runs) >= 3
    assert max(photo_runs) <= 5


def test_mixed_media_fallback_groups_each_sport_chapter_and_photo_run() -> None:
    def ref(media_id: str, kind: str, subject: str) -> MediaRef:
        return MediaRef(
            lane="asset" if kind == "image" else "clip",
            media_id=media_id,
            gcs_path=f"users/test/{media_id}.{'jpg' if kind == 'image' else 'mp4'}",
            generation="1",
            kind=kind,
            duration_s=None if kind == "image" else 4,
            analysis={"subject": subject, "description": subject},
        )

    media = []
    for sport, label in (
        ("football", "soccer match on a grass field"),
        ("basketball", "basketball game on a court"),
        ("beach-volleyball", "beach volleyball on a sand court"),
    ):
        media.extend(
            [
                ref(f"{sport}-video", "video", label),
                ref(f"{sport}-photo-1", "image", label),
                ref(f"{sport}-photo-2", "image", label),
                ref(f"{sport}-photo-3", "image", label),
            ]
        )
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.2,
        video_hold="longer",
        boundary_style="cut",
        image_grouping="runs",
        sequence_grouping="sport_context",
        sequence_group_order=["football", "basketball", "beach_volleyball"],
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 10, profile)
    by_id = {row.media_id: row for row in media}
    groups = [
        media_context_group(
            by_id[cut.media_id].user_context,
            by_id[cut.media_id].analysis.get("subject"),
            by_id[cut.media_id].analysis.get("description"),
        )
        for cut in cuts
    ]
    collapsed_groups = [
        group for index, group in enumerate(groups) if index == 0 or group != groups[index - 1]
    ]

    assert collapsed_groups == ["football", "basketball", "beach_volleyball"]
    for sport in collapsed_groups:
        chapter_kinds = "".join(
            "i" if by_id[cut.media_id].kind == "image" else "v"
            for cut, group in zip(cuts, groups, strict=True)
            if group == sport
        )
        assert "iii" in chapter_kinds


def test_mixed_media_fallback_folds_beach_context_into_requested_volleyball_chapter() -> None:
    def ref(media_id: str, kind: str, subject: str) -> MediaRef:
        return MediaRef(
            lane="asset" if kind == "image" else "clip",
            media_id=media_id,
            gcs_path=f"users/test/{media_id}.{'jpg' if kind == 'image' else 'mp4'}",
            generation="1",
            kind=kind,
            duration_s=None if kind == "image" else 4,
            analysis={"subject": subject, "description": subject},
        )

    media = [
        ref("volleyball-video", "video", "beach volleyball match"),
        ref("volleyball-photo-1", "image", "beach volleyball players"),
        ref("volleyball-photo-2", "image", "beach volleyball net"),
        ref("beach-scoreboard", "image", "scoreboard on a sandy beach"),
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.2,
        video_hold="longer",
        boundary_style="cut",
        image_grouping="runs",
        sequence_grouping="sport_context",
        sequence_group_order=["beach_volleyball"],
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 3, profile)
    by_id = {row.media_id: row for row in media}

    assert {cut.media_id for cut in cuts} == {row.media_id for row in media}
    kinds = "".join("i" if by_id[cut.media_id].kind == "image" else "v" for cut in cuts)
    assert "iii" in kinds


def test_mixed_media_fallback_pairs_singleton_context_photos_into_runs() -> None:
    def ref(media_id: str, kind: str, subject: str) -> MediaRef:
        return MediaRef(
            lane="asset" if kind == "image" else "clip",
            media_id=media_id,
            gcs_path=f"users/test/{media_id}.{'jpg' if kind == 'image' else 'mp4'}",
            generation="1",
            kind=kind,
            duration_s=None if kind == "image" else 3,
            analysis={"subject": subject, "description": subject},
        )

    media = [
        ref("football-video", "video", "soccer match"),
        ref("football-photo-1", "image", "soccer team"),
        ref("football-photo-2", "image", "soccer field"),
        ref("football-photo-3", "image", "soccer player"),
        ref("basketball-video", "video", "basketball game"),
        ref("basketball-photo", "image", "basketball bench"),
        ref("volleyball-video", "video", "beach volleyball match"),
        ref("volleyball-photo-1", "image", "beach volleyball players"),
        ref("volleyball-photo-2", "image", "beach volleyball net"),
        ref("volleyball-photo-3", "image", "sand volleyball court"),
        ref("track-video", "video", "running track"),
        ref("track-photo", "image", "track and field lines"),
    ]
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.2,
        video_hold="longer",
        boundary_style="cut",
        image_grouping="runs",
        sequence_grouping="sport_context",
        sequence_group_order=["football", "basketball", "beach_volleyball"],
    )

    cuts = edit_direction_planner.deterministic_fast_cuts(media, 8, profile)
    by_id = {row.media_id: row for row in media}
    pattern = "".join("i" if by_id[cut.media_id].kind == "image" else "v" for cut in cuts)
    photo_runs = [len(run) for run in pattern.split("v") if run]

    assert min(photo_runs) >= 2


def test_mixed_media_agent_rejects_sparse_repeated_sources_when_more_fit() -> None:
    profile = MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.2,
        video_hold="longer",
        boundary_style="cut",
    )
    media = [
        EditProposalMedia(
            media_id=f"video-{index}",
            lane="clip",
            kind="video",
            duration_s=30,
        )
        for index in range(35)
    ] + [
        EditProposalMedia(media_id=f"photo-{index}", lane="asset", kind="image")
        for index in range(23)
    ]
    video_offsets = {f"video-{index}": 0.0 for index in range(4)}
    cut_sources = [
        "photo-0",
        "video-0",
        "photo-1",
        "photo-2",
        "video-0",
        "video-1",
        "video-0",
        "video-2",
        "video-0",
        "video-3",
        "video-0",
        "video-1",
        "video-0",
    ]
    cuts = []
    for index, media_id in enumerate(cut_sources):
        duration_s = 0.2 if media_id.startswith("photo-") else 2.4 if index == 12 else 3.0
        start_s = 0.0
        if media_id.startswith("video-"):
            start_s = video_offsets[media_id]
            video_offsets[media_id] += duration_s
        cuts.append(
            {
                "cut_id": f"cut-{index}",
                "media_id": media_id,
                "source_start_s": start_s,
                "source_end_s": start_s + duration_s,
                "output_duration_s": duration_s,
                "role": "hook" if index == 0 else "payoff" if index == 12 else "build",
            }
        )

    with pytest.raises(SchemaError, match="need at least 39"):
        EditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "title": "Sparse repeated edit",
                    "duration_s": 30,
                    "story_beats": [],
                    "fast_cuts": cuts,
                }
            ),
            EditProposalAgentInput(
                direction="fast_montage",
                pace="fast",
                target_duration_s=30,
                mixed_media_timing=profile,
                media=media,
            ),
        )
