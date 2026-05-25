"""Celery task for admin lyrics-only previews."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.exc import DBAPIError, OperationalError

from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.pipeline.lyrics_preview import LyricsPreviewInputError, render_lyrics_preview
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
    try:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                log.error("lyrics_preview_job_not_found", job_id=job_id)
                return
            if not job.music_track_id:
                raise LyricsPreviewInputError("Preview job has no music_track_id.")
            track = db.get(MusicTrack, job.music_track_id)
            if track is None:
                raise LyricsPreviewInputError("Music track not found.")
            job.status = "processing"
            db.commit()

            override_payload = job.all_candidates or {}
            lyrics_config_effective = override_payload.get("lyrics_config_effective") or {}

        output_url, debug_meta = render_lyrics_preview(
            track, lyrics_config_effective, job_id=job_id
        )

        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                existing = job.assembly_plan or {}
                job.status = "music_ready"
                job.assembly_plan = {
                    **existing,
                    **debug_meta,
                    "output_url": output_url,
                    "lyrics_config_effective": lyrics_config_effective,
                }
                db.commit()
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
                job = db.get(Job, uuid.UUID(job_id))
                if job:
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
