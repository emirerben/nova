"""Background question lifecycle, ownership fences, and repeat-turn cache reuse."""

import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from celery.exceptions import Retry

from app.agents._runtime import RunContext
from app.agents.clip_question import ClipQuestionOutput
from app.models import ContentPlan, Persona, PlanItem, PlanItemAsset
from app.services.clip_intent_resolution import (
    ANSWER_QUERIES_KEY,
    ANSWERS_KEY,
    DeferredVisionQuery,
    IntentClip,
    normalize_question,
    vision_query_pending,
)
from app.tasks import clip_intent_requery as task


@pytest.fixture
def harness(monkeypatch):
    user_id, plan_id, item_id, asset_id, persona_id = [uuid.uuid4() for _ in range(5)]
    plan = SimpleNamespace(
        id=plan_id,
        user_id=user_id,
        persona_id=persona_id,
        ownership_epoch=2,
        ownership_quarantined_at=None,
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id)
    persona = SimpleNamespace(id=persona_id, user_id=user_id)
    asset = SimpleNamespace(
        id=asset_id,
        plan_item_id=item_id,
        user_id=user_id,
        kind="video",
        status="ready",
        deduplicated_to_asset_id=None,
        gcs_path=f"users/{user_id}/plan/{item_id}/pool/clip.mp4",
        gcs_generation="123",
        analysis={"summary": "people playing with balls"},
    )
    rows = {ContentPlan: plan, PlanItem: item, PlanItemAsset: asset, Persona: persona}
    db = MagicMock()
    db.get.side_effect = lambda model, ident, **kw: (
        rows[model] if rows.get(model) is not None and rows[model].id == ident else None
    )
    db.execute.return_value.scalar_one_or_none.side_effect = lambda: rows.get(Persona)
    opened = []

    @contextmanager
    def session():
        opened.append(True)
        try:
            yield db
        finally:
            opened.pop()

    monkeypatch.setattr(task, "sync_session", session)
    monkeypatch.setattr(task.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(task, "default_client", lambda: SimpleNamespace())
    publish = MagicMock()
    monkeypatch.setattr(task.requery_clip_intent, "apply_async", publish)
    clip = IntentClip(
        media_id=f"asset-{asset_id}",
        asset_id=str(asset_id),
        kind="video",
        gcs_path=asset.gcs_path,
        generation="123",
        analysis=asset.analysis,
    )
    query = DeferredVisionQuery(clip.media_id, "What sport?")
    enqueue_kwargs = dict(
        plan_id=str(plan_id),
        item_id=str(item_id),
        user_id=str(user_id),
        ownership_epoch=2,
        clips=[clip],
        queries=[query],
        context=RunContext(creator_id=str(user_id), request_id="turn-2"),
    )
    return SimpleNamespace(
        db=db,
        asset=asset,
        plan=plan,
        item=item,
        persona=persona,
        rows=rows,
        clip=clip,
        query=query,
        key=normalize_question(query.question),
        kwargs=enqueue_kwargs,
        publish=publish,
        opened=opened,
    )


def _enqueue(h):
    assert task.enqueue_clip_intent_requeries(**h.kwargs)
    return h.publish.call_args.kwargs["kwargs"]["payload"]


def test_duplicate_turns_and_deliveries_query_once_and_merge_latest_analysis(harness, monkeypatch):
    h = harness
    payload = _enqueue(h)
    assert vision_query_pending(h.asset.analysis, h.query.question, "123")
    assert task.enqueue_clip_intent_requeries(**h.kwargs)
    h.publish.assert_called_once()
    assert h.publish.call_args.kwargs["queue"] == task.settings.pool_asset_analysis_queue

    def vision(clip, question, **kwargs):
        assert not h.opened  # all DB locks are released during external work
        assert clip.generation == "123"
        assert kwargs["run_context"].request_id == "turn-2"
        # Another delivery while the first is running must not buy a second call.
        task.requery_clip_intent.run(payload)
        # Concurrent enrichment must survive the final locked merge.
        h.asset.analysis = {
            **h.asset.analysis,
            "other_analysis": "new",
            ANSWERS_KEY: {
                "old question": {"answer": "yes", "confidence": 0.9},
            },
        }
        return ClipQuestionOutput(answer="soccer", confidence=0.95, evidence="ball and goal")

    vision_mock = MagicMock(side_effect=vision)
    monkeypatch.setattr(task, "query_clip_vision", vision_mock)
    task.requery_clip_intent.run(payload)
    task.requery_clip_intent.run(payload)
    vision_mock.assert_called_once()
    assert h.asset.analysis[ANSWERS_KEY][h.key]["answer"] == "soccer"
    assert h.asset.analysis[ANSWERS_KEY]["old question"]["answer"] == "yes"
    assert h.asset.analysis["other_analysis"] == "new"
    assert not vision_query_pending(h.asset.analysis, h.query.question, "123")
    assert all(call.kwargs["with_for_update"] for call in h.db.get.call_args_list)


def test_publish_failure_releases_claim_and_can_retry(harness):
    h = harness
    h.publish.side_effect = RuntimeError("broker down")
    assert not task.enqueue_clip_intent_requeries(**h.kwargs)
    assert not vision_query_pending(h.asset.analysis, h.query.question, "123")
    assert ANSWERS_KEY not in h.asset.analysis
    h.publish.side_effect = None
    _enqueue(h)
    assert h.publish.call_count == 2


def test_expired_claim_requeues_and_old_delivery_cannot_write(harness, monkeypatch):
    h = harness
    old_payload = _enqueue(h)
    h.asset.analysis[ANSWER_QUERIES_KEY][h.key]["expires_at"] = time.time() - 1
    new_payload = _enqueue(h)
    assert old_payload["token"] != new_payload["token"]
    vision = MagicMock(return_value=ClipQuestionOutput(answer="tennis", confidence=0.9))
    monkeypatch.setattr(task, "query_clip_vision", vision)
    task.requery_clip_intent.run(old_payload)
    vision.assert_not_called()
    task.requery_clip_intent.run(new_payload)
    assert h.asset.analysis[ANSWERS_KEY][h.key]["answer"] == "tennis"


@pytest.mark.parametrize("mutation", ["user", "epoch", "path", "generation", "deleted", "moved"])
@pytest.mark.parametrize("stage", ["before", "during"])
def test_stale_or_unowned_delivery_never_reads_or_publishes(harness, monkeypatch, mutation, stage):
    h = harness
    payload = _enqueue(h)

    def mutate():
        if mutation == "user":
            h.asset.user_id = uuid.uuid4()
        elif mutation == "epoch":
            h.plan.ownership_epoch += 1
        elif mutation == "path":
            h.asset.gcs_path = "different.mp4"
        elif mutation == "generation":
            h.asset.gcs_generation = "456"
        elif mutation == "deleted":
            h.rows[PlanItemAsset] = None
        else:
            h.asset.plan_item_id = uuid.uuid4()

    def vision(*args, **kwargs):
        mutate()
        return ClipQuestionOutput(answer="soccer", confidence=0.9)

    vision_mock = MagicMock(side_effect=vision)
    monkeypatch.setattr(task, "query_clip_vision", vision_mock)
    if stage == "before":
        mutate()
    task.requery_clip_intent.run(payload)
    assert vision_mock.call_count == (0 if stage == "before" else 1)
    assert ANSWERS_KEY not in h.asset.analysis


@pytest.mark.parametrize("invalid", ["persona", "quarantine", "asset_owner", "plan_owner"])
def test_enqueue_fails_closed_on_ownership_mismatch(harness, invalid):
    h = harness
    if invalid == "persona":
        h.persona.user_id = uuid.uuid4()
    elif invalid == "quarantine":
        h.plan.ownership_quarantined_at = "now"
    elif invalid == "asset_owner":
        h.asset.user_id = uuid.uuid4()
    else:
        h.plan.user_id = uuid.uuid4()
    assert not task.enqueue_clip_intent_requeries(**h.kwargs)
    h.publish.assert_not_called()
    assert ANSWER_QUERIES_KEY not in h.asset.analysis


def test_transient_failure_retries_then_preserves_technical_error_on_exhaustion(
    harness, monkeypatch
):
    h = harness
    payload = _enqueue(h)
    monkeypatch.setattr(
        task, "query_clip_vision", MagicMock(side_effect=RuntimeError("unavailable"))
    )
    retry = MagicMock(side_effect=Retry())
    monkeypatch.setattr(task.requery_clip_intent, "retry", retry)
    with pytest.raises(Retry):
        task.requery_clip_intent.run(payload)
    assert ANSWERS_KEY not in h.asset.analysis
    assert h.asset.analysis[ANSWER_QUERIES_KEY][h.key]["status"] == "queued"
    task.requery_clip_intent.push_request(retries=2)
    try:
        task.requery_clip_intent.run(payload)
    finally:
        task.requery_clip_intent.pop_request()
    assert ANSWERS_KEY not in h.asset.analysis
    assert h.asset.analysis[ANSWER_QUERIES_KEY][h.key]["error_code"] == "vision_provider_error"
    assert not vision_query_pending(h.asset.analysis, h.query.question, "123")
    task.enqueue_clip_intent_requeries(**h.kwargs)
    h.publish.assert_called_once()


def test_refresh_during_query_drops_old_answer(harness, monkeypatch):
    h = harness
    payload = _enqueue(h)

    def vision(*args, **kwargs):
        h.asset.analysis = {"summary": "fresh analysis"}  # drops the old claim
        return ClipQuestionOutput(answer="soccer", confidence=0.9)

    monkeypatch.setattr(task, "query_clip_vision", vision)
    task.requery_clip_intent.run(payload)
    assert h.asset.analysis == {"summary": "fresh analysis"}


def test_flag_off_and_non_cacheable_media_never_enqueue(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(task.settings, "clip_intents_enabled", False)
    assert not task.enqueue_clip_intent_requeries(**h.kwargs)
    h.publish.assert_not_called()
    monkeypatch.setattr(task.settings, "clip_intents_enabled", True)
    for clip in [replace(h.clip, kind="image"), replace(h.clip, asset_id=None)]:
        assert not task.enqueue_clip_intent_requeries(**{**h.kwargs, "clips": [clip]})
    h.publish.assert_not_called()


def test_worker_rollout_flag_stops_queued_work(harness, monkeypatch):
    h = harness
    payload = _enqueue(h)
    monkeypatch.setattr(task.settings, "clip_intents_enabled", False)
    vision = MagicMock()
    monkeypatch.setattr(task, "query_clip_vision", vision)
    task.requery_clip_intent.run(payload)
    vision.assert_not_called()


@pytest.mark.parametrize("error_kind", ["budget", "quota", "unknown"])
def test_provider_stops_are_not_retried_or_cached_as_visual_unknown(
    harness, monkeypatch, error_kind
):
    from app.agents._runtime import (
        AiBudgetExceededError,
        ProviderOutcomeUnknownError,
        ProviderQuotaExceededError,
    )

    errors = {
        "budget": (
            AiBudgetExceededError(
                scope="test", reset_at="tomorrow", cached_behavior_available=False
            ),
            "ai_budget_exhausted",
        ),
        "quota": (ProviderQuotaExceededError(), "provider_quota_exceeded"),
        "unknown": (ProviderOutcomeUnknownError("outcome unknown"), "provider_outcome_unknown"),
    }
    error, code = errors[error_kind]
    h = harness
    payload = _enqueue(h)
    monkeypatch.setattr(task, "query_clip_vision", MagicMock(side_effect=error))
    retry = MagicMock()
    monkeypatch.setattr(task.requery_clip_intent, "retry", retry)
    task.requery_clip_intent.run(payload)
    retry.assert_not_called()
    assert ANSWERS_KEY not in h.asset.analysis
    assert h.asset.analysis[ANSWER_QUERIES_KEY][h.key]["error_code"] == code
    assert not task.enqueue_clip_intent_requeries(**h.kwargs)
    h.publish.assert_called_once()


def test_worker_answers_use_generation_aware_resolver_cache(harness, monkeypatch):
    from app.services.clip_intent_resolution import _cached_answer

    h = harness
    # A previous generation's answer must not suppress the new question.
    h.asset.analysis[ANSWERS_KEY] = {h.key: {"answer": "tennis", "generation": "old"}}
    payload = _enqueue(h)
    monkeypatch.setattr(
        task,
        "query_clip_vision",
        MagicMock(return_value=ClipQuestionOutput(answer="soccer", confidence=0.9)),
    )
    task.requery_clip_intent.run(payload)
    cached = _cached_answer(replace(h.clip, analysis=h.asset.analysis), h.key)
    assert cached["answer"] == "soccer"
    assert cached["generation"] == "123"
    assert (
        _cached_answer(replace(h.clip, generation="456", analysis=h.asset.analysis), h.key) is None
    )
