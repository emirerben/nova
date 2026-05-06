"""Celery tasks: orchestrate_job, render_clip, finalize_job.

Pipeline:
  orchestrate_job(job_id)
    → probe → transcribe → scene_detect → score_candidates
    → chord([render_clip × 3], finalize_job)

  render_clip(job_id, clip_db_id)
    → asyncio.gather(generate_copy, select_thumbnail) [parallel]
    → reframe_export
    → upload_to_storage

  finalize_job([render_results], job_id)
    → set job.status = clips_ready | clips_ready_partial
"""

import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from celery import chord, group
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, JobClip
from app.pipeline import probe as probe_mod
from app.pipeline import scene_detect
from app.pipeline import transcribe as transcribe_mod
from app.pipeline.agents.copy_writer import generate_copy
from app.pipeline.agents.gemini_analyzer import gemini_upload_and_wait
from app.pipeline.captions import generate_ass
from app.pipeline.reframe import ReframeError, reframe_and_export
from app.pipeline.score import TOP_N, select_candidates
from app.pipeline.thumbnail import select_thumbnail
from app.pipeline.validator import validate_output
from app.storage import download_to_file, upload_bytes_public_read, upload_public_read
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(
    name="tasks.orchestrate_job", bind=True, max_retries=0,
    soft_time_limit=1080, time_limit=1200,
)
def orchestrate_job(self, job_id: str) -> None:
    log.info("orchestrate_start", job_id=job_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_local = os.path.join(tmpdir, "raw.mp4")
        try:
            # Snapshot raw_storage_path before session closes (matches render_clip pattern)
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job is None:
                    log.error("job_not_found", job_id=job_id)
                    return
                raw_storage_path = job.raw_storage_path
                _set_job_status(db, job, "processing")

            # [1] Download raw video from GCS
            log.info("downloading_raw", job_id=job_id, path=raw_storage_path)
            download_to_file(raw_storage_path, raw_local)

            # [1b] Upload to Gemini File API (one upload; reused for all 9 segment analyses)
            source_ref = None
            if settings.gemini_api_key:
                try:
                    log.info("gemini_upload_start", job_id=job_id)
                    source_ref = gemini_upload_and_wait(raw_local)
                    log.info("gemini_upload_done", job_id=job_id, name=source_ref.name)
                except Exception as exc:
                    log.warning("gemini_upload_failed_continuing_without", error=str(exc))

            # [1c] Probe
            video_probe = probe_mod.probe_video(raw_local)
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                job.probe_metadata = {
                    "duration_s": video_probe.duration_s,
                    "fps": video_probe.fps,
                    "width": video_probe.width,
                    "height": video_probe.height,
                    "has_audio": video_probe.has_audio,
                    "codec": video_probe.codec,
                    "aspect_ratio": video_probe.aspect_ratio,
                    "file_size_bytes": video_probe.file_size_bytes,
                    "color_transfer": video_probe.color_transfer,
                }
                db.commit()

            # [2] Transcribe (Gemini primary when source_ref available, else Whisper)
            transcript = transcribe_mod.transcribe(raw_local, file_ref=source_ref)
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                job.transcript = {
                    "full_text": transcript.full_text,
                    "low_confidence": transcript.low_confidence,
                    "words": [
                        {"text": w.text, "start_s": w.start_s, "end_s": w.end_s,
                         "confidence": w.confidence}
                        for w in transcript.words
                    ],
                }
                db.commit()

            # [3] Scene detection
            cuts = scene_detect.detect_scenes(raw_local)
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                job.scene_cuts = [{"timestamp_s": c.timestamp_s, "score": c.score} for c in cuts]
                db.commit()

            # [4] Score candidates (Gemini analyzes each segment via source_ref)
            candidates = select_candidates(video_probe, transcript, cuts, source_ref=source_ref)
            top3 = candidates[:TOP_N]
            held = candidates[TOP_N:]

            # Persist all 9 candidates + create JobClip rows for top 3
            clip_db_ids: list[str] = []
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                job.all_candidates = [
                    {
                        "rank": c.rank,
                        "start_s": c.start_s,
                        "end_s": c.end_s,
                        "hook_text": c.hook_text,
                        "hook_score": c.hook_score,
                        "engagement_score": c.engagement_score,
                        "combined_score": c.combined_score,
                    }
                    for c in candidates
                ]

                for candidate in top3:
                    clip = JobClip(
                        job_id=uuid.UUID(job_id),
                        rank=candidate.rank,
                        hook_score=candidate.hook_score,
                        engagement_score=candidate.engagement_score,
                        combined_score=candidate.combined_score,
                        start_s=candidate.start_s,
                        end_s=candidate.end_s,
                        hook_text=candidate.hook_text,
                        render_status="pending",
                    )
                    db.add(clip)
                    db.flush()
                    clip_db_ids.append(str(clip.id))

                # Also persist held candidates (rank 4-9) as JobClip rows (render_status=pending)
                for candidate in held:
                    held_clip = JobClip(
                        job_id=uuid.UUID(job_id),
                        rank=candidate.rank,
                        hook_score=candidate.hook_score,
                        engagement_score=candidate.engagement_score,
                        combined_score=candidate.combined_score,
                        start_s=candidate.start_s,
                        end_s=candidate.end_s,
                        hook_text=candidate.hook_text,
                        render_status="pending",
                    )
                    db.add(held_clip)

                db.commit()

        except SoftTimeLimitExceeded:
            log.error("orchestrate_timeout", job_id=job_id)
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job:
                    job.status = "processing_failed"
                    job.error_detail = "Job timed out (exceeded 1080s soft limit)"
                    db.commit()
            return
        except Exception as exc:
            log.error("orchestrate_failed", job_id=job_id, error=str(exc))
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job:
                    job.status = "processing_failed"
                    job.error_detail = str(exc)[:1000]
                    db.commit()
            return

    # Fan-out: render top 3 clips in parallel, then finalize
    if not clip_db_ids:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "processing_failed"
                job.error_detail = "No scoreable segments found"
                db.commit()
        return

    render_tasks = group(
        render_clip.s(job_id, clip_id) for clip_id in clip_db_ids
    )
    workflow = chord(render_tasks, finalize_job.s(job_id))
    workflow.apply_async()


@celery_app.task(
    name="tasks.render_clip", bind=True, max_retries=0,
    soft_time_limit=540, time_limit=600,
)
def render_clip(self, job_id: str, clip_db_id: str) -> dict:
    """Render a single clip. Returns {clip_id, success, error}.

    NEVER raises — errors are caught here so the chord callback always fires.
    """
    log.info("render_clip_start", job_id=job_id, clip_id=clip_db_id)

    with _sync_session() as db:
        clip = db.get(JobClip, uuid.UUID(clip_db_id))
        job = db.get(Job, uuid.UUID(job_id))
        if clip is None or job is None:
            return {"clip_id": clip_db_id, "success": False, "error": "DB record not found"}

        _set_clip_render_status(db, clip, "rendering")

        # Snapshot what we need from DB before closing session
        start_s = clip.start_s
        end_s = clip.end_s
        hook_text = clip.hook_text or ""
        rank = clip.rank
        raw_path = job.raw_storage_path
        probe_meta = job.probe_metadata or {}
        aspect_ratio = probe_meta.get("aspect_ratio", "16:9")
        input_color_transfer = str(probe_meta.get("color_transfer") or "")
        selected_platforms = job.selected_platforms or ["instagram", "youtube"]
        user_id = str(job.user_id)

        transcript_data = job.transcript or {}
        has_transcript = not transcript_data.get("low_confidence", True)
        transcript_excerpt = _extract_excerpt(transcript_data, start_s, end_s)
        scene_cut_timestamps = [
            c["timestamp_s"] for c in (job.scene_cuts or [])
        ]

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            raw_local = os.path.join(tmpdir, "raw.mp4")
            download_to_file(raw_path, raw_local)

            # [5+6 parallel] generate_copy + select_thumbnail concurrently
            # These are sync calls here (Celery worker thread); parallelism via threads
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                copy_future = pool.submit(
                    generate_copy,
                    hook_text=hook_text,
                    transcript_excerpt=transcript_excerpt,
                    platforms=selected_platforms,
                    has_transcript=has_transcript,
                )
                thumb_future = pool.submit(
                    select_thumbnail,
                    video_path=raw_local,
                    start_s=start_s,
                    end_s=end_s,
                    cut_timestamps=scene_cut_timestamps,
                    output_dir=tmpdir,
                )
                platform_copy, copy_status = copy_future.result()
                thumb_result = thumb_future.result()

            # [5 cont.] ASS captions
            ass_path = os.path.join(tmpdir, "captions.ass")
            _generate_captions(transcript_data, start_s, end_s, ass_path)

            # [7] Reframe + export
            output_path = os.path.join(tmpdir, f"clip_{rank}.mp4")
            reframe_and_export(
                input_path=raw_local,
                start_s=start_s,
                end_s=end_s,
                aspect_ratio=aspect_ratio,
                ass_subtitle_path=ass_path,
                output_path=output_path,
                input_color_transfer=input_color_transfer,
            )

            # Validate output spec
            validation = validate_output(output_path)
            if not validation.passed:
                raise ReframeError(f"Output spec validation failed: {validation.errors}")

            # [8] Upload to GCS
            clip_gcs_path = f"{user_id}/{job_id}/clip_{rank}.mp4"
            thumb_gcs_path = f"{user_id}/{job_id}/thumb_{rank}.jpg"

            video_url = upload_public_read(output_path, clip_gcs_path)
            with open(thumb_result.jpeg_path, "rb") as tf:
                thumb_url = upload_bytes_public_read(tf.read(), thumb_gcs_path)

            file_size = os.path.getsize(output_path)
            duration_s = end_s - start_s
            expires_at = datetime.now(UTC) + timedelta(days=30)

            with _sync_session() as db:
                clip = db.get(JobClip, uuid.UUID(clip_db_id))
                clip.render_status = "ready"
                clip.video_path = video_url
                clip.thumbnail_path = thumb_url
                clip.duration_s = duration_s
                clip.file_size_bytes = file_size
                clip.platform_copy = platform_copy.model_dump()
                clip.copy_status = copy_status
                clip.storage_expires_at = expires_at
                db.commit()

            log.info("render_clip_done", clip_id=clip_db_id, video_url=video_url)
            return {"clip_id": clip_db_id, "success": True, "error": None}

        except SoftTimeLimitExceeded:
            log.error("render_clip_timeout", clip_id=clip_db_id)
            with _sync_session() as db:
                clip = db.get(JobClip, uuid.UUID(clip_db_id))
                if clip:
                    clip.render_status = "failed"
                    clip.error_detail = "Clip render timed out (exceeded 540s soft limit)"
                    db.commit()
            return {"clip_id": clip_db_id, "success": False, "error": "render timeout"}
        except Exception as exc:
            log.error("render_clip_failed", clip_id=clip_db_id, error=str(exc))
            with _sync_session() as db:
                clip = db.get(JobClip, uuid.UUID(clip_db_id))
                if clip:
                    clip.render_status = "failed"
                    clip.error_detail = str(exc)[:1000]
                    db.commit()
            # Return failure dict — do NOT raise (would break chord callback)
            return {"clip_id": clip_db_id, "success": False, "error": str(exc)}


@celery_app.task(name="tasks.finalize_job")
def finalize_job(render_results: list[dict], job_id: str) -> None:
    """Chord callback — fires after all render_clip tasks complete (success or failure).

    Sets job status: clips_ready | clips_ready_partial | processing_failed
    """
    successes = [r for r in render_results if r.get("success")]
    failures = [r for r in render_results if not r.get("success")]

    log.info(
        "finalize_job",
        job_id=job_id,
        successes=len(successes),
        failures=len(failures),
    )

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return

        if len(successes) == 0:
            job.status = "processing_failed"
            job.error_detail = "All clip renders failed"
        elif len(failures) > 0:
            job.status = "clips_ready_partial"
        else:
            job.status = "clips_ready"

        db.commit()

    log.info("job_finalized", job_id=job_id, status=job.status)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _set_job_status(db: Session, job: Job, status: str) -> None:
    job.status = status
    db.commit()


def _set_clip_render_status(db: Session, clip: JobClip, status: str) -> None:
    clip.render_status = status
    db.commit()


def _extract_excerpt(transcript_data: dict, start_s: float, end_s: float) -> str:
    words = transcript_data.get("words", [])
    in_window = [w["text"] for w in words if start_s <= w.get("start_s", 0) < end_s]
    return " ".join(in_window[:100])


def _generate_captions(
    transcript_data: dict, start_s: float, end_s: float, output_path: str
) -> None:
    from app.pipeline.transcribe import Transcript, Word
    words = [
        Word(
            text=w["text"],
            start_s=w["start_s"],
            end_s=w["end_s"],
            confidence=w.get("confidence", 1.0),
        )
        for w in transcript_data.get("words", [])
    ]
    transcript = Transcript(words=words)
    generate_ass(transcript, start_s, end_s, output_path)
