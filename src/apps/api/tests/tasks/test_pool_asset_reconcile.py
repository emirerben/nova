from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, Job, Persona, PlanItem, PlanItemAsset, User
from app.tasks import maintenance


class _Rows:
    def __init__(self, rows):  # noqa: ANN001
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows, *, preview_rows=()):  # noqa: ANN001
        self.rows = rows
        # Deliberately NOT `rows` by default: the preview-backfill select is a
        # distinct query (`WHERE ... preview_gcs_path IS NULL ...`) that most
        # tests here never intend to exercise. Reusing `rows` would silently
        # dispatch a real (unmocked) `generate_pool_asset_preview.apply_async`
        # for every reconcile test. Pass a real list to opt in.
        self.preview_rows = list(preview_rows)
        self.commits = 0
        self.deleted = []
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        # The preview-backfill select is `select(PlanItemAsset.id)` (a single
        # column) — every other query in this module selects the whole
        # `PlanItemAsset` entity, whose compiled column list ALSO happens to
        # include `preview_gcs_path` now that it's a real column. Match the
        # single-column SELECT prefix specifically, not just the substring.
        if str(stmt).startswith("SELECT plan_item_assets.id \nFROM plan_item_assets"):
            return _Rows(self.preview_rows)
        return _Rows(self.rows)

    def commit(self):
        self.commits += 1

    def delete(self, row):  # noqa: ANN001
        self.deleted.append(row)

    def get(self, _model, row_id, **_kwargs):  # noqa: ANN001
        return next((row for row in self.rows if row.id == row_id), None)


def _asset(*, attempts: int):
    return SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        status="queued",
        analysis_attempt_token="old",
        analysis_attempt_count=attempts,
        analysis_last_dispatched_at=None,
        analysis_started_at=None,
        error_code=None,
        error_detail=None,
        error_retryable=False,
        correlation_id="batch-correlation",
        created_at=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        gcs_path=f"users/u/plan/i/pool/{uuid.uuid4()}.png",
    )


def test_reconcile_requeues_with_new_fenced_attempt(monkeypatch) -> None:
    asset = _asset(attempts=1)
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    publish = MagicMock()
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_asset.apply_async", publish)
    monkeypatch.setattr("app.config.settings.pool_asset_analysis_queue", "autoplace-jobs")

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "queued"
    assert asset.analysis_attempt_count == 2
    assert asset.analysis_attempt_token != "old"
    publish.assert_called_once_with(
        args=[str(asset.id), False],
        queue="autoplace-jobs",
        headers={
            "pool_asset_attempt_token": asset.analysis_attempt_token,
            "x-correlation-id": "batch-correlation",
        },
    )


def test_reconcile_requeues_once_for_heif_decoder_regression(monkeypatch) -> None:
    asset = _asset(attempts=1)
    asset.status = "failed"
    asset.error_code = "analysis_unreadable"
    asset.error_detail = "Kria couldn't read this file."
    asset.upload_content_type = "image/heic"
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    publish = MagicMock()
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_asset.apply_async", publish)
    monkeypatch.setattr("app.config.settings.pool_asset_analysis_queue", "autoplace-jobs")

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "queued"
    assert asset.analysis_attempt_count == 2
    publish.assert_called_once()

    recovery_query = str(session.statements[0])
    assert "plan_item_assets.error_code" in recovery_query
    assert "plan_item_assets.upload_content_type" in recovery_query
    assert "plan_item_assets.analysis_attempt_count" in recovery_query
    recovery_params = repr(session.statements[0].compile().params)
    assert "analysis_unreadable" in recovery_params
    assert "image/heic" in recovery_params
    assert "image/heif" in recovery_params
    assert maintenance._POOL_HEIF_DECODER_RECOVERY_MAX_ATTEMPTS in (
        session.statements[0].compile().params.values()
    )


