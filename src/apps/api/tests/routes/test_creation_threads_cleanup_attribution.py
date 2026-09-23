"""Speech-cleanup attribution after a NON-cleanup render failure (prod 2026-09-23).

Chat F07C6DF9, plan item 26bf79fe: a guided narrated voiceover story consented
to "Clean up speech" (analysis 55eb9e4c, decision clean). Phone Job 76db6913
then failed at recipe compile for an unrelated label effect
(``phone_plan_unsupported``), and three defects chained:

A. ``_fail_job`` published ``speech_cleanup_outcome = failed/internal_error``
   for that non-cleanup failure, so the card offered "Retry cleanup" /
   "Create without cleanup" that the chat route can never honor.
B. "Create without cleanup" on the ready + decided analysis was forwarded as a
   plain choice; the guided attempt planned the raw voiceover and dispatch
   refused it (create_without_cleanup is only the unchecked bypass).
C. That post-draft dispatch refusal could not settle because the item still
   pointed at the failed phone Job, so the chat stayed "preparing your
   footage" until the ~27 min receipt lease.

With A fixed, the 13:05 direction's card no longer offers cleanup recovery:
it shows "Create this video" with only the analysis id, because a decided
analysis stops asking (B2). The route re-sends the recorded choice instead of
answering speech_cleanup_choice_required on every tap.

Each test reproduces one prod step with synthetic data;
``test_prod_sequence_clean_then_failed_phone_job_then_card_actions`` runs the
whole sequence against the fixed behavior.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import app.tasks.edit_proposal_build as proposal_build
import app.tasks.generative_build as gb
from app.agents._schemas.creator_agent import CreativeStrategy, CreatorEditPlan
from app.config import settings
from app.models import CreatorAgentSession, Job, PlanItem
from app.pipeline.speech_cleanup_apply import cut_fingerprint, hydrate_speech_cleanup_snapshot
from app.routes import creation_threads as routes
from app.schemas.edit_proposal import MAIN_CREATOR_FAIL_CLOSED, parse_edit_proposal
from app.services import creator_sessions
from app.services.speech_cleanup_outcome import build_preflight_public_outcome
from app.services.speech_cleanup_preflight import analysis_snapshot, public_projection
from app.tasks import content_plan_build as cpb
from app.tasks.content_plan_build import DispatchResult
from tests.routes.test_creation_threads_speech_cleanup import _request
from tests.services.test_guided_speech_cleanup import (
    analysis_row,
    guided_voiceover_item,
    load_fixture,
    raw_narration,
)
from tests.tasks.conftest import FakeJob, patch_job_session
from tests.tasks.test_edit_proposal_build import _Db, _prod_item, _Result

_MARKED = {
    "speech_cleanup_contract": "required_v1",
    "speech_cleanup_preflight_contract": "snapshot_v1",
}
_EARLIER_ATTEMPT = "attempt-1254"
_ATTEMPT = "22525a2b-attempt-1306"


# ── shared prod-shaped graph ─────────────────────────────────────────────────


class _AnalysisSession:
    """The sync dispatcher's single row lookup (and pool-count query)."""

    def __init__(self, row):  # noqa: ANN001
        self.row = row

    def get(self, _model, identifier, **_kwargs):  # noqa: ANN001, ANN003
        return self.row if self.row.id == identifier else None

    def execute(self, _query):  # noqa: ANN001
        return SimpleNamespace(scalar_one=lambda: 0)


def _prod_graph(**row_overrides):  # noqa: ANN003
    fixture = load_fixture()
    item = guided_voiceover_item(
        fixture,
        edit_format="narrated_planned",
        clip_gcs_paths=["users/u/analysis-proxy-ios-A.mp4"],
        speech_cleanup_enabled=True,
    )
    # 12:54:44 action_generate {speech_cleanup_choice: clean}
    values = {
        "decision": "clean",
        "decision_at": None,
        "category_counts": {"filler_sounds": 2, "long_pauses": 7},
        "estimated_removed_ms": 6700,
        "failure_code": None,
    }
    values.update(row_overrides)
    return fixture, item, analysis_row(item, fixture, **values)


def _consented_plan(row) -> dict:  # noqa: ANN001
    # The Job consented while the row was checked; a later reset or a
    # still-unchecked row in a test only changes the current analysis.
    checked = (
        row
        if row.status in {"ready", "no_findings"}
        else SimpleNamespace(**{**vars(row), "status": "ready"})
    )
    return {
        **_MARKED,
        "creator_generation_id": uuid.uuid4().hex,
        "_speech_cleanup_internal": {"preflight_snapshot": analysis_snapshot(checked)},
        "_phone_sources_v1": [],
        "guided_edit": {"generation_attempt_id": _EARLIER_ATTEMPT},
    }


