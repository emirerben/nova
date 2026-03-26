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
  → extract template audio → GCS (idempotent)
  → cache recipe in VideoTemplate.recipe_cached

Both tasks follow the render_clip "never raises" invariant:
all errors caught → job.status = 'processing_failed'
"""

import bisect
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC

import structlog
from sqlalchemy import create_engine
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
                existing_audio_gcs = template.audio_gcs_path

            download_to_file(gcs_path, local_path)

            file_ref = gemini_upload_and_wait(local_path)
            recipe = analyze_template(file_ref)

            # Extract template audio (idempotent — skip if already stored)
            audio_gcs: str | None = existing_audio_gcs
            audio_local = os.path.join(tmpdir, "audio.m4a")
            if not existing_audio_gcs:
                if _extract_template_audio(local_path, audio_local):
                    audio_gcs = f"templates/{template_id}/audio.m4a"
                    upload_public_read(audio_local, audio_gcs)
                    log.info("template_audio_extracted", template_id=template_id)
            elif not os.path.exists(audio_local):
                # Re-analysis: audio already in GCS but not local — download for beat detection
                try:
                    download_to_file(existing_audio_gcs, audio_local)
                except Exception as exc:
                    log.warning("template_audio_redownload_failed", error=str(exc))

            # Beat detection: merge Gemini beats with FFmpeg energy onsets
            ffmpeg_beats = (
                _detect_audio_beats(audio_local)
                if os.path.exists(audio_local)
                else []
            )
            merged_beats = _merge_beat_sources(recipe.beat_timestamps_s, ffmpeg_beats)

            # Compute per-slot energy from beat density
            enriched_slots = _enrich_slots_with_energy(recipe.slots, merged_beats)

            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    from datetime import datetime  # noqa: PLC0415
                    template.recipe_cached = {
                        "shot_count": recipe.shot_count,
                        "total_duration_s": recipe.total_duration_s,
                        "hook_duration_s": recipe.hook_duration_s,
                        "slots": enriched_slots,
                        "copy_tone": recipe.copy_tone,
                        "caption_style": recipe.caption_style,
                        "beat_timestamps_s": merged_beats,
                    }
                    template.recipe_cached_at = datetime.now(UTC)
                    template.analysis_status = "ready"
                    if audio_gcs and not template.audio_gcs_path:
                        template.audio_gcs_path = audio_gcs
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
        user_subject = all_candidates.get("subject", "")
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
        audio_gcs_path = template.audio_gcs_path  # may be None

    try:
        recipe = TemplateRecipe(**recipe_data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Template recipe in DB is malformed: {exc}") from exc

    with tempfile.TemporaryDirectory() as tmpdir:
        # [2] Download all clip files in parallel
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)

        # [2b] Probe all clips — get aspect_ratio + duration in a single FFprobe pass
        probe_map = _probe_clips(local_clip_paths)

        # [3] Upload all clips to Gemini in parallel
        log.info("gemini_upload_clips_start", job_id=job_id, count=len(local_clip_paths))
        file_refs = _upload_clips_parallel(local_clip_paths)
        log.info("gemini_upload_clips_done", job_id=job_id)

        # [4] Analyze all clips in parallel
        log.info("gemini_analyze_clips_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(file_refs, local_clip_paths, probe_map)

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
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        clip_id_to_local = {
            ref.name: path for ref, path in zip(file_refs, local_clip_paths)
        }
        _assemble_clips(
            assembly_plan.steps, clip_id_to_local, probe_map,
            assembled_path, tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=clip_metas,
            global_color_grade=recipe.copy_tone if hasattr(recipe, "copy_tone") else "none",
            job_id=job_id,
            user_subject=user_subject,
        )

        # [6b] Mix template audio if available
        if audio_gcs_path:
            final_path = os.path.join(tmpdir, "final.mp4")
            _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)
            output_path = final_path
        else:
            output_path = assembled_path

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


def _probe_clips(local_paths: list[str]) -> dict:
    """Return {local_path: VideoProbe} for each clip. Falls back on error.

    Replaces _get_clip_duration() — probe_video() returns both aspect_ratio
    and duration_s in a single FFprobe call.
    """
    from app.pipeline.probe import VideoProbe, probe_video  # noqa: PLC0415

    _FALLBACK_DURATION = 30.0

    result: dict = {}
    for path in local_paths:
        try:
            result[path] = probe_video(path)
        except Exception as exc:
            log.warning("clip_probe_failed_using_default", path=path, error=str(exc))
            result[path] = VideoProbe(
                duration_s=_FALLBACK_DURATION,
                fps=30.0,
                width=1920,
                height=1080,
                has_audio=True,
                codec="h264",
                aspect_ratio="16:9",
                file_size_bytes=0,
            )
    return result


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
    file_refs: list, local_paths: list[str], probe_map: dict | None = None
) -> tuple[list[ClipMeta], int]:
    """Analyze all clips with Gemini in parallel.

    Returns (successful_metas, failed_count).
    Per-clip failures are logged but don't raise — caller applies threshold.
    probe_map is used in the Whisper fallback path to get clip duration without
    a second FFprobe call.
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
                clip_dur = (
                    probe_map[path].duration_s
                    if probe_map and path in probe_map
                    else 30.0
                )
                fallback_meta = ClipMeta(
                    clip_id=getattr(ref, "name", f"clip_{idx}"),
                    transcript=transcript.full_text,
                    hook_text=transcript.full_text[:100] if transcript.full_text else "",
                    hook_score=5.0,
                    best_moments=_fallback_moments(clip_dur),
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


# ── Fallback helpers ──────────────────────────────────────────────────────────


def _fallback_moments(clip_dur: float) -> list[dict]:
    """Generate overlapping moments at multiple durations for Whisper-fallback clips.

    Covers short (3–5s), medium (8–12s), and long (15s+) slot ranges so that
    a fallback clip can satisfy template slots of any target_duration_s.
    """
    moments = [
        {"start_s": 0.0, "end_s": min(clip_dur, 5.0), "energy": 5.0, "description": "fallback"},
        {"start_s": 0.0, "end_s": min(clip_dur, 10.0), "energy": 5.0, "description": "fallback"},
        {"start_s": 0.0, "end_s": min(clip_dur, 15.0), "energy": 5.0, "description": "fallback"},
    ]
    # Add a full-clip moment only if it meaningfully extends beyond 15s
    if clip_dur > 21.0:
        moments.append(
            {"start_s": 0.0, "end_s": clip_dur, "energy": 5.0, "description": "fallback"}
        )
    return moments


# ── FFmpeg assembly ────────────────────────────────────────────────────────────
#
# Pipeline:
#   for each slot → _render_slot() → slot_i.mp4
#   join slots   → _join_or_concat() → assembled.mp4
#
# _render_slot encapsulates: cursor logic, beat-snap, speed ramp source-duration,
# text overlay routing (ASS animated vs PNG static), color grading, and reframe.
# ──────────────────────────────────────────────────────────────────────────────


def _render_slot(
    step,
    clip_path: str,
    probe,
    beats: list[float],
    clip_cursors: dict[str, float],
    cumulative_s: float,
    slot_idx: int,
    clip_metas: list | None,
    global_color_grade: str,
    tmpdir: str,
    subject: str = "",
) -> tuple[str, float, float]:
    """Render a single slot to a temp .mp4 file.

    Returns (reframed_path, slot_visual_duration, updated_cumulative_s).

    Speed ramp: source_duration = slot_target_dur * speed_factor
    (2x speed needs 2x source footage compressed into slot_target_dur).
    """
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415
    from app.pipeline.text_overlay import (  # noqa: PLC0415
        ASS_ANIMATED_EFFECTS,
        generate_animated_overlay_ass,
        generate_text_overlay_png,
    )

    clip_id = step.clip_id
    moment = step.moment
    slot_target_dur = float(step.slot.get("target_duration_s", 5.0))
    slot_target_dur = max(slot_target_dur, 0.5)

    # Beat-snap: adjust slot duration so the cut lands on a musical beat
    if beats:
        expected_end = cumulative_s + slot_target_dur
        snapped_end = _snap_to_beat(expected_end, beats)
        slot_target_dur = max(0.5, snapped_end - cumulative_s)
        cumulative_s = snapped_end
    else:
        cumulative_s += slot_target_dur

    # Speed ramp: source_duration = slot_target_dur * speed_factor
    # 2x speed (speed_factor=2.0) needs 2x source footage → setpts=PTS/2.0 compresses it
    speed_factor = float(step.slot.get("speed_factor", 1.0))
    source_duration = slot_target_dur * speed_factor

    # Time-cursor logic
    if clip_id not in clip_cursors:
        start_s = float(moment.get("start_s", 0.0))
    else:
        start_s = clip_cursors[clip_id]

    clip_dur = probe.duration_s if probe else 30.0
    if start_s + source_duration > clip_dur:
        start_s = max(0.0, clip_dur - source_duration)
        log.warning(
            "clip_footage_exhausted",
            clip_id=clip_id,
            position=step.slot.get("position"),
            clip_dur=clip_dur,
            cursor=clip_cursors.get(clip_id, 0.0),
        )

    clip_cursors[clip_id] = start_s + source_duration
    end_s = min(start_s + source_duration, clip_dur)

    aspect_ratio = probe.aspect_ratio if probe else "16:9"

    # ── Text overlay routing (ASS animated vs PNG static) ─────────
    text_overlay_pngs: list[dict] | None = None
    ass_overlay_paths: list[str] | None = None
    darkening_windows: list[tuple[float, float]] | None = None
    narrowing_windows: list[tuple[float, float]] | None = None

    raw_overlays = step.slot.get("text_overlays", [])
    if raw_overlays:
        clip_meta = _find_clip_meta_by_id(clip_id, clip_metas)
        resolved_overlays = []
        dark_wins: list[tuple[float, float]] = []
        narrow_wins: list[tuple[float, float]] = []

        for ov in raw_overlays:
            text = _resolve_overlay_text(ov.get("role", "label"), clip_meta, ov, subject=subject)
            if not text or not text.strip():
                continue

            ov_start = float(ov.get("start_s", 0.0))
            ov_end = float(ov.get("end_s", slot_target_dur))
            resolved_overlays.append({
                "text": text,
                "start_s": ov_start,
                "end_s": ov_end,
                "position": ov.get("position", "center"),
                "effect": ov.get("effect", "none"),
                "font_style": ov.get("font_style", "sans"),
                "text_size": ov.get("text_size", "medium"),
                "text_color": ov.get("text_color", "#FFFFFF"),
            })
            if ov.get("has_darkening"):
                dark_wins.append((ov_start, ov_end))
            if ov.get("has_narrowing"):
                narrow_wins.append((ov_start, ov_end))

        if resolved_overlays:
            # Always generate styled PNGs for ALL overlays (fallback + static path)
            text_overlay_pngs = generate_text_overlay_png(
                resolved_overlays, slot_target_dur, tmpdir, slot_idx,
            )

            # Additionally generate ASS for animated effects (preferred rendering)
            ass_overlays = [
                o for o in resolved_overlays if o["effect"] in ASS_ANIMATED_EFFECTS
            ]
            if ass_overlays:
                ass_overlay_paths = generate_animated_overlay_ass(
                    ass_overlays, slot_target_dur, tmpdir, slot_idx,
                )

            if text_overlay_pngs or ass_overlay_paths:
                darkening_windows = dark_wins or None
                narrowing_windows = narrow_wins or None
                log.info(
                    "slot_text_overlay",
                    slot=slot_idx,
                    png_overlays=len(text_overlay_pngs or []),
                    ass_overlays=len(ass_overlay_paths or []),
                )

    # ── Color grading ─────────────────────────────────────────────
    slot_color = step.slot.get("color_hint") or global_color_grade or "none"

    log.debug(
        "slot_timed",
        position=step.slot.get("position"),
        target_s=slot_target_dur,
        actual_s=round(end_s - start_s, 2),
        speed_factor=speed_factor,
    )

    reframed = os.path.join(tmpdir, f"slot_{slot_idx}.mp4")
    reframe_and_export(
        input_path=clip_path,
        start_s=start_s,
        end_s=end_s,
        aspect_ratio=aspect_ratio,
        ass_subtitle_path=None,
        output_path=reframed,
        text_overlay_pngs=text_overlay_pngs,
        ass_overlay_paths=ass_overlay_paths,
        color_hint=slot_color,
        speed_factor=speed_factor,
        darkening_windows=darkening_windows,
        narrowing_windows=narrowing_windows,
    )
    return reframed, slot_target_dur, cumulative_s


def _assemble_clips(
    steps: list,
    clip_id_to_local: dict[str, str],
    clip_probe_map: dict,
    output_path: str,
    tmpdir: str,
    beat_timestamps_s: list[float] | None = None,
    clip_metas: list | None = None,
    global_color_grade: str = "none",
    job_id: str | None = None,
    user_subject: str = "",
) -> None:
    """Assemble clips in slot order: render each slot, then join with transitions.

    Uses _render_slot() per slot (cursor, beat-snap, speed ramp, overlays, reframe),
    then joins with xfade transitions or concat demuxer fallback.

    When EVAL_HARNESS_ENABLED, preserves per-slot .mp4 files in GCS for visual QA.
    """
    beats = beat_timestamps_s or []
    clip_cursors: dict[str, float] = {}
    cumulative_s = 0.0

    # User-provided subject takes priority over auto-detected
    subject = user_subject or _consensus_subject(clip_metas)
    if subject:
        log.info("subject_resolved", subject=subject, source="user" if user_subject else "auto")

    reframed_paths: list[str] = []
    slot_durations: list[float] = []
    transition_types: list[str] = []

    for i, step in enumerate(steps):
        clip_id = step.clip_id
        local_path = clip_id_to_local.get(clip_id)
        if not local_path or not os.path.exists(local_path):
            raise ValueError(f"Local path not found for clip_id {clip_id}")

        probe = clip_probe_map.get(local_path)

        reframed, vis_dur, cumulative_s = _render_slot(
            step=step,
            clip_path=local_path,
            probe=probe,
            beats=beats,
            clip_cursors=clip_cursors,
            cumulative_s=cumulative_s,
            slot_idx=i,
            clip_metas=clip_metas,
            global_color_grade=global_color_grade,
            tmpdir=tmpdir,
            subject=subject,
        )
        reframed_paths.append(reframed)
        slot_durations.append(vis_dur)

        # Collect transition types for boundaries (skip first slot)
        if i > 0:
            transition_types.append(
                str(step.slot.get("transition_in", "none"))
            )

    if not reframed_paths:
        raise ValueError("No slots to assemble")

    if len(reframed_paths) == 1:
        shutil.copy2(reframed_paths[0], output_path)
    else:
        _join_or_concat(
            reframed_paths, transition_types, slot_durations,
            output_path, tmpdir,
        )

    # Eval harness: upload per-slot files for visual comparison
    if settings.eval_harness_enabled and job_id:
        _upload_eval_slots(reframed_paths, slot_durations, steps, job_id)


def _join_or_concat(
    reframed_paths: list[str],
    transition_types: list[str],
    slot_durations: list[float],
    output_path: str,
    tmpdir: str,
) -> None:
    """Join slots with xfade transitions, falling back to concat demuxer on error."""
    has_transitions = any(t != "none" for t in transition_types)

    if has_transitions and len(reframed_paths) > 1:
        try:
            from app.pipeline.transitions import join_with_transitions  # noqa: PLC0415

            join_with_transitions(
                reframed_paths, transition_types, slot_durations, output_path,
            )
            return
        except Exception as exc:
            log.warning("transition_join_failed_falling_back", error=str(exc))

    # Fallback: concat demuxer (fast, no re-encode)
    _concat_demuxer(reframed_paths, output_path, tmpdir)


def _concat_demuxer(
    reframed_paths: list[str], output_path: str, tmpdir: str,
) -> None:
    """FFmpeg concat demuxer — lossless stream copy, fast."""
    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in reframed_paths:
            f.write(f"file '{p}'\n")

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


def _upload_eval_slots(
    reframed_paths: list[str],
    slot_durations: list[float],
    steps: list,
    job_id: str,
) -> None:
    """Upload per-slot .mp4 files to GCS for the eval harness. Non-fatal."""
    try:
        for i, path in enumerate(reframed_paths):
            if os.path.exists(path):
                gcs_path = f"jobs/{job_id}/slots/slot_{i}.mp4"
                upload_public_read(path, gcs_path)
        log.info("eval_slots_uploaded", job_id=job_id, count=len(reframed_paths))
    except Exception as exc:
        log.warning("eval_slot_upload_failed", job_id=job_id, error=str(exc))


def _find_clip_meta_by_id(clip_id: str, clip_metas: list | None) -> "ClipMeta | None":
    """Find a ClipMeta by clip_id. Returns None if not found."""
    if not clip_metas:
        return None
    for meta in clip_metas:
        if meta.clip_id == clip_id:
            return meta
    return None


def _is_subject_placeholder(sample: str) -> bool:
    """Return True if sample_text looks like a variable place/topic name.

    Heuristic: short ALL-CAPS text (e.g. "PERU", "NYC") or a short
    title-cased phrase where every word is capitalized (e.g. "New York")
    is likely a template variable that should be replaced with the user's
    subject.  Fixed phrases like "Welcome to" (has lowercase word) or
    "discovering a hidden river" (long, lowercase) do NOT match.
    """
    if not sample or not sample.strip():
        return False
    words = sample.split()
    # ALL-CAPS up to 3 words: "PERU", "NEW YORK", "SAN JUAN PR"
    if sample.isupper() and len(words) <= 3:
        return True
    # Title-cased 1-2 words where EVERY word is capitalized: "Peru", "New York"
    # Excludes mixed-case phrases like "Welcome to", "Check this"
    if len(words) <= 2 and all(w[0].isupper() for w in words):
        return True
    return False


def _resolve_overlay_text(
    role: str, clip_meta: "ClipMeta | None", overlay: dict,
    subject: str = "",
) -> str:
    """Resolve overlay text based on role, substituting detected subject.

    Template text like "PERU" is replaced with the detected subject from user clips
    (e.g. "Puerto Rico"). Fixed text like "Welcome to" passes through unchanged.
    The template_subject field on the overlay (if set) tells us which word to replace.

    Subject substitution takes priority over role-based hook text when the
    sample_text looks like a place-name placeholder — this prevents clip
    analysis text (e.g. "YOU", "discovering a hidden river") from overriding
    the user's subject.
    """
    sample = overlay.get("sample_text", "")

    if role == "cta":
        return ""  # CTA text generated later by copy_writer

    # Subject substitution FIRST: if the template text looks like a variable
    # place/topic name and we have a subject, substitute it regardless of role.
    if subject and _is_subject_placeholder(sample):
        return subject.upper() if sample.isupper() else subject

    # Role-based fallback: hook overlays use clip analysis hook text.
    if role == "hook" and clip_meta:
        return clip_meta.hook_text or sample

    return sample


def _consensus_subject(clip_metas: list | None) -> str:
    """Determine the consensus detected_subject across all analyzed clips.

    Uses majority vote — the most common non-empty subject wins.
    Returns empty string if no clips have a detected subject.
    """
    if not clip_metas:
        return ""

    subjects: dict[str, int] = {}
    for meta in clip_metas:
        s = (meta.detected_subject or "").strip()
        if s:
            # Normalize: lowercase for counting, keep original casing of first occurrence
            key = s.lower()
            subjects[key] = subjects.get(key, 0) + 1

    if not subjects:
        return ""

    # Return the most common subject (original casing from first clip)
    best_key = max(subjects, key=subjects.get)
    for meta in clip_metas:
        s = (meta.detected_subject or "").strip()
        if s.lower() == best_key:
            return s
    return ""


# ── Beat detection helpers ─────────────────────────────────────────────────────

BEAT_SNAP_TOLERANCE_S = 0.4


def _detect_audio_beats(audio_path: str, threshold_db: float = -35.0) -> list[float]:
    """Detect energy onset timestamps in audio using FFmpeg silencedetect.

    Parses silence_end markers from FFmpeg stderr — each silence_end marks a
    transition from quiet→loud, which corresponds to a beat or energy onset.

    Non-fatal: returns [] on any error.
    """
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=noise={threshold_db}dB:d=0.1",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            log.warning("beat_detect_ffmpeg_failed", stderr=result.stderr.decode()[:200])
            return []

        # Parse silence_end timestamps from stderr
        # Format: [silencedetect @ 0x...] silence_end: 1.234 | silence_duration: 0.567
        stderr_text = result.stderr.decode()
        beats = sorted(
            float(m.group(1))
            for m in re.finditer(r"silence_end:\s*([\d.]+)", stderr_text)
        )
        log.info("beat_detect_done", count=len(beats))
        return beats

    except Exception as exc:
        log.warning("beat_detect_failed", error=str(exc))
        return []


def _merge_beat_sources(
    gemini_beats: list[float],
    ffmpeg_beats: list[float],
    dedup_threshold_s: float = 0.15,
) -> list[float]:
    """Merge two beat timestamp lists, deduplicating within threshold.

    When Gemini and FFmpeg beats are within dedup_threshold_s of each other,
    the FFmpeg timestamp is kept (frame-accurate) and the Gemini one is dropped.
    """
    if not gemini_beats and not ffmpeg_beats:
        return []
    if not gemini_beats:
        return sorted(ffmpeg_beats)
    if not ffmpeg_beats:
        return sorted(gemini_beats)

    # Start with FFmpeg beats (higher accuracy)
    merged = sorted(ffmpeg_beats)

    # Add Gemini beats that aren't near any FFmpeg beat
    for gb in gemini_beats:
        idx = bisect.bisect_left(merged, gb)
        too_close = False
        for check_idx in (idx - 1, idx):
            if 0 <= check_idx < len(merged):
                if abs(merged[check_idx] - gb) <= dedup_threshold_s:
                    too_close = True
                    break
        if not too_close:
            bisect.insort(merged, gb)

    return merged


def _enrich_slots_with_energy(slots: list[dict], beats: list[float]) -> list[dict]:
    """Add an 'energy' field (0-10) to each slot based on beat density.

    Energy is proportional to the number of beats that fall within the slot's
    time range, normalized so the densest slot gets 10 and silence gets 0.
    This gives the matcher a music-derived energy profile per slot.
    """
    if not beats or not slots:
        return slots

    # Compute cumulative start/end for each slot
    cum = 0.0
    slot_ranges: list[tuple[float, float]] = []
    for s in sorted(slots, key=lambda x: x.get("position", 0)):
        dur = float(s.get("target_duration_s", 1.0))
        slot_ranges.append((cum, cum + dur))
        cum += dur

    # Count beats in each slot's time range
    beat_counts = []
    for start, end in slot_ranges:
        count = sum(1 for b in beats if start <= b < end)
        # Normalize by duration to get beat density (beats per second)
        density = count / max(end - start, 0.1)
        beat_counts.append(density)

    # Normalize to 0-10 scale
    max_density = max(beat_counts) if beat_counts else 1.0
    if max_density == 0:
        return slots

    enriched = []
    position_sorted = sorted(slots, key=lambda x: x.get("position", 0))
    for s, density in zip(position_sorted, beat_counts):
        enriched_slot = {**s, "energy": round(density / max_density * 10.0, 1)}
        enriched.append(enriched_slot)

    return enriched


def _snap_to_beat(target_end_s: float, beats: list[float]) -> float:
    """Snap target timestamp to nearest beat if within tolerance.

    Uses bisect for O(log n) lookup. No-op if beats is empty.
    """
    if not beats:
        return target_end_s

    idx = bisect.bisect_left(beats, target_end_s)
    candidates = []
    if idx < len(beats):
        candidates.append(beats[idx])
    if idx > 0:
        candidates.append(beats[idx - 1])

    nearest = min(candidates, key=lambda b: abs(b - target_end_s))
    if abs(nearest - target_end_s) <= BEAT_SNAP_TOLERANCE_S:
        return nearest
    return target_end_s


# ── Template audio helpers ─────────────────────────────────────────────────────


def _extract_template_audio(local_video_path: str, output_path: str) -> bool:
    """Extract audio track from template video. Returns True on success.

    Non-fatal: some templates may be silent.
    Uses .m4a container (AAC in MP4) for broad FFmpeg compatibility.
    """
    cmd = [
        "ffmpeg", "-i", local_video_path,
        "-vn",          # no video
        "-acodec", "aac",
        "-b:a", "192k",
        "-f", "mp4",    # force mp4 container (more compatible than raw ADTS .aac)
        "-y", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        log.warning(
            "template_audio_extract_failed",
            stderr=result.stderr.decode()[:200],
        )
        return False
    return os.path.exists(output_path) and os.path.getsize(output_path) > 1000


def _mix_template_audio(
    video_path: str, audio_gcs_path: str, output_path: str, tmpdir: str
) -> None:
    """Replace assembled video's audio with template music track.

    Non-fatal if anything fails — falls back to copying video_path → output_path.
    """
    audio_local = os.path.join(tmpdir, "template_audio.m4a")
    try:
        download_to_file(audio_gcs_path, audio_local)
    except Exception as exc:
        log.warning("template_audio_download_failed", error=str(exc))
        shutil.copy2(video_path, output_path)
        return

    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-i", audio_local,
        "-map", "0:v",  # video from assembled
        "-map", "1:a",  # audio from template
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",    # cut to shorter of video/audio
        "-y", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        log.warning("template_audio_mix_failed", stderr=result.stderr.decode()[:200])
        shutil.copy2(video_path, output_path)


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
