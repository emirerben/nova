"""Unit tests for app.services.clip_intent_resolution.resolve_clip_intents_for_turn
(KRI-127 Lane C). Every network call (resolver agent, vision download/upload/
question agent) is faked — these tests exercise the grounding + vision-cap +
deadline + graceful-degradation logic only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RunContext,
    TerminalError,
)
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
    IntentResolution,
    _build_resolver_input,
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


def test_build_resolver_input_replaces_only_known_media_ids_with_aliases() -> None:
    clips = [
        _video_clip("asset-pub-01", subject="friends in a pub"),
        _video_clip("asset-pub-13", subject="friends at a cafe"),
    ]
    request = (
        "Use asset-pub-01 and asset-pub-13 for the pub chapter; "
        "do not infer unknown-media-id or asset-pub-130."
    )

    resolver_input, alias_to_media, _aliases = _build_resolver_input([], request, clips)

    assert alias_to_media == {"m001": "asset-pub-01", "m002": "asset-pub-13"}
    assert "m001 and m002" in resolver_input.creator_request
    assert "unknown-media-id" in resolver_input.creator_request
    assert "asset-pub-130" in resolver_input.creator_request


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
    assert result.question is None
    assert result.status == "pending"
    assert not result.needs_creator


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

    assert result.question is None
    assert result.status == "pending"
    assert result.intents[0].status == "needs_creator"
    assert not result.needs_creator


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
    assert result.status == "pending"
    assert result.question is None
    assert not result.needs_creator


async def test_caption_authoring_quota_returns_budget_exhausted_without_question(
    monkeypatch,
) -> None:
    intent = ClipIntent(
        intent_id="i_weather",
        op="caption",
        attribute="park clips",
        caption_attribute="the weather",
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_weather",
                    assignments=[ResolverAssignment(media="m001", confidence=0.9)],
                    caption="cold and rainy",
                )
            ]
        ),
    )
    calls: list[str] = []

    async def _quota(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        calls.append(candidate.media_id)
        raise ProviderQuotaExceededError(provider="gemini")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _quota)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="Add a caption about the weather on the park clips",
        clips=[_video_clip("m1", subject="people at a park")],
        run_context=RunContext(),
    )

    assert calls == ["m1"]
    assert result.status == "budget_exhausted"
    assert result.error_code == "provider_quota_exceeded"
    assert result.question is None
    assert not result.needs_creator


async def test_caption_authoring_is_skipped_after_membership_quota(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="i_weather",
        op="caption",
        attribute="park clips",
        caption_attribute="the weather",
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_weather",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="Who is here?")],
                    caption="cold and rainy",
                )
            ]
        ),
    )
    calls: list[str] = []

    async def _quota(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        calls.append(candidate.question)
        raise ProviderQuotaExceededError(provider="gemini")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _quota)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="Add a caption about the weather on the park clips",
        clips=[_video_clip("m1", subject="people at a park")],
        run_context=RunContext(),
    )

    assert calls == [
        'Does this clip match this description: "park clips"? Answer only "yes" or "no".'
    ]
    assert result.status == "budget_exhausted"
    assert result.question is None
    assert result.intents[0].caption_text is None


async def test_caption_authoring_timeout_preserves_completed_sibling(monkeypatch) -> None:
    intents = [
        ClipIntent(
            intent_id="i_one",
            op="caption",
            attribute="first park clips",
            caption_attribute="the weather",
        ),
        ClipIntent(
            intent_id="i_two",
            op="caption",
            attribute="second park clips",
            caption_attribute="the weather",
        ),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_one",
                    assignments=[ResolverAssignment(media="m001", confidence=0.9)],
                    caption="cold and rainy",
                ),
                ResolverIntentOut(
                    intent_id="i_two",
                    assignments=[ResolverAssignment(media="m002", confidence=0.9)],
                    caption="cold and rainy",
                ),
            ]
        ),
    )

    async def _partial(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        if candidate.intent_id == "i_two":
            raise TimeoutError("caption provider timed out")
        return ClipQuestionOutput(answer="cold and rainy", confidence=0.9, evidence="seen")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _partial)
    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request="Add weather captions",
        clips=[
            _video_clip("m1", subject="people at a park"),
            _video_clip("m2", subject="people at a park"),
        ],
        run_context=RunContext(),
    )

    by_id = {item.intent_id: item for item in result.intents}
    assert result.status == "provider_unavailable"
    assert by_id["i_one"].caption_text == "cold and rainy"
    assert by_id["i_two"].caption_text is None


async def test_background_caption_authoring_concurrency_is_capped_at_four(monkeypatch) -> None:
    intents = [
        ClipIntent(
            intent_id=f"i_{i}",
            op="caption",
            attribute=f"park clips {i}",
            caption_attribute="the weather",
        )
        for i in range(6)
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=f"i_{i}",
                    assignments=[ResolverAssignment(media=f"m{i + 1:03d}", confidence=0.9)],
                    caption="cold and rainy",
                )
                for i in range(6)
            ]
        ),
    )
    active = 0
    max_active = 0

    async def _concurrent(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ClipQuestionOutput(answer="cold and rainy", confidence=0.9, evidence="seen")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _concurrent)
    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request="Add weather captions",
        clips=[_video_clip(f"m{i + 1}", subject="people at a park") for i in range(6)],
        run_context=RunContext(),
        background=True,
    )

    assert max_active <= 4
    assert result.status == "resolved"


async def test_generation_pinned_caption_cache_and_checkpoint(monkeypatch) -> None:
    from app.services.clip_intent_resolution import _caption_authoring_question

    cached_intent = ClipIntent(
        intent_id="i_cached",
        op="caption",
        attribute="cached park clips",
        caption_attribute="the weather",
    )
    fresh_intent = ClipIntent(
        intent_id="i_fresh",
        op="caption",
        attribute="fresh park clips",
        caption_attribute="the weather",
    )
    question = _caption_authoring_question(cached_intent)
    cached_clip = IntentClip(
        media_id="m1",
        kind="video",
        gcs_path="users/u/m1.mp4",
        generation="generation-1",
        analysis={
            **_record_analysis(subject="people at a park"),
            ANSWERS_KEY: {
                normalize_question(question): {
                    "answer": "sunny day",
                    "confidence": 0.9,
                    "evidence": "bright sky",
                    "generation": "generation-1",
                }
            },
        },
    )
    fresh_clip = IntentClip(
        media_id="m2",
        kind="video",
        gcs_path="users/u/m2.mp4",
        generation="generation-2",
        analysis=_record_analysis(subject="people at a park"),
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_cached",
                    assignments=[ResolverAssignment(media="m001", confidence=0.9)],
                    caption="sunny day",
                ),
                ResolverIntentOut(
                    intent_id="i_fresh",
                    assignments=[ResolverAssignment(media="m002", confidence=0.9)],
                    caption="windy afternoon",
                ),
            ]
        ),
    )
    calls: list[str] = []
    checkpointed: list[dict] = []

    async def _fresh(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        calls.append(candidate.media_id)
        return ClipQuestionOutput(answer="windy afternoon", confidence=0.9, evidence="trees moving")

    async def _checkpoint(answers):  # noqa: ANN001
        checkpointed.append(answers.copy())

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _fresh)
    result = await resolve_clip_intents_for_turn(
        intents=[cached_intent, fresh_intent],
        creator_request="Add weather captions",
        clips=[cached_clip, fresh_clip],
        run_context=RunContext(),
        background=True,
        checkpoint=_checkpoint,
    )

    assert calls == ["m2"]
    assert result.status == "resolved"
    assert result.intents[0].caption_text == "sunny day"
    assert result.intents[1].caption_text == "windy afternoon"
    assert result.vision_answers["m2"][normalize_question(question)]["generation"] == "generation-2"
    assert checkpointed[-1]["m2"][normalize_question(question)]["generation"] == "generation-2"


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


async def test_duplicate_media_generation_question_is_queried_once_and_fanned_out(
    monkeypatch,
) -> None:
    intents = [
        ClipIntent(intent_id="i_one", op="label", attribute="sport being played"),
        ClipIntent(intent_id="i_two", op="label", attribute="sport being played"),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=intent.intent_id,
                    needs_vision=[ResolverVisionQuestion(media="m001", question="What sport?")],
                )
                for intent in intents
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "Soccer", 0.9, evidence="ball")

    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request="label each sport",
        clips=[_video_clip("m1", subject="people playing a ball game")],
        run_context=RunContext(),
    )

    assert calls[0] == 1
    assert result.status == "resolved"
    assert all(intent.status == "resolved" for intent in result.intents)
    assert all(intent.assignments[0].value == "Soccer" for intent in result.intents)


async def test_foreground_cap_round_robins_intents_before_marking_pending(monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 4)
    intents = [
        ClipIntent(intent_id="i_one", op="label", attribute="sport being played"),
        ClipIntent(intent_id="i_two", op="label", attribute="activity"),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_one",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question=f"What is clip {i}?")
                        for i in range(1, 4)
                    ],
                ),
                ResolverIntentOut(
                    intent_id="i_two",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question=f"What is clip {i}?")
                        for i in range(4, 7)
                    ],
                ),
            ]
        ),
    )
    asked: list[str] = []

    async def _fake_run(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        asked.append(candidate.media_id)
        return ClipQuestionOutput(answer="Soccer", confidence=0.9, evidence="seen")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _fake_run)
    clips = [_video_clip(f"m{i}", subject="people playing a ball game") for i in range(1, 7)]

    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request="label the clips",
        clips=clips,
        run_context=RunContext(),
    )

    assert asked == ["m1", "m4", "m2", "m5"]
    assert result.status == "pending"
    assert not result.needs_creator


async def test_background_batch_keeps_completed_siblings_and_checkpoints(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 5)
                    ],
                )
            ]
        ),
    )
    checkpointed: list[dict] = []

    async def _fake_run(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        if candidate.media_id == "m4":
            raise TimeoutError("provider request timed out")
        return ClipQuestionOutput(answer="Soccer", confidence=0.9, evidence="seen")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _fake_run)
    clips = [_video_clip(f"m{i}", subject="people playing a ball game") for i in range(1, 5)]

    async def _checkpoint(answers):  # noqa: ANN001
        checkpointed.append(answers.copy())

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label each sport",
        clips=clips,
        run_context=RunContext(),
        background=True,
        checkpoint=_checkpoint,
    )

    assert result.status == "provider_unavailable"
    assert set(result.vision_answers) == {"m1", "m2", "m3"}
    assert checkpointed and set(checkpointed[-1]) == {"m1", "m2", "m3"}


async def test_explicit_ai_budget_has_distinct_safe_code(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
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

    async def _budget(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AiBudgetExceededError(
            scope="clip_question", reset_at="tomorrow", cached_behavior_available=False
        )

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _budget)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label each sport",
        clips=[_video_clip("m1", subject="people playing a ball game")],
        run_context=RunContext(),
    )

    assert result.status == "budget_exhausted"
    assert result.error_code == "ai_budget_exhausted"
    assert not result.needs_creator


async def test_quota_wins_over_sibling_unknown_output(monkeypatch) -> None:
    intents = [
        ClipIntent(intent_id="i_unknown", op="label", attribute="sport"),
        ClipIntent(intent_id="i_quota", op="label", attribute="activity"),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_unknown",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="What sport?")],
                ),
                ResolverIntentOut(
                    intent_id="i_quota",
                    needs_vision=[ResolverVisionQuestion(media="m002", question="What activity?")],
                ),
            ]
        ),
    )

    async def _mixed(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        if candidate.media_id == "m2":
            raise ProviderQuotaExceededError(provider="gemini")
        return ClipQuestionOutput(answer="", confidence=0.0)

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _mixed)
    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request="label the clips",
        clips=[
            _video_clip("m1", subject="people sitting on grass"),
            _video_clip("m2", subject="people sitting on grass"),
        ],
        run_context=RunContext(),
    )

    assert result.status == "budget_exhausted"
    assert result.error_code == "provider_quota_exceeded"
    assert result.question is None
    assert not result.needs_creator


async def test_background_quota_stops_later_batches_and_keeps_first_batch_successes(
    monkeypatch,
) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 20)
                    ],
                )
            ]
        ),
    )
    calls: list[str] = []

    async def _quota_first_batch(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        calls.append(candidate.media_id)
        if candidate.media_id == "m1":
            raise ProviderQuotaExceededError(provider="gemini")
        return ClipQuestionOutput(answer="Soccer", confidence=0.9, evidence="seen")

    monkeypatch.setattr(
        "app.services.clip_intent_resolution._run_vision_candidate", _quota_first_batch
    )
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label each sport",
        clips=[_video_clip(f"m{i}", subject="people playing a ball game") for i in range(1, 20)],
        run_context=RunContext(),
        background=True,
    )

    assert len(calls) == 4
    assert set(calls) == {"m1", "m2", "m3", "m4"}
    assert result.status == "budget_exhausted"
    assert result.error_code == "provider_quota_exceeded"
    assert set(result.vision_answers) == {"m2", "m3", "m4"}


async def test_background_provider_outcome_unknown_stops_19_candidates_and_keeps_done(
    monkeypatch,
) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 20)
                    ],
                )
            ]
        ),
    )
    calls: list[str] = []

    async def _unknown_first_batch(candidate, clip, *, question_agent, run_context):  # noqa: ANN001
        calls.append(candidate.media_id)
        if candidate.media_id == "m1":
            raise ProviderOutcomeUnknownError()
        return ClipQuestionOutput(answer="Soccer", confidence=0.9, evidence="seen")

    monkeypatch.setattr(
        "app.services.clip_intent_resolution._run_vision_candidate", _unknown_first_batch
    )
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label each sport",
        clips=[_video_clip(f"m{i}", subject="people playing a ball game") for i in range(1, 20)],
        run_context=RunContext(),
        background=True,
    )

    assert len(calls) <= 4
    assert set(calls) == {"m1", "m2", "m3", "m4"}
    assert result.status == "provider_unavailable"
    assert result.error_code == "provider_outcome_unknown"
    assert result.question is None
    assert not result.needs_creator
    assert {assignment.media_id for assignment in result.intents[0].assignments} == {
        "m2",
        "m3",
        "m4",
    }


async def test_clarification_unions_duplicate_attribute_refs_with_bounded_tail(monkeypatch) -> None:
    intents = [
        ClipIntent(intent_id="i_one", op="label", attribute="sport"),
        ClipIntent(intent_id="i_two", op="label", attribute="sport"),
    ]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="i_one",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 4)
                    ],
                ),
                ResolverIntentOut(
                    intent_id="i_two",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(4, 7)
                    ],
                ),
            ]
        ),
    )
    _patch_vision_success(monkeypatch, "unknown", 0.0)

    result = await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request="label each sport",
        clips=[_video_clip(f"m{i}", subject="people sitting on grass") for i in range(1, 7)],
        run_context=RunContext(),
        background=True,
    )

    assert result.status == "needs_creator"
    assert result.question is not None and len(result.question) <= 300
    assert result.question.count("I couldn't tell the sport") == 1
    assert "clips 1, 2, 3, 4, 5 and 1 more" in result.question


async def test_legacy_question_shape_still_needs_creator_but_technical_statuses_do_not() -> None:
    assert IntentResolution(question="Which clips?").needs_creator
    assert not IntentResolution(question="retry later", status="pending").needs_creator
    assert not IntentResolution(
        question="provider failed", status="provider_unavailable"
    ).needs_creator


async def test_provider_quota_is_budget_exhausted_and_not_creator_question(monkeypatch) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
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

    async def _quota(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ProviderQuotaExceededError(provider="gemini", reason="monthly_cap")

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", _quota)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label each sport",
        clips=[_video_clip("m1", subject="people playing a ball game")],
        run_context=RunContext(),
    )

    assert result.status == "budget_exhausted"
    assert result.error_code == "provider_quota_exceeded"
    assert result.question is None
    assert not result.needs_creator


async def test_generation_cache_mismatch_is_not_reused_and_new_answer_is_pinned(
    monkeypatch,
) -> None:
    intent = ClipIntent(intent_id="i_sport", op="label", attribute="sport being played")
    question = "What sport?"
    clip = IntentClip(
        media_id="m1",
        kind="video",
        gcs_path="users/u/m1.mp4",
        generation="new-generation",
        analysis={
            **_record_analysis(subject="people playing a ball game"),
            ANSWERS_KEY: {
                normalize_question(question): {
                    "answer": "Old Sport",
                    "confidence": 0.95,
                    "evidence": "stale",
                    "generation": "old-generation",
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
    calls = _patch_vision_success(monkeypatch, "Soccer", 0.9, evidence="new frame")

    def _fake_generation_download(object_path, local_path, *, generation):  # noqa: ANN001
        with open(local_path, "wb") as fh:
            fh.write(b"new-generation-bytes")

    monkeypatch.setattr("app.storage.download_generation_to_file", _fake_generation_download)

    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label each sport",
        clips=[clip],
        run_context=RunContext(),
    )

    assert calls[0] == 1
    assert (
        result.vision_answers["m1"][normalize_question(question)]["generation"] == "new-generation"
    )


async def test_thirty_clip_overflow_resolves_next_turn_without_more_vision(monkeypatch):
    """The KRI-154 report: 8 vague clips in a 30-clip upload, cap 4."""
    from dataclasses import replace

    from app.services.clip_intent_resolution import DeferredVisionQuery

    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 4)
    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clips = [replace(_video_clip(f"m{i}"), asset_id=f"asset-{i}") for i in range(1, 31)]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 9)
                    ],
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "soccer", 0.95)
    first = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=clips,
        run_context=RunContext(),
    )
    assert calls[0] == 4
    assert first.status == "pending"
    assert first.question is None
    assert first.deferred_queries == [
        DeferredVisionQuery(f"m{i}", "What sport?") for i in range(5, 9)
    ]
    assert first.intents[0].status == "needs_creator"  # partial assignments never ship

    # Reload the cache shape written by the chat and Celery, as the next turn does.
    answers = dict(first.vision_answers)
    for deferred in first.deferred_queries:
        answers[deferred.media_id] = {
            normalize_question(deferred.question): {
                "answer": "soccer",
                "confidence": 0.95,
                "evidence": "ball and goal",
            }
        }
    reloaded = [
        replace(c, analysis={**c.analysis, ANSWERS_KEY: answers.get(c.media_id, {})}) for c in clips
    ]
    second = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=reloaded,
        run_context=RunContext(),
    )
    assert calls[0] == 4
    assert second.question is None
    assert not second.deferred_queries
    assert len(second.intents[0].assignments) == 8
    assert all(a.grounding == "vision_verified" for a in second.intents[0].assignments)


async def test_pending_query_skips_sync_and_expired_claim_can_resume(monkeypatch):
    import time
    from dataclasses import replace

    from app.services.clip_intent_resolution import ANSWER_QUERIES_KEY

    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clip = replace(
        _video_clip("m1"),
        asset_id="asset-1",
        analysis={
            ANSWER_QUERIES_KEY: {
                "what sport?": {"status": "running", "expires_at": time.time() + 60}
            },
        },
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="What sport?")],
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "soccer", 0.9)
    pending = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=[clip],
        run_context=RunContext(),
    )
    assert calls[0] == 0
    assert pending.status == "pending"
    assert pending.question is None
    assert len(pending.deferred_queries) == 1
    clip.analysis[ANSWER_QUERIES_KEY]["what sport?"]["expires_at"] = 0
    resumed = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=[clip],
        run_context=RunContext(),
    )
    assert calls[0] == 1
    assert resumed.question is None


async def test_deadline_retains_fast_answer_and_defers_only_slow_clip(monkeypatch):
    import asyncio
    from dataclasses import replace

    from app.services import clip_intent_resolution as service

    monkeypatch.setattr(settings, "clip_intents_vision_deadline_s", 0.03)
    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clips = [replace(_video_clip(f"m{i}"), asset_id=f"asset-{i}") for i in (1, 2)]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in (1, 2)
                    ],
                )
            ]
        ),
    )

    async def vision(candidate, clip, **kwargs):
        if clip.media_id == "m2":
            await asyncio.sleep(5)
        return ClipQuestionOutput(answer="soccer", confidence=0.9)

    monkeypatch.setattr(service, "_run_vision_candidate", vision)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=clips,
        run_context=RunContext(),
    )
    assert set(result.vision_answers) == {"m1"}
    assert [q.media_id for q in result.deferred_queries] == ["m2"]
    assert result.intents[0].status == "needs_creator"


async def test_non_cacheable_overflow_still_asks_creator(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 0)
    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clips = [_video_clip("m1"), replace(_video_clip("m2"), kind="image", asset_id="photo")]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in (1, 2)
                    ],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=clips,
        run_context=RunContext(),
    )
    assert result.status == "media_unavailable"
    assert result.question is None
    assert not result.deferred_queries


async def test_caption_overflow_uses_same_worker_cache_even_at_zero_chat_budget(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 0)
    intent = ClipIntent(
        intent_id="park", op="caption", attribute="park clips", caption_attribute="weather"
    )
    clip = replace(_video_clip("m1", subject="people at a park"), asset_id="asset-1")
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="park",
                    assignments=[ResolverAssignment(media="m001", confidence=0.9)],
                    caption="cold and rainy",
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="caption the weather",
        clips=[clip],
        run_context=RunContext(),
    )
    assert len(result.deferred_queries) == 1
    key = normalize_question(result.deferred_queries[0].question)
    clip.analysis[ANSWERS_KEY] = {key: {"answer": "cold and rainy", "confidence": 0.9}}
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="caption the weather",
        clips=[clip],
        run_context=RunContext(),
    )
    assert result.question is None
    assert result.intents[0].caption_text == "cold and rainy"
    assert result.intents[0].caption_grounding == "vision_verified"


@pytest.mark.parametrize("background", [False, True])
async def test_preparation_and_overflow_share_pending_and_failed_claims(monkeypatch, background):
    import time
    from dataclasses import replace

    from app.services.clip_intent_resolution import ANSWER_QUERIES_KEY

    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clip = replace(
        _video_clip("m1"),
        asset_id="asset-1",
        generation="123",
        analysis={
            ANSWER_QUERIES_KEY: {
                "what sport?": {
                    "status": "queued",
                    "expires_at": time.time() + 60,
                    "generation": "123",
                }
            },
        },
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[ResolverVisionQuestion(media="m001", question="What sport?")],
                )
            ]
        ),
    )
    _patch_vision_forbidden(monkeypatch)
    pending = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=[clip],
        run_context=RunContext(),
        background=background,
    )
    assert pending.status == "pending"
    assert len(pending.deferred_queries) == (0 if background else 1)
    marker = clip.analysis[ANSWER_QUERIES_KEY]["what sport?"]
    marker.update(status="failed", error_code="provider_outcome_unknown")
    failed = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=[clip],
        run_context=RunContext(),
        background=background,
    )
    assert failed.status == "provider_unavailable"
    assert failed.error_code == "provider_outcome_unknown"
    assert not failed.needs_creator
    assert not failed.deferred_queries


async def test_background_preparation_finishes_pool_overflow_without_separate_tasks(monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 1)
    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clips = [replace(_video_clip(f"m{i}"), asset_id=f"asset-{i}") for i in range(1, 9)]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 9)
                    ],
                )
            ]
        ),
    )
    calls = _patch_vision_success(monkeypatch, "soccer", 0.9)
    checkpoint = AsyncMock()
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=clips,
        run_context=RunContext(),
        background=True,
        checkpoint=checkpoint,
    )
    assert calls[0] == 8
    assert result.status == "resolved"
    assert len(result.intents[0].assignments) == 8
    assert not result.deferred_queries
    assert checkpoint.await_count == 2


async def test_foreground_budget_stop_does_not_escape_through_overflow_worker(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(settings, "clip_intents_max_vision_requeries", 1)
    intent = ClipIntent(intent_id="sport", op="label", attribute="sport being played")
    clips = [replace(_video_clip(f"m{i}"), asset_id=f"asset-{i}") for i in range(1, 3)]
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="sport",
                    needs_vision=[
                        ResolverVisionQuestion(media=f"m{i:03d}", question="What sport?")
                        for i in range(1, 3)
                    ],
                )
            ]
        ),
    )

    async def budget(*args, **kwargs):
        raise AiBudgetExceededError(
            scope="test", reset_at="tomorrow", cached_behavior_available=False
        )

    monkeypatch.setattr("app.services.clip_intent_resolution._run_vision_candidate", budget)
    result = await resolve_clip_intents_for_turn(
        intents=[intent],
        creator_request="label the sport",
        clips=clips,
        run_context=RunContext(),
    )
    assert result.status == "budget_exhausted"
    assert not result.deferred_queries
