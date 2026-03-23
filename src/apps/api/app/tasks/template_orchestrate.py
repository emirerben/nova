"""Celery tasks for template-mode jobs.

orchestrate_template_job(job_id)
  → parallel Gemini upload × N clips
  → parallel analyze_clip × N
  → threshold check (>50% fail → fatal)
  → template_match (greedy + variety penalty)
  → ffmpeg_assemble (concat + reframe each slot)
  → generate_copy (with template tone)
  → set job.status = template_ready

analyze_template_task(template_id)
  → download template video from GCS
  → Gemini upload + analyze_template
  → cache recipe in VideoTemplate.recipe_cached

Both tasks follow the render_clip "never raises" invariant:
all errors caught → job.status = 'processing_failed'
"""

import json
import os
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, VideoTemplate
from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    GeminiRefusalError,
    TemplateRecipe,
    analyze_clip,
    analyze_template,
    gemini_upload_and_wait,
)
from app.pipeline.template_matcher import TemplateMismatchError, match
from app.storage import download_to_file, upload_public_read
from app.worker import celery_app

log = structlog.get_logger()

# Sync engine (same pattern as orchestrate.py)
_sync_db_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
_sync_engine = create_engine(_sync_db_url)


def _sync_session() -> Session:
    return Session(_sync_engine, expire_on_commit=False)


# ── analyze_template_task ─────────────────────────────────────────────────────


@celery_app.task(name="tasks.analyze_template_task", bind=True, max_retries=0, time_limit=600)
def analyze_template_task(self, template_id: str) -> None:
    """Download template video, analyze with Gemini, cache recipe in DB."""
    log.info("analyze_template_start", template_id=template_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "template.mp4")
        try:
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template is None:
                    log.error("template_not_found", template_id=template_id)
                    return
                gcs_path = template.gcs_path

            download_to_file(gcs_path, local_path)

            file_ref = gemini_upload_and_wait(local_path)
            recipe = analyze_template(file_ref)

            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    from datetime import datetime, timezone  # noqa: PLC0415
                    template.recipe_cached = {
                        "shot_count": recipe.shot_count,
                        "total_duration_s": recipe.total_duration_s,
                        "hook_duration_s": recipe.hook_duration_s,
                        "slots": recipe.slots,
                        "copy_tone": recipe.copy_tone,
                        "caption_style": recipe.caption_style,
                    }
                    template.recipe_cached_at = datetime.now(timezone.utc)
                    template.analysis_status = "ready"
                    db.commit()

            log.info("analyze_template_done", template_id=template_id, slots=len(recipe.slots))

        except Exception as exc:
            log.error("analyze_template_failed", template_id=template_id, error=str(exc))
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    db.commit()


# ── orchestrate_template_job ──────────────────────────────────────────────────


@celery_app.task(name="tasks.orchestrate_template_job", bind=True, max_retries=0, time_limit=1800)
def orchestrate_template_job(self, job_id: str) -> None:
    """Full template-mode pipeline. Never raises — all errors go to processing_failed."""
    log.info("template_job_start", job_id=job_id)

    try:
        _run_template_job(job_id)
    except Exception as exc:
        log.error("template_job_fatal", job_id=job_id, error=str(exc))
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "processing_failed"
                job.error_detail = str(exc)[:1000]
                db.commit()


