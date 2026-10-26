"""Killer screens S4-e/S4-j/S4-m: phone-plan failures are classified honestly and recover.

Each test drives the real Celery task (eager ``apply``, so ``self.retry`` really re-runs
the delivery) against Postgres, then the real chat observer. Only the AI planner
(``_guided_execution_plan``) or the render body is stubbed, raising the prod shape.

Failure modes, written before the change:
1. A bare ValueError / pydantic ValidationError (prod 5df2e3ec, b5dadbce) is reported as
   ``phone_plan_unsupported`` ("start a new edit", no retry) and its raw
   "1 validation error for EditRecipeV2" text lands on ``error_detail``.
2. A typed plan defect is wrongly made retryable, so Retry fails identically forever.
3. The ops kill switch (``PHONE_RENDERING_ENABLED=false``) reads as a device limit (N7).
4. An AI timeout fails the video on the first blip; or the retry loops (an outcome-unknown
   call may still bill, so exactly one retry); or a WRAPPED timeout (prod 1e79ff4f) skips
   the retry and shows the generic "something broke".
5. A spend cap is retried, or shown as "try again" (A1), on the render and on a chat turn.
6. Storage I/O fails the video at once; or retries never end; or after the budget the job is
   left non-terminal (a forever spinner) instead of failing with a retryable code.
7. A cloud guided-story storage blip counts toward the 3-strike render block (S4-m), or a
   retry inside the 45 s render lease no-ops as a "busy second worker".
8. A PERMANENT guided failure (approved file replaced or deleted) is treated as a blip:
   worker retries burn every Generate, the plan never blocks, and Retry fails forever.
"""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound
from pydantic import ValidationError
from sqlalchemy import select

from app.agents._runtime import ProviderOutcomeUnknownError, ProviderQuotaExceededError
from app.config import settings
from app.database import sync_session
from app.kria.recipes_v2 import EditRecipeV2
from app.models import CreationThreadEvent, CreatorAgentExecution, Job, PlanItem
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from app.tasks import kria_runtime as rt
from tests.kria.test_render_card_kick_postgres import _seed_dispatched_render
from tests.pipeline.test_phone_guided_plan import fixture

# Prod 215521c6: a genuine engine gap, authored and typed.
PORTRAIT_ONLY = (
    "phone subtitled plan requires a portrait source clip -- the phone engine aspect-fills "
    "with no face tracking, so a landscape or square clip would silently center-crop the "
    "speaker"
)
SOMETHING_BROKE = "Something broke while planning this edit"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())
    published: list[dict] = []
    monkeypatch.setattr(rt.observe_kria_job, "apply_async", lambda **kw: published.append(kw))
    monkeypatch.setattr(rt.run_kria_turn, "apply_async", lambda **_kw: None)
    return published


def _seed_phone_job(*, bindings: bool = False) -> SimpleNamespace:
    plan, phone_bindings = fixture()
    seeded = _seed_dispatched_render(
        job_status="queued",
        assembly_plan={
            PHONE_SOURCES_FIELD: (
                [b.model_dump(mode="json") for b in phone_bindings] if bindings else []
            ),
            "creator_generation_id": "generation-1",
            "guided_edit": {"approved": True},
        },
    )
    seeded.plan = plan
    return seeded


def _planner(monkeypatch: pytest.MonkeyPatch, *outcomes) -> list[int]:  # noqa: ANN002
    """Stub the AI planner: each call raises (or returns) the next outcome."""
    calls: list[int] = []

    def plan(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(gb, "_guided_execution_plan", plan)
    return calls


def _run(job_id) -> None:  # noqa: ANN001
    gb.orchestrate_generative_job.apply(args=[str(job_id)])


def _job(job_id) -> Job:  # noqa: ANN001
    with sync_session() as db:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job


def _trace_events(job: Job) -> list[dict]:
    return [
        row for row in (job.pipeline_trace or []) if row.get("event", "").startswith("phone_plan")
    ]


def _card(seeded: SimpleNamespace) -> CreationThreadEvent:
    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 1}
    with sync_session() as db:
        [card] = db.execute(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == seeded.thread_id,
                CreationThreadEvent.event_type == "assistant_render_failed",
            )
        ).scalars()
        error = db.get(CreatorAgentExecution, seeded.execution_id).error
        db.expunge(card)
    card.execution_error = error
    return card


def _validation_error() -> ValidationError:
    try:
        EditRecipeV2.model_validate({"canvas": {"width": 1080}})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a pydantic ValidationError")