def _failed_phone_job(monkeypatch, row, item) -> FakeJob:  # noqa: ANN001
    """12:59 Job 76db6913: the phone compiler refuses an unrelated label effect."""

    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

    job_id = str(uuid.uuid4())
    job = FakeJob(
        status="queued",
        job_id=job_id,
        assembly_plan=_consented_plan(row),
        all_candidates={"edit_format": "narrated_planned"},
    )
    job.mode = "content_plan"
    job.content_plan_item_id = item.id
    job.user_id = uuid.uuid4()
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _jid: (job, None))
    monkeypatch.setattr(gb, "mark_failed_phase", lambda *_a, **_k: None)

    def _compile_refuses(*_a, **_k):  # noqa: ANN002, ANN003
        # Raised after require_guided_cleanup_binding already proved the
        # cleaned derivative: nothing about this failure is cleanup.
        raise UnsupportedPhonePlan("sequence effect needs composite-stream parity")

    monkeypatch.setattr(gb, "_run_phone_guided_job", _compile_refuses)
    gb._run_generative_job_impl(job_id)
    return job


async def _chat_action(  # noqa: ANN001
    monkeypatch,
    *,
    action,
    row,
    item,
    job,
    session_status,
    sent_analysis_id=None,
    mode="enforce",
):
    """Drive one card action; ``row`` is the CURRENT analysis the route re-reads."""

    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=job.user_id,
        status="active",
        revision=7,
        state={"edit_format": "narrated_planned", "media_count": 10},
        content_plan_id=uuid.uuid4(),
        active_plan_item_id=item.id,
        active_creator_agent_session_id=uuid.uuid4(),
        active_job_id=job.id,
    )
    session = SimpleNamespace(
        id=thread.active_creator_agent_session_id,
        creator_id=thread.creator_id,
        plan_item_id=item.id,
        target_job_id=None,
        status=session_status,
        revision=2,
        render_attempts=1 if session_status == "failed" else 0,
        max_render_attempts=2,
        active_plan={"edit_format": "narrated_planned", "version": 1, "plan_hash": "a" * 64},
    )
    db = Mock()

    async def get(model, identifier, **_kwargs):  # noqa: ANN001, ANN003
        if model is CreatorAgentSession:
            return session
        if model is Job and identifier == job.id:
            return job
        if model is PlanItem:
            return item
        return None

    db.get = AsyncMock(side_effect=get)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    receipt_result = Mock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=receipt_result)
    controller = AsyncMock(return_value=SimpleNamespace(id=str(session.id), current_job_id=None))
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", mode)
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_sync_render_projection", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock())
    monkeypatch.setattr(routes, "_available_formats", lambda: {"narrated": "narrated_planned"})
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", controller)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.refresh_policy_stale_analysis_async",
        AsyncMock(return_value=SimpleNamespace(refreshed=False, analysis_id=None)),
    )
    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action=action,
            payload={"speech_cleanup_analysis_id": str(sent_analysis_id or row.id)},
            client_action_id=f"card-{action}",
            expected_revision=7,
        ),
        SimpleNamespace(id=thread.creator_id),
        db,
    )
    return controller


def _projection(row, job):  # noqa: ANN001
    return public_projection(
        row, applicable=True, unavailable_reason=None, video_present=True, active_job=job
    )


# ── A: only a genuine cleanup failure publishes a failed cleanup receipt ─────


@pytest.mark.asyncio
async def test_phone_compile_failure_publishes_no_cleanup_receipt(monkeypatch) -> None:
    _fixture, item, row = _prod_graph()

    job = _failed_phone_job(monkeypatch, row, item)

    assert job.status == "processing_failed"
    assert job.failure_reason == "phone_plan_unsupported"
    assert "speech_cleanup_outcome" not in job.assembly_plan
    projection = _projection(row, job)
    # iOS cleanupActions: ready + decided + no failed outcome -> "Retry generation".
    assert projection["outcome"] is None
    assert projection["decision"] == "clean"
    assert projection["requires_choice"] is False