def _run_template_job(job_id: str) -> None:
    """Core template job logic. May raise — caller wraps in try/except."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("template_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        db.commit()

        # Snapshot fields before session closes
        template_id = job.template_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs = all_candidates.get("clip_paths", [])
        selected_platforms = job.selected_platforms or ["tiktok", "instagram", "youtube"]

    if not clip_paths_gcs:
        raise ValueError("No clip paths found in job")

    # [1] Load template recipe from DB cache
    with _sync_session() as db:
        template = db.get(VideoTemplate, template_id)
        if template is None:
            raise ValueError(f"Template {template_id} not found")
        if template.analysis_status != "ready" or not template.recipe_cached:
            raise ValueError(f"Template {template_id} analysis not ready")

        recipe_data = template.recipe_cached
        template_gcs_path = template.gcs_path

    try:
        recipe = TemplateRecipe(**recipe_data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Template recipe in DB is malformed: {exc}") from exc

    with tempfile.TemporaryDirectory() as tmpdir:
        # [2] Download all clip files in parallel
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)

        # [3] Upload all clips to Gemini in parallel
        log.info("gemini_upload_clips_start", job_id=job_id, count=len(local_clip_paths))
        file_refs = _upload_clips_parallel(local_clip_paths)
        log.info("gemini_upload_clips_done", job_id=job_id)

        # [4] Analyze all clips in parallel
        log.info("gemini_analyze_clips_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(file_refs, local_clip_paths)

        # Threshold check: >50% failure → fatal
        total = len(clip_metas) + failed_count
        if failed_count > total * 0.5:
            raise GeminiAnalysisError(
                f"{failed_count}/{total} clips failed Gemini analysis — "
                "too many failures to produce quality output"
            )
        log.info(
            "gemini_analyze_clips_done",
            job_id=job_id,
            success=len(clip_metas),
            failed=failed_count,
        )

        # [5] Template match
        log.info("template_match_start", job_id=job_id)
        try:
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        # Persist assembly plan
        plan_data = {
            "steps": [
                {
                    "slot": step.slot,
                    "clip_id": step.clip_id,
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

        # [6] FFmpeg assemble
        log.info("ffmpeg_assemble_start", job_id=job_id)
        output_path = os.path.join(tmpdir, "assembled.mp4")
        clip_id_to_local = {
            ref.name: path for ref, path in zip(file_refs, local_clip_paths)
        }
        _assemble_clips(assembly_plan.steps, clip_id_to_local, output_path, tmpdir)

        # [7] Generate copy (with template tone)
        hook_text = _extract_hook_text(clip_metas, assembly_plan.steps)
        transcript_excerpt = _extract_transcript(clip_metas, assembly_plan.steps)
        from app.pipeline.agents.copy_writer import generate_copy  # noqa: PLC0415
        platform_copy, copy_status = generate_copy(
            hook_text=hook_text,
            transcript_excerpt=transcript_excerpt,
            platforms=selected_platforms,
            has_transcript=bool(transcript_excerpt),
            template_tone=recipe.copy_tone,
        )

        # [8] Upload assembled video to GCS
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        video_url = upload_public_read(output_path, gcs_output_path)

        # [9] Finalize
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "template_ready"
                job.assembly_plan = {
                    **plan_data,
                    "output_url": video_url,
                    "platform_copy": platform_copy.model_dump(),
                    "copy_status": copy_status,
                }
                db.commit()

        log.info("template_job_done", job_id=job_id, output_url=video_url)


# ── Parallelism helpers ────────────────────────────────────────────────────────


def _download_clips_parallel(gcs_paths: list[str], tmpdir: str) -> list[str]:
    """Download all clips from GCS in parallel. Returns local file paths."""
    local_paths = [os.path.join(tmpdir, f"clip_{i}.mp4") for i in range(len(gcs_paths))]

    def _download_one(args: tuple[str, str]) -> str:
        gcs_path, local_path = args
        download_to_file(gcs_path, local_path)
        return local_path

    with ThreadPoolExecutor(max_workers=min(len(gcs_paths), 8)) as pool:
        list(pool.map(_download_one, zip(gcs_paths, local_paths)))

    return local_paths


def _upload_clips_parallel(local_paths: list[str]) -> list:
    """Upload all clips to Gemini File API in parallel. Returns file refs in order."""
    results: dict[int, object] = {}

    def _upload_one(idx_path: tuple[int, str]) -> tuple[int, object]:
        idx, path = idx_path
        ref = gemini_upload_and_wait(path)
        return idx, ref

    with ThreadPoolExecutor(max_workers=min(len(local_paths), 8)) as pool:
        futures = {pool.submit(_upload_one, (i, p)): i for i, p in enumerate(local_paths)}
        for future in as_completed(futures):
            idx, ref = future.result()
            results[idx] = ref

    return [results[i] for i in range(len(local_paths))]


def _analyze_clips_parallel(
    file_refs: list, local_paths: list[str]
) -> tuple[list[ClipMeta], int]:
    """Analyze all clips with Gemini in parallel.

    Returns (successful_metas, failed_count).
    Per-clip failures are logged but don't raise — caller applies threshold.
    """
    clip_metas: list[ClipMeta] = []
    failed_count = 0

    def _analyze_one(args: tuple[int, object, str]) -> tuple[ClipMeta | None, str | None]:
        idx, ref, path = args
        try:
            meta = analyze_clip(ref)
            meta.clip_path = path
            return meta, None
        except (GeminiRefusalError, GeminiAnalysisError, Exception) as exc:
            log.warning("clip_analysis_failed", clip_idx=idx, error=str(exc))
            # Whisper heuristic fallback for failed clips
            from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
            try:
                transcript = transcribe_whisper(path)
                fallback_meta = ClipMeta(
                    clip_id=getattr(ref, "name", f"clip_{idx}"),
                    transcript=transcript.full_text,
                    hook_text=transcript.full_text[:100] if transcript.full_text else "",
                    hook_score=5.0,
                    best_moments=[{"start_s": 0.0, "end_s": 15.0, "energy": 5.0, "description": ""}],
                    analysis_degraded=True,
                    clip_path=path,
                )
                return fallback_meta, None
            except Exception as whisper_exc:
                log.warning("whisper_fallback_failed", clip_idx=idx, error=str(whisper_exc))
                return None, str(exc)

    with ThreadPoolExecutor(max_workers=min(len(file_refs), 8)) as pool:
        futures = [
            pool.submit(_analyze_one, (i, ref, path))
            for i, (ref, path) in enumerate(zip(file_refs, local_paths))
        ]
        for future in as_completed(futures):
            meta, error = future.result()
            if meta is not None:
                clip_metas.append(meta)
            else:
                failed_count += 1

    return clip_metas, failed_count


# ── FFmpeg assembly ────────────────────────────────────────────────────────────


def _assemble_clips(
    steps: list,
    clip_id_to_local: dict[str, str],
    output_path: str,
    tmpdir: str,
) -> None:
    """Assemble clips in slot order using FFmpeg concat.

    Each step is trimmed to the moment's [start_s, end_s] then reframed to 9:16.
    """
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

    # Step 1: reframe each slot to a temp file
    reframed_paths: list[str] = []
    for i, step in enumerate(steps):
        clip_id = step.clip_id
        local_path = clip_id_to_local.get(clip_id)
        if not local_path or not os.path.exists(local_path):
            raise ValueError(f"Local path not found for clip_id {clip_id}")

        moment = step.moment
        start_s = float(moment.get("start_s", 0.0))
        end_s = float(moment.get("end_s", start_s + 5.0))
        reframed = os.path.join(tmpdir, f"slot_{i}.mp4")

        reframe_and_export(
            input_path=local_path,
            start_s=start_s,
            end_s=end_s,
            aspect_ratio="9:16",
            ass_subtitle_path=None,
            output_path=reframed,
        )
        reframed_paths.append(reframed)

    if not reframed_paths:
        raise ValueError("No slots to assemble")

    if len(reframed_paths) == 1:
        # Single slot — just copy
        import shutil  # noqa: PLC0415
        shutil.copy2(reframed_paths[0], output_path)
        return

    # Step 2: write concat list
    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in reframed_paths:
            f.write(f"file '{p}'\n")

    # Step 3: FFmpeg concat (demuxer — fast, no re-encode)
    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        "-y",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg concat failed: {result.stderr.decode()[:300]}")


# ── Copy helpers ───────────────────────────────────────────────────────────────


def _extract_hook_text(clip_metas: list[ClipMeta], steps: list) -> str:
    """Get hook text from the first step's clip."""
    if not steps:
        return ""
    first_clip_id = steps[0].clip_id
    for meta in clip_metas:
        if meta.clip_id == first_clip_id:
            return meta.hook_text or ""
    return ""


def _extract_transcript(clip_metas: list[ClipMeta], steps: list) -> str:
    """Concatenate transcripts from clips used in the assembly."""
    used_clip_ids = {step.clip_id for step in steps}
    parts = [
        meta.transcript
        for meta in clip_metas
        if meta.clip_id in used_clip_ids and meta.transcript
    ]
    return " ".join(parts)[:500]
