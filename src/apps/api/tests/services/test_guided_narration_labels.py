from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.agents.narration_annotations import NarrationAnnotation, NarrationAnnotationOutput
from app.pipeline.guided_story import GuidedStoryError
from app.schemas.edit_proposal import NarrationTrack
from app.services.guided_narration_labels import materialize_guided_narration_labels


@pytest.fixture(autouse=True)
def focus_frames(monkeypatch):
    monkeypatch.setattr(
        "app.services.guided_narration_focus.analyze_guided_participant_focus",
        lambda timeline, **_kwargs: timeline,
    )


def inputs():
    snapshot = SimpleNamespace(
        narration=NarrationTrack(
            gcs_path="voiceover-uploads/take.mp3",
            generation="7",
            duration_s=5,
            words=[
                {"text": "Football", "start_s": 0, "end_s": 0.4},
                {"text": "1-0", "start_s": 1, "end_s": 1.4},
                {"text": "1-1", "start_s": 3, "end_s": 3.4},
            ],
        ),
        media=[SimpleNamespace(media_id="one", kind="video", analysis={"single_subject": True})],
    )
    plan = {
        "story_timeline": [
            {
                "moment_id": "shot",
                "media_id": "one",
                "output_start_s": 0,
                "output_end_s": 5,
            }
        ]
    }
    strategy = {"score_labels": True, "sport_labels": True, "participant_labels": "single_subject"}
    return snapshot, plan, strategy


def test_guided_labels_use_final_timeline_and_pinned_transcript():
    snapshot, plan, strategy = inputs()
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(
        annotations=[
            NarrationAnnotation(
                kind="topic",
                start_word_id="w000000",
                end_word_id="w000000",
                text="Football",
            )
        ]
    )
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        result = materialize_guided_narration_labels(
            plan,
            snapshot=snapshot,
            strategy=strategy,
            creator_request="Use all",
            job_id="job",
        )
    texts = {row["text"] for row in result["narration_label_text_elements"]}
    assert texts == {"1-0", "1-1", "Football", "PLAYER 1"}
    passed = agent.run.call_args.args[0]
    assert passed.creator_request == "Use all"
    assert passed.timeline[0]["timeline_id"] == "shot"
    participant = next(
        row for row in result["narration_label_text_elements"] if row["text"] == "PLAYER 1"
    )
    assert participant["start_s"] == 0 and participant["end_s"] == 5
    assert participant["source_params"]["source_asset_id"] == "one"


def test_required_labels_fail_visibly_when_semantic_agent_fails():
    snapshot, plan, strategy = inputs()
    with patch(
        "app.services.guided_narration_labels.NarrationAnnotationAgent",
        side_effect=RuntimeError("provider"),
    ):
        with pytest.raises(GuidedStoryError, match="could not be grounded"):
            materialize_guided_narration_labels(
                plan,
                snapshot=snapshot,
                strategy=strategy,
                creator_request="Labels",
                job_id="job",
            )


@pytest.mark.parametrize("legacy", [True, False])
def test_unrequested_labels_and_legacy_jobs_do_not_call_agents(legacy):
    snapshot, plan, _strategy = inputs()
    if legacy:
        snapshot.narration = None
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent") as agent:
        assert (
            materialize_guided_narration_labels(
                plan,
                snapshot=snapshot,
                strategy={},
                creator_request="",
                job_id="job",
            )
            == plan
        )
        agent.assert_not_called()


def test_persisted_labels_are_not_regenerated_on_the_repair_pass():
    snapshot, plan, strategy = inputs()
    plan.update(
        narration_label_text_elements=[], narration_label_receipt={"requirements": strategy}
    )
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent") as agent:
        assert (
            materialize_guided_narration_labels(
                plan, snapshot=snapshot, strategy=strategy, creator_request="Use all", job_id="job"
            )
            is plan
        )
        agent.assert_not_called()