@pytest.mark.parametrize(
    ("failure_reason", "cleanup_reason", "expected"),
    [
        ("phone_plan_unsupported", None, None),
        ("phone_plan_failed", None, None),
        ("render_oom", None, None),
        ("processing_timeout", None, None),
        ("unknown", None, None),
        (None, None, None),
        ("speech_cleanup_failed", "apply_failed", {"code": "internal_error", "retryable": True}),
        # A cleanup failure with no detailed reason is still a cleanup failure.
        ("speech_cleanup_failed", None, {"code": "internal_error", "retryable": True}),
    ],
)
def test_fail_job_publishes_a_failed_receipt_only_for_cleanup_failures(
    monkeypatch, failure_reason, cleanup_reason, expected
) -> None:
    job_id = str(uuid.uuid4())
    job = FakeJob(
        status="processing",
        job_id=job_id,
        assembly_plan={**_MARKED, "creator_generation_id": uuid.uuid4().hex, "variants": []},
    )
    patch_job_session(monkeypatch, job)

    assert gb._fail_job(
        job_id,
        "boom",
        failure_reason=failure_reason,
        speech_cleanup_failure_reason=cleanup_reason,
    )

    assert job.status == "processing_failed"
    receipt = job.assembly_plan.get("speech_cleanup_outcome")
    if expected is None:
        assert receipt is None
    else:
        assert receipt["status"] == "failed"
        assert receipt["error"] == expected


_APPLIED_CONTEXT = {"output_removal_count": 9, "output_removed_ms": 6700}


@pytest.mark.parametrize(
    ("results", "status"),
    [
        ([{"ok": False, "render_status": "failed", "error_class": "ffmpeg_failed"}], None),
        # Variant handlers copy any exception's ``reason`` (here a single-hero
        # policy error) into speech_cleanup_failure_reason: not a cleanup failure.
        (
            [
                {
                    "ok": False,
                    "error_class": "single_hero_too_few_clips",
                    "speech_cleanup_failure_reason": "too_few_clips",
                }
            ],
            None,
        ),
        (
            [
                {
                    "ok": False,
                    "error_class": "speech_cleanup_failed",
                    "speech_cleanup_failure_reason": "apply_failed",
                }
            ],
            "failed",
        ),
        # A partial render: the ready variant proves cleanup, the other failed
        # for an unrelated reason.
        (
            [
                {"ok": True, "_speech_cleanup_outcome_context": _APPLIED_CONTEXT},
                {"ok": False, "error_class": "render_oom"},
            ],
            "applied",
        ),
        # A ready render without bounded apply evidence stays a failure.
        ([{"ok": True}], "failed"),
    ],
)
def test_finalizer_receipt_follows_cleanup_evidence_only(results, status) -> None:
    receipt = build_preflight_public_outcome(
        {**_MARKED, "creator_generation_id": uuid.uuid4().hex}, job_id="job-1", results=results
    )

    assert (receipt or {}).get("status") == status


def _job_with_failed_receipt(row, *, status: str, failure_reason: str | None):  # noqa: ANN001
    job_id, generation = uuid.uuid4(), uuid.uuid4().hex
    return SimpleNamespace(
        id=job_id,
        status=status,
        failure_reason=failure_reason,
        assembly_plan={
            **_MARKED,
            "creator_generation_id": generation,
            "_speech_cleanup_internal": {"preflight_snapshot": analysis_snapshot(row)},
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "failed",
                "removal_count": 0,
                "removed_ms": 0,
                "error": {"code": "internal_error", "retryable": True},
            },
        },
    )


@pytest.mark.parametrize(
    ("status", "failure_reason", "projected"),
    [
        # Rows already written by the old _fail_job (prod job 76db6913).
        ("processing_failed", "phone_plan_unsupported", False),
        ("variants_failed", "render_oom", False),
        # A genuine cleanup failure keeps its recovery card.
        ("processing_failed", "speech_cleanup_failed", True),
        # A READY render whose cleanup could not be proven stays visible.
        ("variants_ready", None, True),
    ],
)
def test_projection_hides_only_misattributed_failed_receipts(
    status, failure_reason, projected
) -> None:
    _fixture, _item, row = _prod_graph()
    job = _job_with_failed_receipt(row, status=status, failure_reason=failure_reason)

    outcome = _projection(row, job)["outcome"]

    assert (outcome is not None) is projected
    if projected:
        assert outcome["status"] == "failed"


def test_projection_keeps_a_cleanup_failure_reported_by_a_variant() -> None:
    _fixture, _item, row = _prod_graph()
    job = _job_with_failed_receipt(row, status="variants_failed", failure_reason="unknown")
    job.assembly_plan["variants"] = [{"error_class": "speech_cleanup_failed"}]

    assert _projection(row, job)["outcome"]["status"] == "failed"


# ── A2: "Retry generation" after a non-cleanup failure keeps the consent ─────


