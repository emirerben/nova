"""Unit tests for app.services.clip_intent_resolution.resolve_clip_intents_for_turn
(KRI-127 Lane C). Every network call (resolver agent, vision download/upload/
question agent) is faked — these tests exercise the grounding + vision-cap +
deadline + graceful-degradation logic only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents._runtime import RunContext, TerminalError
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionOutput
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.config import settings
from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_resolution import (
    ANSWERS_KEY,
    IntentClip,
    grounded_labels,
    normalize_question,
    resolve_clip_intents_for_turn,
)

pytestmark = pytest.mark.asyncio


def _record_analysis(subject: str = "", description: str = "") -> dict:
    return {"subject": subject, "description": description}


def _video_clip(media_id: str, *, subject: str = "", gcs_path: str | None = None) -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis=_record_analysis(subject=subject),
        gcs_path=gcs_path or f"users/u/clips/{media_id}.mp4",
    )


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, output: ClipRequestResolverOutput) -> None:
    def _fake_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        return output

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _fake_run)


def _patch_vision_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert the vision path is never touched (download/upload/agent)."""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("vision re-query must not run for this scenario")

    monkeypatch.setattr("app.storage.download_to_file", _boom)
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait", _boom)
    monkeypatch.setattr(ClipQuestionAgent, "run", _boom)


def _patch_vision_success(
    monkeypatch: pytest.MonkeyPatch, answer: str, confidence: float, *, evidence: str = ""
) -> list[int]:
    """Wire a working (fake) vision path; returns a mutable [call_count]."""
    calls = [0]

    def _fake_download(object_path, local_path):  # noqa: ANN001
        with open(local_path, "wb") as fh:
            fh.write(b"fake")

    def _fake_upload(path, timeout=120):  # noqa: ANN001
        return SimpleNamespace(uri="files/fake-ref", mime_type="video/mp4")

    def _fake_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        return ClipQuestionOutput(answer=answer, confidence=confidence, evidence=evidence)

    monkeypatch.setattr("app.storage.download_to_file", _fake_download)
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait", _fake_upload)
    monkeypatch.setattr(ClipQuestionAgent, "run", _fake_run)
    return calls


async def test_record_span_label_grounded_without_vision_call(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clip = _video_clip("m1", subject="people playing soccer")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    assignments=[
                        ResolverAssignment(media="m001", value="Soccer", confidence=0.9),
                    ],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=[clip], run_context=RunContext()
    )

    assert result.question is None
    assert result.intents[0].status == "resolved"
    assert result.intents[0].assignments[0].value == "Soccer"
    assert result.intents[0].assignments[0].grounding == "record_span"


async def test_made_up_value_not_grounded_triggers_vision_and_confirms(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    # Record has NOTHING about soccer — a 0.99-confidence guess must not ground.
    clip = _video_clip("m1", subject="people sitting on grass")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    assignments=[
                        ResolverAssignment(media="m001", value="Soccer", confidence=0.99),
                    ],
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "Soccer", 0.9, evidence="players kicking a ball")

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=[clip], run_context=RunContext()
    )

    assert calls[0] == 1  # vision WAS called to confirm the ungrounded guess
    assert result.question is None
    assert result.intents[0].status == "resolved"
    assert result.intents[0].assignments[0].value == "Soccer"
    assert result.intents[0].assignments[0].grounding == "vision_verified"


async def test_cached_answer_skips_download_upload_and_agent_call(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    question = "What sport is being played?"
    clip = IntentClip(
        media_id="m1",
        kind="video",
        gcs_path="users/u/clips/m1.mp4",
        analysis={
            **_record_analysis(subject="people playing a ball game"),
            ANSWERS_KEY: {
                normalize_question(question): {
                    "answer": "Volleyball",
                    "confidence": 0.95,
                    "evidence": "net visible",
                }
            },
        },
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[ResolverVisionQuestion(media="m001", question=question)],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)  # cache hit must never touch the network

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=[clip], run_context=RunContext()
    )

    assert result.question is None
    assert result.intents[0].assignments[0].value == "Volleyball"
    assert result.intents[0].assignments[0].grounding == "vision_verified"


