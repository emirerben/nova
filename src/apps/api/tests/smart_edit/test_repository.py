from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import Job, SmartEditDispatch, SmartEditPlan, SmartEditPlanRevision
from app.smart_edit.repository import (
    SmartEditIdempotencyConflictError,
    SmartEditRevisionConflictError,
    SmartEditStateError,
    accept_ready_revision,
    append_revision,
    create_initial_plan,
    lock_pending_dispatches,
    mark_revision_failed,
    mark_revision_ready,
    record_dispatch_failed,
    record_dispatch_succeeded,
    revision_output_gcs_path,
)
from app.smart_edit.schemas import (
    SMART_EDIT_SCHEMA_VERSION,
    SmartEditCorrectionCommand,
    SmartEditPlanDocument,
    SmartWord,
)


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.value

    def scalars(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self.value


class _Session:
    def __init__(
        self,
        plan: SmartEditPlan | None = None,
        *execute_values: Any,
        job: Any | None = None,
    ) -> None:
        self.plan = plan
        self.job = job
        self.execute_values = list(execute_values)
        self.added: list[Any] = []
        self.get_calls: list[tuple[Any, Any, dict[str, Any]]] = []
        self.executed: list[Any] = []
        self.flush_count = 0

    def get(self, model, pk, **kwargs):  # noqa: ANN001, ANN201
        self.get_calls.append((model, pk, kwargs))
        if model is SmartEditPlan:
            return self.plan
        if model is Job:
            return self.job
        return None

    def execute(self, statement):  # noqa: ANN001, ANN201
        self.executed.append(statement)
        if not self.execute_values:
            raise AssertionError("unexpected execute")
        return _Result(self.execute_values.pop(0))

    def add_all(self, rows) -> None:  # noqa: ANN001
        self.added.extend(rows)

    def flush(self) -> None:
        self.flush_count += 1


def _document() -> SmartEditPlanDocument:
    return SmartEditPlanDocument.model_validate(
        {
            "baseline_captions": [
                {
                    "cue_id": "cue-1",
                    "word_ids": ["w000001"],
                    "display_text": "Birinci",
                }
            ],
            "events": [],
        }
    )


def _word() -> SmartWord:
    return SmartWord(
        word_id="w000001",
        spoken_text="Birinci",
        display_text="Birinci",
        normalized_text="birinci",
        start_ms=0,
        end_ms=300,
        timing_quality="aligned",
        display_alignment=["w000001"],
        language="tr",
    )


def _command(
    *, expected_revision: int, idempotency_key: str, zone: str = "top_right"
) -> SmartEditCorrectionCommand:
    return SmartEditCorrectionCommand.model_validate(
        {
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "event_id": "0" * 24,
            "operation": {"kind": "set_zone", "value": zone},
        }
    )


def _plan(*, requested: int = 0, ready: int | None = None) -> SmartEditPlan:
    return SmartEditPlan(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        variant_id="subtitled",
        source_base_gcs_path="users/u/base.mp4",
        source_base_sha256="a" * 64,
        transcript_hash="b" * 64,
        schema_version=SMART_EDIT_SCHEMA_VERSION,
        preset_id="cigdem",
        preset_version="v1",
        asset_pack_id="cigdem-core",
        asset_pack_version="v1",
        language="tr",
        normalized_words=[],
        requested_revision=requested,
        ready_revision=ready,
        accepted_revision=None,
        state="rendering" if ready is None else "rerendering",
    )


def _ready_kwargs(plan_id: uuid.UUID, generation: str, revision: int = 1) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "revision": revision,
        "render_generation_id": generation,
        "output_gcs_path": revision_output_gcs_path(
            plan_id=plan_id,
            revision=revision,
            render_generation_id=generation,
        ),
        "output_sha256": "c" * 64,
        "output_gcs_generation": "12345",
        "output_size_bytes": 1000,
        "output_duration_ms": 12000,
        "output_probe_receipt": {"verified": True, "codec": "h264"},
        "stage_artifacts": {"caption_base": "sha256:abc"},
        "render_receipt": {"applied": ["captions"]},
    }