def test_pydantic_bug_is_retryable_and_never_shows_raw_text(monkeypatch) -> None:
    """Prod 5df2e3ec: "1 validation error for EditRecipeV2" was phone_plan_unsupported."""
    seeded = _seed_phone_job()
    calls = _planner(monkeypatch, _validation_error())

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 1, "a bug is not a transient fault: no automatic re-run"
    assert job.status == "processing_failed"
    assert job.failure_reason == "phone_plan_failed"
    assert "validation error" not in job.error_detail
    assert job.error_detail.startswith(SOMETHING_BROKE)
    [event] = _trace_events(job)
    assert event["data"]["kind"] == "bug"
    assert event["data"]["error_class"] == "ValidationError"
    assert "for EditRecipeV2" in event["data"]["error_tail"]
    # The tail keeps the innermost frames: the raising function (here the planner stub).
    assert ", in plan\n" in event["data"]["traceback_tail"]

    card = _card(seeded)
    assert card.payload["code"] == "phone_plan_failed"
    assert card.payload["recovery"] == "retry"
    assert card.execution_error["retryable"] is True
    assert card.content.startswith(SOMETHING_BROKE)
    assert "validation error" not in card.content


def test_typed_plan_defect_stays_deterministic_with_its_authored_detail(monkeypatch) -> None:
    seeded = _seed_phone_job()
    calls = _planner(monkeypatch, UnsupportedPhonePlan(PORTRAIT_ONLY))

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 1
    assert job.failure_reason == "phone_plan_unsupported"
    assert job.error_detail == PORTRAIT_ONLY
    card = _card(seeded)
    assert card.payload["recovery"] == "ask_user"
    assert card.execution_error["retryable"] is False


def test_ops_kill_switch_is_a_retryable_rollout_gate_not_a_device_limit(monkeypatch) -> None:
    """N7: PHONE_RENDERING_ENABLED=false mid-flight (no planner stub: the real gate raises)."""
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "ios_native_device_only_enabled", False)
    seeded = _seed_phone_job()
    calls = _planner(monkeypatch, AssertionError("the gate refuses before planning"))

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert calls == []
    assert job.failure_reason == "phone_capability_unavailable"
    card = _card(seeded)
    assert card.payload["recovery"] == "retry"
    assert "can't render yet" not in card.content
    assert "switched off right now" in card.content


@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(lambda: ProviderOutcomeUnknownError("gemini outcome unknown"), id="direct"),
        # Prod 1e79ff4f: the composer's timeout arrived wrapped and read as "something broke".
        pytest.param(lambda: _wrapped(ProviderOutcomeUnknownError("after 90.0s")), id="wrapped"),
    ],
)
def test_ai_timeout_retries_once_then_fails_as_a_timeout(monkeypatch, timeout) -> None:
    seeded = _seed_phone_job()
    calls = _planner(monkeypatch, timeout(), timeout(), timeout())

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 2, "an outcome-unknown call may still bill: exactly one retry"
    assert job.failure_reason == "plan_ai_timeout"
    assert "outcome unknown" not in job.error_detail
    kinds = [(e["event"], e["data"]["kind"]) for e in _trace_events(job)]
    assert kinds == [
        ("phone_plan_retry_scheduled", "ai_timeout"),
        ("phone_plan_failed", "ai_timeout"),
    ]
    card = _card(seeded)
    assert card.payload["recovery"] == "retry"
    assert card.payload["title"] == "Planning took too long"


def test_ai_timeout_recovers_on_the_automatic_retry(monkeypatch) -> None:
    seeded = _seed_phone_job(bindings=True)
    calls = _planner(
        monkeypatch,
        ProviderOutcomeUnknownError("gemini outcome unknown"),
        (seeded.plan.model_dump(mode="json"), None),
    )

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 2
    assert job.status != "processing_failed"
    assert job.failure_reason is None
    assert device_status(job, "guided_story").phase == "awaiting_device"


def test_spend_cap_is_its_own_code_and_is_never_retried(monkeypatch) -> None:
    seeded = _seed_phone_job()
    quota = _wrapped(ProviderQuotaExceededError(provider="gemini", reason="monthly_spend_limit"))
    calls = _planner(monkeypatch, quota)

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 1
    assert job.failure_reason == "ai_quota_exhausted"
    assert "quota" not in job.error_detail
    card = _card(seeded)
    assert card.payload["code"] == "ai_quota_exhausted"
    assert card.payload["recovery"] == "ask_user"
    assert card.execution_error["retryable"] is False
    assert "usage limit" in card.content
    assert "won't help" in card.content


def _io_blip() -> ValueError:
    # phone_visuals.bind_phone_visuals: a GCS NotFound wrapped as a bare ValueError.
    try:
        raise NotFound("gs://bucket/visual-0")
    except NotFound as exc:
        try:
            raise ValueError("approved video bytes are unavailable") from exc
        except ValueError as wrapped:
            return wrapped


