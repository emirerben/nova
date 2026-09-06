"""Regression tests for durable preflight identity reactivation and fencing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.models import ContentPlan, Job, Persona, PlanItem, SpeechCleanupAnalysis
from app.services.active_narration_source import (
    ActiveNarrationResolution,
    ActiveNarrationSource,
)
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    SPEECH_CLEANUP_MAX_ATTEMPTS,
    SPEECH_CLEANUP_PAYLOAD_VERSION,
    analysis_snapshot,
    claim_analysis,
    ensure_current_analysis_async,
    ensure_current_analysis_sync,
    prepare_snapshot_mismatch_reanalysis,
    retry_failed_analysis,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION


def _source(fingerprint: str, *, media_id: str) -> ActiveNarrationSource:
    return ActiveNarrationSource(
        source_kind="voiceover",
        media_id=media_id,
        storage_path=f"users/u/{media_id}.wav",
        generation=f"generation-{media_id}",
        media_kind="audio",
        manifest_identity=media_id,
        window_start_s=0.0,
        window_end_s=10.0,
        edit_format="narrated",
        audio_mode="voiceover",
        resolved_renderer="narrated",
        detector_policy="policy-v1",
        source_policy_fingerprint=fingerprint,
    )


def _no_findings_payload(source: ActiveNarrationSource) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_fingerprint": source.source_policy_fingerprint,
        "detector_version": DETECTOR_VERSION,
        "source_window_start_s": source.window_start_s,
        "source_window_end_s": source.window_end_s,
        "language": "en",
        "timed_words": [],
        "cut_plan": {
            "keep_segments": [{"start_s": 0.0, "end_s": 10.0}],
            "removed": [],
            "time_saved_s": 0.0,
            "version": 2,
            "bailout_reason": None,
            "clamped": False,
        },
        "findings": [],
        "safety_signals": {
            "transcript_low_confidence": False,
            "silence_detection_status": "ok",
            "selected_plan": "baseline",
            "candidate_status": "not_run",
            "bailout_reason": None,
            "clamped": False,
        },
        "diagnostics": {},
        "public_receipt": {
            "candidate_count": 0,
            "category_counts": {
                "filler_sounds": 0,
                "long_pauses": 0,
                "retakes": 0,
            },
            "estimated_removed_ms": 0,
        },
    }


def _analysis(
    source: ActiveNarrationSource,
    *,
    status: str,
    superseded_at: datetime | None,
) -> SpeechCleanupAnalysis:
    row = SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        source_kind=source.source_kind,
        source_media_identity=source.media_id,
        source_storage_path=source.storage_path,
        source_generation=source.generation,
        window_start_s=source.window_start_s,
        window_end_s=source.window_end_s,
        source_policy_fingerprint=source.source_policy_fingerprint,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        analysis_payload_version=SPEECH_CLEANUP_PAYLOAD_VERSION,
        status=status,
        completed_at=(datetime.now(UTC) if status in {"ready", "no_findings", "failed"} else None),
        superseded_at=superseded_at,
    )
    return row


class _SyncDB:
    def __init__(self, rows: tuple[SpeechCleanupAnalysis, ...] = ()) -> None:
        self.rows = rows
        self.flushes: list[tuple[tuple[datetime | None, str, str | None], ...]] = []
        self.added: list[SpeechCleanupAnalysis] = []

    def add(self, row: SpeechCleanupAnalysis) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushes.append(
            tuple((row.superseded_at, row.status, row.attempt_token) for row in self.rows)
        )


class _AsyncDB(_SyncDB):
    async def flush(self) -> None:
        super().flush()


def _item(identifier: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        speech_cleanup_enabled=True,
        speech_cleanup_notice={"state": "accepted"},
    )


def test_a_b_a_reuses_valid_terminal_evidence_but_clears_old_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unique identity row for A is promoted only after B releases current."""

    source_a = _source("a" * 64, media_id="source-a")
    source_b = _source("b" * 64, media_id="source-b")
    item_id = uuid.uuid4()
    current_b = _analysis(source_b, status="no_findings", superseded_at=None)
    historical_a = _analysis(
        source_a,
        status="no_findings",
        superseded_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    current_b.plan_item_id = historical_a.plan_item_id = item_id
    historical_a.analysis_payload = _no_findings_payload(source_a)
    historical_a.candidate_count = 0
    historical_a.category_counts = {
        "filler_sounds": 0,
        "long_pauses": 0,
        "retakes": 0,
    }
    historical_a.estimated_removed_ms = 0
    historical_a.decision = "clean"
    historical_a.decision_at = datetime.now(UTC) - timedelta(minutes=2)
    db = _SyncDB((current_b, historical_a))
    item = _item(item_id)

    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_sync",
        lambda *_args, **_kwargs: current_b,
    )

    def historical(*_args, **_kwargs):
        # PostgreSQL's partial current key must have been released before A is
        # promoted. This catches a single-flush implementation whose UPDATE
        # ordering is undefined.
        assert db.flushes
        assert current_b.superseded_at is not None
        assert historical_a.superseded_at is not None
        return historical_a

    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight._exact_historical_analysis_sync",
        historical,
    )

    intent = ensure_current_analysis_sync(
        db,
        item,
        ActiveNarrationResolution(source=source_a, reason=None, video_present=True),
    )

    assert intent is not None
    assert intent.analysis_id == historical_a.id
    assert intent.created is False
    assert db.added == []
    assert current_b.superseded_at is not None
    assert historical_a.superseded_at is None
    assert historical_a.status == "no_findings"
    assert historical_a.analysis_payload == _no_findings_payload(source_a)
    assert historical_a.decision is None
    assert historical_a.decision_at is None
    assert item.speech_cleanup_enabled is False
    assert item.speech_cleanup_notice is None