@pytest.mark.asyncio
async def test_retry_after_non_cleanup_failure_carries_the_accepted_clean_consent(
    monkeypatch,
) -> None:
    _fixture, item, row = _prod_graph()
    job = _failed_phone_job(monkeypatch, row, item)

    controller = await _chat_action(
        monkeypatch, action="retry", row=row, item=item, job=job, session_status="failed"
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice == "clean"
    assert controller.await_args.kwargs["speech_cleanup_recovery_action"] is None
    assert controller.await_args.kwargs["retry_target_job_id"] == job.id


@pytest.mark.asyncio
async def test_retry_carries_a_no_findings_check_without_a_choice(monkeypatch) -> None:
    _fixture, item, row = _prod_graph(status="no_findings", candidate_count=0, decision=None)
    job = _failed_phone_job(monkeypatch, row, item)

    controller = await _chat_action(
        monkeypatch, action="retry", row=row, item=item, job=job, session_status="failed"
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice is None


@pytest.mark.asyncio
async def test_retry_carries_an_unchecked_bypass_while_the_analysis_is_unchecked(
    monkeypatch,
) -> None:
    _fixture, item, row = _prod_graph(status="failed", decision="create_without_cleanup")
    job = _failed_phone_job(monkeypatch, row, item)
    # The bypass Job shape: off_v1, no snapshot, the analysis id kept privately.
    job.assembly_plan = {
        "creator_generation_id": job.assembly_plan["creator_generation_id"],
        "speech_cleanup_contract": "off_v1",
        "_speech_cleanup_internal": {"outcome_analysis_id": str(row.id)},
        "speech_cleanup_outcome": {
            "status": "bypassed_unchecked",
            "removal_count": 0,
            "removed_ms": 0,
        },
    }

    controller = await _chat_action(
        monkeypatch, action="retry", row=row, item=item, job=job, session_status="failed"
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice == "create_without_cleanup"


@pytest.mark.asyncio
async def test_retry_carries_nothing_when_the_analysis_changed(monkeypatch) -> None:
    _fixture, item, row = _prod_graph()
    job = _failed_phone_job(monkeypatch, row, item)
    replacement = SimpleNamespace(**{**vars(row), "id": uuid.uuid4(), "decision": None})

    controller = await _chat_action(
        monkeypatch, action="retry", row=replacement, item=item, job=job, session_status="failed"
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id is None
    assert confirmation.speech_cleanup_choice is None


# ── B: create_without_cleanup keeps its non-guided meaning ───────────────────


@pytest.mark.asyncio
async def test_create_without_cleanup_on_a_decided_ready_analysis_is_refused_before_an_attempt(
    monkeypatch,
) -> None:
    _fixture, item, row = _prod_graph()
    job = _failed_phone_job(monkeypatch, row, item)

    with pytest.raises(HTTPException) as refused:
        await _chat_action(
            monkeypatch,
            action="create_without_cleanup",
            row=row,
            item=item,
            job=job,
            session_status="awaiting_confirmation",
        )

    assert refused.value.status_code == 409
    assert refused.value.detail == "speech_cleanup_choice_not_allowed"
    routes.creator_agent.confirm_creator_plan_controller.assert_not_awaited()
    assert row.decision == "clean"


@pytest.mark.asyncio
async def test_create_without_cleanup_for_another_analysis_is_refused(monkeypatch) -> None:
    _fixture, item, row = _prod_graph(status="failed")
    job = _failed_phone_job(monkeypatch, row, item)
    current = SimpleNamespace(**{**vars(row), "id": uuid.uuid4()})

    with pytest.raises(HTTPException) as refused:
        await _chat_action(
            monkeypatch,
            action="create_without_cleanup",
            row=current,
            item=item,
            job=job,
            session_status="awaiting_confirmation",
            sent_analysis_id=row.id,
        )
    # The card sent the id it rendered; the route re-reads the current row.
    assert refused.value.detail == "speech_cleanup_analysis_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "failed"])
async def test_create_without_cleanup_stays_the_unchecked_bypass(monkeypatch, status) -> None:
    _fixture, item, row = _prod_graph(status=status, decision=None)
    job = _failed_phone_job(monkeypatch, row, item)

    controller = await _chat_action(
        monkeypatch,
        action="create_without_cleanup",
        row=row,
        item=item,
        job=job,
        session_status="awaiting_confirmation",
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice == "create_without_cleanup"
    # The sync dispatcher accepts the same bypass for this analysis status.
    contract, snapshot, outcome = cpb._speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row), item, analysis_id=str(row.id), choice="create_without_cleanup"
    )
    assert (contract, snapshot, outcome["status"]) == ("off_v1", None, "bypassed_unchecked")


def test_keep_original_after_clean_dispatches_the_declined_raw_contract() -> None:
    _fixture, item, row = _prod_graph()

    contract, snapshot, outcome = cpb._speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row), item, analysis_id=str(row.id), choice="keep_original"
    )

    assert (contract, outcome["status"], row.decision) == ("off_v1", "declined", "keep_original")
    # The planner's raw narration satisfies the guided guard under this contract.
    raw = raw_narration(load_fixture()).model_dump(mode="json")
    assert cpb._guided_cleanup_matches_contract(raw, contract, snapshot) is True


# ── C: a post-draft dispatch refusal never leaves the session executing ──────


def _attempt_graph(*, current_job_id):  # noqa: ANN001
    item_id, owner_id = uuid.uuid4(), uuid.uuid4()
    item = _prod_item(item_id, approval_mode="auto")
    item.current_job_id = current_job_id
    item.content_plan_id = uuid.uuid4()
    item.edit_proposal = (
        parse_edit_proposal(item.edit_proposal)
        .model_copy(
            update={
                "generation_attempt_id": _ATTEMPT,
                "status": "approved",
                "design_fallback": MAIN_CREATOR_FAIL_CLOSED,
            }
        )
        .model_dump(mode="json")
    )
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        plan_item_id=item_id,
        ownership_epoch=0,
        status="executing",
        phase="executing",
        target_job_id=None,
        revision=4,
        render_attempts=1,
        iteration_count=1,
        max_render_attempts=2,
        last_review=None,
        last_error=None,
        active_plan={
            "guided_generation_attempt_id": _ATTEMPT,
            "plan_hash": "confirmed",
            "edit_plan": CreatorEditPlan(
                manifest_hash="a" * 64, context_hash="b" * 64, strategy=CreativeStrategy()
            ).model_dump(mode="json"),
            "guided_speech_cleanup": {
                "generation_attempt_id": _ATTEMPT,
                "analysis_id": str(uuid.uuid4()),
                "choice": "create_without_cleanup",
            },
        },
    )
    return item, owner_id, session


