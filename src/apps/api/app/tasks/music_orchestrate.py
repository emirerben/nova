"""Celery tasks for music beat-sync jobs.

analyze_music_track_task(track_id)
  → download audio from GCS (already uploaded by audio_download.py)
  → _detect_audio_beats()           — FFmpeg energy onset detection
  → auto_best_section()             — 45s peak-density window
  → store beat_timestamps_s + track_config in MusicTrack

orchestrate_music_job(job_id)
  → load MusicTrack (must be ready + audio_gcs_path present)
  → generate_music_recipe()         — beat-based slots
  → parallel Gemini analyze_clip() × N
  → template_matcher.match()        — unchanged
  → _assemble_clips()               — unchanged (receives beat_timestamps_s)
  → _mix_template_audio()           — music track replaces template audio
  → set job.status = music_ready

Both tasks follow the "never raises" invariant:
  all errors caught → job/track status = 'failed' with error_detail
"""

import math
import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.pipeline.music_recipe import auto_best_section, generate_music_recipe
from app.storage import download_to_file
from app.tasks.template_orchestrate import (
    _analyze_clips_parallel,
    _assemble_clips,
    _detect_audio_beats,
    _download_clips_parallel,
    _enrich_slots_with_energy,
    _mix_template_audio,
    _probe_clips,
    _upload_clips_parallel,
)
from app.worker import celery_app

try:
    from app.pipeline.agents.gemini_analyzer import (
        GeminiAnalysisError,
    )
    from app.pipeline.template_matcher import TemplateMismatchError, consolidate_slots, match
except ImportError:
    GeminiAnalysisError = Exception  # type: ignore[assignment, misc]
    TemplateMismatchError = Exception  # type: ignore[assignment, misc]

log = structlog.get_logger()


# ── analyze_music_track_task ──────────────────────────────────────────────────