def test_create_initial_plan_stages_revision_zero_and_outbox_together() -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = _Session(None, None, job=SimpleNamespace(id=job_id, user_id=user_id))
    plan, receipt = create_initial_plan(
        db,
        job_id=job_id,
        user_id=user_id,
        variant_id="subtitled",
        source_base_gcs_path="users/u/base.mp4",
        source_base_sha256="a" * 64,
        transcript_hash="b" * 64,
        preset_id="cigdem",
        preset_version="v1",
        asset_pack_id="cigdem-core",
        asset_pack_version="v1",
        language="tr",
        normalized_words=[_word()],
        face_observations=None,
        document=_document(),
        compiled_patch={"captions": []},
        planner_versions={"planner": "v1"},
        validation_receipt={"valid": True},
        render_generation_id="generation-0",
    )

    assert db.flush_count == 1
    assert [type(row) for row in db.added] == [
        SmartEditPlan,
        SmartEditPlanRevision,
        SmartEditDispatch,
    ]
    revision = db.added[1]
    dispatch = db.added[2]
    assert revision.document["baseline_captions"][0]["display_text"] == "Birinci"
    assert dispatch.plan_id == plan.id == receipt.plan_id
    assert dispatch.revision == receipt.revision == 0
    assert dispatch.render_generation_id == "generation-0"


def test_create_initial_plan_rejects_a_job_owned_by_another_user() -> None:
    job_id = uuid.uuid4()
    db = _Session(job=SimpleNamespace(id=job_id, user_id=uuid.uuid4()))

    with pytest.raises(LookupError):
        create_initial_plan(
            db,
            job_id=job_id,
            user_id=uuid.uuid4(),
            variant_id="subtitled",
            source_base_gcs_path="users/u/base.mp4",
            source_base_sha256="a" * 64,
            transcript_hash="b" * 64,
            preset_id="cigdem",
            preset_version="v1",
            asset_pack_id="cigdem-core",
            asset_pack_version="v1",
            language="tr",
            normalized_words=[_word()],
            face_observations=None,
            document=_document(),
            compiled_patch={},
            planner_versions={},
            validation_receipt={},
        )

    assert db.added == []


def test_create_initial_plan_replays_the_active_plan_under_the_job_lock() -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    existing = _plan(requested=1, ready=0)
    existing.job_id = job_id
    existing.user_id = user_id
    requested = SimpleNamespace(render_generation_id="generation-1")
    db = _Session(
        None,
        existing,
        requested,
        job=SimpleNamespace(id=job_id, user_id=user_id),
    )

    plan, receipt = create_initial_plan(
        db,
        job_id=job_id,
        user_id=user_id,
        variant_id="subtitled",
        source_base_gcs_path="users/u/base.mp4",
        source_base_sha256="a" * 64,
        transcript_hash="b" * 64,
        preset_id="cigdem",
        preset_version="v1",
        asset_pack_id="cigdem-core",
        asset_pack_version="v1",
        language="tr",
        normalized_words=[_word()],
        face_observations=None,
        document=_document(),
        compiled_patch={},
        planner_versions={},
        validation_receipt={},
        render_generation_id="ignored-redelivery-generation",
    )

    assert plan is existing
    assert receipt.replayed is True
    assert receipt.revision == 1
    assert receipt.render_generation_id == "generation-1"
    assert db.added == []


def test_create_initial_plan_can_atomically_supersede_changed_source_identity() -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    existing = _plan(requested=0, ready=0)
    existing.job_id = job_id
    existing.user_id = user_id
    db = _Session(
        None,
        existing,
        None,
        job=SimpleNamespace(id=job_id, user_id=user_id),
    )

    successor, receipt = create_initial_plan(
        db,
        job_id=job_id,
        user_id=user_id,
        variant_id="subtitled",
        source_base_gcs_path="users/u/new-base.mp4",
        source_base_sha256="d" * 64,
        transcript_hash="e" * 64,
        preset_id="cigdem",
        preset_version="v1",
        asset_pack_id="cigdem-core",
        asset_pack_version="v1",
        language="tr",
        normalized_words=[_word()],
        face_observations=None,
        document=_document(),
        compiled_patch={},
        planner_versions={},
        validation_receipt={},
        render_generation_id="successor-generation",
        expected_active_plan_id=existing.id,
    )

    assert existing.state == "retired"
    assert existing.retired_at is not None
    assert successor.supersedes_plan_id == existing.id
    assert successor.id != existing.id
    assert receipt.replayed is False
    assert db.flush_count == 2
    cancel_statement = db.executed[1]
    assert cancel_statement.compile().params["state"] == "cancelled"