class _JobDb(_Db):
    """``_Db`` plus the one Job row settlement locks after the PlanItem."""

    def __init__(self, rows, job):  # noqa: ANN001
        super().__init__(_Result(rows=rows))
        self.job = job
        self.job_locks: list[dict] = []

    def get(self, _model, identifier, **kwargs):  # noqa: ANN001, ANN003
        self.job_locks.append({"id": identifier, **kwargs})
        return self.job if self.job is not None and self.job.id == identifier else None


def _earlier_failed_phone_job(job_id):  # noqa: ANN001
    return SimpleNamespace(
        id=job_id,
        status="processing_failed",
        failure_reason="phone_plan_unsupported",
        assembly_plan={"guided_edit": {"generation_attempt_id": _EARLIER_ATTEMPT}},
    )


async def _reconcile(monkeypatch, item, session, *, receipt_age_s: float):  # noqa: ANN001
    now = datetime.now(UTC)
    receipt = SimpleNamespace(
        status="running", created_at=now - timedelta(seconds=receipt_age_s), error=None
    )
    old_job = SimpleNamespace(
        id=item.current_job_id,
        status="processing_failed",
        failure_reason="phone_plan_unsupported",
        created_at=now - timedelta(minutes=7),
        user_id=session.creator_id,
        content_plan_item_id=item.id,
        content_plan_ownership_epoch=0,
        assembly_plan={"guided_edit": {"generation_attempt_id": _EARLIER_ATTEMPT}},
        all_candidates={},
    )

    async def get(_model, identifier, **_kwargs):  # noqa: ANN001, ANN003
        if identifier == item.id:
            return item
        if identifier == item.current_job_id:
            return old_job
        return None

    adb = AsyncMock()
    adb.get.side_effect = get
    result = Mock()
    result.scalar_one_or_none.return_value = receipt
    adb.execute.return_value = result
    monkeypatch.setattr(creator_sessions, "append_event", AsyncMock())
    changed = await creator_sessions.reconcile_render_state(adb, session)
    return changed, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "code", "refunded"),
    [
        # The prod refusal: the same plan would repeat it, so no refund.
        ("speech_cleanup_analysis_conflict", "speech_cleanup_changed", False),
        ("speech_cleanup_recovery_conflict", "speech_cleanup_changed", False),
        ("video_required", "video_required", False),
        # A transient refusal is refunded (R-A rules).
        ("missing_row", "creator_dispatch_failed", True),
    ],
)
async def test_refusal_with_an_earlier_terminal_job_fails_the_attempt(
    monkeypatch, outcome, code, refunded
) -> None:
    stale = uuid.uuid4()
    item, owner_id, session = _attempt_graph(current_job_id=stale)
    db = _JobDb([session], _earlier_failed_phone_job(stale))
    monkeypatch.setattr(proposal_build, "sync_session", lambda: nullcontext(db))
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a: (item, owner_id))
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        Mock(return_value=DispatchResult(outcome)),
    )

    proposal_build._dispatch_after_auto_design(item.id, str(item.id), _ATTEMPT, 0)

    # The Job is locked (after the PlanItem) before settlement decides.
    assert db.job_locks == [{"id": stale, "with_for_update": True, "populate_existing": True}]
    failed = parse_edit_proposal(item.edit_proposal)
    assert failed.status == "failed"
    assert failed.failure.code == code
    # The next poll settles the session instead of waiting for the lease.
    changed, receipt = await _reconcile(monkeypatch, item, session, receipt_age_s=30)
    assert changed is True
    assert session.phase == "failed"
    assert session.last_error["code"] == code
    assert session.last_error["retryable"] is refunded
    assert receipt.status == "failed"
    assert session.render_attempts == (0 if refunded else 1)