async def test_over_cap_candidates_ask_and_call_exactly_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 2)
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clips = [_video_clip(f"m{i}", subject="people sitting on grass") for i in range(1, 4)]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 4)
                    ],
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "Soccer", 0.9)

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=clips, run_context=RunContext()
    )

    assert calls[0] == 2  # exactly the cap, never the full candidate count
    assert result.question is not None


async def test_deadline_exceeded_asks_without_raising(monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_vision_deadline_s", 0.05)
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clip = _video_clip("m1", subject="people sitting on grass")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="What sport?")],
                )
            ]
        ),
    )

    def _fake_download(object_path, local_path):  # noqa: ANN001
        with open(local_path, "wb") as fh:
            fh.write(b"fake")

    def _slow_upload(path, timeout=120):  # noqa: ANN001
        import time

        time.sleep(0.3)
        return SimpleNamespace(uri="files/fake-ref", mime_type="video/mp4")

    monkeypatch.setattr("app.storage.download_to_file", _fake_download)
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait", _slow_upload)

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=[clip], run_context=RunContext()
    )

    assert result.question is not None
    assert result.intents[0].status == "needs_creator"


async def test_vision_unknown_answer_asks(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clip = _video_clip("m1", subject="people sitting on grass")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="What sport?")],
                )
            ]
        ),
    )
    _patch_vision_success(monkeypatch, "unknown", 0.0)

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=[clip], run_context=RunContext()
    )

    assert result.question is not None
    assert result.intents[0].status == "needs_creator"
    assert result.intents[0].assignments == []


async def test_membership_intent_with_zero_members_asks(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="i_pub", op="group", attribute="pub videos", creator_text="post match pub"
    )
    clip = _video_clip("m1", subject="people sitting on grass")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="i_pub")]),
    )
    _patch_vision_forbidden(monkeypatch)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="group the pub videos and say post match pub",
        clips=[clip],
        run_context=RunContext(),
    )

    assert result.question is not None
    assert result.intents[0].status == "needs_creator"
    assert result.intents[0].assignments == []


async def test_creator_text_label_grounds_as_creator_text(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="i_pub", op="label", attribute="pub videos", creator_text="Post Match Pub"
    )
    clip = _video_clip("m1", subject="friends at a pub")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_pub",
                    assignments=[ResolverAssignment(media="m001", value=None, confidence=0.85)],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="group the pub clips and say Post Match Pub",
        clips=[clip],
        run_context=RunContext(),
    )

    assert result.question is None
    assert result.intents[0].status == "resolved"
    assert result.intents[0].assignments[0].value == "Post Match Pub"
    assert result.intents[0].assignments[0].grounding == "creator_text"


async def test_resolver_terminal_error_degrades_gracefully(monkeypatch) -> None:
    def _boom(self, input, *, ctx=None):  # noqa: A002, ANN001
        raise TerminalError("nova.plan.clip_request_resolver: exhausted 1 model(s)")

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _boom)
    _patch_vision_forbidden(monkeypatch)

    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clip = _video_clip("m1", subject="people playing soccer")

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=[clip], run_context=RunContext()
    )

    assert result.question is not None
    assert result.intents[0].status == "needs_creator"


