"""Static/decorator contracts for the dedicated speech-analysis worker lane."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.pipeline import speech_cleanup_analysis as engine
from app.services import speech_cleanup_preflight as preflight
from app.services.speech_cleanup_rollout import audit_rollout_evidence
from app.tasks import speech_cleanup_analysis as tasks
from app.worker import celery_app

API_ROOT = Path(__file__).parents[1]
REPO_ROOT = Path(__file__).parents[4]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_analysis_task_has_explicit_dedicated_queue_limits_and_late_ack() -> None:
    task = tasks.analyze_speech_cleanup

    assert task.name == "tasks.analyze_speech_cleanup"
    assert task.queue == tasks.settings.speech_cleanup_analysis_queue == "speech-analysis"
    assert task.soft_time_limit == preflight.SPEECH_CLEANUP_TASK_SOFT_LIMIT_S
    assert task.time_limit == preflight.SPEECH_CLEANUP_TASK_HARD_LIMIT_S
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True
    assert task.max_retries == 0


def test_analysis_limits_fit_broker_lease_and_operation_envelopes() -> None:
    visibility_timeout = celery_app.conf.broker_transport_options["visibility_timeout"]

    assert preflight.SPEECH_CLEANUP_TASK_SOFT_LIMIT_S < preflight.SPEECH_CLEANUP_TASK_HARD_LIMIT_S
    assert preflight.SPEECH_CLEANUP_TASK_HARD_LIMIT_S < visibility_timeout
    assert preflight.SPEECH_CLEANUP_LEASE_S > (
        preflight.SPEECH_CLEANUP_TASK_HARD_LIMIT_S + preflight.SPEECH_CLEANUP_CLOCK_SKEW_S
    )
    assert (
        tasks.SPEECH_CLEANUP_AUDIO_EXTRACTION_TIMEOUT_S < preflight.SPEECH_CLEANUP_TASK_SOFT_LIMIT_S
    )
    assert preflight.SPEECH_CLEANUP_MAX_DURATION_S > 0

    derived_max = (
        preflight.SPEECH_CLEANUP_MAX_DURATION_S
        * tasks.SPEECH_CLEANUP_SAMPLE_RATE_HZ
        * tasks.SPEECH_CLEANUP_CHANNELS
        * tasks.SPEECH_CLEANUP_SAMPLE_WIDTH_BYTES
        + tasks.SPEECH_CLEANUP_WAV_HEADER_ALLOWANCE_BYTES
    )
    assert tasks._maximum_pcm_artifact_bytes(preflight.SPEECH_CLEANUP_MAX_DURATION_S) == int(
        derived_max
    )


def test_reconciler_is_bounded_late_ack_maintenance_work() -> None:
    task = tasks.reconcile_speech_cleanup_analyses

    assert task.name == "tasks.reconcile_speech_cleanup_analyses"
    assert task.queue == "maintenance"
    assert task.soft_time_limit == tasks.SPEECH_CLEANUP_RECONCILE_SOFT_LIMIT_S
    assert task.time_limit == tasks.SPEECH_CLEANUP_RECONCILE_HARD_LIMIT_S
    assert task.soft_time_limit < task.time_limit < preflight.SPEECH_CLEANUP_TASK_SOFT_LIMIT_S
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True

    claim_source = inspect.getsource(preflight.claim_reconciliation_batch)
    assert ".with_for_update(skip_locked=True)" in claim_source
    assert ".limit(bounded)" in claim_source
    assert "SPEECH_CLEANUP_RECONCILE_BATCH = 25" in inspect.getsource(preflight)


def test_worker_routes_and_beat_register_exact_task_names() -> None:
    routes = celery_app.conf.task_routes
    schedule = celery_app.conf.beat_schedule

    assert routes["tasks.analyze_speech_cleanup"] == {"queue": "speech-analysis"}
    assert routes["tasks.reconcile_speech_cleanup_analyses"] == {"queue": "maintenance"}
    assert schedule["reconcile-speech-cleanup-analyses-every-30s"]["task"] == (
        "tasks.reconcile_speech_cleanup_analyses"
    )
    assert "app.tasks.speech_cleanup_analysis" in celery_app.conf.include


def test_fly_processes_isolate_analysis_from_render_and_maintenance() -> None:
    fly_source = (REPO_ROOT / "fly.toml").read_text()
    process_lines = {
        line.strip().split(" = ", 1)[0]: line.strip()
        for line in fly_source.splitlines()
        if "celery -A app.worker worker" in line
    }

    assert "-Q speech-analysis" in process_lines["speech_analysis"]
    assert "--concurrency=1" in process_lines["speech_analysis"]
    assert "speech-analysis" not in process_lines["worker"]
    assert "speech-analysis" not in process_lines["light"]
    assert "maintenance" not in process_lines["speech_analysis"]


def test_task_streams_generation_without_full_video_download_or_hash() -> None:
    task_path = API_ROOT / "app/tasks/speech_cleanup_analysis.py"
    source = task_path.read_text()

    assert "signed_get_url_for_generation(" in source
    assert "download_to_file" not in source
    assert "download_bytes" not in source
    assert "sha256" not in source
    assert '"-ac"' in source and "SPEECH_CLEANUP_CHANNELS" in source
    assert '"-ar"' in source and "SPEECH_CLEANUP_SAMPLE_RATE_HZ" in source
    assert '"-fs"' in source and "_maximum_pcm_artifact_bytes" in source
    assert "start_new_session=True" in source
    assert "os.killpg" in source


def test_signed_get_url_for_generation_pins_generation_in_query(monkeypatch) -> None:
    """The signed URL must carry the exact generation, not just the path.

    Consent binds to immutable bytes. ``bucket.blob(path, generation=...)`` does
    NOT propagate into ``generate_signed_url`` -- its own ``generation=None``
    default is forwarded to the signer -- so a path-only URL would silently read
    a replacement object. Behavioural guard: the name-only source grep above
    cannot catch this, and the local-storage branch enforces identity by a
    different route, so only GCS regresses.
    """
    from app import storage

    signed: dict[str, object] = {}

    class _FakeBlob:
        def __init__(self, name: str, generation: int | None) -> None:
            self.name = name
            self.generation = generation

        def generate_signed_url(self, **kwargs: object) -> str:
            signed.update(kwargs)
            gen = kwargs.get("generation")
            query = f"?generation={gen}" if gen is not None else ""
            return f"https://signed.test/{self.name}{query}"

    class _FakeBucket:
        def blob(self, name: str, generation: int | None = None) -> _FakeBlob:
            return _FakeBlob(name, generation)

    class _FakeClient:
        def bucket(self, _name: str) -> _FakeBucket:
            return _FakeBucket()

    monkeypatch.setattr(storage, "_uses_local_storage", lambda: False)
    monkeypatch.setattr(storage, "_get_client", _FakeClient)

    url = storage.signed_get_url_for_generation(
        "voiceover-uploads/direct/u/voice.mp3",
        generation="1758000000000001",
    )

    assert signed["generation"] == 1758000000000001
    assert "generation=1758000000000001" in url


def test_pure_engine_has_no_job_orm_celery_or_render_task_imports() -> None:
    engine_path = API_ROOT / "app/pipeline/speech_cleanup_analysis.py"
    imported = _imported_modules(engine_path)
    forbidden = {"app.models", "celery", "app.tasks.generative_build"}

    assert engine.analyze_speech_cleanup.__module__ == "app.pipeline.speech_cleanup_analysis"
    assert not forbidden & imported


def test_detector_policy_identity_matches_worker_and_ignores_exposure_controls(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.services.plan_item_media import current_detector_policy

    before = current_detector_policy()
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    monkeypatch.setattr(settings, "speech_cleanup_mixed_gap_mode", "off")
    monkeypatch.setattr(settings, "speech_cleanup_mixed_gap_rollout_percent", 0)

    assert current_detector_policy() == before
    assert f"mixed-gap={preflight.SPEECH_CLEANUP_MIXED_GAP_MODE}" in before
    assert f"over-budget={preflight.SPEECH_CLEANUP_OVER_BUDGET_POLICY}" in before
    assert preflight.SPEECH_CLEANUP_ENGINE_VERSION in before


def test_rollout_halts_without_worker_canary_and_receipts() -> None:
    """Configuration alone can never authorize a broader production cohort."""

    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    evidence = {
        "schema_version": 1,
        "observed_mode": "enforce",
        "observed_rollout_percent": 10,
        "observed_mixed_gap_mode": "apply",
        "observed_mixed_gap_rollout_percent": 10,
        "window_started_at": (now - timedelta(minutes=10)).isoformat(),
        "window_ended_at": (now - timedelta(minutes=1)).isoformat(),
        "worker_canary": {
            "queue": "speech-analysis",
            "consumer_count": 0,
            "published_count": 0,
            "completed_count": 0,
            "api_revision": "sha-a",
            "worker_revision": "sha-a",
        },
        "metrics": {
            "analysis_count": 100,
            "unexpected_failure_count": 0,
            "stuck_count": 0,
            "snapshot_mismatch_count": 0,
            "invalidation_error_count": 0,
            "duplicate_dispatch_count": 0,
            "private_payload_leak_count": 0,
            "queue_latency_p95_ms": 1,
            "run_latency_p95_ms": 1,
        },
        "source_receipts": {
            "embedded_video": 0,
            "narrated_upload_or_recording": 0,
            "audio_only_then_video": 0,
        },
        "outcome_receipts": {
            "applied": 0,
            "checked_no_change": 0,
            "declined": 0,
            "bypassed_unchecked": 0,
            "failed": 0,
        },
        "rollback_drill": {
            "completed": False,
            "completed_at": None,
            "restored_preflight_mode": None,
            "restored_preflight_percent": 0,
            "restored_mixed_gap_mode": None,
            "restored_mixed_gap_percent": 0,
            "inflight_contracts_preserved": False,
        },
    }

    audit = audit_rollout_evidence(evidence, target_percent=25, now=now)

    assert audit.promotion_allowed is False
    assert set(audit.halt_reasons) >= {
        "dedicated_worker_consumer_missing",
        "worker_canary_publish_missing",
        "worker_canary_completion_missing",
        "source_receipt_missing:embedded_video",
        "source_receipt_missing:narrated_upload_or_recording",
        "source_receipt_missing:audio_only_then_video",
        "outcome_receipt_missing:applied",
        "outcome_receipt_missing:checked_no_change",
        "outcome_receipt_missing:declined",
        "outcome_receipt_missing:bypassed_unchecked",
        "outcome_receipt_missing:failed",
    }
