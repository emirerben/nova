"""Celery adapters for generation-pinned speech-cleanup preflight.

The analysis task intentionally owns only orchestration and media acquisition:

    claim/CAS -> exact-generation signed stream -> bounded PCM -> pure engine -> CAS terminal

It never downloads or hashes the source video. FFmpeg receives a short-lived URL
for the generation captured at media registration and decodes only the persisted
renderer window into a disposable 16 kHz mono artifact.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from billiard.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.database import sync_session
from app.models import SpeechCleanupAnalysis
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
)
from app.pipeline.speech_cleanup_analysis import (
    analyze_speech_cleanup as run_speech_cleanup_engine,
)
from app.pipeline.transcribe import TranscribeError
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    SPEECH_CLEANUP_MAX_DURATION_S,
    SPEECH_CLEANUP_MIXED_GAP_MODE,
    SPEECH_CLEANUP_OVER_BUDGET_POLICY,
    SPEECH_CLEANUP_RECONCILE_BATCH,
    SPEECH_CLEANUP_TASK_HARD_LIMIT_S,
    SPEECH_CLEANUP_TASK_SOFT_LIMIT_S,
    ClaimedSpeechCleanupAnalysis,
    SpeechCleanupOperationalError,
    claim_analysis,
    claim_reconciliation_batch,
    finalize_analysis_failure,
    finalize_analysis_success,
    mark_dispatch_failed,
)
from app.storage import signed_get_url_for_generation
from app.worker import celery_app

log = structlog.get_logger()

SPEECH_CLEANUP_SAMPLE_RATE_HZ = 16_000
SPEECH_CLEANUP_CHANNELS = 1
SPEECH_CLEANUP_SAMPLE_WIDTH_BYTES = 2
SPEECH_CLEANUP_AUDIO_EXTRACTION_TIMEOUT_S = 240
SPEECH_CLEANUP_SIGNED_URL_TTL_MINUTES = 15
SPEECH_CLEANUP_PROCESS_KILL_GRACE_S = 5
SPEECH_CLEANUP_WAV_HEADER_ALLOWANCE_BYTES = 64 * 1024
SPEECH_CLEANUP_DURATION_TOLERANCE_S = 0.25
SPEECH_CLEANUP_RECONCILE_SOFT_LIMIT_S = 20
SPEECH_CLEANUP_RECONCILE_HARD_LIMIT_S = 25
SPEECH_CLEANUP_PUBLISH_RETRY_S = 30

_PROGRAMMING_ERRORS = (
    AssertionError,
    AttributeError,
    ImportError,
    KeyError,
    NameError,
    TypeError,
    ValueError,
)
_TRANSCRIPTION_PROVIDER_MODULES = frozenset({"httpcore", "httpx", "openai", "requests", "urllib3"})
_STORAGE_PROVIDER_MODULES = frozenset({"google", "httpcore", "httpx", "requests", "urllib3"})


@dataclass(frozen=True, slots=True)
class _ClaimedWork:
    claim: ClaimedSpeechCleanupAnalysis
    source_storage_path: str
    source_generation: str
    window_start_s: float
    window_end_s: float
    engine_version: str
    detector_version: str
    source_kind: str = "unknown"
    queued_at: Any | None = None

    @property
    def duration_s(self) -> float:
        return self.window_end_s - self.window_start_s


def _claim_work(analysis_id: str) -> _ClaimedWork | None:
    """Claim one row and copy private inputs before releasing its DB lock."""

    with sync_session() as db:
        claim = claim_analysis(db, analysis_id)
        if claim is None:
            # claim_analysis may terminalize a row that exhausted its attempts.
            db.commit()
            return None
        row = db.get(SpeechCleanupAnalysis, claim.analysis_id)
        if row is None:
            raise RuntimeError("claimed speech-cleanup analysis disappeared")
        if row.window_end_s is None:
            work = _ClaimedWork(
                claim=claim,
                source_storage_path=row.source_storage_path,
                source_generation=row.source_generation,
                window_start_s=float(row.window_start_s),
                window_end_s=float("nan"),
                engine_version=row.engine_version,
                detector_version=row.detector_version,
                source_kind=str(getattr(row, "source_kind", "unknown") or "unknown"),
                queued_at=getattr(row, "created_at", None),
            )
        else:
            work = _ClaimedWork(
                claim=claim,
                source_storage_path=row.source_storage_path,
                source_generation=row.source_generation,
                window_start_s=float(row.window_start_s),
                window_end_s=float(row.window_end_s),
                engine_version=row.engine_version,
                detector_version=row.detector_version,
                source_kind=str(getattr(row, "source_kind", "unknown") or "unknown"),
                queued_at=getattr(row, "created_at", None),
            )
        db.commit()
        return work


def _validate_work(work: _ClaimedWork) -> None:
    values = (work.window_start_s, work.window_end_s, work.duration_s)
    if not all(math.isfinite(value) for value in values):
        raise SpeechCleanupOperationalError("snapshot_mismatch", detail="non_finite_window")
    if work.window_start_s < 0 or work.duration_s <= 0:
        raise SpeechCleanupOperationalError("snapshot_mismatch", detail="invalid_window")
    if work.duration_s > SPEECH_CLEANUP_MAX_DURATION_S:
        raise SpeechCleanupOperationalError("unsupported_media", detail="window_too_long")
    if work.engine_version != SPEECH_CLEANUP_ENGINE_VERSION:
        raise SpeechCleanupOperationalError("snapshot_mismatch", detail="engine_version")
    if not work.source_storage_path.strip() or not work.source_generation.strip():
        raise SpeechCleanupOperationalError("snapshot_mismatch", detail="source_identity")


def _maximum_pcm_artifact_bytes(duration_s: float) -> int:
    pcm_bytes = math.ceil(
        duration_s
        * SPEECH_CLEANUP_SAMPLE_RATE_HZ
        * SPEECH_CLEANUP_CHANNELS
        * SPEECH_CLEANUP_SAMPLE_WIDTH_BYTES
    )
    return pcm_bytes + SPEECH_CLEANUP_WAV_HEADER_ALLOWANCE_BYTES


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate FFmpeg and descendants created in its isolated process group."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        # Continue to SIGKILL below; a best-effort TERM must never strand the
        # child while the Celery process exits on a soft limit.
        pass
    try:
        process.wait(timeout=SPEECH_CLEANUP_PROCESS_KILL_GRACE_S)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=SPEECH_CLEANUP_PROCESS_KILL_GRACE_S)
    except (OSError, subprocess.TimeoutExpired):
        # The original bounded-operation error remains authoritative. Never
        # replace it with cleanup noise that could contain process arguments.
        pass


def _ffmpeg_command(*, signed_url: str, output_path: Path, work: _ClaimedWork) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{work.window_start_s:.6f}",
        "-i",
        signed_url,
        "-t",
        f"{work.duration_s:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        str(SPEECH_CLEANUP_CHANNELS),
        "-ar",
        str(SPEECH_CLEANUP_SAMPLE_RATE_HZ),
        "-c:a",
        "pcm_s16le",
        "-threads",
        "1",
        "-fs",
        str(_maximum_pcm_artifact_bytes(work.duration_s)),
        "-y",
        str(output_path),
    ]


def _read_pcm_duration(output_path: Path, *, requested_duration_s: float) -> float:
    try:
        output_bytes = output_path.stat().st_size
    except OSError as exc:
        raise SpeechCleanupOperationalError(
            "unsupported_media", detail="missing_audio_artifact"
        ) from exc
    if output_bytes <= 44:
        raise SpeechCleanupOperationalError("no_speech_track", detail="empty_audio_artifact")
    if output_bytes > _maximum_pcm_artifact_bytes(requested_duration_s):
        raise SpeechCleanupOperationalError("internal_error", detail="audio_artifact_too_large")
    try:
        with wave.open(str(output_path), "rb") as artifact:
            if (
                artifact.getnchannels() != SPEECH_CLEANUP_CHANNELS
                or artifact.getframerate() != SPEECH_CLEANUP_SAMPLE_RATE_HZ
                or artifact.getsampwidth() != SPEECH_CLEANUP_SAMPLE_WIDTH_BYTES
            ):
                raise SpeechCleanupOperationalError(
                    "internal_error", detail="invalid_audio_artifact_format"
                )
            frame_count = artifact.getnframes()
    except SpeechCleanupOperationalError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise SpeechCleanupOperationalError(
            "unsupported_media", detail="invalid_audio_artifact"
        ) from exc
    if frame_count <= 0:
        raise SpeechCleanupOperationalError("no_speech_track", detail="empty_audio_artifact")
    duration_s = frame_count / SPEECH_CLEANUP_SAMPLE_RATE_HZ
    if abs(duration_s - requested_duration_s) > SPEECH_CLEANUP_DURATION_TOLERANCE_S:
        raise SpeechCleanupOperationalError("snapshot_mismatch", detail="source_duration")
    return duration_s


def _extract_bounded_audio(*, signed_url: str, output_path: Path, work: _ClaimedWork) -> float:
    """Stream exactly one source window into a size- and time-bounded PCM WAV."""

    command = _ffmpeg_command(signed_url=signed_url, output_path=output_path, work=work)
    try:
        process = subprocess.Popen(  # noqa: S603 - static executable + argv, never a shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise SpeechCleanupOperationalError("internal_error", detail="ffmpeg_unavailable") from exc
    try:
        _, stderr = process.communicate(timeout=SPEECH_CLEANUP_AUDIO_EXTRACTION_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise SpeechCleanupOperationalError(
            "audio_extraction_timeout", detail="ffmpeg_timeout"
        ) from exc
    except BaseException:
        _terminate_process_group(process)
        raise

    if process.returncode != 0:
        # stderr is inspected only to distinguish a missing audio stream. It is
        # never logged or persisted because FFmpeg commonly echoes the signed URL.
        safe_probe = bytes(stderr or b"")[-4096:].decode(errors="replace").lower()
        no_audio = "matches no streams" in safe_probe or "does not contain any stream" in safe_probe
        code = "no_speech_track" if no_audio else "unsupported_media"
        raise SpeechCleanupOperationalError(code, detail=f"ffmpeg_exit_{process.returncode}")
    return _read_pcm_duration(output_path, requested_duration_s=work.duration_s)


def _engine_diagnostic_receipt(result: SpeechCleanupAnalysisResult) -> dict[str, object]:
    """Return scalar/enumerated diagnostics safe for the active-thread projection."""

    signals = result.safety_signals
    return {
        "schema_version": 1,
        "selected_plan": signals.selected_plan,
        "candidate_status": signals.candidate_status,
        "silence_detection_status": signals.silence_detection_status,
        "transcript_low_confidence": signals.transcript_low_confidence,
        "bailout": signals.bailout_reason is not None,
        "clamped": signals.clamped,
    }


def _run_engine(work: _ClaimedWork, local_audio_path: Path) -> SpeechCleanupAnalysisResult:
    analysis_input = SpeechCleanupAnalysisInput(
        source_fingerprint=work.claim.source_policy_fingerprint,
        local_media_path=str(local_audio_path),
        duration_s=work.duration_s,
        source_window_start_s=work.window_start_s,
        source_window_end_s=work.window_end_s,
        detector_version=work.detector_version,
        mixed_gap_mode=SPEECH_CLEANUP_MIXED_GAP_MODE,
        include_silence_and_fillers=True,
        over_budget_policy=SPEECH_CLEANUP_OVER_BUDGET_POLICY,
    )
    try:
        return run_speech_cleanup_engine(analysis_input)
    except _PROGRAMMING_ERRORS:
        raise
    except TranscribeError as exc:
        raise SpeechCleanupOperationalError(
            "transcription_unavailable", detail=type(exc).__name__
        ) from exc
    except TimeoutError as exc:
        raise SpeechCleanupOperationalError("analysis_timeout", detail=type(exc).__name__) from exc
    except OSError as exc:
        raise SpeechCleanupOperationalError(
            "transcription_unavailable", detail=type(exc).__name__
        ) from exc
    except Exception as exc:
        if type(exc).__module__.partition(".")[0] in _TRANSCRIPTION_PROVIDER_MODULES:
            raise SpeechCleanupOperationalError(
                "transcription_unavailable", detail=type(exc).__name__
            ) from exc
        raise


def _analyze_work(work: _ClaimedWork) -> SpeechCleanupAnalysisResult:
    _validate_work(work)
    try:
        signed_url = signed_get_url_for_generation(
            work.source_storage_path,
            generation=work.source_generation,
            expiration_minutes=SPEECH_CLEANUP_SIGNED_URL_TTL_MINUTES,
        )
    except FileNotFoundError as exc:
        raise SpeechCleanupOperationalError(
            "source_temporarily_unavailable", detail="generation_unavailable"
        ) from exc
    except ValueError as exc:
        raise SpeechCleanupOperationalError("snapshot_mismatch", detail="generation") from exc
    except OSError as exc:
        raise SpeechCleanupOperationalError(
            "source_temporarily_unavailable", detail=type(exc).__name__
        ) from exc
    except Exception as exc:
        if type(exc).__module__.partition(".")[0] in _STORAGE_PROVIDER_MODULES:
            raise SpeechCleanupOperationalError(
                "source_temporarily_unavailable", detail=type(exc).__name__
            ) from exc
        raise

    with tempfile.TemporaryDirectory(prefix="nova-speech-cleanup-") as temporary_dir:
        audio_path = Path(temporary_dir) / "narration.wav"
        decoded_duration_s = _extract_bounded_audio(
            signed_url=signed_url,
            output_path=audio_path,
            work=work,
        )
        log.info(
            "speech_cleanup_analysis.artifact_ready",
            analysis_id=str(work.claim.analysis_id),
            source_kind=work.source_kind,
            duration_ms=max(0, int(round(decoded_duration_s * 1000))),
            artifact_bytes=audio_path.stat().st_size,
        )
        return _run_engine(work, audio_path)


def _persist_success(work: _ClaimedWork, result: SpeechCleanupAnalysisResult) -> bool:
    receipt = result.public_receipt
    with sync_session() as db:
        persisted = finalize_analysis_success(
            db,
            work.claim,
            analysis_payload=result.to_payload(),
            candidate_count=receipt.candidate_count,
            category_counts=receipt.category_counts.model_dump(mode="json"),
            estimated_removed_ms=receipt.estimated_removed_ms,
            diagnostic_receipt=_engine_diagnostic_receipt(result),
        )
        db.commit()
        return persisted


def _persist_failure(work: _ClaimedWork, error: SpeechCleanupOperationalError) -> bool:
    with sync_session() as db:
        persisted = finalize_analysis_failure(db, work.claim, error)
        db.commit()
        return persisted


@celery_app.task(
    name="tasks.analyze_speech_cleanup",
    queue=settings.speech_cleanup_analysis_queue,
    soft_time_limit=SPEECH_CLEANUP_TASK_SOFT_LIMIT_S,
    time_limit=SPEECH_CLEANUP_TASK_HARD_LIMIT_S,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=0,
)
def analyze_speech_cleanup(analysis_id: str) -> dict[str, object]:
    """Analyze one durable row; stale/duplicate deliveries are safe no-ops."""

    started_at = time.monotonic()
    work = _claim_work(analysis_id)
    if work is None:
        return {"status": "not_claimed"}
    queue_delay_ms = None
    if work.queued_at is not None:
        queued_at = work.queued_at
        if getattr(queued_at, "tzinfo", None) is None:
            queued_at = queued_at.replace(tzinfo=UTC)
        queue_delay_ms = max(
            0,
            int(round((datetime.now(UTC) - queued_at).total_seconds() * 1000)),
        )
    try:
        result = _analyze_work(work)
    except SoftTimeLimitExceeded:
        error = SpeechCleanupOperationalError("analysis_timeout", detail="soft_time_limit")
        persisted = _persist_failure(work, error)
        log.warning(
            "speech_cleanup_analysis.failed",
            analysis_id=str(work.claim.analysis_id),
            error_code=error.code,
            retryable=error.retryable,
            persisted=persisted,
            source_kind=work.source_kind,
            queue_delay_ms=queue_delay_ms,
            run_duration_ms=max(0, int(round((time.monotonic() - started_at) * 1000))),
        )
        return {"status": "failed" if persisted else "stale", "error_code": error.code}
    except SpeechCleanupOperationalError as error:
        persisted = _persist_failure(work, error)
        log.warning(
            "speech_cleanup_analysis.failed",
            analysis_id=str(work.claim.analysis_id),
            error_code=error.code,
            retryable=error.retryable,
            persisted=persisted,
            source_kind=work.source_kind,
            queue_delay_ms=queue_delay_ms,
            run_duration_ms=max(0, int(round((time.monotonic() - started_at) * 1000))),
        )
        return {"status": "failed" if persisted else "stale", "error_code": error.code}

    persisted = _persist_success(work, result)
    status = "ready" if result.public_receipt.candidate_count > 0 else "no_findings"
    log.info(
        "speech_cleanup_analysis.completed",
        analysis_id=str(work.claim.analysis_id),
        status=status,
        candidate_count=result.public_receipt.candidate_count,
        persisted=persisted,
        source_kind=work.source_kind,
        queue_delay_ms=queue_delay_ms,
        run_duration_ms=max(0, int(round((time.monotonic() - started_at) * 1000))),
    )
    return {"status": status if persisted else "stale"}


def _mark_publish_retry(analysis_id: uuid.UUID) -> None:
    with sync_session() as db:
        mark_dispatch_failed(
            db,
            analysis_id,
            retry_after_s=SPEECH_CLEANUP_PUBLISH_RETRY_S,
        )
        db.commit()


@celery_app.task(
    name="tasks.reconcile_speech_cleanup_analyses",
    queue="maintenance",
    soft_time_limit=SPEECH_CLEANUP_RECONCILE_SOFT_LIMIT_S,
    time_limit=SPEECH_CLEANUP_RECONCILE_HARD_LIMIT_S,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=0,
)
def reconcile_speech_cleanup_analyses() -> dict[str, int]:
    """Publish one bounded, leased outbox page after its DB transaction commits."""

    with sync_session() as db:
        analysis_ids = claim_reconciliation_batch(
            db,
            limit=SPEECH_CLEANUP_RECONCILE_BATCH,
        )
        db.commit()

    published = 0
    publish_failed = 0
    for analysis_id in analysis_ids:
        try:
            analyze_speech_cleanup.apply_async(
                args=[str(analysis_id)],
                queue=settings.speech_cleanup_analysis_queue,
            )
            published += 1
        except Exception as exc:  # noqa: BLE001 - each durable row must remain retryable
            publish_failed += 1
            _mark_publish_retry(analysis_id)
            log.warning(
                "speech_cleanup_analysis.publish_failed",
                analysis_id=str(analysis_id),
                error_class=type(exc).__name__,
            )
    return {
        "claimed": len(analysis_ids),
        "published": published,
        "publish_failed": publish_failed,
    }
