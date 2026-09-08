from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.tasks import template_upload_promotion


def test_reconciler_resumes_stale_journal_then_dispatches(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    calls: list[str] = []
    monkeypatch.setattr(
        template_upload_promotion,
        "_candidate_ids",
        lambda *, now: [job_id],
    )
    monkeypatch.setattr(
        template_upload_promotion,
        "resume_template_upload_promotion",
        lambda candidate: calls.append(f"resume:{candidate}") or SimpleNamespace(state="promoted"),
    )
    monkeypatch.setattr(
        template_upload_promotion,
        "_dispatch_if_ready",
        lambda candidate: calls.append(f"dispatch:{candidate}") or True,
    )

    recovered = template_upload_promotion.reconcile_template_upload_promotions(
        now=datetime(2026, 9, 8, tzinfo=UTC)
    )

    assert recovered == 1
    assert calls == [f"resume:{job_id}", f"dispatch:{job_id}"]


def test_reconciler_leaves_failed_promotion_for_next_beat(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    dispatched: list[str] = []
    monkeypatch.setattr(
        template_upload_promotion,
        "_candidate_ids",
        lambda *, now: [job_id],
    )

    def interrupted(_candidate: str):
        raise RuntimeError("copy interrupted")

    monkeypatch.setattr(
        template_upload_promotion,
        "resume_template_upload_promotion",
        interrupted,
    )
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        template_upload_promotion,
        "record_template_upload_promotion_failure",
        lambda candidate, *, error_type, now: recorded.append((candidate, error_type)),
    )
    monkeypatch.setattr(
        template_upload_promotion,
        "_dispatch_if_ready",
        lambda candidate: dispatched.append(candidate) or True,
    )

    recovered = template_upload_promotion.reconcile_template_upload_promotions(
        now=datetime(2026, 9, 8, tzinfo=UTC)
    )

    assert recovered == 0
    assert dispatched == []
    assert recorded == [(job_id, "RuntimeError")]


def test_reconciler_is_beat_scheduled_on_maintenance_queue() -> None:
    from app.worker import MAINTENANCE_TASK_NAMES, celery_app

    task_name = "tasks.reconcile_template_upload_promotions"
    assert task_name in MAINTENANCE_TASK_NAMES
    assert celery_app.conf.task_routes[task_name] == {"queue": "maintenance"}
    assert any(entry["task"] == task_name for entry in celery_app.conf.beat_schedule.values())
