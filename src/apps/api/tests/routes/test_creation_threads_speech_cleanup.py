"""Chat-detail and action contracts for generation-bound speech cleanup."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import settings
from app.models import ContentPlan, CreatorAgentSession, Job, Persona, PlanItem
from app.routes import creation_threads as routes
from app.services.speech_cleanup_preflight import public_projection


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/creation-threads/thread/actions",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "scheme": "http",
        }
    )


def _analysis(
    item_id: uuid.UUID,
    *,
    status: str = "ready",
    candidate_count: int | None = 2,
    video_present: bool = True,
) -> SimpleNamespace:
    _ = video_present
    return SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item_id,
        source_kind="embedded_spine",
        source_media_identity="media-1",
        source_storage_path="users/private/talking.mp4",
        source_generation="secret-generation",
        window_start_s=0.0,
        window_end_s=12.0,
        source_policy_fingerprint="private-fingerprint",
        engine_version="speech-cleanup-v2",
        detector_version="mixed-gap-v2",
        analysis_payload={
            "timed_words": [{"text": "private transcript", "start_s": 1.0, "end_s": 1.4}],
            "cut_plan": {"removed": [{"start_s": 1.0, "end_s": 1.4}]},
        },
        diagnostic_receipt={"provider_error": "secret upstream details"},
        status=status,
        superseded_at=None,
        candidate_count=candidate_count,
        category_counts={"filler_sounds": candidate_count or 0, "long_pauses": 0},
        estimated_removed_ms=400 if candidate_count else 0,
        failure_code="transcription_unavailable" if status == "failed" else None,
        failure_retryable=True if status == "failed" else None,
        decision=None,
        decision_at=None,
    )


def _outcome_snapshot(row: SimpleNamespace) -> dict[str, object]:
    return {
        "schema_version": 1,
        "analysis_id": str(row.id),
        "engine_version": row.engine_version,
        "detector_version": row.detector_version,
        "source": {
            "kind": row.source_kind,
            "media_identity": row.source_media_identity,
            "storage_path": row.source_storage_path,
            "generation": row.source_generation,
            "window_start_s": row.window_start_s,
            "window_end_s": row.window_end_s,
            "source_policy_fingerprint": row.source_policy_fingerprint,
        },
        "analysis": dict(row.analysis_payload),
    }


@pytest.mark.parametrize(
    ("status", "count", "expected_choice", "expected_findings", "expected_error"),
    [
        ("queued", None, False, False, None),
        ("running", None, False, False, None),
        ("ready", 2, True, True, None),
        ("no_findings", 0, False, False, None),
        (
            "failed",
            None,
            False,
            False,
            {"code": "transcription_unavailable", "retryable": True},
        ),
    ],
)
def test_bounded_projection_hydrates_each_analysis_state(
    status: str,
    count: int | None,
    expected_choice: bool,
    expected_findings: bool,
    expected_error: dict[str, object] | None,
) -> None:
    row = _analysis(uuid.uuid4(), status=status, candidate_count=count)

    projection = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
    )

    assert projection is not None
    assert projection["requires_choice"] is expected_choice
    assert projection["render_blocker"] is None
    assert projection["analysis"] == {
        "id": str(row.id),
        "status": status,
        "detector_version": "mixed-gap-v2",
        "has_findings": expected_findings,
        "candidate_count": count,
        "category_counts": {
            "filler_sounds": count or 0,
            "long_pauses": 0,
        },
        "estimated_removed_ms": 400 if count else 0,
        "error": expected_error,
    }
    serialized = json.dumps(projection)
    for private_value in (
        "private transcript",
        "users/private",
        "secret-generation",
        "private-fingerprint",
        "start_s",
        "provider_error",
        "secret upstream details",
    ):
        assert private_value not in serialized


def test_audio_only_analysis_is_visible_but_never_offers_a_choice() -> None:
    row = _analysis(uuid.uuid4(), status="ready", candidate_count=2)

    projection = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=False,
    )

    assert projection is not None
    assert projection["analysis"]["has_findings"] is True
    assert projection["decision"] is None
    assert projection["requires_choice"] is False
    assert projection["render_blocker"] == "video_required"


@pytest.mark.parametrize("mismatch", ["job", "generation", "missing_generation"])
def test_detail_outcome_uses_only_active_job_generation_and_is_private(mismatch: str) -> None:
    job_id = uuid.uuid4()
    generation = "generation-current"
    receipt_job_id = str(uuid.uuid4()) if mismatch == "job" else str(job_id)
    receipt_generation = "generation-old" if mismatch == "generation" else generation
    assembly_plan = {
        "creator_generation_id": generation,
        "speech_cleanup_outcome": {
            "job_id": receipt_job_id,
            "render_generation_id": receipt_generation,
            "status": "applied",
            "removal_count": 2,
            "removed_ms": 400,
            "private_extra": "must-not-project",
        },
        "_speech_cleanup_internal": {"preflight_snapshot": {"timed_words": ["secret"]}},
    }
    if mismatch == "missing_generation":
        assembly_plan.pop("creator_generation_id")
    job = SimpleNamespace(id=job_id, assembly_plan=assembly_plan)

    projection = public_projection(
        _analysis(uuid.uuid4()),
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=job,
    )

    assert projection is not None
    assert projection["outcome"] is None


def test_current_generation_outcome_projects_only_bounded_receipt() -> None:
    job_id = uuid.uuid4()
    generation = "generation-current"
    row = _analysis(uuid.uuid4())
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "creator_generation_id": generation,
            "_speech_cleanup_internal": {"preflight_snapshot": _outcome_snapshot(row)},
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "applied",
                "removal_count": 2,
                "removed_ms": 400,
                "private_extra": "must-not-project",
            },
        },
    )

    projection = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=job,
    )

    assert projection is not None
    assert projection["outcome"] == {
        "job_id": str(job_id),
        "render_generation_id": generation,
        "status": "applied",
        "removal_count": 2,
        "removed_ms": 400,
    }


def test_required_ready_snapshot_can_truthfully_project_checked_no_change() -> None:
    job_id = uuid.uuid4()
    generation = "generation-current"
    row = _analysis(uuid.uuid4())
    row.decision = "clean"
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "creator_generation_id": generation,
            "speech_cleanup_contract": "required_v1",
            "_speech_cleanup_internal": {"preflight_snapshot": _outcome_snapshot(row)},
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "checked_no_change",
                "removal_count": 0,
                "removed_ms": 0,
            },
        },
    )

    projection = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=job,
    )

    assert projection is not None
    assert projection["outcome"] == {
        "job_id": str(job_id),
        "render_generation_id": generation,
        "status": "checked_no_change",
        "removal_count": 0,
        "removed_ms": 0,
    }


def test_failed_outcome_redacts_private_error_and_unknown_category_keys() -> None:
    job_id = uuid.uuid4()
    generation = "generation-current"
    # The render failure is distinct from the accepted preflight analysis. A
    # reset/queued analysis intentionally suppresses the old render outcome.
    row = _analysis(uuid.uuid4(), status="ready", candidate_count=2)
    row.decision = "clean"
    row.category_counts = {
        "filler_sounds": 1,
        "long_pauses": 2,
        "provider_diagnostic": "raw upstream response",
    }
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "creator_generation_id": generation,
            "_speech_cleanup_internal": {"preflight_snapshot": _outcome_snapshot(row)},
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "failed",
                "removal_count": -4,
                "removed_ms": "not-an-integer",
                "error": {
                    "code": "transcription_unavailable",
                    "retryable": False,
                    "message": "private provider timeout body",
                },
            },
        },
    )

    projection = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=job,
    )

    assert projection is not None
    assert projection["analysis"]["category_counts"] == {
        "filler_sounds": 1,
        "long_pauses": 2,
    }
    assert projection["outcome"] == {
        "job_id": str(job_id),
        "render_generation_id": generation,
        "status": "failed",
        "removal_count": 0,
        "removed_ms": 0,
        "error": {"code": "transcription_unavailable", "retryable": True},
    }
    serialized = json.dumps(projection)
    assert "raw upstream response" not in serialized
    assert "private provider timeout body" not in serialized
    assert "provider_diagnostic" not in serialized


def test_stale_failed_job_outcome_is_suppressed_for_replacement_analysis() -> None:
    item_id = uuid.uuid4()
    old_row = _analysis(item_id)
    old_row.decision = "clean"
    replacement = _analysis(item_id)
    replacement.source_media_identity = "media-replacement"
    replacement.source_storage_path = "users/private/replacement.mp4"
    replacement.source_generation = "replacement-generation"
    replacement.source_policy_fingerprint = "replacement-fingerprint"
    job_id = uuid.uuid4()
    generation = "failed-generation"
    failed_job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "creator_generation_id": generation,
            "_speech_cleanup_internal": {"preflight_snapshot": _outcome_snapshot(old_row)},
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "failed",
                "removal_count": 0,
                "removed_ms": 0,
                "error": {"code": "snapshot_mismatch", "retryable": False},
            },
        },
    )

    projection = public_projection(
        replacement,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=failed_job,
    )

    assert projection is not None
    assert projection["analysis"]["id"] == str(replacement.id)
    assert projection["requires_choice"] is True
    assert projection["outcome"] is None


def test_reset_analysis_hides_old_failed_outcome_until_new_decision() -> None:
    item_id = uuid.uuid4()
    row = _analysis(item_id, status="ready", candidate_count=2)
    row.decision = "clean"
    failed_snapshot = _outcome_snapshot(row)
    row.status = "queued"
    row.analysis_payload = None
    row.candidate_count = None
    row.decision = None
    job_id = uuid.uuid4()
    generation = "failed-generation"
    failed_job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "creator_generation_id": generation,
            "speech_cleanup_contract": "required_v1",
            "_speech_cleanup_internal": {"preflight_snapshot": failed_snapshot},
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "failed",
                "removal_count": 0,
                "removed_ms": 0,
                "error": {"code": "snapshot_mismatch", "retryable": False},
            },
        },
    )

    checking = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=failed_job,
    )
    assert checking is not None
    assert checking["analysis"]["status"] == "queued"
    assert checking["outcome"] is None

    row.status = "ready"
    # Even deterministic reanalysis can reproduce byte-identical evidence. The
    # reset cleared consent, so the old Job's mismatch receipt must stay hidden
    # while the newly-ready row asks for a fresh decision.
    row.analysis_payload = failed_snapshot["analysis"]
    row.candidate_count = 2
    ready = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=failed_job,
    )
    assert ready is not None
    assert ready["requires_choice"] is True
    assert ready["outcome"] is None

    row.status = "no_findings"
    row.analysis_payload = {"fresh": True}
    row.candidate_count = 0
    no_findings = public_projection(
        row,
        applicable=True,
        unavailable_reason=None,
        video_present=True,
        active_job=failed_job,
    )
    assert no_findings is not None
    assert no_findings["requires_choice"] is False
    assert no_findings["outcome"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_reason", "source_changed", "valid_snapshot"),
    [
        ("apply_failed", True, True),
        ("snapshot_mismatch", False, True),
        ("snapshot_mismatch", False, False),
    ],
)
async def test_stale_cleanup_failure_detaches_old_job_and_reopens_current_plan(
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str,
    source_changed: bool,
    valid_snapshot: bool,
) -> None:
    owner_id = uuid.uuid4()
    plan_id, item_id, job_id, session_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    old_source = SimpleNamespace(
        source_kind="embedded_spine",
        media_id="old-media",
        storage_path="users/private/old.mp4",
        generation="old-generation",
        source_policy_fingerprint="old-fingerprint",
    )
    current_source = (
        SimpleNamespace(
            source_kind="embedded_spine",
            media_id="new-media",
            storage_path="users/private/new.mp4",
            generation="new-generation",
            source_policy_fingerprint="new-fingerprint",
        )
        if source_changed
        else old_source
    )
    plan = SimpleNamespace(
        id=plan_id,
        user_id=owner_id,
        persona_id=uuid.uuid4(),
        ownership_epoch=5,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=plan.persona_id, user_id=owner_id)
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
        edit_format="subtitled",
        speech_cleanup_enabled=True,
        speech_cleanup_notice={"accepted": True},
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        state={
            "edit_format": "subtitled",
            "generation": {"job_id": str(job_id), "status": "failed"},
            "selected_variant_id": "old-variant",
        },
    )
    strategy = SimpleNamespace(render_program="native", edit_format="subtitled")
    session = SimpleNamespace(
        id=session_id,
        creator_id=owner_id,
        plan_item_id=item_id,
        ownership_epoch=5,
        target_job_id=job_id,
        target_variant_id="old-variant",
        target_generation_id="old-generation",
        status="failed",
        revision=4,
        active_plan={
            "version": 1,
            "summary": "Keep the delivery crisp.",
            "creator_request": "Make this concise",
            "edit_plan": {"typed": True},
        },
        manifest_hash="old-manifest",
        max_render_attempts=2,
        render_attempts=2,
        last_review={"old": True},
        last_good={"old": True},
        last_error={"code": "speech_cleanup_failed"},
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=owner_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=5,
        status="processing_failed",
        failure_reason="speech_cleanup_failed",
        assembly_plan={
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "speech_cleanup_failure_reason": failure_reason,
            "_speech_cleanup_internal": {"preflight_snapshot": {"private": True}},
        },
    )
    db = Mock(spec=AsyncSession)

    async def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        return {
            (PlanItem, item_id): item,
            (ContentPlan, plan_id): plan,
            (Persona, persona.id): persona,
            (Job, job_id): job,
            (CreatorAgentSession, session_id): session,
        }.get((model, identifier))

    db.get = AsyncMock(side_effect=get)
    fresh_analysis_id = uuid.uuid4()
    schedule = AsyncMock(return_value=fresh_analysis_id)
    sync_agent = AsyncMock()
    append_creator_event = AsyncMock()
    manifest = SimpleNamespace(manifest_hash="new-manifest")
    monkeypatch.setattr(routes, "_ensure_speech_cleanup_preflight", schedule)
    monkeypatch.setattr(routes, "_sync_agent", sync_agent)
    if valid_snapshot:
        monkeypatch.setattr(
            "app.pipeline.speech_cleanup_apply.hydrate_speech_cleanup_snapshot",
            lambda _raw: SimpleNamespace(
                source_kind=old_source.source_kind,
                media_identity=old_source.media_id,
                storage_path=old_source.storage_path,
                generation=old_source.generation,
                source_policy_fingerprint=old_source.source_policy_fingerprint,
            ),
        )
    else:
        from app.pipeline.speech_cleanup_apply import SpeechCleanupSnapshotError

        monkeypatch.setattr(
            "app.pipeline.speech_cleanup_apply.hydrate_speech_cleanup_snapshot",
            Mock(side_effect=SpeechCleanupSnapshotError("missing snapshot")),
        )
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(source=current_source),
    )
    monkeypatch.setattr(
        "app.agents._schemas.creator_agent.CreatorEditPlan.model_validate",
        lambda _raw: SimpleNamespace(strategy=strategy),
    )
    normalize = Mock(return_value=strategy)
    monkeypatch.setattr(
        "app.agents._schemas.creator_policy.normalize_creator_strategy_media",
        normalize,
    )
    monkeypatch.setattr(
        "app.services.creator_sessions.resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(
        "app.services.creator_sessions.compile_active_plan",
        Mock(
            return_value={
                "version": 2,
                "summary": "Keep the delivery crisp.",
                "plan_hash": "b" * 64,
                "edit_plan": {"fresh": True},
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.creator_sessions.append_event",
        append_creator_event,
    )

    repaired, publish_id = await routes._repair_stale_cleanup_failure_graph(
        db,
        thread,
        SimpleNamespace(id=owner_id),
    )

    assert repaired is True
    assert publish_id == fresh_analysis_id
    assert item.current_job_id is None
    assert thread.active_job_id is None
    assert "generation" not in thread.state
    assert "selected_variant_id" not in thread.state
    assert session.target_job_id is None
    assert session.target_variant_id is None
    assert session.target_generation_id is None
    assert session.status == "awaiting_confirmation"
    assert session.manifest_hash == "new-manifest"
    assert session.active_plan["plan_hash"] == "b" * 64
    assert session.max_render_attempts == 3
    assert session.last_error is None
    assert job.id == job_id
    assert job.status == "processing_failed"  # retained as immutable audit history
    schedule.assert_awaited_once_with(db, item)
    append_creator_event.assert_awaited_once()
    sync_agent.assert_awaited_once_with(db, thread)


@pytest.mark.asyncio
async def test_stale_cleanup_failure_unsafe_recompile_falls_back_to_briefing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = uuid.uuid4()
    plan_id, item_id, job_id, session_id = [uuid.uuid4() for _ in range(4)]
    plan = SimpleNamespace(
        id=plan_id,
        user_id=owner_id,
        persona_id=uuid.uuid4(),
        ownership_epoch=1,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=plan.persona_id, user_id=owner_id)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        state={"generation": {"status": "failed"}},
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=owner_id,
        plan_item_id=item_id,
        ownership_epoch=1,
        target_job_id=job_id,
        target_variant_id=None,
        target_generation_id="generation",
        status="failed",
        active_plan={"edit_plan": {"unsafe": True}},
        manifest_hash="old",
        last_review=None,
        last_good=None,
        last_error={"old": True},
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=owner_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=1,
        status="processing_failed",
        failure_reason="speech_cleanup_failed",
        assembly_plan={
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "speech_cleanup_failure_reason": "snapshot_mismatch",
            "_speech_cleanup_internal": {"preflight_snapshot": {}},
        },
    )
    db = Mock(spec=AsyncSession)

    async def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        return {
            (PlanItem, item_id): item,
            (ContentPlan, plan_id): plan,
            (Persona, persona.id): persona,
            (Job, job_id): job,
            (CreatorAgentSession, session_id): session,
        }.get((model, identifier))

    db.get = AsyncMock(side_effect=get)
    monkeypatch.setattr(
        "app.pipeline.speech_cleanup_apply.hydrate_speech_cleanup_snapshot",
        lambda _raw: SimpleNamespace(
            source_kind="embedded_spine",
            media_identity="media",
            storage_path="path",
            generation="generation",
            source_policy_fingerprint="fingerprint",
        ),
    )
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(source=None),
    )
    monkeypatch.setattr(
        "app.agents._schemas.creator_agent.CreatorEditPlan.model_validate",
        Mock(side_effect=ValueError("removed cadence source")),
    )
    append_creator_event = AsyncMock()
    monkeypatch.setattr("app.services.creator_sessions.append_event", append_creator_event)
    monkeypatch.setattr(routes, "_ensure_speech_cleanup_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())

    repaired, publish_id = await routes._repair_stale_cleanup_failure_graph(
        db,
        thread,
        SimpleNamespace(id=owner_id),
    )

    assert repaired is True
    assert publish_id is None
    assert thread.active_job_id is None
    assert item.current_job_id is None
    assert session.target_job_id is None
    assert session.status == "briefing"
    assert session.active_plan is None
    assert session.last_error["code"] == "source_changed_replan_required"
    assert append_creator_event.await_args.kwargs["event_type"] == "assistant_question"


@pytest.mark.asyncio
async def test_get_thread_commits_stale_cleanup_repair_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = SimpleNamespace(id=uuid.uuid4(), active_creator_agent_session_id=None)
    analysis_id = uuid.uuid4()
    db = Mock(spec=AsyncSession)
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    async def publish(_callable: object, value: object) -> bool:
        assert value == analysis_id
        assert order == ["commit"]
        order.append("publish")
        return True

    db.commit = AsyncMock(side_effect=commit)
    db.refresh = AsyncMock()
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes,
        "_repair_stale_cleanup_failure_graph",
        AsyncMock(return_value=(True, analysis_id)),
    )
    monkeypatch.setattr(
        routes,
        "_repair_missing_thread_job_projection",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(routes, "_sync_render_projection", AsyncMock(return_value=False))
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes.asyncio, "to_thread", publish)

    response = await routes.get_thread(
        str(thread.id),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    assert response is thread
    assert order == ["commit", "publish"]


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight_mode", ["enforce", "off"])
async def test_creation_detail_never_serializes_private_analysis_or_job_namespace(
    monkeypatch: pytest.MonkeyPatch,
    preflight_mode: str,
) -> None:
    owner_id = uuid.uuid4()
    plan_id, item_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    generation = "generation-current"
    row = _analysis(item_id)
    row.decision = "clean"
    row.decision_at = datetime.now(UTC)
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
        clip_gcs_paths=["users/private/talking.mp4"],
        clip_assignments=[],
        edit_format="subtitled",
        audio_mode="kria",
        voiceover_gcs_path=None,
    )
    plan = SimpleNamespace(id=plan_id, user_id=owner_id, ownership_epoch=0)
    job = SimpleNamespace(
        id=job_id,
        user_id=owner_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        status="done",
        current_phase=None,
        failure_reason=None,
        assembly_plan={
            "creator_generation_id": generation,
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "variants": [],
            "_speech_cleanup_internal": {
                "preflight_snapshot": {
                    **_outcome_snapshot(row),
                    "analysis": row.analysis_payload,
                }
            },
            "speech_cleanup_outcome": {
                "job_id": str(job_id),
                "render_generation_id": generation,
                "status": "applied",
                "removal_count": 2,
                "removed_ms": 400,
            },
        },
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=None,
        active_job_id=job_id,
        status="active",
        revision=3,
        state={"media_count": 1},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db = Mock(spec=AsyncSession)

    async def get(model: object, _identifier: object, **_kwargs: object) -> object | None:
        return {PlanItem: item, ContentPlan: plan, Job: job}.get(model)

    count_result = Mock()
    count_result.scalar_one.return_value = 0
    events_result = Mock()
    events_result.scalars.return_value.all.return_value = []
    db.get = AsyncMock(side_effect=get)
    db.execute = AsyncMock(side_effect=[count_result, events_result])
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", preflight_mode)
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="private-fingerprint"),
            reason=None,
            video_present=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.preflight_enabled_for_source",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("app.routes.generative_jobs._variants_for_response", lambda _job: [])

    response = await routes._response(db, thread)
    serialized = response.model_dump_json()

    assert response.speech_cleanup is not None
    assert response.speech_cleanup["applicable"] is True
    assert response.speech_cleanup["outcome"]["status"] == "applied"
    for private_value in (
        "_speech_cleanup_internal",
        "preflight_snapshot",
        "private transcript",
        "users/private",
        "secret-generation",
        "private-fingerprint",
        "provider_error",
        "private_extra",
    ):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_thread_list_omits_cleanup_without_analysis_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        status="active",
        revision=1,
        state={"media_count": 1},
        content_plan_id=uuid.uuid4(),
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=None,
        active_job_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [row]
    db = Mock()
    db.execute = AsyncMock(return_value=result)

    summaries = await routes.list_threads(
        SimpleNamespace(id=owner_id, email="u@example.com"), db, limit=20
    )

    assert len(summaries) == 1
    assert summaries[0].speech_cleanup is None
    assert summaries[0].job is None
    assert summaries[0].events == []
    db.execute.assert_awaited_once()


def _action_graph() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    item_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        status="active",
        revision=4,
        state={"edit_format": "subtitled", "media_count": 1},
        content_plan_id=uuid.uuid4(),
        active_plan_item_id=item_id,
        active_creator_agent_session_id=uuid.uuid4(),
        active_job_id=None,
    )
    session = SimpleNamespace(
        id=thread.active_creator_agent_session_id,
        creator_id=thread.creator_id,
        plan_item_id=item_id,
        target_job_id=None,
        status="awaiting_confirmation",
        revision=2,
        active_plan={
            "edit_format": "subtitled",
            "version": 1,
            "plan_hash": "a" * 64,
        },
    )
    item = SimpleNamespace(id=item_id)
    return thread, session, item


@pytest.mark.asyncio
async def test_format_selection_schedules_preflight_without_any_chat_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread, _session, item = _action_graph()
    thread.state = {"media_count": 1}
    item.voiceover_gcs_path = None
    item.audio_mode = "kria"
    analysis_id = uuid.uuid4()
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    async def to_thread(_callable: object, identifier: object) -> bool:
        assert order == ["commit"]
        assert identifier == analysis_id
        order.append("publish")
        return True

    db.commit.side_effect = commit
    mutate = Mock()
    schedule = AsyncMock(return_value=analysis_id)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_reject_input_mutation_while_rendering", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_ensure_speech_cleanup_preflight", schedule)
    monkeypatch.setattr("app.services.plan_item_media.mutate_plan_item_media", mutate)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(routes.asyncio, "to_thread", to_thread)

    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="select_format",
            payload={"format": "talking_to_camera"},
            client_action_id="select-format-no-prose",
            expected_revision=4,
        ),
        SimpleNamespace(id=thread.creator_id),
        db,
    )

    assert "intent" not in thread.state
    assert thread.state["edit_format"] == "subtitled"
    schedule.assert_awaited_once_with(db, item)
    assert mutate.call_args.kwargs["edit_format"] == "subtitled"
    assert mutate.call_args.kwargs["audio_mode"] == "original"
    assert order == ["commit", "publish"]


async def _run_generate_action(
    monkeypatch: pytest.MonkeyPatch,
    *,
    analysis: SimpleNamespace | None,
    payload: dict[str, object],
    action: str = "generate",
) -> tuple[object, AsyncMock]:
    thread, session, item = _action_graph()
    if analysis is not None:
        analysis.plan_item_id = item.id
    result_job_id = uuid.uuid4()
    db = Mock()

    async def get(model: object, _identifier: object, **_kwargs: object) -> object | None:
        if model is CreatorAgentSession:
            return session
        if model is PlanItem:
            return item
        return None

    db.get = AsyncMock(side_effect=get)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    controller = AsyncMock(
        return_value=SimpleNamespace(id=str(session.id), current_job_id=str(result_job_id))
    )
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", controller)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=analysis),
    )
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="private-fingerprint"),
            reason=None,
            video_present=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.preflight_enabled_for_source",
        lambda *_args, **_kwargs: True,
    )
    action_payload = (
        {
            "session_revision": 2,
            "plan_version": 1,
            "plan_hash": "a" * 64,
            **payload,
        }
        if action == "generate"
        else payload
    )
    body = routes.ActionBody(
        action=action,
        payload=action_payload,
        client_action_id="generate-with-cleanup",
        expected_revision=4,
    )
    output = await routes.action_thread(
        _request(), str(thread.id), body, SimpleNamespace(id=thread.creator_id), db
    )
    return output, controller


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["clean", "keep_original"])
async def test_findings_choice_is_forwarded_atomically_with_generate(
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
) -> None:
    row = _analysis(uuid.uuid4(), status="ready", candidate_count=2)

    output, controller = await _run_generate_action(
        monkeypatch,
        analysis=row,
        payload={
            "speech_cleanup_analysis_id": str(row.id),
            "speech_cleanup_choice": choice,
        },
    )

    assert output.active_job_id is not None
    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice == choice


@pytest.mark.asyncio
async def test_no_findings_generate_requires_current_id_and_omitted_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _analysis(uuid.uuid4(), status="no_findings", candidate_count=0)

    _output, controller = await _run_generate_action(
        monkeypatch,
        analysis=row,
        payload={"speech_cleanup_analysis_id": str(row.id)},
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice is None


@pytest.mark.asyncio
async def test_create_without_cleanup_is_a_distinct_recovery_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _analysis(uuid.uuid4(), status="failed", candidate_count=None)

    _output, controller = await _run_generate_action(
        monkeypatch,
        analysis=row,
        payload={"speech_cleanup_analysis_id": str(row.id)},
        action="create_without_cleanup",
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id == row.id
    assert confirmation.speech_cleanup_choice == "create_without_cleanup"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "action",
        "expected_recovery",
        "failure_reason",
        "receipt_status",
        "expected_session_status",
    ),
    [
        ("retry", "retry_required", "apply_failed", None, "awaiting_confirmation"),
        ("retry", "retry_required", "apply_failed", "succeeded", "awaiting_feedback"),
        (
            "create_without_cleanup",
            "disable_and_create",
            "apply_failed",
            None,
            "awaiting_confirmation",
        ),
        (
            "create_without_cleanup",
            "disable_and_create",
            "snapshot_mismatch",
            None,
            "awaiting_confirmation",
        ),
    ],
)
async def test_cleanup_application_recovery_is_pinned_to_failed_job_and_reaches_controller(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_recovery: str,
    failure_reason: str,
    receipt_status: str | None,
    expected_session_status: str,
) -> None:
    thread, session, item = _action_graph()
    analysis_id = uuid.uuid4()
    job_id = uuid.uuid4()
    generation = "failed-render-generation"
    thread.active_job_id = job_id
    session.status = "failed"
    session.render_attempts = 1
    session.max_render_attempts = 3
    failed_job = SimpleNamespace(
        id=job_id,
        user_id=thread.creator_id,
        content_plan_item_id=item.id,
        status="variants_failed",
        failure_reason="speech_cleanup_failed",
        assembly_plan={
            "creator_generation_id": generation,
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "speech_cleanup_failure_reason": failure_reason,
            "variants": [
                {
                    "error_class": "speech_cleanup_failed",
                    "speech_cleanup_failure_reason": failure_reason,
                }
            ],
            "_speech_cleanup_internal": {"preflight_snapshot": {"analysis_id": str(analysis_id)}},
        },
    )
    new_job_id = uuid.uuid4()
    if receipt_status == "succeeded":
        # Creator committed the replacement Job/receipt, then the outer thread
        # projection/event transaction crashed. The replay still derives its
        # private digest from the old thread Job.
        item.current_job_id = new_job_id
    db = Mock()

    async def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        if model is CreatorAgentSession:
            return session
        if model is Job and identifier == job_id:
            return failed_job
        if model is PlanItem:
            return item
        return None

    db.get = AsyncMock(side_effect=get)
    receipt_result = Mock()
    receipt_result.scalar_one_or_none.return_value = receipt_status
    db.execute = AsyncMock(return_value=receipt_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    controller = AsyncMock(
        return_value=SimpleNamespace(id=str(session.id), current_job_id=str(new_job_id))
    )
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    repair_projection = AsyncMock(
        side_effect=AssertionError("cleanup receipt replay must precede projection repair")
    )
    monkeypatch.setattr(routes, "_repair_missing_thread_job_projection", repair_projection)
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    async def reconcile(_db: object, _session: object) -> None:
        if receipt_status == "succeeded":
            session.status = "awaiting_feedback"

    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock(side_effect=reconcile))
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", controller)

    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action=action,
            payload={"speech_cleanup_analysis_id": str(analysis_id)},
            client_action_id=f"cleanup-recovery:{thread.revision}:{action}",
            expected_revision=thread.revision,
        ),
        SimpleNamespace(id=thread.creator_id),
        db,
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id is None
    assert confirmation.speech_cleanup_choice is None
    assert controller.await_args.kwargs == {
        "allow_chat": True,
        "speech_cleanup_recovery_action": expected_recovery,
        "speech_cleanup_recovery_job_id": job_id,
        "speech_cleanup_recovery_generation_id": generation,
        "speech_cleanup_recovery_analysis_id": analysis_id,
    }
    routes.reconcile_render_state.assert_awaited_once_with(db, session)
    repair_projection.assert_not_awaited()
    assert session.status == expected_session_status


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", ["required_v1", "bypass"])
async def test_publish_failure_retry_preserves_exact_preflight_contract(
    monkeypatch: pytest.MonkeyPatch,
    contract: str,
) -> None:
    thread, session, item = _action_graph()
    analysis_id = uuid.uuid4()
    job_id = uuid.uuid4()
    new_job_id = uuid.uuid4()
    generation = "publish-failed-generation"
    thread.active_job_id = job_id
    session.status = "failed"
    session.render_attempts = 2
    session.max_render_attempts = 3
    assembly_plan: dict[str, object] = {
        "creator_generation_id": generation,
        "speech_cleanup_contract": "required_v1" if contract == "required_v1" else "off_v1",
    }
    if contract == "required_v1":
        assembly_plan.update(
            {
                "speech_cleanup_preflight_contract": "snapshot_v1",
                "_speech_cleanup_internal": {
                    "preflight_snapshot": {"analysis_id": str(analysis_id)}
                },
            }
        )
    else:
        assembly_plan.update(
            {
                "_speech_cleanup_internal": {"outcome_analysis_id": str(analysis_id)},
                "speech_cleanup_outcome": {
                    "status": "bypassed_unchecked",
                    "removal_count": 0,
                    "removed_ms": 0,
                    "job_id": str(job_id),
                    "render_generation_id": generation,
                },
            }
        )
    failed_job = SimpleNamespace(
        id=job_id,
        user_id=thread.creator_id,
        content_plan_item_id=item.id,
        status="processing_failed",
        failure_reason="dispatch_publish_failed",
        assembly_plan=assembly_plan,
    )
    db = Mock()

    async def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        if model is CreatorAgentSession:
            return session
        if model is Job and identifier == job_id:
            return failed_job
        if model is PlanItem:
            return item
        return None

    db.get = AsyncMock(side_effect=get)
    receipt_result = Mock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=receipt_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    controller = AsyncMock(
        return_value=SimpleNamespace(id=str(session.id), current_job_id=str(new_job_id))
    )
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_repair_missing_thread_job_projection", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock())
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", controller)

    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="retry",
            payload={},
            client_action_id=f"publish-retry:{thread.revision}:{contract}",
            expected_revision=thread.revision,
        ),
        SimpleNamespace(id=thread.creator_id),
        db,
    )

    confirmation = controller.await_args.args[1]
    assert confirmation.speech_cleanup_analysis_id is None
    assert confirmation.speech_cleanup_choice is None
    assert controller.await_args.kwargs == {
        "allow_chat": True,
        "speech_cleanup_recovery_action": "retry_preflight_dispatch",
        "speech_cleanup_recovery_job_id": job_id,
        "speech_cleanup_recovery_generation_id": generation,
        "speech_cleanup_recovery_analysis_id": analysis_id,
    }
    routes.reconcile_render_state.assert_awaited_once_with(db, session)
    assert session.status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_generic_publish_failure_retries_without_inventing_cleanup_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread, session, item = _action_graph()
    job_id = uuid.uuid4()
    thread.active_job_id = job_id
    session.status = "failed"
    session.render_attempts = 1
    session.max_render_attempts = 3
    new_job_id = uuid.uuid4()
    failed_job = SimpleNamespace(
        id=job_id,
        user_id=thread.creator_id,
        content_plan_item_id=item.id,
        status="processing_failed",
        failure_reason="dispatch_publish_failed",
        assembly_plan={"creator_generation_id": "generation-without-cleanup-evidence"},
    )
    db = Mock()

    async def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        if model is CreatorAgentSession:
            return session
        if model is Job and identifier == job_id:
            return failed_job
        return None

    db.get = AsyncMock(side_effect=get)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    controller = AsyncMock(
        return_value=SimpleNamespace(id=str(session.id), current_job_id=str(new_job_id))
    )
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_repair_missing_thread_job_projection", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock())
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", controller)

    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="retry",
            payload={},
            client_action_id="publish-retry-without-evidence",
            expected_revision=thread.revision,
        ),
        SimpleNamespace(id=thread.creator_id),
        db,
    )

    assert controller.await_args.kwargs == {
        "allow_chat": True,
        "speech_cleanup_recovery_action": None,
        "speech_cleanup_recovery_job_id": None,
        "speech_cleanup_recovery_generation_id": None,
        "speech_cleanup_recovery_analysis_id": None,
    }
    routes.reconcile_render_state.assert_awaited_once_with(db, session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "count", "analysis_id", "choice", "detail"),
    [
        ("queued", None, "current", "clean", "speech_cleanup_pending"),
        ("running", None, "current", "clean", "speech_cleanup_pending"),
        ("failed", None, "current", None, "speech_cleanup_failed"),
        ("ready", 2, "current", None, "speech_cleanup_choice_required"),
        ("no_findings", 0, "current", "clean", "speech_cleanup_choice_not_allowed"),
        ("ready", 2, "stale", "clean", "speech_cleanup_analysis_changed"),
    ],
)
async def test_generate_rejects_noncurrent_or_unresolved_cleanup_action(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    count: int | None,
    analysis_id: str,
    choice: str | None,
    detail: str,
) -> None:
    row = _analysis(uuid.uuid4(), status=status, candidate_count=count)
    supplied = str(row.id) if analysis_id == "current" else str(uuid.uuid4())
    payload: dict[str, object] = {"speech_cleanup_analysis_id": supplied}
    if choice is not None:
        payload["speech_cleanup_choice"] = choice

    with pytest.raises(HTTPException) as exc_info:
        await _run_generate_action(monkeypatch, analysis=row, payload=payload)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == detail


@pytest.mark.asyncio
async def test_enforced_source_without_current_analysis_remains_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _run_generate_action(monkeypatch, analysis=None, payload={})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "speech_cleanup_pending"


@pytest.mark.asyncio
async def test_retry_speech_check_requeues_current_retryable_analysis_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread, _session, item = _action_graph()
    row = _analysis(item.id, status="failed", candidate_count=None)
    row.failure_retryable = True
    db = Mock()
    db.get = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    order: list[str] = []

    async def commit() -> None:
        assert row.status == "queued"
        order.append("commit")

    async def to_thread(_callable: object, analysis_id: object) -> bool:
        assert order == ["commit"]
        assert analysis_id == row.id
        order.append("publish")
        return True

    db.commit.side_effect = commit
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes.asyncio, "to_thread", to_thread)

    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="retry_speech_cleanup",
            payload={"speech_cleanup_analysis_id": str(row.id)},
            client_action_id="retry-speech-check",
            expected_revision=4,
        ),
        SimpleNamespace(id=thread.creator_id),
        db,
    )

    assert order == ["commit", "publish"]
    assert row.status == "queued"
    assert row.failure_code is None
    assert row.failure_retryable is None


@pytest.mark.asyncio
async def test_retry_speech_check_reports_only_cleanup_staleness_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread, _session, item = _action_graph()
    stale = _analysis(uuid.uuid4(), status="failed", candidate_count=None)
    stale.failure_retryable = True
    db = Mock()
    db.get = AsyncMock(return_value=stale)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc_info:
        await routes.action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="retry_speech_cleanup",
                payload={"speech_cleanup_analysis_id": str(stale.id)},
                client_action_id="retry-stale-speech-check",
                expected_revision=4,
            ),
            SimpleNamespace(id=thread.creator_id),
            db,
        )

    assert stale.plan_item_id != item.id
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "speech_cleanup_analysis_changed"


@pytest.mark.asyncio
async def test_cleanup_action_is_revision_fenced_before_analysis_or_job_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread, _session, _item = _action_graph()
    db = Mock()
    db.get = AsyncMock()
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    controller = AsyncMock()
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", controller)

    with pytest.raises(HTTPException) as exc_info:
        await routes.action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="generate",
                payload={},
                client_action_id="stale-revision",
                expected_revision=3,
            ),
            SimpleNamespace(id=thread.creator_id),
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Creation thread changed"
    db.get.assert_not_awaited()
    controller.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight_mode", ["off", "shadow", "enforce"])
async def test_stamped_required_v1_without_analysis_row_projects_nothing(
    monkeypatch: pytest.MonkeyPatch, preflight_mode: str
) -> None:
    """A stamped required_v1 Job with no analysis row must never surface a card.

    ``applicable`` has two routes: the rollout cohort, and a Job already stamped
    ``required_v1``. The second route carries no analysis, so the card renders
    its "Checking for filler sounds…" spinner with nothing able to resolve it —
    and while the rollout percent is 0 no row can be minted at all, so the
    spinner is permanent and replaces the confirm/retry CTA.

    The carve-out that suppresses this used to be keyed on the global mode, so
    merely flipping to ``enforce`` (even at percent 0, which changes nothing
    else) stranded every legacy required_v1 Job. It is keyed on cohort
    membership instead: an item genuinely in the cohort still gets its card.
    """

    owner_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    job_id = uuid.uuid4()

    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
        edit_format="subtitled",
        audio_mode="original",
        clip_gcs_paths=["users/u/a.mp4"],
        clip_assignments=[{"gcs_path": "users/u/a.mp4", "media_id": "m0"}],
        voiceover_gcs_path=None,
        speech_cleanup_enabled=True,
        speech_cleanup_notice=None,
    )
    plan = SimpleNamespace(id=plan_id, user_id=owner_id, ownership_epoch=0)
    job = SimpleNamespace(
        id=job_id,
        user_id=owner_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        # The unrecoverable shape: a FAILED job still holds the confirmation
        # artifact, so the spinner would replace the retry affordance.
        status="variants_failed",
        current_phase=None,
        failure_reason="speech_cleanup_failed",
        assembly_plan={"speech_cleanup_contract": "required_v1", "variants": []},
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=None,
        active_job_id=job_id,
        status="active",
        revision=3,
        state={"media_count": 1},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    db = Mock(spec=AsyncSession)

    async def get(model: object, _identifier: object, **_kwargs: object) -> object | None:
        return {PlanItem: item, ContentPlan: plan, Job: job}.get(model)

    count_result = Mock()
    count_result.scalar_one.return_value = 0
    events_result = Mock()
    events_result.scalars.return_value.all.return_value = []
    db.get = AsyncMock(side_effect=get)
    db.execute = AsyncMock(side_effect=[count_result, events_result])

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", preflight_mode)
    # Percent 0 is the production value: no analysis row can exist.
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 0)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="private-fingerprint"),
            reason=None,
            video_present=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("app.routes.generative_jobs._variants_for_response", lambda _job: [])

    response = await routes._response(db, thread)

    assert response.speech_cleanup is None, (
        f"mode={preflight_mode} at percent 0 surfaced an unresolvable card: "
        f"{response.speech_cleanup}"
    )
