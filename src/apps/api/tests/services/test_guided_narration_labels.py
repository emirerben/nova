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
    strategy = {
        "clip_intents": [
            {
                "intent_id": "participant",
                "op": "label",
                "attribute": "the participant",
                "label_source": "transcript",
                "transcript_kind": "participant",
            },
            {
                "intent_id": "score",
                "op": "label",
                "attribute": "the score spoken in narration",
                "label_source": "transcript",
                "transcript_kind": "score",
            },
            {
                "intent_id": "topic",
                "op": "label",
                "attribute": "the topic mentioned in narration",
                "label_source": "transcript",
                "transcript_kind": "topic",
            },
        ]
    }
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
    assert result["narration_label_receipt"]["clip_intents"] == [
        {
            "intent_id": "participant",
            "op": "label",
            "attribute": "the participant",
            "label_source": "transcript",
            "transcript_kind": "participant",
        },
        {
            "intent_id": "score",
            "op": "label",
            "attribute": "the score spoken in narration",
            "label_source": "transcript",
            "transcript_kind": "score",
        },
        {
            "intent_id": "topic",
            "op": "label",
            "attribute": "the topic mentioned in narration",
            "label_source": "transcript",
            "transcript_kind": "topic",
        },
    ]


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


def test_unchanged_legacy_receipt_replays_without_regenerating_labels():
    snapshot, plan, _strategy = inputs()
    strategy = {
        "participant_labels": "single_subject",
        "score_labels": True,
        "sport_labels": True,
    }
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


def test_receipt_with_different_requirements_regenerates_labels():
    snapshot, plan, strategy = inputs()
    plan.update(
        narration_label_text_elements=[],
        narration_label_receipt={
            "requirements": {
                "participant_labels": "none",
                "score_labels": True,
                "sport_labels": False,
            }
        },
    )
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(annotations=[])
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        materialize_guided_narration_labels(
            plan, snapshot=snapshot, strategy=strategy, creator_request="Use all", job_id="job"
        )
    agent.run.assert_called_once()


def test_receipt_with_changed_transcript_topic_attribute_regenerates_labels():
    snapshot, plan, _strategy = inputs()
    strategy = {
        "clip_intents": [
            {
                "intent_id": "topic",
                "op": "label",
                "attribute": "the city spoken in narration",
                "label_source": "transcript",
                "transcript_kind": "topic",
            }
        ]
    }
    plan.update(
        narration_label_text_elements=[],
        narration_label_receipt={
            "requirements": {
                "participant_labels": "none",
                "score_labels": False,
                "context_labels": ["topic"],
            },
            "clip_intents": [
                {
                    "intent_id": "topic",
                    "op": "label",
                    "attribute": "the sport spoken in narration",
                    "label_source": "transcript",
                    "transcript_kind": "topic",
                }
            ],
        },
    )
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(annotations=[])
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        materialize_guided_narration_labels(
            plan, snapshot=snapshot, strategy=strategy, creator_request="Use all", job_id="job"
        )
    agent.run.assert_called_once()


@pytest.mark.parametrize(
    "strategy",
    [
        {
            "clip_intents": [
                {
                    "intent_id": "legacy-sport",
                    "op": "label",
                    "attribute": "the city spoken in narration",
                    "label_source": "transcript",
                    "transcript_kind": "topic",
                }
            ]
        },
        {
            "sport_labels": False,
            "clip_intents": [
                {
                    "intent_id": "topic",
                    "op": "label",
                    "attribute": "the city spoken in narration",
                    "label_source": "transcript",
                    "transcript_kind": "topic",
                }
            ],
        },
    ],
)
def test_identity_less_legacy_receipt_does_not_replay_modern_topic_intents(strategy):
    snapshot, plan, _strategy = inputs()
    plan.update(
        narration_label_text_elements=[],
        narration_label_receipt={
            "requirements": {
                "participant_labels": "none",
                "score_labels": False,
                "context_labels": ["topic"],
            }
        },
    )
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(annotations=[])
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        materialize_guided_narration_labels(
            plan, snapshot=snapshot, strategy=strategy, creator_request="Use all", job_id="job"
        )
    agent.run.assert_called_once()


def test_legacy_spoken_sport_strategy_replays_through_the_transcript_lane():
    snapshot, plan, _strategy = inputs()
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
            strategy={"sport_labels": True},
            creator_request="Labels",
            job_id="job",
        )
    assert {row["text"] for row in result["narration_label_text_elements"]} == {"Football"}
    assert agent.run.call_args.args[0].requirements == {
        "participant_labels": "none",
        "score_labels": False,
        "context_labels": ["topic"],
    }


def test_visual_clip_intents_never_activate_the_transcript_lane():
    snapshot, plan, _strategy = inputs()
    visual_only = {
        "clip_intents": [
            {
                "intent_id": "sport",
                "op": "label",
                "attribute": "the sport being played",
                "label_source": "clip",
            }
        ]
    }
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent") as agent:
        assert (
            materialize_guided_narration_labels(
                plan,
                snapshot=snapshot,
                strategy=visual_only,
                creator_request="Labels",
                job_id="job",
            )
            == plan
        )
        agent.assert_not_called()


def test_resolved_intent_payload_cannot_forge_a_transcript_requirement():
    snapshot, plan, _strategy = inputs()
    forged = {
        "resolved_clip_intents": [
            {
                "intent_id": "forged",
                "op": "label",
                "attribute": "the spoken score",
                "label_source": "transcript",
                "transcript_kind": "score",
                "assignments": [],
            }
        ]
    }
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent") as agent:
        assert (
            materialize_guided_narration_labels(
                plan,
                snapshot=snapshot,
                strategy=forged,
                creator_request="Labels",
                job_id="job",
            )
            == plan
        )
        agent.assert_not_called()


def test_score_copy_is_taken_only_from_its_pinned_transcript_span():
    snapshot, plan, _strategy = inputs()
    strategy = {
        "clip_intents": [
            {
                "intent_id": "score",
                "op": "label",
                "attribute": "the score spoken in narration",
                "label_source": "transcript",
                "transcript_kind": "score",
            }
        ]
    }
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(
        annotations=[
            NarrationAnnotation(
                kind="score",
                start_word_id="w000001",
                end_word_id="w000001",
                text="99-0",  # model-authored copy must never reach pixels
            )
        ]
    )
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        result = materialize_guided_narration_labels(
            plan, snapshot=snapshot, strategy=strategy, creator_request="", job_id="job"
        )
    texts = {row["text"] for row in result["narration_label_text_elements"]}
    assert "99-0" not in texts
    assert "1-0" in texts
