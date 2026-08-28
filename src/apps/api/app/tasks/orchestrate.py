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

import copy
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from celery import chord, group
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import JobClip
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
from app.services.media_filenames import safe_media_basename
from app.services.video_poster_cleanup import (
    append_video_poster_cleanup_receipt,
    reconcile_video_poster_cleanup_receipts,
)
from app.storage import download_to_file, upload_bytes_public_read, upload_public_read
from app.tasks._finalization_commit import (
    FinalizationCommitState,
    confirm_job_clip_finalization,
)
from app.tasks._job_cancel_fence import (
    active_job_for_update,
    delete_task_owned_outputs,
    new_task_run_id,
)
from app.worker import celery_app

log = structlog.get_logger()


def _merge_probe_metadata(existing: object, probe: dict) -> dict:
    """Keep safe upload metadata when adding measured probe fields."""

    metadata = dict(existing) if isinstance(existing, dict) else {}
    source_filename = safe_media_basename(metadata.get("source_filename"))
    if source_filename is None:
        source_filename = safe_media_basename(metadata.get("drive_filename"))
    if source_filename is not None:
        metadata["source_filename"] = source_filename
    metadata.update(probe)
    return metadata


@celery_app.task(
    name="tasks.orchestrate_job",
    bind=True,
    # Retry on transient Postgres outages (incident 2026-05-18 07:45:57Z).
    # 7 retries × deterministic exp backoff (123s budget). retry_jitter=False
    # so the budget is predictable, not halved on average.
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,  # deterministic exp backoff — jitter halves avg budget
    max_retries=7,
    soft_time_limit=1080,
    time_limit=1200,
)
def orchestrate_job(self, job_id: str) -> None:
    log.info("orchestrate_start", job_id=job_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_local = os.path.join(tmpdir, "raw.mp4")
        try:
            # Snapshot raw_storage_path before session closes (matches render_clip pattern)
            with _sync_session() as db:
                job = active_job_for_update(db, job_id, operation="legacy_orchestrate_start")
                if job is None:
                    log.info("legacy_orchestrate_start_skipped", job_id=job_id)
                    return
                raw_storage_path = job.raw_storage_path
                job.status = "processing"
                db.commit()

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
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="legacy_orchestrate_persist_probe",
                )
                if job is None:
                    return
                job.probe_metadata = _merge_probe_metadata(
                    job.probe_metadata,
                    {
                        "duration_s": video_probe.duration_s,
                        "fps": video_probe.fps,
                        "width": video_probe.width,
                        "height": video_probe.height,
                        "has_audio": video_probe.has_audio,
                        "codec": video_probe.codec,
                        "aspect_ratio": video_probe.aspect_ratio,
                        "file_size_bytes": video_probe.file_size_bytes,
                        "color_transfer": video_probe.color_transfer,
                    },
                )
                db.commit()

            # [2] Transcribe (Gemini primary when source_ref available, else Whisper)
            transcript = transcribe_mod.transcribe(
                raw_local,
                file_ref=source_ref,
                job_id=job_id,
            )
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="legacy_orchestrate_persist_transcript",
                )
                if job is None:
                    return
                job.transcript = {
                    "full_text": transcript.full_text,
                    "low_confidence": transcript.low_confidence,
                    "words": [
                        {
                            "text": w.text,
                            "start_s": w.start_s,
                            "end_s": w.end_s,
                            "confidence": w.confidence,
                        }
                        for w in transcript.words
                    ],
                }
                db.commit()

            # [3] Scene detection
            cuts = scene_detect.detect_scenes(raw_local)
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="legacy_orchestrate_persist_scenes",
                )
                if job is None:
                    return
                job.scene_cuts = [{"timestamp_s": c.timestamp_s, "score": c.score} for c in cuts]
                db.commit()

            # [4] Score candidates (Gemini analyzes each segment via source_ref)
            candidates = select_candidates(
                video_probe,
                transcript,
                cuts,
                source_ref=source_ref,
                job_id=job_id,
            )
            top3 = candidates[:TOP_N]
            held = candidates[TOP_N:]

            # Persist all 9 candidates + create JobClip rows for top 3
            clip_db_ids: list[str] = []
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="legacy_orchestrate_persist_candidates",
                )
                if job is None:
                    return
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
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="legacy_orchestrate_timeout",
                )
                if job is not None:
                    job.status = "processing_failed"
                    job.error_detail = "Job timed out (exceeded 1080s soft limit)"
                    db.commit()
            return
        except OperationalError as db_exc:
            # Transient Postgres outage — re-raise for Celery autoretry.
            # See incident 2026-05-18 07:45:57Z.
            log.warning(
                "orchestrate_transient_db_error_retry",
                job_id=job_id,
                error=str(db_exc),
                retry_count=self.request.retries,
            )
            raise
        except Exception as exc:
            log.error("orchestrate_failed", job_id=job_id, error=str(exc))
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation="legacy_orchestrate_failed",
                )
                if job is not None:
                    job.status = "processing_failed"
                    job.error_detail = str(exc)[:1000]
                    db.commit()
            return

    # Fan-out: render top 3 clips in parallel, then finalize
    if not clip_db_ids:
        with _sync_session() as db:
            job = active_job_for_update(
                db,
                job_id,
                operation="legacy_orchestrate_no_segments",
            )
            if job is not None:
                job.status = "processing_failed"
                job.error_detail = "No scoreable segments found"
                db.commit()
        return

    # Final broker checkpoint. The lock is released before apply_async; render
    # task entry fences are the backstop if cancellation wins after this read.
    with _sync_session() as db:
        if (
            active_job_for_update(
                db,
                job_id,
                operation="legacy_orchestrate_before_fanout",
            )
            is None
        ):
            return

    render_tasks = group(render_clip.s(job_id, clip_id) for clip_id in clip_db_ids)
    workflow = chord(render_tasks, finalize_job.s(job_id))
    workflow.apply_async()