def test_supersession_rejects_a_stale_expected_active_plan() -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    active = _plan(requested=0, ready=0)
    active.job_id = job_id
    active.user_id = user_id
    db = _Session(
        None,
        active,
        job=SimpleNamespace(id=job_id, user_id=user_id),
    )

    with pytest.raises(SmartEditStateError, match="changed before supersession"):
        create_initial_plan(
            db,
            job_id=job_id,
            user_id=user_id,
            variant_id="subtitled",
            source_base_gcs_path="users/u/new-base.mp4",
            source_base_sha256="d" * 64,
            transcript_hash="e" * 64,
            preset_id="cigdem",
            preset_version="v1",
            asset_pack_id="cigdem-core",
            asset_pack_version="v1",
            language="tr",
            normalized_words=[_word()],
            face_observations=None,
            document=_document(),
            compiled_patch={},
            planner_versions={},
            validation_receipt={},
            expected_active_plan_id=uuid.uuid4(),
        )

    assert active.state != "retired"
    assert db.added == []


def test_append_revision_locks_parent_and_preserves_last_ready() -> None:
    plan = _plan(requested=0, ready=0)
    parent = SmartEditPlanRevision(plan_id=plan.id, revision=0, document={}, planner_versions={})
    db = _Session(plan, None, parent)

    receipt = append_revision(
        db,
        plan_id=plan.id,
        user_id=plan.user_id,
        command=_command(expected_revision=0, idempotency_key="request-123"),
        document=_document(),
        compiled_patch={"captions": []},
        planner_versions={"planner": "v1"},
        validation_receipt={"valid": True},
        render_generation_id="generation-1",
    )

    assert db.get_calls == [(SmartEditPlan, plan.id, {"with_for_update": True})]
    assert plan.requested_revision == 1
    assert plan.ready_revision == 0
    assert plan.state == "rerendering"
    assert receipt.revision == 1
    assert receipt.ready_revision == 0
    assert receipt.replayed is False
    assert [type(row) for row in db.added] == [SmartEditPlanRevision, SmartEditDispatch]


def test_append_revision_returns_idempotent_replay_before_stale_conflict() -> None:
    plan = _plan(requested=2, ready=1)
    command = _command(expected_revision=1, idempotency_key="request-123")
    existing = SimpleNamespace(
        revision=2,
        parent_revision=1,
        correction=command.model_dump(mode="json"),
        render_generation_id="generation-2",
    )
    db = _Session(plan, existing)

    receipt = append_revision(
        db,
        plan_id=plan.id,
        user_id=plan.user_id,
        command=command,
        document=_document(),
        compiled_patch={},
        planner_versions={},
        validation_receipt={},
    )

    assert receipt.replayed is True
    assert receipt.revision == 2
    assert db.added == []


def test_append_revision_rejects_idempotency_key_reuse_for_another_correction() -> None:
    plan = _plan(requested=2, ready=1)
    original = _command(expected_revision=1, idempotency_key="request-123")
    existing = SimpleNamespace(
        revision=2,
        parent_revision=1,
        correction=original.model_dump(mode="json"),
        render_generation_id="generation-2",
    )
    db = _Session(plan, existing)

    with pytest.raises(SmartEditIdempotencyConflictError):
        append_revision(
            db,
            plan_id=plan.id,
            user_id=plan.user_id,
            command=_command(
                expected_revision=1,
                idempotency_key="request-123",
                zone="bottom_left",
            ),
            document=_document(),
            compiled_patch={},
            planner_versions={},
            validation_receipt={},
        )


