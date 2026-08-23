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
    assert sum(cut.output_duration_s for cut in snapshot.fast_cuts or []) == 3


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
    assert sum(cut.output_duration_s for cut in cuts) == 4
    assert [cut.media_id for cut in cuts] == ["strong", "support", "strong", "support"]
    assert cuts[0].role == "hook"
    assert cuts[-1].role == "payoff"
    assert all(cut.transition == "none" for cut in cuts)
    assert cuts[0].source_start_s == 2.5


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
    assert sum(cut.output_duration_s for cut in cuts) == duration_s
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