@pytest.mark.asyncio
async def test_reactivated_running_row_fences_old_attempt_and_schedules_fresh_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a = _source("a" * 64, media_id="source-a")
    item_id = uuid.uuid4()
    historical_a = _analysis(
        source_a,
        status="running",
        superseded_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    historical_a.plan_item_id = item_id
    historical_a.attempt_token = "old-attempt-must-not-complete"
    historical_a.attempt_count = SPEECH_CLEANUP_MAX_ATTEMPTS - 1
    historical_a.dispatched_at = datetime.now(UTC) - timedelta(minutes=2)
    historical_a.started_at = datetime.now(UTC) - timedelta(minutes=2)
    historical_a.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    historical_a.decision = "keep_original"
    historical_a.decision_at = datetime.now(UTC) - timedelta(minutes=3)
    db = _AsyncDB((historical_a,))
    item = _item(item_id)

    async def no_current(*_args, **_kwargs):
        return None

    async def historical(*_args, **_kwargs):
        return historical_a

    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        no_current,
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight._exact_historical_analysis_async",
        historical,
    )

    intent = await ensure_current_analysis_async(
        db,
        item,
        ActiveNarrationResolution(source=source_a, reason=None, video_present=True),
    )

    assert intent is not None and intent.created is True
    assert intent.analysis_id == historical_a.id
    assert historical_a.superseded_at is None
    assert historical_a.status == "queued"
    assert historical_a.attempt_token is None
    assert historical_a.attempt_count == 0
    assert historical_a.dispatched_at is None
    assert historical_a.next_dispatch_at is not None
    assert historical_a.started_at is None
    assert historical_a.lease_expires_at is None
    assert historical_a.decision is None
    assert db.added == []