@celery_app.task(
    name="tasks.render_clip",
    bind=True,
    # Retry on transient Postgres outages (incident 2026-05-18 07:45:57Z).
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,  # deterministic exp backoff — jitter halves avg budget
    max_retries=7,
    soft_time_limit=540,
    time_limit=600,
)
def render_clip(self, job_id: str, clip_db_id: str) -> dict:
    """Render a single clip. Returns {clip_id, success, error}.

    NEVER raises — errors are caught here so the chord callback always fires.
    """
    log.info("render_clip_start", job_id=job_id, clip_id=clip_db_id)

    with _sync_session() as db:
        job = active_job_for_update(
            db,
            job_id,
            operation=f"legacy_render_clip_{clip_db_id}_start",
        )
        if job is None:
            return {"clip_id": clip_db_id, "success": False, "error": "cancelled"}
        clip = db.get(JobClip, uuid.UUID(clip_db_id), with_for_update=True)
        if clip is None or clip.job_id != job.id:
            return {"clip_id": clip_db_id, "success": False, "error": "DB record not found"}

        clip.render_status = "rendering"

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
        scene_cut_timestamps = [c["timestamp_s"] for c in (job.scene_cuts or [])]
        db.commit()

    created_output_paths: list[str] = []
    finalization_may_have_committed = False
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
                    job_id=job_id,
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
            task_run_id = new_task_run_id()
            clip_gcs_path = f"{user_id}/{job_id}/task-runs/{task_run_id}/clip_{rank}.mp4"
            thumb_gcs_path = f"{user_id}/{job_id}/task-runs/{task_run_id}/thumb_{rank}.jpg"
            created_output_paths = [clip_gcs_path, thumb_gcs_path]

            video_url = upload_public_read(output_path, clip_gcs_path)
            with open(thumb_result.jpeg_path, "rb") as tf:
                upload_bytes_public_read(tf.read(), thumb_gcs_path)

            file_size = os.path.getsize(output_path)
            duration_s = end_s - start_s
            # Matches the GCS lifecycle rule in infra/gcs-lifecycle.json (age=1 day
            # on dev-user/*). The column is informational today (no sweeper reads
            # it); keeping it truthful so a future sweeper isn't surprised.
            expires_at = datetime.now(UTC) + timedelta(days=1)

            finalized = False
            finalize_error = "cancelled"
            poster_cleanup_journaled = False
            finalize_commit_attempted = False
            finalize_commit_error: Exception | None = None
            try:
                with _sync_session() as db:
                    job = active_job_for_update(
                        db,
                        job_id,
                        operation=f"legacy_render_clip_{clip_db_id}_finalize",
                    )
                    if job is not None:
                        clip = db.get(JobClip, uuid.UUID(clip_db_id), with_for_update=True)
                        if clip is not None and clip.job_id == job.id:
                            if job.assembly_plan is not None and not isinstance(
                                job.assembly_plan, dict
                            ):
                                # Never repair corrupt JSONB by replacing it with an
                                # empty object.  Keep both the prior ready clip and
                                # the exact forensic plan untouched; the chord can
                                # surface this render as failed and the task-owned
                                # uploads are removed below.
                                finalize_error = "invalid assembly plan"
                                log.error(
                                    "legacy_render_clip_invalid_assembly_plan",
                                    job_id=job_id,
                                    clip_id=clip_db_id,
                                    assembly_plan_type=type(job.assembly_plan).__name__,
                                )
                            else:
                                plan = copy.deepcopy(job.assembly_plan or {})
                                previous_thumbnail_path = clip.thumbnail_path
                                clip.render_status = "ready"
                                # Keep the existing admin/debug playback contract for
                                # the MP4 URL. The /me library normalizes this GCS URL
                                # back to its object key and re-signs it on every read.
                                clip.video_path = video_url
                                clip.thumbnail_path = thumb_gcs_path
                                clip.duration_s = duration_s
                                clip.file_size_bytes = file_size
                                clip.platform_copy = platform_copy.model_dump()
                                clip.copy_status = copy_status
                                clip.storage_expires_at = expires_at
                                poster_cleanup_journaled = append_video_poster_cleanup_receipt(
                                    plan,
                                    old_path=previous_thumbnail_path,
                                    replacement_path=thumb_gcs_path,
                                )
                                if poster_cleanup_journaled:
                                    job.assembly_plan = plan
                                    flag_modified(job, "assembly_plan")
                                finalize_commit_attempted = True
                                finalization_may_have_committed = True
                                try:
                                    db.commit()
                                    finalized = True
                                except Exception as exc:  # noqa: BLE001 - ambiguous
                                    # Catch inside the session so it closes before the
                                    # independent proof read below.
                                    finalize_commit_error = exc
            except Exception as exc:
                if not finalize_commit_attempted:
                    raise
                finalize_commit_error = finalize_commit_error or exc
            if finalize_commit_error is not None:
                commit_state = confirm_job_clip_finalization(
                    _sync_session,
                    job_id=job_id,
                    clip_id=clip_db_id,
                    expected_clip_fields={
                        "render_status": "ready",
                        "video_path": video_url,
                        "thumbnail_path": thumb_gcs_path,
                    },
                    attempt_references=(*created_output_paths, video_url),
                )
                if commit_state is FinalizationCommitState.CONFIRMED:
                    finalized = True
                    log.warning(
                        "legacy_render_clip_finalize_commit_confirmed_after_error",
                        job_id=job_id,
                        clip_id=clip_db_id,
                        error_class=type(finalize_commit_error).__name__,
                    )
                elif commit_state is FinalizationCommitState.UNKNOWN:
                    # The commit may own live references. Keep the private
                    # attempt objects and let redelivery/lifecycle repair the
                    # row; never turn uncertainty into user-visible data loss.
                    log.error(
                        "legacy_render_clip_finalize_commit_inconclusive",
                        job_id=job_id,
                        clip_id=clip_db_id,
                        error_class=type(finalize_commit_error).__name__,
                    )
                    return {
                        "clip_id": clip_db_id,
                        "success": False,
                        "error": "finalization commit outcome is uncertain",
                        "finalization_uncertain": True,
                    }
                else:
                    finalization_may_have_committed = False
                    raise finalize_commit_error
            if not finalized:
                delete_task_owned_outputs(job_id, created_output_paths)
                return {"clip_id": clip_db_id, "success": False, "error": finalize_error}

            if poster_cleanup_journaled:
                try:
                    reconcile_video_poster_cleanup_receipts(job_id)
                except SoftTimeLimitExceeded:
                    # Ready state, output paths, and the durable receipt were
                    # already committed.  The Beat sweep must retry cleanup;
                    # the outer render timeout handler must not delete this
                    # successfully accepted clip or mark it failed.
                    log.warning(
                        "legacy_render_clip_poster_cleanup_soft_timeout_deferred",
                        job_id=job_id,
                        clip_id=clip_db_id,
                    )
                except Exception as cleanup_exc:  # noqa: BLE001 — receipt retries durably
                    log.warning(
                        "legacy_render_clip_poster_cleanup_deferred",
                        job_id=job_id,
                        clip_id=clip_db_id,
                        error=str(cleanup_exc),
                    )

            log.info("render_clip_done", clip_id=clip_db_id, video_url=video_url)
            return {
                "clip_id": clip_db_id,
                "success": True,
                "error": None,
                "output_paths": created_output_paths,
            }

        except SoftTimeLimitExceeded:
            log.error("render_clip_timeout", clip_id=clip_db_id)
            if finalization_may_have_committed:
                log.error(
                    "legacy_render_clip_timeout_after_finalize_commit_attempt",
                    job_id=job_id,
                    clip_id=clip_db_id,
                    confirmed=finalized,
                )
                return {
                    "clip_id": clip_db_id,
                    "success": finalized,
                    "error": None if finalized else "finalization commit outcome is uncertain",
                    **({"finalization_uncertain": True} if not finalized else {}),
                    **({"output_paths": created_output_paths} if finalized else {}),
                }
            delete_task_owned_outputs(job_id, created_output_paths)
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation=f"legacy_render_clip_{clip_db_id}_timeout",
                )
                if job is not None:
                    clip = db.get(JobClip, uuid.UUID(clip_db_id), with_for_update=True)
                    if clip is not None and clip.job_id == job.id:
                        clip.render_status = "failed"
                        clip.error_detail = "Clip render timed out (exceeded 540s soft limit)"
                        db.commit()
            return {"clip_id": clip_db_id, "success": False, "error": "render timeout"}
        except OperationalError as db_exc:
            if finalization_may_have_committed:
                log.error(
                    "legacy_render_clip_db_error_after_finalize_commit_attempt",
                    job_id=job_id,
                    clip_id=clip_db_id,
                    confirmed=finalized,
                )
                return {
                    "clip_id": clip_db_id,
                    "success": finalized,
                    "error": None if finalized else "finalization commit outcome is uncertain",
                    **({"finalization_uncertain": True} if not finalized else {}),
                    **({"output_paths": created_output_paths} if finalized else {}),
                }
            delete_task_owned_outputs(job_id, created_output_paths)
            # Transient Postgres outage — re-raise for Celery autoretry. The
            # chord callback (finalize_job) sees the eventual retry result;
            # if all retries are exhausted, Celery will mark the task FAILED
            # and the on-boot reaper + Beat sweeper will catch the row.
            log.warning(
                "render_clip_transient_db_error_retry",
                clip_id=clip_db_id,
                error=str(db_exc),
                retry_count=self.request.retries,
            )
            raise
        except Exception as exc:
            log.error("render_clip_failed", clip_id=clip_db_id, error=str(exc))
            if finalization_may_have_committed:
                log.error(
                    "legacy_render_clip_error_after_finalize_commit_attempt",
                    job_id=job_id,
                    clip_id=clip_db_id,
                    confirmed=finalized,
                )
                return {
                    "clip_id": clip_db_id,
                    "success": finalized,
                    "error": None if finalized else "finalization commit outcome is uncertain",
                    **({"finalization_uncertain": True} if not finalized else {}),
                    **({"output_paths": created_output_paths} if finalized else {}),
                }
            delete_task_owned_outputs(job_id, created_output_paths)
            with _sync_session() as db:
                job = active_job_for_update(
                    db,
                    job_id,
                    operation=f"legacy_render_clip_{clip_db_id}_failed",
                )
                if job is not None:
                    clip = db.get(JobClip, uuid.UUID(clip_db_id), with_for_update=True)
                    if clip is not None and clip.job_id == job.id:
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
    uncertain = [r for r in render_results if r.get("finalization_uncertain") is True]
    successes = [r for r in render_results if r.get("success")]
    failures = [r for r in render_results if not r.get("success")]

    log.info(
        "finalize_job",
        job_id=job_id,
        successes=len(successes),
        failures=len(failures),
        uncertain=len(uncertain),
    )

    if uncertain:
        # At least one child may already have committed its ready clip while
        # losing the COMMIT acknowledgement.  A chord-level failure/partial
        # write would overwrite that potentially accepted state.  Leave the
        # job non-terminal for the normal recovery path and retain every
        # attempt-owned object until the ambiguity can be reconciled.
        log.error(
            "finalize_job_deferred_for_uncertain_clip_commit",
            job_id=job_id,
            uncertain_clip_ids=[result.get("clip_id") for result in uncertain],
        )
        return

    finalized_status: str | None = None
    with _sync_session() as db:
        job = active_job_for_update(db, job_id, operation="legacy_finalize_job")
        if job is not None:
            if len(successes) == 0:
                job.status = "processing_failed"
                job.error_detail = "All clip renders failed"
            elif len(failures) > 0:
                job.status = "clips_ready_partial"
            else:
                job.status = "clips_ready"

            db.commit()
            finalized_status = job.status

    if finalized_status is None:
        delete_task_owned_outputs(
            job_id,
            [str(path) for result in successes for path in (result.get("output_paths") or [])],
        )
        return

    log.info("job_finalized", job_id=job_id, status=finalized_status)


# ── Helpers ──────────────────────────────────────────────────────────────────


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