def test_storage_blip_is_retried_and_the_retry_can_succeed(monkeypatch) -> None:
    seeded = _seed_phone_job(bindings=True)
    calls = _planner(
        monkeypatch, _io_blip(), _io_blip(), (seeded.plan.model_dump(mode="json"), None)
    )

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 3
    assert job.failure_reason is None
    assert device_status(job, "guided_story").phase == "awaiting_device"


def test_storage_that_stays_down_ends_terminal_and_retryable(monkeypatch) -> None:
    seeded = _seed_phone_job()
    calls = _planner(monkeypatch, _io_blip())

    _run(seeded.job_id)

    job = _job(seeded.job_id)
    assert len(calls) == 1 + gb._PLANNING_RETRY_BUDGET["transient"]
    assert job.status == "processing_failed"
    assert job.failure_reason == "phone_plan_failed"
    assert "bytes are unavailable" not in job.error_detail
    assert _card(seeded).payload["recovery"] == "retry"


def _wrapped(cause: BaseException) -> RuntimeError:
    try:
        raise cause
    except BaseException as exc:  # noqa: BLE001
        try:
            raise RuntimeError("Kria could not compose the creation text") from exc
        except RuntimeError as wrapped:
            return wrapped


# ── S4-m: cloud guided story ────────────────────────────────────────────────


def _approved_guided_item(plan_item_id) -> None:  # noqa: ANN001
    from tests.services.test_edit_proposals import _approved_item

    proposal = copy.deepcopy(_approved_item().edit_proposal)
    with sync_session() as db:
        db.get(PlanItem, plan_item_id).edit_proposal = json.loads(json.dumps(proposal))
        db.commit()