@pytest.mark.parametrize(
    "shape", ["minted_by_this_attempt", "fallback_minted_by_this_attempt", "active", "missing"]
)
def test_a_job_of_this_attempt_or_an_active_render_still_blocks_settlement(
    monkeypatch, shape
) -> None:
    job_id = uuid.uuid4()
    item, owner_id, session = _attempt_graph(current_job_id=job_id)
    snapshot_key = "creator_guided_fallback" if shape.startswith("fallback") else "guided_edit"
    job = SimpleNamespace(
        id=job_id,
        status="processing" if shape == "active" else "processing_failed",
        failure_reason=None if shape == "active" else "dispatch_publish_failed",
        assembly_plan={
            snapshot_key: {
                "generation_attempt_id": _EARLIER_ATTEMPT if shape == "active" else _ATTEMPT
            }
        },
    )
    db = _JobDb([session], None if shape == "missing" else job)
    original = dict(item.edit_proposal)
    monkeypatch.setattr(proposal_build, "sync_session", lambda: nullcontext(db))
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a: (item, owner_id))

    assert not proposal_build._settle_creator_dispatch_failure(
        item_id=item.id, owner_id=owner_id, attempt_id=_ATTEMPT, ownership_epoch=0
    )
    assert item.edit_proposal == original
    assert db.commits == 0
    assert session.status == "executing"


# ── The exact prod sequence, fixed ───────────────────────────────────────────


def _cleaned_narration(row) -> dict:  # noqa: ANN001
    """The planner's cleaned derivative for ``row`` (consent choice "clean")."""

    snapshot = hydrate_speech_cleanup_snapshot(analysis_snapshot(row))
    return {
        "gcs_path": f"users/u/plan/{row.plan_item_id}/speech-cleanup/{row.id}/{'0a' * 16}.wav",
        "generation": "5",
        "duration_s": 41.9,
        "speech_cleanup": {
            "analysis_id": str(row.id),
            "source_gcs_path": row.source_storage_path,
            "source_generation": row.source_generation,
            "source_duration_s": row.window_end_s,
            "cut_sha256": cut_fingerprint(snapshot),
        },
    }


def _guided_attempt_session(confirmation, owner_id, item_id, attempt):  # noqa: ANN001
    """Mirror confirm_creator_plan_controller's per-attempt guided consent stamp."""

    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        plan_item_id=item_id,
        ownership_epoch=0,
        status="executing",
        phase="executing",
        target_job_id=None,
        render_attempts=2,
        iteration_count=2,
        max_render_attempts=2,
        last_review=None,
        last_error=None,
        active_plan={
            "guided_generation_attempt_id": attempt,
            "edit_plan": CreatorEditPlan(
                manifest_hash="a" * 64, context_hash="b" * 64, strategy=CreativeStrategy()
            ).model_dump(mode="json"),
            "guided_speech_cleanup": {
                "generation_attempt_id": attempt,
                "analysis_id": str(confirmation.speech_cleanup_analysis_id),
                "choice": confirmation.speech_cleanup_choice,
            },
        },
    )