async def test_fully_resolved_turn_has_no_question_and_grounded_labels_work(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clips = [
        _video_clip("m1", subject="people playing soccer"),
        _video_clip("m2", subject="people playing volleyball"),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    assignments=[
                        ResolverAssignment(media="m001", value="Soccer", confidence=0.9),
                        ResolverAssignment(media="m002", value="Volleyball", confidence=0.9),
                    ],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=clips, run_context=RunContext()
    )

    assert result.question is None
    labels = grounded_labels(result.intents)
    assert {(label.media_id, label.text) for label in labels} == {
        ("m1", "Soccer"),
        ("m2", "Volleyball"),
    }


async def test_partial_label_resolution_still_asks_but_keeps_grounded(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    clips = [
        _video_clip("m1", subject="people playing soccer"),  # will ground directly
        _video_clip("m2", subject="people sitting on grass"),  # never grounds
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    assignments=[
                        ResolverAssignment(media="m001", value="Soccer", confidence=0.9),
                    ],
                    needs_vision=[
                        ResolverVisionQuestion(media="m002", question="What sport?"),
                    ],
                )
            ]
        ),
    )
    _patch_vision_success(monkeypatch, "unknown", 0.0)

    result = await resolve_clip_intents_for_turn(
        intents=[intent], creator_request="label each sport", clips=clips, run_context=RunContext()
    )

    assert result.question is not None
    assert result.intents[0].status == "needs_creator"
    # The grounded clip's assignment is retained even though the intent overall
    # still needs the creator's help.
    values = {a.media_id: a.value for a in result.intents[0].assignments}
    assert values == {"m1": "Soccer"}
    # grounded_labels() never surfaces partials — only fully "resolved" intents.
    assert grounded_labels(result.intents) == []


# ── KRI-129: op="caption" — one on-screen phrase for the whole chapter ──────


async def test_creator_text_caption_grounds_after_membership_confirmed(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="i_food", op="caption", attribute="food clips", creator_text="post match feast"
    )
    clips = [
        _video_clip("m1", subject="friends eating dinner"),
        _video_clip("m2", subject="dessert table"),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_food",
                    assignments=[
                        ResolverAssignment(media="m001", confidence=0.9),
                        ResolverAssignment(media="m002", confidence=0.85),
                    ],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request='Say "post match feast" on the food clips.',
        clips=clips,
        run_context=RunContext(),
    )

    assert result.question is None
    resolved = result.intents[0]
    assert resolved.status == "resolved"
    assert resolved.caption_text == "post match feast"
    assert resolved.caption_grounding == "creator_text"
    assert {a.media_id for a in resolved.assignments} == {"m1", "m2"}
    assert all(a.value is None for a in resolved.assignments)  # membership only, like group


async def test_creator_text_caption_low_membership_confidence_escalates_to_vision(
    monkeypatch,
) -> None:
    """Mirrors the label+creator_text path: below the LABEL bar, membership is
    confirmed with a yes/no vision check before the (verbatim) text is applied."""
    intent = ClipIntent(
        intent_id="i_food", op="caption", attribute="food clips", creator_text="post match feast"
    )
    clip = _video_clip("m1", subject="a table")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_food",
                    assignments=[ResolverAssignment(media="m001", confidence=0.5)],
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "yes", 0.9)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request='Say "post match feast" on the food clips.',
        clips=[clip],
        run_context=RunContext(),
    )

    assert calls[0] == 1
    resolved = result.intents[0]
    assert result.question is None
    assert resolved.status == "resolved"
    assert resolved.caption_text == "post match feast"
    assert resolved.caption_grounding == "creator_text"


async def test_described_caption_grounds_via_record_span_over_union_of_members(
    monkeypatch,
) -> None:
    intent = ClipIntent(intent_id="i_park", op="caption", attribute="park clips")
    clip_a = _video_clip("m1", subject="a rainy park bench")
    clip_b = _video_clip("m2", subject="a windy afternoon walk")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_park",
                    assignments=[
                        ResolverAssignment(media="m001", confidence=0.9),
                        ResolverAssignment(media="m002", confidence=0.9),
                    ],
                    caption="rainy windy",
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)  # union grounds it directly, no re-query needed

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="Add a caption about the weather on the park clips",
        clips=[clip_a, clip_b],
        run_context=RunContext(),
    )

    assert result.question is None
    resolved = result.intents[0]
    assert resolved.status == "resolved"
    assert resolved.caption_text == "rainy windy"
    assert resolved.caption_grounding == "record_span"


