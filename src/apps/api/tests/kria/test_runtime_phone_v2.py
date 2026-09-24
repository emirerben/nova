"""KRI-187: phone-rendering accounts on Kria runtime v2.

Offline coverage of the seams a phone thread crosses on v2: the capabilities
gate, the strategy approval -> device-job dispatch, the editor approval ->
`prepare_phone_editor_commit` (device) path, and the observer treating
`awaiting_device` as pending until the phone publishes or the record fails.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import Settings, settings
from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    PlanItem,
)
from app.services.device_render import (
    device_record,
    mark_device_failed,
    pin_device_request,
    save_device_record,
)
from app.tasks import kria_runtime
from app.tasks.kria_runtime import (
    _claim_approval_dispatch,
    _device_render_state,
    _observe_dispatched_execution,
    execute_kria_approval,
)

_RECIPE = EditRecipeV1.model_validate_json(
    (Path(__file__).resolve().parents[1] / "fixtures/kria_edit_recipe_v1.json").read_text()
)
VARIANT = "guided_story"


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _device_job(*, revision: int = 1, status: str = "awaiting_device") -> SimpleNamespace:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=status,
        content_plan_ownership_epoch=1,
        failure_reason=None,
        error_detail=None,
        assembly_plan={
            "variants": [
                {
                    "variant_id": VARIANT,
                    "render_status": "awaiting_device",
                    "render_destination": "device",
                    "render_generation_id": "edit-gen-2",
                }
            ]
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id=VARIANT, revision=revision, recipe=_RECIPE),
        base_generation="edit-gen-2",
    )
    return job


def _publish(job: SimpleNamespace, *, attempt: str = "upload-attempt-1") -> None:
    """Mirror `complete_device_export` (routes/device_render.py)."""
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "published"
    record.update(published_attempt=attempt, base_generation=attempt)
    save_device_record(job, VARIANT, record)
    job.assembly_plan["variants"] = [
        {
            **job.assembly_plan["variants"][0],
            "ok": True,
            "render_status": "ready",
            "render_generation_id": attempt,
            "output_url": "https://example.test/device.mp4",
        }
    ]
    job.status = "variants_ready"


def _execution(*, variant: str | None, minimum: int | None = None) -> SimpleNamespace:
    prep = {"device_recipe_revision": minimum} if minimum is not None else None
    return SimpleNamespace(
        target_variant_id=variant,
        result={"editor_prep": prep} if prep else {},
    )


# ---------------------------------------------------------------- settings / gate


def test_v2_phone_flag_defaults_off_and_allowlist_narrows_it() -> None:
    member, other = uuid.uuid4(), uuid.uuid4()
    assert Settings.model_fields["kria_runtime_v2_phone_enabled"].default is False
    off = Settings(storage_bucket="b", kria_runtime_v2_phone_user_ids=[member])
    assert off.kria_runtime_v2_phone_for(member) is False
    on_everyone = Settings(storage_bucket="b", kria_runtime_v2_phone_enabled=True)
    assert on_everyone.kria_runtime_v2_phone_for(other) is True
    on_listed = Settings(
        storage_bucket="b",
        kria_runtime_v2_phone_enabled=True,
        kria_runtime_v2_phone_user_ids=[member],
    )
    assert on_listed.kria_runtime_v2_phone_for(member) is True
    assert on_listed.kria_runtime_v2_phone_for(other) is False


# ---------------------------------------------------------------- device state


def test_a_cloud_job_is_not_a_device_job() -> None:
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={"variants": []})
    assert _device_render_state(job, _execution(variant=None)) is None


def test_awaiting_device_is_pending_not_failed() -> None:
    job = _device_job()
    assert _device_render_state(job, _execution(variant=VARIANT, minimum=1)) == "pending"
    assert _device_render_state(job, _execution(variant=None)) == "pending"


def test_published_record_is_ready_and_needs_attention_is_failed() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="export_failed", detail="")
    assert _device_render_state(job, _execution(variant=VARIANT, minimum=1)) == "failed"
    published = _device_job()
    _publish(published)
    assert _device_render_state(published, _execution(variant=VARIANT, minimum=1)) == "ready"


def test_a_record_older_than_the_pinned_edit_revision_stays_pending() -> None:
    job = _device_job(revision=1)
    _publish(job)  # revision 1 published; the approved edit pinned revision 2
    assert _device_render_state(job, _execution(variant=VARIANT, minimum=2)) == "pending"


# ---------------------------------------------------------------- observer


class _Db:
    """`sync_session` double: `get` by model, `execute` results in call order."""

    def __init__(self, gets: dict, executes: list) -> None:
        self._gets = gets
        self._executes = list(executes)
        self.commit = MagicMock()

    def get(self, model, _identity):  # noqa: ANN001, ANN201
        return self._gets[model]

    def execute(self, _statement):  # noqa: ANN001, ANN201
        row = self._executes.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: row)


def _observe(job: SimpleNamespace, execution_fields: dict) -> tuple[str, SimpleNamespace, list]:
    user_id = job.user_id
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, ownership_epoch=1)
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id, current_job_id=job.id)
    job.content_plan_item_id = item.id
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        plan_item_id=item.id,
        target_job_id=job.id,
        ownership_epoch=1,
        status="rendering",
        target_variant_id=None,
        target_generation_id=None,
        last_good=None,
        last_error=None,
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        runtime_version=2,
        creator_id=user_id,
        active_creator_agent_session_id=session.id,
        active_plan_item_id=item.id,
        active_job_id=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        status="observing",
        completed_at=None,
        error=None,
        observed_event_id=None,
    )
    fields = dict(
        id=uuid.uuid4(),
        status="dispatched",
        target_job_id=job.id,
        turn_id=turn.id,
        session_id=session.id,
        target_thread_id=thread.id,
        target_draft_id=None,
        target_variant_id=None,
        target_generation_id=None,
        result={},
        error=None,
        completed_at=None,
        observed_at=None,
        observed_event_id=None,
    )
    fields.update(execution_fields)
    execution = SimpleNamespace(**fields)
    db = _Db(
        {CreatorAgentExecution: execution, CreatorAgentSession: session, PlanItem: item},
        [plan, item, job, session, turn, execution, thread],
    )
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime, "_promote_queued_successor_sync", return_value=None),
    ):
        outcome, _ = _observe_dispatched_execution(execution.id)
    return outcome, execution, events


def test_observer_keeps_an_awaiting_device_execution_pending() -> None:
    job = _device_job()
    outcome, execution, events = _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )
    assert outcome == "pending"
    assert execution.status == "dispatched"
    assert events == []


def test_observer_completes_when_the_phone_publishes_under_its_own_attempt_id() -> None:
    job = _device_job()
    _publish(job, attempt="upload-attempt-9")  # != the pinned edit generation
    outcome, execution, events = _observe(
        job,
        {
            "target_variant_id": VARIANT,
            "target_generation_id": "edit-gen-2",
            "result": {"editor_prep": {"device_recipe_revision": 1}},
        },
    )
    assert outcome == "completed"
    assert execution.status == "completed"
    assert execution.result["render_generation_id"] == "upload-attempt-9"
    assert [event["event_type"] for event in events] == ["generation_ready", "assistant_review"]


def test_observer_completes_a_first_render_with_no_pinned_variant() -> None:
    job = _device_job()
    _publish(job)
    outcome, execution, _ = _observe(job, {})
    assert outcome == "completed"
    assert execution.result["variant_id"] == VARIANT


def test_observer_fails_a_device_render_the_phone_gave_up_on() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="thermal", detail="")
    assert job.status == "awaiting_device"  # Job.status never moves on a device failure
    outcome, execution, events = _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )
    assert outcome == "failed"
    assert execution.status == "failed"
    assert execution.error["code"] == "device_render_failed"
    assert events[-1]["event_type"] == "assistant_render_failed"


# ---------------------------------------------------------------- dispatch


def test_editor_approval_on_a_device_variant_enqueues_no_cloud_render() -> None:
    approval_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    prep = {
        "generation": "edit-gen-2",
        "has_render_section": True,
        "render_destination": "device",
        "device_recipe_revision": 2,
    }
    claim = SimpleNamespace(
        draft_kind="editor",
        target_job_id=job_id,
        target_variant_id=VARIANT,
        target_generation_id="edit-gen-2",
        editor_prep=prep,
    )
    with (
        patch("app.tasks.kria_runtime._claim_approval_dispatch", return_value=claim),
        patch("app.tasks.kria_runtime.enqueue_editor_commit_render") as enqueue,
        patch(
            "app.tasks.kria_runtime._finish_approval_dispatch", return_value=("dispatched", None)
        ) as finish,
    ):
        result = execute_kria_approval.run(approval_id)
    enqueue.assert_not_called()
    assert result == {"approval_id": approval_id, "status": "dispatched", "job_id": str(job_id)}
    finish.assert_called_once_with(claim, outcome="dispatched", job_id=str(job_id))


# ---------------------------------------------------------------- claim


def _claim_fixture(job: SimpleNamespace) -> tuple[_Db, SimpleNamespace, SimpleNamespace, list]:
    user_id = job.user_id
    approval_id, execution_id = uuid.uuid4(), uuid.uuid4()
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, ownership_epoch=1)
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id, current_job_id=job.id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        ownership_epoch=1,
        target_job_id=job.id,
        target_variant_id=VARIANT,
        target_generation_id="edit-gen-1",
        manifest_hash="m",
        status="awaiting_approval",
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(), status="awaiting_approval", completed_at=None, error=None
    )
    draft = SimpleNamespace(
        id=uuid.uuid4(), draft_revision=3, is_head=True, snapshot_json={"kind": "editor"}
    )
    approval = SimpleNamespace(
        id=approval_id,
        creator_id=user_id,
        execution_ids=[str(execution_id)],
        turn_id=turn.id,
        session_id=session.id,
        draft_id=draft.id,
        draft_revision=3,
        thread_id=uuid.uuid4(),
        status="approved",
        consumed_at=None,
        target_ownership_epoch=1,
        target_job_id=job.id,
        target_variant_id=VARIANT,
        target_generation_id="edit-gen-1",
        target_manifest_hash="m",
    )
    thread = SimpleNamespace(
        id=approval.thread_id,
        runtime_version=2,
        creator_id=user_id,
        active_creator_agent_session_id=session.id,
    )
    execution = SimpleNamespace(
        id=execution_id,
        status="awaiting_approval",
        target_draft_id=draft.id,
        target_draft_revision=3,
        target_variant_id=VARIANT,
        target_generation_id="edit-gen-1",
        result={},
        error=None,
        completed_at=None,
        accepted_at=None,
        external_task_id=None,
    )
    db = _Db(
        {
            CreatorAgentApproval: approval,
            CreatorAgentTurn: turn,
            CreatorAgentSession: session,
            CreatorEditDraft: draft,
            CreationThread: thread,
            PlanItem: item,
        },
        [plan, item, job, session, turn, draft, approval, execution, thread],
    )
    return db, approval, execution, [turn, session, thread]


def _claim(job: SimpleNamespace, prepare) -> tuple[object, SimpleNamespace, SimpleNamespace, list]:  # noqa: ANN001
    db, approval, execution, _ = _claim_fixture(job)
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    document = SimpleNamespace(
        kind="editor", editor_payload={"base_generation": "edit-gen-1"}, strategy=None, intent="x"
    )
    payload = SimpleNamespace(music_track_id=None)
    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime.KriaDraftDocument, "model_validate", return_value=document),
        patch.object(kria_runtime.EditorCommitRequest, "model_validate", return_value=payload),
        patch.object(kria_runtime, "prepare_editor_commit", prepare),
    ):
        claim = _claim_approval_dispatch(approval.id)
    return claim, approval, execution, events


def test_claim_turns_a_refused_phone_edit_into_a_terminal_reply_not_a_crash_loop() -> None:
    job = _device_job(status="variants_ready")
    _publish(job)

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(422, detail={"code": "unsupported_phone_edit", "reason": "x"})

    claim, approval, execution, events = _claim(job, refuse)
    assert claim is None
    assert approval.status == "cancelled"
    assert execution.status == "failed"
    assert execution.error["code"] == "unsupported_phone_edit"
    assert events[0]["event_type"] == "assistant_error"
    assert "iPhone" in events[0]["content"]


def test_claim_pins_the_device_recipe_revision_the_edit_created() -> None:
    job = _device_job(status="variants_ready")
    _publish(job)

    def prepare(current_job, variant_id, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        # `prepare_phone_editor_commit` pins revision N+1 and flips awaiting_device.
        pin_device_request(
            current_job,
            make_device_request(
                job_id=current_job.id, variant_id=variant_id, revision=2, recipe=_RECIPE
            ),
            base_generation="edit-gen-2",
        )
        return {
            "generation": "edit-gen-2",
            "has_render_section": True,
            "render_destination": "device",
            "sections": {},
        }

    claim, _, execution, _ = _claim(job, prepare)
    assert claim is not None
    assert claim.editor_prep["device_recipe_revision"] == 2
    assert execution.result["editor_prep"]["device_recipe_revision"] == 2


def test_claim_passes_phone_catalog_sfx_paths_to_the_editor_commit(monkeypatch) -> None:
    """A chat edit that leaves a phone Talking edit's sound lane alone still
    persists its effects' real catalog paths (same read as the iOS Save)."""
    job = _device_job(status="variants_ready")
    _publish(job)
    reads: list = []

    def catalog(db, current_job, variant):  # noqa: ANN001, ANN202
        reads.append((current_job, variant))
        return {"pop": "sound-effects/pop/audio.m4a"}

    monkeypatch.setattr(kria_runtime, "phone_subtitled_sfx_paths_sync", catalog)
    seen: dict = {}

    def prepare(current_job, variant_id, *_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.update(kwargs)
        pin_device_request(
            current_job,
            make_device_request(
                job_id=current_job.id, variant_id=variant_id, revision=2, recipe=_RECIPE
            ),
            base_generation="edit-gen-2",
        )
        return {
            "generation": "edit-gen-2",
            "has_render_section": True,
            "render_destination": "device",
            "sections": {},
        }

    claim, _, _, _ = _claim(job, prepare)
    assert claim is not None
    assert seen["phone_sfx_catalog_paths"] == {"pop": "sound-effects/pop/audio.m4a"}
    [(read_job, read_variant)] = reads
    assert read_job is job
    assert read_variant["variant_id"] == VARIANT


def test_claim_refuses_when_the_pinned_recipe_cannot_derive_sfx_paths(monkeypatch) -> None:
    """Deriving the catalog paths re-validates the pinned device recipe; a
    recipe that fails there is the same terminal refusal, not a crash loop."""
    job = _device_job(status="variants_ready")
    _publish(job)

    def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("pinned recipe no longer validates")

    monkeypatch.setattr(kria_runtime, "phone_subtitled_sfx_paths_sync", broken)

    def prepare(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the commit must not run after the lane read failed")

    claim, approval, execution, events = _claim(job, prepare)
    assert claim is None
    assert approval.status == "cancelled"
    assert execution.status == "failed"
    assert events[0]["event_type"] == "assistant_error"


def test_claim_still_raises_for_a_cloud_variant_validation_error() -> None:
    job = _device_job()
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(422, detail={"code": "x"})

    with pytest.raises(HTTPException):
        _claim(job, refuse)


# ---------------------------------------------------------------- review round


def test_editor_edit_with_no_render_section_ignores_a_stale_device_record() -> None:
    """A no-render edit pinned no revision; an older failed record is not its failure."""
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="export_failed", detail="")
    execution = SimpleNamespace(
        target_variant_id=VARIANT, result={"editor_prep": {"generation": "g", "sections": {}}}
    )
    assert _device_render_state(job, execution) is None


def test_device_render_failure_points_the_creator_at_the_phone_not_chat_retry() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="thermal", detail="")
    _, execution, events = _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )
    assert execution.error["recovery"] == "manual"
    assert execution.error["retryable"] is False
    failed = events[-1]
    assert failed["payload"]["recovery"] == "manual"
    assert "iPhone" in failed["content"]
    assert "retry without rebuilding" not in failed["content"]