@celery_app.task(
    name="tasks.analyze_music_track_task",
    bind=True,
    max_retries=0,
    soft_time_limit=300,
    time_limit=360,
)
def analyze_music_track_task(self, track_id: str) -> None:
    """Detect beats + auto-select best section for a music track."""
    log.info("analyze_music_track_start", track_id=track_id)

    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            log.error("music_track_not_found", track_id=track_id)
            return
        if not track.audio_gcs_path:
            log.error("music_track_no_audio_gcs_path", track_id=track_id)
            _fail_track(track_id, "Audio not yet uploaded to GCS — cannot analyze.")
            return
        audio_gcs = track.audio_gcs_path
        duration_s = track.duration_s
        existing_config = track.track_config or {}

        track.analysis_status = "analyzing"
        track.error_detail = None
        db.commit()

    try:
        with tempfile.TemporaryDirectory(prefix="nova_music_analyze_") as tmpdir:
            local_audio = os.path.join(tmpdir, "audio.m4a")
            download_to_file(audio_gcs, local_audio)

            beats = _detect_audio_beats(local_audio)
            log.info("music_beats_detected", track_id=track_id, count=len(beats))

            # Auto-select best section only if not already admin-configured
            best_start: float = float(existing_config.get("best_start_s", 0.0))
            best_end: float = float(existing_config.get("best_end_s", 0.0))
            if best_end <= best_start:
                best_start, best_end = auto_best_section(
                    beats,
                    window_s=45.0,
                    track_duration_s=float(duration_s or 0.0),
                )

            # Slot count from best section
            n = int(existing_config.get("slot_every_n_beats", 8))
            window_beats = [b for b in beats if best_start <= b <= best_end]
            n_slots = max(0, (len(window_beats) - n) // n + (1 if len(window_beats) > n else 0))

            new_config = {
                **existing_config,
                "best_start_s": round(best_start, 3),
                "best_end_s": round(best_end, 3),
                "slot_every_n_beats": n,
                "required_clips_min": max(1, math.floor(n_slots / 2)),
                "required_clips_max": max(1, n_slots),
            }

        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
            if track:
                track.beat_timestamps_s = beats
                track.track_config = new_config
                track.analysis_status = "ready"
                db.commit()

        log.info(
            "analyze_music_track_done",
            track_id=track_id,
            beats=len(beats),
            best_start=best_start,
            best_end=best_end,
            n_slots=n_slots,
        )

    except Exception as exc:
        log.error("analyze_music_track_failed", track_id=track_id, error=str(exc))
        _fail_track(track_id, str(exc))


# ── orchestrate_music_job ─────────────────────────────────────────────────────


@celery_app.task(
    name="tasks.orchestrate_music_job",
    bind=True,
    max_retries=0,
    soft_time_limit=1080,
    time_limit=1200,
)
def orchestrate_music_job(self, job_id: str) -> None:
    """Full beat-sync pipeline for a music job."""
    log.info("music_job_start", job_id=job_id)
    try:
        _run_music_job(job_id)
    except Exception as exc:
        log.error("music_job_failed", job_id=job_id, error=str(exc), exc_info=True)
        _fail_job(job_id, str(exc))


def _run_music_job(job_id: str) -> None:
    """Core music job logic. May raise — caller wraps in try/except."""

    # [1] Load job + track
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("music_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        db.commit()

        music_track_id = job.music_track_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs: list[str] = all_candidates.get("clip_paths", [])

    if not clip_paths_gcs:
        raise ValueError("No clip paths found in job")
    if not music_track_id:
        raise ValueError("Job has no music_track_id")

    # [2] Load music track — must be ready with audio
    with _sync_session() as db:
        track = db.get(MusicTrack, music_track_id)
        if track is None:
            raise ValueError(f"MusicTrack {music_track_id} not found")
        if track.analysis_status != "ready":
            raise ValueError(
                f"MusicTrack {music_track_id} is not ready (status: {track.analysis_status})"
            )
        if not track.audio_gcs_path:
            raise ValueError(
                f"MusicTrack {music_track_id} has no audio_gcs_path — "
                "analysis must have failed silently. Re-analyze the track."
            )

        track_data = {
            "beat_timestamps_s": track.beat_timestamps_s or [],
            "track_config": track.track_config or {},
            "duration_s": track.duration_s,
        }
        audio_gcs_path = track.audio_gcs_path

    # [3] Generate recipe from beats
    recipe_dict = generate_music_recipe(track_data)

    # Build a TemplateRecipe-compatible object (use the existing dataclass)
    from app.pipeline.agents.gemini_analyzer import TemplateRecipe  # noqa: PLC0415
    try:
        recipe = TemplateRecipe(**recipe_dict)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Failed to build TemplateRecipe from music recipe: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="nova_music_job_") as tmpdir:
        # [4] Download clips in parallel
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)

        # [5] Probe clips
        probe_map = _probe_clips(local_clip_paths)

        # [6] Upload to Gemini + analyze in parallel
        log.info("gemini_upload_clips_start", job_id=job_id, count=len(local_clip_paths))
        file_refs = _upload_clips_parallel(local_clip_paths)
        log.info("gemini_upload_clips_done", job_id=job_id)

        log.info("gemini_analyze_clips_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(file_refs, local_clip_paths, probe_map)

        total = len(clip_metas) + failed_count
        if failed_count > total * 0.5:
            raise GeminiAnalysisError(
                f"{failed_count}/{total} clips failed Gemini analysis"
            )
        log.info(
            "gemini_analyze_clips_done",
            job_id=job_id,
            success=len(clip_metas),
            failed=failed_count,
        )

        # [7] Enrich slots with energy from beats
        enriched_slots = _enrich_slots_with_energy(recipe_dict["slots"], track_data["beat_timestamps_s"])
        recipe_dict = {**recipe_dict, "slots": enriched_slots}
        recipe = TemplateRecipe(**recipe_dict)

        # [8] Template match (slot consolidation + greedy assignment)
        log.info("template_match_start", job_id=job_id)
        try:
            recipe = consolidate_slots(recipe, clip_metas)
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        clip_id_to_gcs = {ref.name: gcs for ref, gcs in zip(file_refs, clip_paths_gcs)}
        plan_data = {
            "steps": [
                {
                    "slot": step.slot,
                    "clip_id": step.clip_id,
                    "clip_gcs_path": clip_id_to_gcs.get(step.clip_id) or "",
                    "moment": step.moment,
                }
                for step in assembly_plan.steps
            ]
        }
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.assembly_plan = plan_data
                db.commit()

        # [9] FFmpeg assemble
        log.info("ffmpeg_assemble_start", job_id=job_id)
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        clip_id_to_local = {
            ref.name: path for ref, path in zip(file_refs, local_clip_paths)
        }
        _assemble_clips(
            assembly_plan.steps,
            clip_id_to_local,
            probe_map,
            assembled_path,
            tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=clip_metas,
            global_color_grade=recipe.color_grade,
            job_id=job_id,
            user_subject="",
            interstitials=[],
        )

        # [10] Mix in music track audio
        log.info("mix_audio_start", job_id=job_id)
        final_path = os.path.join(tmpdir, "final.mp4")
        _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)

        # [11] Upload final video
        from app.storage import upload_public_read  # noqa: PLC0415
        output_gcs = f"music-jobs/{job_id}/output.mp4"
        upload_public_read(final_path, output_gcs)
        log.info("music_job_uploaded", job_id=job_id, gcs_path=output_gcs)

    # [12] Mark done
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job:
            job.status = "music_ready"
            existing_plan = job.assembly_plan or {}
            job.assembly_plan = {**existing_plan, "output_url": output_gcs}
            db.commit()

    log.info("music_job_done", job_id=job_id)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fail_job(job_id: str, error_detail: str) -> None:
    try:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "processing_failed"
                job.error_detail = error_detail[:2000]
                db.commit()
    except Exception as exc:
        log.error("fail_job_db_error", job_id=job_id, error=str(exc))


def _fail_track(track_id: str, error_detail: str) -> None:
    try:
        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
            if track:
                track.analysis_status = "failed"
                track.error_detail = error_detail[:2000]
                db.commit()
    except Exception as exc:
        log.error("fail_track_db_error", track_id=track_id, error=str(exc))