def test_corrupt_historical_terminal_payload_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a = _source("a" * 64, media_id="source-a")
    historical_a = _analysis(
        source_a,
        status="no_findings",
        superseded_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    historical_a.analysis_payload = _no_findings_payload(source_a)
    historical_a.analysis_payload["source_fingerprint"] = "c" * 64
    historical_a.candidate_count = 0
    historical_a.category_counts = {
        "filler_sounds": 0,
        "long_pauses": 0,
        "retakes": 0,
    }
    historical_a.estimated_removed_ms = 0
    item = _item(historical_a.plan_item_id)
    db = _SyncDB((historical_a,))

    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_sync",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight._exact_historical_analysis_sync",
        lambda *_args, **_kwargs: historical_a,
    )

    intent = ensure_current_analysis_sync(
        db,
        item,
        ActiveNarrationResolution(source=source_a, reason=None, video_present=True),
    )

    assert intent is not None and intent.created is True
    assert historical_a.status == "queued"
    assert historical_a.analysis_payload is None
    assert historical_a.candidate_count is None
    assert historical_a.category_counts is None
    assert historical_a.estimated_removed_ms is None


def test_manual_retry_renews_exhausted_attempt_budget_and_can_be_claimed() -> None:
    source = _source("a" * 64, media_id="source-a")
    row = _analysis(source, status="failed", superseded_at=None)
    row.failure_code = "analysis_timeout"
    row.failure_retryable = True
    row.attempt_count = SPEECH_CLEANUP_MAX_ATTEMPTS
    row.completed_at = datetime.now(UTC)
    row.analysis_payload = {"stale": "partial"}
    row.candidate_count = 2
    row.category_counts = {"filler_sounds": 2}
    row.estimated_removed_ms = 500
    row.diagnostic_receipt = {"stale": True}
    retry_at = datetime.now(UTC)

    assert retry_failed_analysis(row, now=retry_at) is True
    assert row.attempt_count == 0
    assert row.analysis_payload is None
    assert row.candidate_count is None
    assert row.category_counts is None
    assert row.estimated_removed_ms is None
    assert row.diagnostic_receipt is None

    class ClaimDB:
        def get(self, *_args, **_kwargs):
            return row

        def flush(self):
            return None

    claim = claim_analysis(ClaimDB(), row.id, now=retry_at)

    assert claim is not None
    assert claim.analysis_id == row.id
    assert row.status == "running"
    assert row.attempt_count == 1
    assert row.attempt_token == claim.attempt_token


def test_manual_retry_uses_server_taxonomy_not_mutable_retryable_column() -> None:
    source = _source("a" * 64, media_id="source-a")
    row = _analysis(source, status="failed", superseded_at=None)
    row.failure_code = "unsupported_media"
    # A drifted or tampered column must not turn a server-owned nonretryable
    # failure into a retry authorization.
    row.failure_retryable = True

    assert retry_failed_analysis(row) is False
    assert row.status == "failed"
    assert row.failure_code == "unsupported_media"


@pytest.mark.parametrize("valid_snapshot", [True, False])
def test_snapshot_mismatch_resets_exact_analysis_under_canonical_locks(
    monkeypatch: pytest.MonkeyPatch,
    valid_snapshot: bool,
) -> None:
    source = _source("a" * 64, media_id="source-a")
    owner_id = uuid.uuid4()
    persona = SimpleNamespace(id=uuid.uuid4(), user_id=owner_id)
    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        persona_id=persona.id,
        ownership_epoch=7,
        ownership_quarantined_at=None,
    )
    item = _item(uuid.uuid4())
    item.content_plan_id = plan.id
    item.current_job_id = uuid.uuid4()
    row = _analysis(source, status="ready", superseded_at=None)
    row.plan_item_id = item.id
    row.analysis_payload = _no_findings_payload(source)
    row.candidate_count = 1
    row.category_counts = {"filler_sounds": 1, "long_pauses": 0, "retakes": 0}
    row.estimated_removed_ms = 250
    row.decision = "clean"
    row.decision_at = datetime.now(UTC)
    snapshot = analysis_snapshot(row)
    job = SimpleNamespace(
        id=item.current_job_id,
        user_id=owner_id,
        content_plan_item_id=item.id,
        content_plan_ownership_epoch=7,
        mode="content_plan",
        status="processing",
        failure_reason=None,
        assembly_plan={
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "_speech_cleanup_internal": {
                "preflight_snapshot": snapshot if valid_snapshot else {"malformed": True}
            },
        },
    )

    class LockingDB:
        def __init__(self) -> None:
            self.lock_order: list[type[object]] = []

        def get(self, model: type[object], identifier: object, **kwargs: object) -> object | None:
            if kwargs.get("with_for_update"):
                self.lock_order.append(model)
            values = {
                (Job, job.id): job,
                (PlanItem, item.id): item,
                (ContentPlan, plan.id): plan,
                (Persona, persona.id): persona,
            }
            return values.get((model, identifier))

    db = LockingDB()
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_sync",
        lambda *_args, **_kwargs: row,
    )
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: ActiveNarrationResolution(
            source=source,
            reason=None,
            video_present=True,
        ),
    )

    reset = prepare_snapshot_mismatch_reanalysis(db, job.id)

    assert reset.job is job
    assert reset.analysis_id == row.id
    assert db.lock_order == [ContentPlan, Persona, PlanItem, Job]
    assert row.status == "queued"
    assert row.attempt_count == 0
    assert row.analysis_payload is None
    assert row.candidate_count is None
    assert row.decision is None
    assert row.next_dispatch_at is not None
    assert item.speech_cleanup_enabled is False
    assert item.speech_cleanup_notice is None