def test_a_cloud_render_failure_keeps_the_chat_retry_recovery() -> None:
    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}  # no device records
    job.failure_reason = "render_failed"
    _, execution, events = _observe(job, {})
    assert execution.error["recovery"] == "retry"
    assert events[-1]["payload"]["recovery"] == "retry"


def test_claim_reports_a_baseline_conflict_as_a_stale_video_not_an_unsupported_edit() -> None:
    job = _device_job(status="variants_ready")
    _publish(job)

    def conflict(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(409, detail={"code": "baseline_conflict"})

    claim, _, execution, events = _claim(job, conflict)
    assert claim is None
    assert execution.error["code"] == "baseline_conflict"
    assert "changed" in events[0]["content"]
    assert "can't be rendered" not in events[0]["content"]


def test_a_refused_phone_dispatch_is_not_a_retry_loop() -> None:
    job = _device_job()
    db, _approval, execution, _rows = _claim_fixture(job)
    execution.status = "accepted"
    session = db._gets[CreatorAgentSession]
    turn = db._gets[CreatorAgentTurn]
    approval = db._gets[CreatorAgentApproval]
    thread = db._gets[CreationThread]
    session.render_attempts = 0
    session.last_error = None
    db._executes = [session, turn, approval, execution, thread]
    claim = SimpleNamespace(
        session_id=session.id,
        turn_id=turn.id,
        approval_id=approval.id,
        execution_id=execution.id,
        thread_id=thread.id,
        target_variant_id=None,
        target_generation_id=None,
    )
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime, "_promote_queued_successor_sync", return_value=None),
    ):
        status, _ = kria_runtime._finish_approval_dispatch(
            claim, outcome="invalid_clips", job_id=None, reason="unsupported_format"
        )
    assert status == "failed"
    assert execution.error["reason"] == "unsupported_format"
    assert execution.error["retryable"] is False
    assert execution.error["recovery"] == "ask_user"
    assert events[-1]["payload"]["recovery"] == "ask_user"
    assert "retry without" not in events[-1]["content"]