def test_append_revision_rejects_stale_expected_revision() -> None:
    plan = _plan(requested=2, ready=1)
    db = _Session(plan, None)

    with pytest.raises(SmartEditRevisionConflictError) as exc_info:
        append_revision(
            db,
            plan_id=plan.id,
            user_id=plan.user_id,
            command=_command(expected_revision=1, idempotency_key="request-456"),
            document=_document(),
            compiled_patch={},
            planner_versions={},
            validation_receipt={},
        )

    assert exc_info.value.current_revision == 2
    assert db.added == []


def test_ready_cas_keeps_last_good_when_newer_revision_is_requested() -> None:
    plan = _plan(requested=2, ready=0)
    revision = SmartEditPlanRevision(
        plan_id=plan.id,
        revision=1,
        parent_revision=0,
        document={},
        planner_versions={},
        render_generation_id="generation-1",
        status="rendering",
    )
    dispatch = SmartEditDispatch(
        plan_id=plan.id,
        revision=1,
        render_generation_id="generation-1",
        state="dispatched",
        attempt_count=1,
        available_at=datetime.now(UTC),
    )
    db = _Session(plan, revision, dispatch)

    receipt = mark_revision_ready(db, **_ready_kwargs(plan.id, "generation-1"))

    assert receipt.advanced_ready_pointer is False
    assert plan.ready_revision == 0
    assert revision.status == "ready"
    assert dispatch.state == "completed"


def test_ready_cas_advances_only_verified_current_generation() -> None:
    plan = _plan(requested=1, ready=0)
    revision = SmartEditPlanRevision(
        plan_id=plan.id,
        revision=1,
        parent_revision=0,
        document={},
        planner_versions={},
        render_generation_id="generation-1",
        status="rendering",
    )
    dispatch = SmartEditDispatch(
        plan_id=plan.id,
        revision=1,
        render_generation_id="generation-1",
        state="dispatched",
        attempt_count=1,
        available_at=datetime.now(UTC),
    )
    db = _Session(plan, revision, dispatch)

    receipt = mark_revision_ready(db, **_ready_kwargs(plan.id, "generation-1"))

    assert receipt.advanced_ready_pointer is True
    assert plan.ready_revision == 1
    assert plan.state == "ready"

    invalid = _ready_kwargs(plan.id, "generation-1")
    invalid["output_probe_receipt"] = {"verified": False}
    with pytest.raises(SmartEditStateError, match="probe"):
        mark_revision_ready(_Session(plan), **invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("output_gcs_path", ""),
        ("output_gcs_generation", ""),
        ("output_sha256", "ü" * 64),
        ("output_size_bytes", 0),
        ("output_duration_ms", 0),
    ],
)
def test_ready_rejects_incomplete_or_invalid_output_evidence(field: str, value: Any) -> None:
    plan = _plan(requested=1, ready=0)
    invalid = _ready_kwargs(plan.id, "generation-1")
    invalid[field] = value

    with pytest.raises(SmartEditStateError, match="evidence"):
        mark_revision_ready(_Session(plan), **invalid)


def test_duplicate_ready_delivery_cannot_rewrite_immutable_output_evidence() -> None:
    plan = _plan(requested=1, ready=1)
    kwargs = _ready_kwargs(plan.id, "generation-1")
    revision = SmartEditPlanRevision(
        plan_id=plan.id,
        revision=1,
        parent_revision=0,
        document={},
        planner_versions={},
        render_generation_id="generation-1",
        status="ready",
        output_gcs_path=kwargs["output_gcs_path"],
        output_sha256=kwargs["output_sha256"],
        output_gcs_generation=kwargs["output_gcs_generation"],
        output_size_bytes=kwargs["output_size_bytes"],
        output_duration_ms=kwargs["output_duration_ms"],
        output_probe_receipt=kwargs["output_probe_receipt"],
        stage_artifacts=kwargs["stage_artifacts"],
        render_receipt=kwargs["render_receipt"],
    )
    dispatch = SmartEditDispatch(
        plan_id=plan.id,
        revision=1,
        render_generation_id="generation-1",
        state="completed",
        attempt_count=1,
        available_at=datetime.now(UTC),
    )

    replay = mark_revision_ready(_Session(plan, revision, dispatch), **kwargs)
    assert replay.advanced_ready_pointer is False

    changed = dict(kwargs)
    changed["output_sha256"] = "d" * 64
    with pytest.raises(SmartEditStateError, match="immutable"):
        mark_revision_ready(_Session(plan, revision, dispatch), **changed)


