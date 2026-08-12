"""Celery task for admin lyrics-only previews."""

from __future__ import annotations

import structlog
from sqlalchemy.exc import DBAPIError, OperationalError

from app.database import sync_session as _sync_session
from app.models import MusicTrack
from app.pipeline.lyrics_preview import LyricsPreviewInputError, render_lyrics_preview
from app.services.lyrics_cache_refresh import ensure_fresh_lyrics_cached_for_render
from app.services.pipeline_trace import pipeline_trace_for
from app.tasks._job_cancel_fence import (
    active_job_for_update,
    delete_task_owned_outputs,
    new_task_run_id,
)
from app.worker import celery_app

log = structlog.get_logger()
MAX_ERROR_DETAIL_LEN = 2000


@celery_app.task(
    name="tasks.render_lyrics_preview_task",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def render_lyrics_preview_task(self, job_id: str) -> None:
    log.info("lyrics_preview_start", job_id=job_id)
    # Bind job_id to the pipeline-trace contextvar for the duration of this
    # task. Without this wrapper, every `record_pipeline_event(...)` call
    # inside `_inject_line` (and any other pipeline-level decision point) is
    # a silent no-op for preview jobs — the trace contextvar is empty, so
    # the helper returns early. That gap is exactly why we couldn't diagnose
    # the override-gate bug from worker logs alone after PR #343 deployed.
    # Mirrors the contract documented in CLAUDE.md → "Admin job-debug view".
    with pipeline_trace_for(job_id):
        try:
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="lyrics_preview_start",
                )
                if job is None:
                    log.info("lyrics_preview_start_skipped", job_id=job_id)
                    return
                if not job.music_track_id:
                    raise LyricsPreviewInputError("Preview job has no music_track_id.")
                track = db.get(MusicTrack, job.music_track_id)
                if track is None:
                    raise LyricsPreviewInputError("Music track not found.")
                job.status = "processing"
                override_payload = job.all_candidates or {}
                lyrics_config_effective = override_payload.get("lyrics_config_effective") or {}
                db.commit()

            fresh_lyrics_cached = ensure_fresh_lyrics_cached_for_render(
                track_id=str(track.id),
                lyrics_cached=track.lyrics_cached,
                lyrics_config=lyrics_config_effective,
                reason="lyrics_preview",
            )
            track.lyrics_cached = fresh_lyrics_cached

            task_run_id = new_task_run_id()
            output_url, debug_meta = render_lyrics_preview(
                track,
                lyrics_config_effective,
                job_id=job_id,
                task_run_id=task_run_id,
            )

            output_gcs_path = str(debug_meta.get("output_gcs_path") or "")
            finalized = False
            try:
                with _sync_session() as db:
                    job = active_job_for_update(
                        db,
                        job_id,
                        operation="lyrics_preview_finalize",
                    )
                    if job is not None:
                        existing = job.assembly_plan or {}
                        job.status = "music_ready"
                        job.assembly_plan = {
                            **existing,
                            **debug_meta,
                            "output_url": output_url,
                            "lyrics_config_effective": lyrics_config_effective,
                        }
                        db.commit()
                        finalized = True
            except Exception:
                delete_task_owned_outputs(job_id, [output_gcs_path])
                raise
            if not finalized:
                delete_task_owned_outputs(job_id, [output_gcs_path])
                return
            log.info("lyrics_preview_done", job_id=job_id)
        except OperationalError:
            raise
        except Exception as exc:
            log.error("lyrics_preview_failed", job_id=job_id, error=str(exc), exc_info=True)
            _fail_preview_job(job_id, str(exc))


def _fail_preview_job(job_id: str, error_detail: str) -> None:
    for attempt in range(3):
        try:
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="lyrics_preview_mark_failed",
                )
                if job is None:
                    return
                job.status = "processing_failed"
                job.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                db.commit()
            return
        except (OperationalError, DBAPIError) as exc:
            if attempt == 2:
                log.error("lyrics_preview_fail_db_unreachable", job_id=job_id, error=str(exc))
                return
        except Exception as exc:
            log.error("lyrics_preview_fail_db_error", job_id=job_id, error=str(exc))
            return