def test_heif_recovery_predicate_selects_only_eligible_database_rows() -> None:
    if not (make_url(settings.database_url).database or "").endswith("_test"):
        pytest.skip("requires a *_test database")
    try:
        with sync_session() as probe:
            probe.execute(text("select 1"))
    except OperationalError:
        pytest.skip("nova_test Postgres not reachable")

    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        persona = Persona(
            user_id=user_id,
            persona_status="ready",
            persona={"content_mode": "travel", "tone": "direct"},
        )
        db.add(persona)
        db.flush()
        plan = ContentPlan(user_id=user_id, persona_id=persona.id)
        db.add(plan)
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan.id,
                position=1,
                idea="HEIF recovery predicate",
                item_status="awaiting_clips",
                clip_gcs_paths=[],
            )
        )
        db.flush()

        def add_asset(
            *,
            status: str = "failed",
            error_code: str | None = "analysis_unreadable",
            content_type: str = "image/heic",
            attempts: int = 1,
        ) -> uuid.UUID:
            asset_id = uuid.uuid4()
            db.add(
                PlanItemAsset(
                    id=asset_id,
                    plan_item_id=item_id,
                    user_id=user_id,
                    gcs_path=f"users/{user_id}/plan/{item_id}/pool/{asset_id}.heic",
                    kind="image",
                    source_filename=f"{asset_id}.heic",
                    upload_content_type=content_type,
                    status=status,
                    error_code=error_code,
                    error_detail="test",
                    analysis_attempt_count=attempts,
                )
            )
            return asset_id

        eligible = add_asset()
        current = add_asset(
            status="queued",
            error_code=None,
            content_type="image/png",
        )
        controls = {
            add_asset(attempts=2),
            add_asset(content_type="image/png"),
            add_asset(error_code="analysis_temporarily_unavailable"),
            add_asset(status="ready"),
        }
        db.commit()

        selected = set(
            db.execute(
                select(PlanItemAsset.id).where(
                    PlanItemAsset.id.in_({eligible, *controls}),
                    maintenance._heif_decoder_recovery_predicate(),
                )
            ).scalars()
        )

    assert selected == {eligible}

    with sync_session() as db:
        first = db.execute(
            select(PlanItemAsset.id)
            .where(PlanItemAsset.id.in_({eligible, current}))
            .order_by(maintenance._pool_reconcile_priority())
            .limit(1)
        ).scalar_one()

    assert first == current


def test_reconcile_terminalizes_exhausted_attempt(monkeypatch) -> None:
    asset = _asset(attempts=3)
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    publish = MagicMock()
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_asset.apply_async", publish)

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "failed"
    assert asset.error_code == "analysis_timed_out"
    assert asset.error_retryable is True
    publish.assert_not_called()


