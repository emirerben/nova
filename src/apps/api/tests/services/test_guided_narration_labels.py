from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.agents.narration_annotations import NarrationAnnotation, NarrationAnnotationOutput
from app.pipeline.guided_story import GuidedStoryError
from app.pipeline.narration_labels import TOPIC_SENTENCE_REJECTION
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


# Synthetic stand-ins for a creator's per-shot voiceover script. Never copy
# real creator text into this fixture.
_SPOKEN_LINES = (
    "The old bridge opened before the railway came and it still carries traffic",
    "Market stalls fill the lanes every morning before the heat arrives",
    "We ended the walk at the harbour watching the boats come home",
)


def _spoken_script_inputs():
    """Prod shape (2026-09-23): the Main Creator turned "show each shot while I
    say its line" into one transcript topic intent per line, the attribute
    being the spoken line itself."""

    snapshot, plan, _strategy = inputs()
    words = []
    cursor = 0.3
    for line in _SPOKEN_LINES:
        for token in line.split():
            words.append(
                {"text": token, "start_s": round(cursor, 3), "end_s": round(cursor + 0.2, 3)}
            )
            cursor += 0.25
        cursor += 0.4
    snapshot.narration = NarrationTrack(
        gcs_path="voiceover-uploads/take.mp3",
        generation="7",
        duration_s=cursor + 1,
        words=words,
    )
    plan["story_timeline"][0]["output_end_s"] = cursor + 1
    strategy = {
        "clip_intents": [
            {
                "intent_id": f"line-{index}",
                "op": "label",
                "attribute": line,
                "label_source": "transcript",
                "transcript_kind": "topic",
            }
            for index, line in enumerate(_SPOKEN_LINES)
        ]
    }
    return snapshot, plan, strategy


def test_spoken_script_topic_intents_materialize_no_labels():
    snapshot, plan, strategy = _spoken_script_inputs()
    first_end = len(_SPOKEN_LINES[0].split()) - 1
    second_start = first_end + 1
    second_end = second_start + len(_SPOKEN_LINES[1].split()) - 1
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(
        annotations=[
            # Exactly copied whole sentences: grounded, but script.
            NarrationAnnotation(
                kind="topic",
                start_word_id="w000000",
                end_word_id=f"w{first_end:06d}",
                text=_SPOKEN_LINES[0],
            ),
            NarrationAnnotation(
                kind="topic",
                start_word_id="w000000",
                end_word_id=f"w{first_end:06d}",
                text=_SPOKEN_LINES[0],
            ),
            NarrationAnnotation(
                kind="topic",
                start_word_id=f"w{second_start:06d}",
                end_word_id=f"w{second_end:06d}",
                text=_SPOKEN_LINES[1],
            ),
            # An anchor wider than any label span is still ungrounded.
            NarrationAnnotation(
                kind="topic",
                start_word_id="w000000",
                end_word_id=f"w{second_end:06d}",
                text=f"{_SPOKEN_LINES[0]} {_SPOKEN_LINES[1]}",
            ),
        ]
    )
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        result = materialize_guided_narration_labels(
            plan, snapshot=snapshot, strategy=strategy, creator_request="Use all", job_id="job"
        )

    assert result["narration_label_text_elements"] == []
    receipt = result["narration_label_receipt"]
    assert receipt["accepted"] == []
    assert [row["reason"] for row in receipt["rejected"]] == [
        TOPIC_SENTENCE_REJECTION,
        TOPIC_SENTENCE_REJECTION,
        TOPIC_SENTENCE_REJECTION,
        "topic text is not copied from its transcript anchor",
    ]

    # A later load of the pinned plan replays the empty lane; it never asks
    # the agent again and never resurrects the sentence.
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent") as replay:
        assert (
            materialize_guided_narration_labels(
                result,
                snapshot=snapshot,
                strategy=strategy,
                creator_request="Use all",
                job_id="job",
            )
            is result
        )
        replay.assert_not_called()


def test_short_transcript_topic_alongside_spoken_script_still_renders():
    snapshot, plan, strategy = _spoken_script_inputs()
    bridge = _SPOKEN_LINES[0].split().index("bridge")
    agent = MagicMock()
    agent.run.return_value = NarrationAnnotationOutput(
        annotations=[
            NarrationAnnotation(
                kind="topic",
                start_word_id="w000000",
                end_word_id=f"w{len(_SPOKEN_LINES[0].split()) - 1:06d}",
                text=_SPOKEN_LINES[0],
            ),
            NarrationAnnotation(
                kind="topic",
                start_word_id=f"w{bridge - 2:06d}",
                end_word_id=f"w{bridge:06d}",
                text="old bridge",
            ),
        ]
    )
    with patch("app.services.guided_narration_labels.NarrationAnnotationAgent", return_value=agent):
        result = materialize_guided_narration_labels(
            plan, snapshot=snapshot, strategy=strategy, creator_request="Use all", job_id="job"
        )

    labels = result["narration_label_text_elements"]
    assert [(row["text"], row["effect"]) for row in labels] == [("old bridge", "pop-in")]
    assert [row["reason"] for row in result["narration_label_receipt"]["rejected"]] == [
        TOPIC_SENTENCE_REJECTION
    ]