@pytest.mark.asyncio
async def test_prod_sequence_clean_then_failed_phone_job_then_card_actions(monkeypatch) -> None:
    _fixture, item, row = _prod_graph()

    # 12:59 the phone Job fails at compile, unrelated to cleanup: no cleanup
    # receipt, so the card shows the Job's own failure and "Retry generation".
    job = _failed_phone_job(monkeypatch, row, item)
    projection = _projection(row, job)
    assert projection["outcome"] is None
    assert projection["decision"] == "clean"

    # 13:06:03 a stale card still sends "Create without cleanup": refused with
    # a refreshable machine code before any attempt is reserved.
    with pytest.raises(HTTPException) as refused:
        await _chat_action(
            monkeypatch,
            action="create_without_cleanup",
            row=row,
            item=item,
            job=job,
            session_status="awaiting_confirmation",
        )
    assert refused.value.detail == "speech_cleanup_choice_not_allowed"
    assert row.decision == "clean"

    # The refreshed card's "Retry generation" carries the accepted clean consent.
    controller = await _chat_action(
        monkeypatch, action="retry", row=row, item=item, job=job, session_status="failed"
    )
    confirmation = controller.await_args.args[1]
    assert (confirmation.speech_cleanup_analysis_id, confirmation.speech_cleanup_choice) == (
        row.id,
        "clean",
    )

    # The guided attempt plans the cleaned voiceover and dispatch accepts it.
    owner_id = job.user_id
    session = _guided_attempt_session(confirmation, owner_id, item.id, _ATTEMPT)
    context = proposal_build._creator_dispatch_context_for_guided_attempt(
        _Db(_Result(rows=[session])),
        item_id=item.id,
        owner_id=owner_id,
        attempt_id=_ATTEMPT,
        ownership_epoch=0,
    )
    assert context["speech_cleanup_choice"] == "clean"
    contract, snapshot, outcome = cpb._speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row),
        item,
        analysis_id=context["speech_cleanup_analysis_id"],
        choice=context["speech_cleanup_choice"],
    )
    assert (contract, outcome) == ("required_v1", None)
    assert cpb._guided_cleanup_matches_contract(_cleaned_narration(row), contract, snapshot)
    raw = raw_narration(load_fixture()).model_dump(mode="json")
    assert not cpb._guided_cleanup_matches_contract(raw, contract, snapshot)

    # Had dispatch still refused after the draft (item.current_job_id is the
    # failed phone Job), the attempt fails visibly instead of "preparing".
    proposal_item = _prod_item(item.id, approval_mode="auto")
    proposal_item.current_job_id = job.id
    proposal_item.content_plan_id = uuid.uuid4()
    proposal_item.edit_proposal = (
        parse_edit_proposal(proposal_item.edit_proposal)
        .model_copy(
            update={
                "generation_attempt_id": _ATTEMPT,
                "status": "approved",
                "design_fallback": MAIN_CREATOR_FAIL_CLOSED,
            }
        )
        .model_dump(mode="json")
    )
    job.status = "processing_failed"
    db = _JobDb([session], job)
    monkeypatch.setattr(proposal_build, "sync_session", lambda: nullcontext(db))
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a: (proposal_item, owner_id))
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        Mock(return_value=DispatchResult("speech_cleanup_analysis_conflict")),
    )
    proposal_build._dispatch_after_auto_design(item.id, str(item.id), _ATTEMPT, 0)
    failed = parse_edit_proposal(proposal_item.edit_proposal)
    assert failed.status == "failed"
    assert failed.failure.code == "speech_cleanup_changed"


# ── B2: a decided card confirms with its recorded consent ─────────────────────
#
# Once a choice is recorded, ``requires_choice`` is False and the native card
# (CreationConfirmationStage.swift) shows "Create this video" / "Retry
# generation" with ONLY the analysis id. After fix A hides the misattributed
# receipt, that is exactly the card the 13:05 direction shows, so the route
# must re-send the recorded choice instead of answering
# speech_cleanup_choice_required (enforce) or letting dispatch refuse it.


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforce", "shadow"])
async def test_new_direction_after_the_failed_phone_job_confirms_with_the_recorded_choice(
    monkeypatch, mode
) -> None:
    _fixture, item, row = _prod_graph()
    job = _failed_phone_job(monkeypatch, row, item)
    monkeypatch.setattr(
        "app.services.creator_direction_snapshot.resolve_snapshot_for_dispatch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.creator_direction_snapshot.serialize_private_snapshot",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        "app.services.creator_direction_receipts.stamp_private_receipt",
        lambda value, *_a: value,
    )

    # The fresh direction's card: no receipt, decision recorded, no choice asked.
    projection = _projection(row, job)
    assert (projection["outcome"], projection["requires_choice"]) == (None, False)

    controller = await _chat_action(
        monkeypatch,
        action="generate",
        row=row,
        item=item,
        job=job,
        session_status="awaiting_confirmation",
        mode=mode,
    )

    confirmation = controller.await_args.args[1]
    assert (confirmation.speech_cleanup_analysis_id, confirmation.speech_cleanup_choice) == (
        row.id,
        "clean",
    )
    # The dispatcher accepts that exact consent (it refuses a ready row with none).
    contract, _snapshot, _outcome = cpb._speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row), item, analysis_id=str(row.id), choice="clean"
    )
    assert contract == "required_v1"
    assert cpb._speech_cleanup_dispatch_snapshot(
        _AnalysisSession(row), item, analysis_id=str(row.id), choice=None
    ) == DispatchResult("speech_cleanup_analysis_conflict")


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["clean", "keep_original"])
async def test_generate_without_a_choice_resends_the_recorded_decision(
    monkeypatch, decision
) -> None:
    from tests.routes.test_creation_threads_speech_cleanup import (
        _analysis,
        _run_generate_action,
    )

    row = _analysis(uuid.uuid4(), status="ready", candidate_count=2)
    row.decision = decision

    _output, controller = await _run_generate_action(
        monkeypatch, analysis=row, payload={"speech_cleanup_analysis_id": str(row.id)}
    )

    confirmation = controller.await_args.args[1]
    assert (confirmation.speech_cleanup_analysis_id, confirmation.speech_cleanup_choice) == (
        row.id,
        decision,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, "create_without_cleanup"])