def test_snapshot_mismatch_never_resets_a_newer_current_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("a" * 64, media_id="source-a")
    owner_id = uuid.uuid4()
    persona = SimpleNamespace(id=uuid.uuid4(), user_id=owner_id)
    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        persona_id=persona.id,
        ownership_epoch=2,
        ownership_quarantined_at=None,
    )
    item = _item(uuid.uuid4())
    item.content_plan_id = plan.id
    item.current_job_id = uuid.uuid4()
    accepted = _analysis(source, status="ready", superseded_at=datetime.now(UTC))
    accepted.plan_item_id = item.id
    accepted.analysis_payload = _no_findings_payload(source)
    accepted.decision = "clean"
    current = _analysis(source, status="queued", superseded_at=None)
    current.plan_item_id = item.id
    snapshot = analysis_snapshot(accepted)
    job = SimpleNamespace(
        id=item.current_job_id,
        user_id=owner_id,
        content_plan_item_id=item.id,
        content_plan_ownership_epoch=2,
        mode="content_plan",
        status="processing",
        failure_reason=None,
        assembly_plan={
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "_speech_cleanup_internal": {"preflight_snapshot": snapshot},
        },
    )

    class DB:
        def get(self, model: type[object], identifier: object, **_kwargs: object) -> object | None:
            return {
                (Job, job.id): job,
                (PlanItem, item.id): item,
                (ContentPlan, plan.id): plan,
                (Persona, persona.id): persona,
            }.get((model, identifier))

    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_sync",
        lambda *_args, **_kwargs: current,
    )

    reset = prepare_snapshot_mismatch_reanalysis(DB(), job.id)

    assert reset.job is job
    assert reset.analysis_id is None
    assert current.status == "queued"
    assert accepted.status == "ready"
    assert accepted.decision == "clean"


def test_post_commit_preflight_publisher_dispatches_real_task_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.plan_item_media import publish_preflight_after_commit
    from app.tasks.speech_cleanup_analysis import analyze_speech_cleanup
    from app.worker import celery_app

    mock_dispatch = Mock()
    monkeypatch.setattr(
        "app.tasks.speech_cleanup_analysis.analyze_speech_cleanup.apply_async",
        mock_dispatch,
    )
    analysis_id = uuid.uuid4()

    assert publish_preflight_after_commit(analysis_id) is True
    mock_dispatch.assert_called_once_with(args=[str(analysis_id)])
    assert analyze_speech_cleanup.name == "tasks.analyze_speech_cleanup"
    assert celery_app.conf.task_routes[analyze_speech_cleanup.name] == {
        "queue": settings.speech_cleanup_analysis_queue
    }
    assert publish_preflight_after_commit(None) is False
