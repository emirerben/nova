"""Evidence extraction downloads and decodes media; the review sweep is database-only."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from app.services.queue_state import RENDER_WORKER_QUEUES
from app.tasks import render_review  # noqa: F401
from app.worker import MAINTENANCE_TASK_NAMES, celery_app

FLY_TOML = (Path(__file__).parents[4] / "fly.toml").read_text()


def _resolved_queue(name: str) -> str:
    return celery_app.amqp.router.route({}, name, args=(), kwargs={})["queue"].name


def test_evidence_extraction_never_lands_on_the_one_gigabyte_light_machine():
    queue = _resolved_queue("tasks.prepare_generation_review")
    assert queue == settings.kria_intent_review_evidence_queue
    assert queue not in {"maintenance", "agent-control"}
    assert "tasks.prepare_generation_review" not in MAINTENANCE_TASK_NAMES


def test_production_evidence_queue_is_consumed_by_a_media_capable_worker():
    configured = re.search(
        r'^\s*KRIA_INTENT_REVIEW_EVIDENCE_QUEUE\s*=\s*"([^"]+)"', FLY_TOML, re.MULTILINE
    )
    assert configured, "fly.toml must pin the evidence queue before the flag can be enabled"
    assert configured.group(1) in RENDER_WORKER_QUEUES
    assert configured.group(1) not in {"celery"}


def test_review_sweep_has_its_own_time_boxed_light_task():
    name = "tasks.reconcile_generation_reviews"
    assert name in MAINTENANCE_TASK_NAMES
    assert _resolved_queue(name) == "maintenance"
    assert any(entry["task"] == name for entry in celery_app.conf.beat_schedule.values())
    task = celery_app.tasks[name]
    assert task.soft_time_limit < task.time_limit < 1900