def _guided_render_stub(monkeypatch, body) -> list[str | None]:  # noqa: ANN001
    """Replace the render body; each call records its claimed attempt id, then ``body()``."""
    attempts: list[str | None] = []

    def render(job_id, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        attempts.append(gb._GUIDED_TASK_ATTEMPT_ID.get())
        body()

    monkeypatch.setattr(gb, "_run_generative_job", render)
    monkeypatch.setattr(settings, "guided_render_recovery_enabled", True)
    # Cloud rendering is on for this legacy guided job.
    monkeypatch.setattr(settings, "cloud_render_execution_enabled", True)
    return attempts


def _selected_plan() -> dict:
    return {
        "selected_media_ids": ["food-photo"],
        "story_timeline": [
            {
                "media_id": "food-photo",
                "gcs_path": "users/u/food.jpg",
                "generation": "12",
                "kind": "image",
            }
        ],
    }


def test_cloud_guided_storage_blip_retries_and_never_blocks_the_plan(monkeypatch, tmp_path) -> None:
    from google.api_core.exceptions import ServiceUnavailable

    from app.pipeline.guided_story import _download_selected
    from app.services.edit_proposals import proposal_generate_error

    seeded = _seed_dispatched_render(job_status="queued", assembly_plan={"variants": []})
    _approved_guided_item(seeded.plan_item_id)

    def gcs_503(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ServiceUnavailable("backend error")

    monkeypatch.setattr("app.storage.download_generation_to_file", gcs_503)
    # The real I/O wrapper classifies the blip from its cause chain.
    attempts = _guided_render_stub(
        monkeypatch, lambda: _download_selected(_selected_plan(), str(tmp_path))
    )

    for _ in range(3):  # three Generates, each ending in the same storage blip
        _run(seeded.job_id)

    # Each Generate retried in the worker (1 + 2 attempts) before failing.
    assert len(attempts) == 3 * (1 + gb._GUIDED_TRANSIENT_RETRIES)
    assert len(set(attempts)) == len(attempts), "each retry is its own claimed attempt"
    job = _job(seeded.job_id)
    assert job.failure_reason == "guided_story_media_unavailable"
    with sync_session() as db:
        item = db.get(PlanItem, seeded.plan_item_id)
        assert item.edit_proposal["render_failure"]["attempts"] == 3
        assert "Try again" in item.edit_proposal["render_failure"]["message"]
        assert proposal_generate_error(item) is None, "three blips must not block the plan"


def test_cloud_guided_deleted_media_blocks_after_three_and_never_worker_retries(
    monkeypatch,
) -> None:
    """A replaced/deleted approved file (24h upload lifecycle) fails the same way forever.

    Before the fix every guided media code counted as a storage blip: each Generate burned
    two worker retries re-downloading every source, the plan never blocked, and the copy
    said "Try again" -- a Retry button that fails identically forever.
    """
    from app.pipeline.guided_story import validate_guided_source_pool_generations
    from app.services.edit_proposals import proposal_generate_error
    from tests.pipeline.test_guided_story import _guided_snapshot

    seeded = _seed_dispatched_render(job_status="queued", assembly_plan={"variants": []})
    _approved_guided_item(seeded.plan_item_id)
    # The object now carries a different generation: media_generations_match_sync -> False.
    monkeypatch.setattr(
        "app.storage.object_metadata", lambda _path: SimpleNamespace(generation="999")
    )
    snapshot = _guided_snapshot()
    attempts = _guided_render_stub(
        monkeypatch, lambda: validate_guided_source_pool_generations(snapshot)
    )

    for generate in range(1, 4):
        _run(seeded.job_id)
        assert len(attempts) == generate, "a mismatch is never retried in the worker"

    job = _job(seeded.job_id)
    assert job.status == "processing_failed"
    assert job.failure_reason == "guided_story_media_missing"
    with sync_session() as db:
        item = db.get(PlanItem, seeded.plan_item_id)
        failure = item.edit_proposal["render_failure"]
        assert failure["code"] == "guided_story_media_missing"
        assert failure["attempts"] == 3
        assert "Try again" not in failure["message"]
        assert "current clips" in failure["message"]
        assert proposal_generate_error(item) == "proposal_render_blocked"


# ── A1: a spend cap during a chat turn ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
async def test_spend_cap_during_a_chat_turn_says_so_and_offers_no_retry(
    monkeypatch, language
) -> None:
    """The brief extractor (the turn's first AI call) hits the provider's monthly cap.

    Before: the cap became a recovery question ("I couldn't reliably read every requested
    change...") or `runtime_turn_failed` ("Try the request again"), retryable forever.
    """
    import asyncio
    import uuid

    from app.database import AsyncSessionLocal
    from app.kria.api_schemas import SubmitTurnBody
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.runtime import read_delta, submit_turn
    from app.models import (
        CreationThread,
        CreativeBriefVersion,
        CreatorAgentSession,
        CreatorAgentTurn,
    )
    from app.tasks.kria_runtime import run_kria_turn
    from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(run_kria_turn, "soft_time_limit", 45)
    user_id, thread_id, session_id = _seed_runtime_project()
    footage = ["users/test/marathon.mp4"]
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r1", kind="text", scope="title", literal="Finish")],
    )
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "edit_format": "montage", "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.content_mode = "existing_footage"
        item.clip_gcs_paths = footage
        item.clip_assignments = [
            {"media_id": "clip-1", "gcs_path": footage[0], "kind": "video", "duration_s": 4.1}
        ]
        job = Job(
            user_id=user_id,
            status="variants_ready",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            all_candidates={"clip_paths": footage},
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "original_text",
                        "resolved_archetype": "montage",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "text_elements": [
                            {"id": "hook", "text": "Finish", "start_s": 0.0, "end_s": 2.0}
                        ],
                    }
                ]
            },
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        session = db.get(CreatorAgentSession, session_id)
        session.target_job_id = job.id
        session.target_variant_id = "original_text"
        session.target_generation_id = "generation-1"
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[req.model_dump(mode="json") for req in brief.requirements],
            )
        )
        db.commit()

    async with AsyncSessionLocal() as db:
        accepted, publish = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Make the title bigger" if language == "en" else "Başlığı büyüt",
                client_event_id=uuid.uuid4().hex,
                expected_thread_revision=2,
            ),
        )
    assert publish
    calls: list[str] = []

    class CappedProvider:
        def invoke(self, **kwargs):  # noqa: ANN003, ANN202
            calls.append(kwargs["model"])
            raise ProviderQuotaExceededError(
                provider="gemini", reason="monthly_spend_limit", status_code=429
            )

    monkeypatch.setattr("app.kria.planner.default_client", lambda: CappedProvider())
    with pytest.raises(ProviderQuotaExceededError):
        await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
    assert len(calls) == 1, "a spend cap is never retried on another model"

    async with AsyncSessionLocal() as db:
        delta = await read_delta(
            db, thread_id=thread_id, creator_id=user_id, after_sequence=1, limit=20
        )
    [reply] = [event for event in delta.events if event.role == "assistant"]
    assert reply.event_type == "assistant_error"
    assert reply.payload["code"] == "ai_quota_exhausted"
    assert reply.payload["retryable"] is False
    assert reply.payload["recovery"] == "ask_user"
    assert ("usage limit" if language == "en" else "kullanım sınırına") in reply.content
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, accepted.turn_id)
        assert turn.status == "failed"
        assert turn.error["code"] == "ai_quota_exhausted"
