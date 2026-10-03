"""KRI-282: "add a text placeholder so I can replace with their real names".

A placeholder is a per-clip label whose text is the system's fixed
``PLACEHOLDER_LABEL_TEXT``. It is not subject to the creator_text source fence
(it is generic, system-owned and explicitly requested), but WHICH clips get it
is still resolved from footage. Every network call is faked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionOutput
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.pipeline.unified_montage import _intent_labels
from app.schemas.clip_intents import (
    PLACEHOLDER_LABEL_TEXT,
    ClipIntent,
    ResolvedClipIntent,
    ground_placeholder_label,
)
from app.services import clip_intent_planning as planning
from app.services.clip_intent_resolution import (
    IntentClip,
    _build_resolver_input,
    resolve_clip_intents_for_turn,
)

pytestmark = pytest.mark.asyncio

REQUEST = (
    "Group content by sport. For individual shots of people, add a text placeholder "
    "so I can replace with their real names"
)


def _clip(media_id: str, subject: str = "") -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis={"subject": subject},
        gcs_path=f"users/u/clips/{media_id}.mp4",
    )


def _intent() -> ClipIntent:
    return ClipIntent(
        intent_id="p1", op="label", attribute="individual shots of people", placeholder=True
    )


def _patch_resolver(monkeypatch, output: ClipRequestResolverOutput) -> list:
    seen: list = []

    def _fake_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        seen.append(input)
        return output

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _fake_run)
    return seen


def _patch_vision(monkeypatch, answer: str, confidence: float) -> list[int]:
    calls = [0]

    def _download(object_path, local_path):  # noqa: ANN001
        with open(local_path, "wb") as fh:
            fh.write(b"fake")

    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
        lambda path, timeout=120: SimpleNamespace(uri="files/x", mime_type="video/mp4"),
    )

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        return ClipQuestionOutput(answer=answer, confidence=confidence, evidence="a person")

    monkeypatch.setattr(ClipQuestionAgent, "run", _run)
    return calls


# ── schema ──────────────────────────────────────────────────────────────────


def test_placeholder_defaults_off_and_old_persisted_intents_still_load() -> None:
    old = {"intent_id": "l1", "op": "label", "attribute": "sport"}
    intent = ResolvedClipIntent.model_validate(
        {**old, "status": "resolved", "assignments": [{"media_id": "a", "value": "Soccer"}]}
    )
    assert intent.placeholder is False
    # Not emitted when false: stored strategies and their hashes stay byte-identical.
    assert "placeholder" not in intent.model_dump(mode="json")
    assert "placeholder" not in ClipIntent.model_validate(old).model_dump(mode="json")


def test_placeholder_round_trips_and_old_grounding_values_still_load() -> None:
    intent = ResolvedClipIntent.model_validate(
        {
            **_intent().model_dump(mode="json"),
            "status": "resolved",
            "assignments": [
                {"media_id": "a", "value": "Name", "grounding": "placeholder", "confidence": 1.0},
                {"media_id": "b", "value": "x", "grounding": "record_span"},
            ],
        }
    )
    again = ResolvedClipIntent.model_validate(intent.model_dump(mode="json"))
    assert again.placeholder is True
    assert [a.grounding for a in again.assignments] == ["placeholder", "record_span"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"op": "group"},
        {"op": "caption", "caption_attribute": "x"},
        {"op": "label", "creator_text": "Name"},
        {"op": "label", "label_source": "transcript", "transcript_kind": "topic"},
    ],
)
def test_placeholder_is_only_a_plain_clip_label(kwargs) -> None:
    with pytest.raises(ValueError):
        ClipIntent(intent_id="p", attribute="people", placeholder=True, **kwargs)


def test_placeholder_label_is_the_fixed_system_constant() -> None:
    label = ground_placeholder_label(media_id="m1", intent_id="p1")
    assert (label.text, label.grounding, label.confidence) == (
        PLACEHOLDER_LABEL_TEXT,
        "placeholder",
        1.0,
    )


def test_resolver_sees_a_placeholder_as_membership_only() -> None:
    resolver_input, _, _ = _build_resolver_input([_intent()], REQUEST, [_clip("a")])
    assert resolver_input.intents[0].op == "include"
    assert resolver_input.intents[0].creator_text is None


# ── resolution ──────────────────────────────────────────────────────────────


async def test_confident_membership_prints_the_fixed_text_on_exactly_the_person_clips(
    monkeypatch,
) -> None:
    clips = [_clip("a", "a portrait of a player"), _clip("b", "a wide pitch"), _clip("c", "p")]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="p1",
                    assignments=[
                        ResolverAssignment(media="m001", confidence=0.95),
                        ResolverAssignment(media="m003", confidence=0.9),
                    ],
                )
            ]
        ),
    )
    result = await resolve_clip_intents_for_turn(
        intents=[_intent()], creator_request=REQUEST, clips=clips, run_context=RunContext()
    )
    assert result.question is None
    (resolved,) = result.intents
    assert resolved.status == "resolved" and resolved.placeholder is True
    assert [(a.media_id, a.value, a.grounding) for a in resolved.assignments] == [
        ("a", "Name", "placeholder"),
        ("c", "Name", "placeholder"),
    ]


async def test_uncertain_membership_is_confirmed_by_a_vision_yes_no(monkeypatch) -> None:
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="p1",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="a person?")],
                )
            ]
        ),
    )
    calls = _patch_vision(monkeypatch, "yes", 0.9)
    result = await resolve_clip_intents_for_turn(
        intents=[_intent()],
        creator_request=REQUEST,
        clips=[_clip("a")],
        run_context=RunContext(),
    )
    assert calls[0] == 1
    assert result.question is None
    assert result.intents[0].assignments[0].value == "Name"
    assert result.intents[0].assignments[0].grounding == "placeholder"


async def test_no_matching_clips_asks_a_focused_question_instead_of_dropping(
    monkeypatch,
) -> None:
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="p1", assignments=[])]),
    )
    result = await resolve_clip_intents_for_turn(
        intents=[_intent()],
        creator_request=REQUEST,
        clips=[_clip("a", "a pitch")],
        run_context=RunContext(),
    )
    assert result.status == "needs_creator"
    assert result.question is not None
    assert "individual shots of people" in result.question
    assert "name placeholder" in result.question
    assert "restate" not in result.question


# ── planner -> resolver regression shaped like the real prompt ───────────────


async def test_real_prompt_shaped_turn_has_no_question_and_a_placeholder_caption(
    monkeypatch,
) -> None:
    """8 planned intents (incl. the placeholder), 0 questions, 'Name' on the person clips."""
    planned = [
        PlannedClipIntent(
            intent_id=f"g{i}", op="group", attribute=f"sport {i}", source_quote="Group content"
        )
        for i in range(7)
    ] + [
        PlannedClipIntent(
            intent_id="p1",
            op="label",
            attribute="individual shots of people",
            placeholder=True,
            source_quote="add a text placeholder",
        )
    ]
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=planned)
    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(planning, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    clips = [_clip("a", "player portrait"), _clip("b", "pitch")]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                *(
                    ResolverIntentOut(
                        intent_id=f"g{i}",
                        assignments=[ResolverAssignment(media="m002", confidence=0.9)],
                    )
                    for i in range(7)
                ),
                ResolverIntentOut(
                    intent_id="p1",
                    assignments=[ResolverAssignment(media="m001", confidence=0.92)],
                ),
            ]
        ),
    )
    result = await planning.plan_and_resolve_clip_intents(
        creator_request=REQUEST,
        latest_user_message=None,
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
    )
    assert len(result.requested_intents) == 8
    assert result.resolution.question is None
    assert result.resolution.status == "resolved"
    (placeholder,) = [i for i in result.resolution.intents if i.placeholder]
    assert [(a.media_id, a.value) for a in placeholder.assignments] == [("a", "Name")]


# ── rendering ───────────────────────────────────────────────────────────────


def _resolved(intent_id: str, value: str, grounding: str, *media: str) -> dict:
    return {
        "intent_id": intent_id,
        "op": "label",
        "attribute": "x",
        "status": "resolved",
        "assignments": [
            {"media_id": m, "value": value, "grounding": grounding, "confidence": 1.0}
            for m in media
        ],
    }


def test_montage_prints_name_on_person_clips_and_it_beats_a_broader_label() -> None:
    strategy = {
        "resolved_clip_intents": [
            _resolved("sport", "Football", "record_span", "a", "b"),
            _resolved("p1", "Name", "placeholder", "a"),
        ]
    }
    rows = _intent_labels(strategy, True)
    # The person shot "a" shows the placeholder (creator's own request, not inferred);
    # the sport label still lands on the clip without a person.
    assert rows == {"a": ("Name", True), "b": ("Football", False)}
    assert _intent_labels(strategy, False) == {}