async def test_described_caption_escalates_to_one_vision_requery_when_ungrounded(
    monkeypatch,
) -> None:
    intent = ClipIntent(
        intent_id="i_park", op="caption", attribute="park clips", caption_attribute="the weather"
    )
    clip = _video_clip("m1", subject="people at a park")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_park",
                    assignments=[ResolverAssignment(media="m001", confidence=0.9)],
                    caption="cold and rainy",  # the record alone does not support this
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "cold and rainy", 0.9)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="Add a caption about the weather on the park clips",
        clips=[clip],
        run_context=RunContext(),
    )

    assert calls[0] == 1  # exactly ONE extra vision call to author the caption
    resolved = result.intents[0]
    assert result.question is None
    assert resolved.status == "resolved"
    assert resolved.caption_text == "cold and rainy"
    assert resolved.caption_grounding == "vision_verified"


async def test_described_caption_still_ungrounded_after_vision_asks_creator(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="i_park", op="caption", attribute="park clips", caption_attribute="the weather"
    )
    clip = _video_clip("m1", subject="people at a park")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_park",
                    assignments=[ResolverAssignment(media="m001", confidence=0.9)],
                    caption="cold and rainy",
                )
            ]
        ),
    )
    _patch_vision_success(monkeypatch, "unknown", 0.0)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="Add a caption about the weather on the park clips",
        clips=[clip],
        run_context=RunContext(),
    )

    resolved = result.intents[0]
    assert resolved.status == "needs_creator"
    assert resolved.caption_text is None
    assert result.question == "What should the caption on the park clips say?. Could you clarify?"


async def test_caption_intent_with_no_members_asks_creator(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="i_food", op="caption", attribute="food clips", creator_text="post match feast"
    )
    clip = _video_clip("m1", subject="people sitting on grass")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="i_food")]),
    )
    _patch_vision_forbidden(monkeypatch)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request='Say "post match feast" on the food clips.',
        clips=[clip],
        run_context=RunContext(),
    )

    resolved = result.intents[0]
    assert resolved.status == "needs_creator"
    assert resolved.caption_text is None
    assert result.question is not None


async def test_caption_text_authoring_respects_the_shared_per_turn_vision_cap(monkeypatch) -> None:
    """The membership round and the caption-authoring round share ONE budget —
    a membership vision call that exhausts the cap must leave nothing for the
    caption-text escalation (it asks the creator instead of over-spending)."""
    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 1)
    intent = ClipIntent(
        intent_id="i_park", op="caption", attribute="park clips", caption_attribute="the weather"
    )
    clip = _video_clip("m1", subject="people at a park")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_park",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="Who is here?")],
                    caption="cold and rainy",
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "yes", 0.9)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="Add a caption about the weather on the park clips",
        clips=[clip],
        run_context=RunContext(),
    )

    assert calls[0] == 1  # only the membership call — the cap was already spent
    resolved = result.intents[0]
    assert resolved.status == "needs_creator"
    assert resolved.caption_text is None
    assert result.question == "What should the caption on the park clips say?. Could you clarify?"


def test_membership_vision_checks_are_closed_yes_no_questions() -> None:
    """A free-form question answered "yes" must never count as belonging to a NOT-group."""
    from app.services.clip_intent_resolution import _VisionCandidate, _yes_no

    member = _VisionCandidate(
        media_id="m1",
        intent_id="i1",
        op="order",
        question="Is anyone playing a sport in this clip?",
        fallback_value=None,
        creator_text=None,
        attribute="park clips with people not playing sports",
    )
    assert member.is_membership_check
    assert "park clips with people not playing sports" in member.question
    assert '"yes" or "no"' in member.question

    label = _VisionCandidate(
        media_id="m1",
        intent_id="i2",
        op="label",
        question="What sport is being played?",
        fallback_value=None,
        creator_text=None,
        attribute="sport being played",
    )
    assert not label.is_membership_check
    assert label.question == "What sport is being played?"

    assert _yes_no("Yes.") is True
    assert _yes_no("no") is False
    assert _yes_no("volleyball") is None
    assert _yes_no("") is None