def test_reconcile_deletes_expired_reservation_and_object(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "preparing"
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_best_effort", cleanup)

    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    assert maintenance.reconcile_stale_pool_assets(now=now) == 1
    assert session.deleted == [asset]
    cleanup.assert_called_once_with(asset.gcs_path)


def test_reconcile_keeps_expired_reservation_when_object_cleanup_fails(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "preparing"
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    cleanup = MagicMock(return_value=False)
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_best_effort", cleanup)

    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    assert maintenance.reconcile_stale_pool_assets(now=now) == 1
    assert session.deleted == []
    assert asset.status == "cleanup_pending"
    cleanup.assert_called_once_with(asset.gcs_path)


def test_reconcile_cleanup_pending_finalized_asset_deletes_exact_generation(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "cleanup_pending"
    asset.gcs_generation = "42"
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_generation_best_effort", cleanup)

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert session.deleted == [asset]
    cleanup.assert_called_once_with(asset.gcs_path, generation="42")


def test_reconcile_stale_promotion_cleans_source_and_unknown_destination(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "promoting"
    asset.upload_expires_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    asset.gcs_generation = None
    source_path = asset.gcs_path
    destination_path = "users/u/plan/i/pool/reservation-x.png"
    asset.analysis = {
        "_upload_promotion": {
            "source_path": source_path,
            "source_generation": "42",
            "destination_path": destination_path,
        }
    }
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    exact_cleanup = MagicMock(return_value=True)
    latest_cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_generation_best_effort", exact_cleanup)
    monkeypatch.setattr("app.storage.delete_object_best_effort", latest_cleanup)

    assert maintenance.reconcile_stale_pool_assets(now=datetime(2026, 8, 14, 9, 0, tzinfo=UTC)) == 1
    assert session.deleted == [asset]
    exact_cleanup.assert_called_once_with(source_path, generation="42")
    latest_cleanup.assert_called_once_with(destination_path)


def test_reconcile_cleanup_retry_restores_asset_when_edit_gained_reference(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "cleanup_pending"
    asset.gcs_generation = "42"
    asset.plan_item_id = uuid.uuid4()
    asset.analysis = {"subject": "screen", "_pool_cleanup_previous_status": "ready"}
    item = SimpleNamespace(
        id=asset.plan_item_id,
        current_job_id=None,
        clip_gcs_paths=[asset.gcs_path],
        clip_assignments=[{"gcs_path": asset.gcs_path, "shot_id": None}],
    )
    session = _Session([asset])

    def _get(model, row_id, **_kwargs):  # noqa: ANN001
        if model is PlanItemAsset and row_id == asset.id:
            return asset
        if model is PlanItem and row_id == item.id:
            return item
        if model is Job:
            return None
        return None

    session.get = _get

    @contextmanager
    def _session():
        yield session

    exact_cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_generation_best_effort", exact_cleanup)

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "ready"
    assert asset.analysis == {"subject": "screen"}
    assert session.deleted == []
    exact_cleanup.assert_not_called()


def test_reconcile_does_not_delete_reservation_renewed_during_cleanup(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "preparing"
    old_path = asset.gcs_path
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    def _cleanup(path: str) -> bool:
        assert path == old_path
        asset.gcs_path = "users/u/plan/i/pool/renewed.png"
        return True

    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_best_effort", _cleanup)

    assert maintenance.reconcile_stale_pool_assets(now=datetime(2026, 8, 14, 9, tzinfo=UTC)) == 1
    assert session.deleted == []


def test_reconcile_publish_failure_becomes_safe_retryable_failure(monkeypatch) -> None:
    asset = _asset(attempts=1)
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    publish = MagicMock(side_effect=RuntimeError("private broker detail"))
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_asset.apply_async", publish)

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "failed"
    assert asset.error_code == "analysis_temporarily_unavailable"
    assert asset.error_retryable is True
    assert "private broker detail" not in asset.error_detail


# ── preview-backfill bounded pass ─────────────────────────────────────────────


def test_reconcile_dispatches_bounded_preview_backfill(monkeypatch) -> None:
    """`ready` + preview_gcs_path IS NULL rows get the backfill task dispatched
    (video, or HEIC/HEIF image); ready JPEG rows and the ""-sentinel
    (attempted-and-failed) rows do NOT (excluded by the predicate itself —
    here we assert only the ids the fake session hands back get dispatched)."""
    video_id = uuid.uuid4()
    heic_id = uuid.uuid4()
    # No stale/queued/analyzing rows in `rows` — the main reconcile pass has
    # nothing to touch, isolating this test to the preview-backfill pass.
    session = _Session([], preview_rows=[video_id, heic_id])

    @contextmanager
    def _session():
        yield session

    dispatch = MagicMock()
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.generate_pool_asset_preview.apply_async", dispatch)
    monkeypatch.setattr("app.config.settings.pool_asset_analysis_queue", "autoplace-jobs")

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 0
    assert dispatch.call_count == 2
    dispatched_ids = {call.kwargs["args"][0] for call in dispatch.call_args_list}
    assert dispatched_ids == {str(video_id), str(heic_id)}
    for call in dispatch.call_args_list:
        assert call.kwargs["queue"] == "autoplace-jobs"

    preview_query = str(session.statements[-1])
    assert "plan_item_assets.preview_gcs_path" in preview_query
    assert "plan_item_assets.status" in preview_query


def test_reconcile_preview_backfill_dispatch_failure_does_not_fail_sweep(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    session = _Session([], preview_rows=[asset_id])

    @contextmanager
    def _session():
        yield session

    dispatch = MagicMock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.generate_pool_asset_preview.apply_async", dispatch)
    monkeypatch.setattr("app.config.settings.pool_asset_analysis_queue", "autoplace-jobs")

    # Must not raise — a preview-backfill dispatch failure is best-effort only.
    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 0
    dispatch.assert_called_once()


def test_preview_backfill_predicate_selects_only_eligible_database_rows() -> None:
    if not (make_url(settings.database_url).database or "").endswith("_test"):
        pytest.skip("requires a *_test database")
    try:
        with sync_session() as probe:
            probe.execute(text("select 1"))
    except OperationalError:
        pytest.skip("nova_test Postgres not reachable")

    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        persona = Persona(
            user_id=user_id,
            persona_status="ready",
            persona={"content_mode": "travel", "tone": "direct"},
        )
        db.add(persona)
        db.flush()
        plan = ContentPlan(user_id=user_id, persona_id=persona.id)
        db.add(plan)
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan.id,
                position=1,
                idea="Preview backfill predicate",
                item_status="awaiting_clips",
                clip_gcs_paths=[],
            )
        )
        db.flush()

        def add_asset(
            *,
            kind: str = "video",
            content_type: str | None = "video/quicktime",
            status: str = "ready",
            preview_gcs_path: str | None = None,
        ) -> uuid.UUID:
            asset_id = uuid.uuid4()
            db.add(
                PlanItemAsset(
                    id=asset_id,
                    plan_item_id=item_id,
                    user_id=user_id,
                    gcs_path=f"users/{user_id}/plan/{item_id}/pool/{asset_id}.mov",
                    kind=kind,
                    source_filename=f"{asset_id}.mov",
                    upload_content_type=content_type,
                    status=status,
                    preview_gcs_path=preview_gcs_path,
                )
            )
            return asset_id

        eligible_video = add_asset()
        eligible_heic = add_asset(kind="image", content_type="image/heic")
        controls = {
            add_asset(status="analyzing"),  # not ready yet
            add_asset(preview_gcs_path="users/x/plan/y/pool/z.jpg"),  # already previewed
            add_asset(preview_gcs_path=""),  # attempted-and-failed sentinel
            add_asset(kind="image", content_type="image/jpeg"),  # browser-safe already
        }
        db.commit()

        selected = set(
            db.execute(
                select(PlanItemAsset.id).where(
                    PlanItemAsset.id.in_({eligible_video, eligible_heic, *controls}),
                    maintenance._preview_backfill_predicate(),
                )
            ).scalars()
        )

    assert selected == {eligible_video, eligible_heic}