def test_late_failure_cannot_downgrade_a_ready_revision() -> None:
    plan = _plan(requested=1, ready=1)
    plan.state = "ready"
    revision = SmartEditPlanRevision(
        plan_id=plan.id,
        revision=1,
        parent_revision=0,
        document={},
        planner_versions={},
        render_generation_id="generation-1",
        status="ready",
    )
    dispatch = SmartEditDispatch(
        plan_id=plan.id,
        revision=1,
        render_generation_id="generation-1",
        state="completed",
        attempt_count=1,
        available_at=datetime.now(UTC),
    )

    mark_revision_failed(
        _Session(plan, revision, dispatch),
        plan_id=plan.id,
        revision=1,
        render_generation_id="generation-1",
        error_code="late_worker_error",
        error_detail="duplicate delivery failed after the winner committed",
    )

    assert revision.status == "ready"
    assert dispatch.state == "completed"
    assert plan.state == "ready"


def test_accept_requires_requested_ready_and_verified_to_match() -> None:
    plan = _plan(requested=2, ready=1)
    with pytest.raises(SmartEditRevisionConflictError):
        accept_ready_revision(
            _Session(plan),
            plan_id=plan.id,
            user_id=plan.user_id,
            revision=1,
        )

    plan.requested_revision = 1
    revision = SmartEditPlanRevision(
        plan_id=plan.id,
        revision=1,
        parent_revision=0,
        document={},
        planner_versions={},
        status="ready",
        output_gcs_path="smart/output.mp4",
        output_sha256="c" * 64,
        output_probe_receipt={"verified": True},
    )
    db = _Session(plan, revision)
    accepted = accept_ready_revision(
        db,
        plan_id=plan.id,
        user_id=plan.user_id,
        revision=1,
    )
    assert accepted is revision
    assert plan.accepted_revision == 1


def test_outbox_claim_uses_skip_locked_and_bounded_batch() -> None:
    rows = [SimpleNamespace(id=uuid.uuid4())]
    db = _Session(None, rows)

    assert lock_pending_dispatches(db, limit=5, now=datetime.now(UTC)) == rows
    statement = db.executed[0]
    assert statement._limit_clause.value == 5  # noqa: SLF001
    assert statement._for_update_arg.skip_locked is True  # noqa: SLF001
    assert "smart_edit_dispatches.state =" in str(statement)
    assert " OR " in str(statement)
    assert "smart_edit_plans.retired_at IS NULL" in str(statement)


def test_outbox_claim_rejects_unbounded_limit() -> None:
    with pytest.raises(ValueError):
        lock_pending_dispatches(_Session(), limit=101)


def test_dispatch_success_sets_a_reclaimable_lease_and_failure_requeues() -> None:
    dispatch = SmartEditDispatch(
        plan_id=uuid.uuid4(),
        revision=0,
        render_generation_id="generation-0",
        state="pending",
        attempt_count=0,
        available_at=datetime.now(UTC),
    )
    revision = SmartEditPlanRevision(
        plan_id=dispatch.plan_id,
        revision=0,
        parent_revision=None,
        document={},
        planner_versions={},
        render_generation_id="generation-0",
        status="requested",
    )
    db = _Session(None, revision)
    before = datetime.now(UTC)

    record_dispatch_succeeded(db, dispatch, delivery_lease=timedelta(minutes=5))

    assert dispatch.state == "dispatched"
    assert dispatch.attempt_count == 1
    assert dispatch.available_at >= before + timedelta(minutes=4, seconds=59)
    assert revision.status == "rendering"
    assert revision.render_started_at is not None

    record_dispatch_failed(db, dispatch, error="broker rejected redelivery")
    assert dispatch.state == "pending"
    assert dispatch.attempt_count == 2
    assert dispatch.last_error == "broker rejected redelivery"