async def test_an_unanswered_check_still_requires_a_choice(monkeypatch, decision) -> None:
    from tests.routes.test_creation_threads_speech_cleanup import (
        _analysis,
        _run_generate_action,
    )

    row = _analysis(uuid.uuid4(), status="ready", candidate_count=2)
    row.decision = decision

    with pytest.raises(HTTPException) as refused:
        await _run_generate_action(
            monkeypatch, analysis=row, payload={"speech_cleanup_analysis_id": str(row.id)}
        )

    assert refused.value.detail == "speech_cleanup_choice_required"
    # ... and the card asks for it, so the creator is never stuck on Create.
    card = public_projection(row, applicable=True, unavailable_reason=None, video_present=True)
    assert card["requires_choice"] is True


@pytest.mark.parametrize(
    ("decision", "asks"),
    [(None, True), ("create_without_cleanup", True), ("clean", False), ("keep_original", False)],
)
def test_only_a_choice_on_the_checked_findings_stops_asking(decision, asks) -> None:
    _fixture, _item, row = _prod_graph(decision=decision)

    card = public_projection(row, applicable=True, unavailable_reason=None, video_present=True)

    assert card["requires_choice"] is asks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "decision", "expected_choice"),
    [("ready", "clean", "clean"), ("ready", "keep_original", "keep_original")],
)
async def test_retry_after_a_failed_planning_attempt_resends_the_recorded_consent(
    monkeypatch, status, decision, expected_choice
) -> None:
    from tests.routes.test_creation_threads_speech_cleanup import (
        _analysis,
        _failed_planning_graph,
        _run_generate_action,
    )

    graph = _failed_planning_graph()
    _thread, session, item = graph
    row = _analysis(item.id, status=status, candidate_count=2)
    row.decision = decision

    _output, controller = await _run_generate_action(
        monkeypatch,
        graph=graph,
        analysis=row,
        action="retry",
        payload={"speech_cleanup_analysis_id": str(row.id)},
    )

    assert session.status == "awaiting_confirmation"
    confirmation = controller.await_args.args[1]
    assert (confirmation.speech_cleanup_analysis_id, confirmation.speech_cleanup_choice) == (
        row.id,
        expected_choice,
    )
    assert controller.await_args.kwargs["retry_target_job_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["no_findings", "stale_id", "undecided", "no_id"])
async def test_retry_after_a_failed_planning_attempt_sends_only_valid_consent(
    monkeypatch, shape
) -> None:
    from tests.routes.test_creation_threads_speech_cleanup import (
        _analysis,
        _failed_planning_graph,
        _run_generate_action,
    )

    graph = _failed_planning_graph()
    row = _analysis(
        graph[2].id,
        status="no_findings" if shape == "no_findings" else "ready",
        candidate_count=0 if shape == "no_findings" else 2,
    )
    row.decision = None if shape in {"no_findings", "undecided"} else "clean"
    payload = {} if shape == "no_id" else {"speech_cleanup_analysis_id": str(row.id)}
    if shape == "stale_id":
        payload["speech_cleanup_analysis_id"] = str(uuid.uuid4())

    _output, controller = await _run_generate_action(
        monkeypatch, graph=graph, analysis=row, action="retry", payload=payload
    )

    confirmation = controller.await_args.args[1]
    expected_id = row.id if shape == "no_findings" else None
    assert (confirmation.speech_cleanup_analysis_id, confirmation.speech_cleanup_choice) == (
        expected_id,
        None,
    )
