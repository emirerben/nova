"""KRI-282 L1: a model-authored intent ``question`` must never discard matches.

A ~47-clip montage asked "What clips show people playing football?" even though
football clips existed: one shard with no football clips authored a question, and
both the shard merge and the per-intent loop let it wipe every other shard's
matches. Also pins the soft vision re-query of low-confidence / unrecorded clips for
group/include ops, and the redacted diagnostics on needs_creator turns.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionOutput
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.kria.planner import _clip_intent_resolution_plan
from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_resolution import (
    IntentClip,
    _merge_resolver_outputs,
    resolve_clip_intents_for_turn,
)

pytestmark = pytest.mark.asyncio


def _clip(media_id: str, subject: str = "people playing football") -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis={"subject": subject, "description": subject} if subject else {},
        gcs_path=f"users/u/clips/{media_id}.mp4",
    )


def _patch_resolver(monkeypatch, output: ClipRequestResolverOutput) -> None:
    monkeypatch.setattr(ClipRequestResolverAgent, "run", lambda self, input, *, ctx=None: output)


def _patch_vision(monkeypatch, answer: str, confidence: float) -> list[int]:
    calls = [0]

    def _dl(object_path, local_path):  # noqa: ANN001
        with open(local_path, "wb") as fh:
            fh.write(b"x")

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        return ClipQuestionOutput(answer=answer, confidence=confidence, evidence="e")

    monkeypatch.setattr("app.storage.download_to_file", _dl)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
        lambda path, timeout=120: SimpleNamespace(uri="files/x", mime_type="video/mp4"),
    )
    monkeypatch.setattr(ClipQuestionAgent, "run", _run)
    return calls


_Q = "What clips show people playing football?"


def _one_match(confidence: float = 0.9) -> ClipRequestResolverOutput:
    return ClipRequestResolverOutput(
        intents=[
            ResolverIntentOut(
                intent_id="g",
                assignments=[ResolverAssignment(media="m001", value=None, confidence=confidence)],
            )
        ]
    )


def _group() -> list[ClipIntent]:
    return [ClipIntent(intent_id="g", op="group", attribute="football")]


def test_merge_drops_shard_question_when_another_shard_matched() -> None:
    matched = ResolverIntentOut(
        intent_id="g",
        assignments=[ResolverAssignment(media="m001", value=None, confidence=0.9)],
    )
    asked = ResolverIntentOut(intent_id="g", question=_Q)
    merged = _merge_resolver_outputs(
        [ClipRequestResolverOutput(intents=[asked]), ClipRequestResolverOutput(intents=[matched])]
    )
    assert merged.intents[0].question is None
    assert len(merged.intents[0].assignments) == 1


def test_merge_keeps_question_when_no_shard_has_candidates() -> None:
    merged = _merge_resolver_outputs(
        [
            ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="g", question=_Q)]),
            ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="g")]),
        ]
    )
    assert merged.intents[0].question == _Q


async def test_stray_question_with_assignments_keeps_assignments(monkeypatch) -> None:
    out = _one_match()
    out.intents[0].question = _Q
    _patch_resolver(monkeypatch, out)
    result = await resolve_clip_intents_for_turn(
        intents=_group(),
        creator_request="group football",
        clips=[_clip("a")],
        run_context=RunContext(),
    )
    assert result.question is None
    assert result.status == "resolved"
    assert [a.media_id for a in result.intents[0].assignments] == ["a"]
    assert result.diagnostics["stray_questions_ignored"] == 1


async def test_genuine_no_candidate_question_still_asks(monkeypatch) -> None:
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(intents=[ResolverIntentOut(intent_id="g", question=_Q)]),
    )
    result = await resolve_clip_intents_for_turn(
        intents=_group(),
        creator_request="group the good ones",
        clips=[_clip("a", subject="a cat")],
        run_context=RunContext(),
    )
    assert result.status == "needs_creator"
    assert _Q in (result.question or "")
    assert result.diagnostics["resolver_question_intents"] == 1


async def test_low_confidence_group_clip_is_requeried_and_kept(monkeypatch) -> None:
    _patch_resolver(monkeypatch, _one_match(0.4))
    calls = _patch_vision(monkeypatch, "yes", 0.9)
    result = await resolve_clip_intents_for_turn(
        intents=_group(),
        creator_request="group football",
        clips=[_clip("a")],
        run_context=RunContext(),
    )
    assert calls[0] == 1
    assert [a.media_id for a in result.intents[0].assignments] == ["a"]
    assert result.status == "resolved"


async def test_empty_record_clip_requeried_and_unknown_stays_silent(monkeypatch) -> None:
    _patch_resolver(monkeypatch, _one_match())
    # Vision cannot tell for the unrecorded clip: must NOT become a question.
    calls = _patch_vision(monkeypatch, "unknown", 0.1)
    result = await resolve_clip_intents_for_turn(
        intents=_group(),
        creator_request="group football",
        clips=[_clip("a"), _clip("b", subject="")],
        run_context=RunContext(),
    )
    assert calls[0] == 1
    assert result.question is None
    assert result.status == "resolved"
    assert [a.media_id for a in result.intents[0].assignments] == ["a"]
    assert result.diagnostics["empty_records"] == 1


async def test_soft_requery_respects_foreground_cap(monkeypatch) -> None:
    _patch_resolver(monkeypatch, _one_match())
    calls = _patch_vision(monkeypatch, "yes", 0.9)
    clips = [_clip("a")] + [_clip(f"e{i}", subject="") for i in range(10)]
    result = await resolve_clip_intents_for_turn(
        intents=_group(),
        creator_request="group football",
        clips=clips,
        run_context=RunContext(),
        max_vision_requeries=3,
    )
    assert calls[0] == 3
    assert result.status == "resolved"  # over-cap soft work is not `pending`
    assert result.deferred_queries == []


def test_needs_creator_turn_carries_redacted_diagnostics() -> None:
    plan = _clip_intent_resolution_plan(
        question="q?",
        status="needs_creator",
        diagnostics={"shards": 4, "intent_stats": ["g:n=3:min=0.50:max=0.90:nv=0:q=0"]},
    )
    assert plan.diagnostics["shards"] == 4
    assert plan.diagnostics["status"] == "needs_creator"
