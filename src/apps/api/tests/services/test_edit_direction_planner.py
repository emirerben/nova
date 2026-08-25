import pytest

from app.agents._runtime import TerminalError
from app.agents.edit_proposal import EditProposalAgentOutput
from app.schemas.edit_proposal import EditProposalSnapshot, FastMontageCut, MediaRef, StoryBeat
from app.services import edit_direction_planner


class FailingAgent:
    def __init__(self, _client) -> None:  # noqa: ANN001
        pass

    def run(self, _input, ctx=None):  # noqa: ANN001, ARG002
        raise TerminalError("provider returned invalid fast-cut arithmetic")


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
    assert cuts[0].source_start_s == 2.5


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


@pytest.mark.parametrize("duration_s", [14, 60])
def test_fast_montage_fallback_allocates_short_source_once_without_shortening_long_cuts(
    monkeypatch,
    duration_s: int,
) -> None:
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
    assert len(short_cuts) == 1
    assert short_cuts[0].output_duration_s == 0.4
    assert all(0.8 <= cut.output_duration_s <= 1.2 for cut in long_cuts)
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(duration_s)
    by_id = {ref.media_id: ref for ref in source.media}
    assert all(cut.source_end_s <= float(by_id[cut.media_id].duration_s or 0) for cut in cuts)


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
    assert len([cut for cut in cuts if cut.media_id == "short"]) == 1
    assert len([cut for cut in cuts if cut.media_id == "long-0"]) == 1
    assert all(cut.source_start_s >= 0 for cut in cuts)
    assert all(
        cut.source_end_s <= float(by_id[cut.media_id].duration_s or 0) + 0.001 for cut in cuts
    )
