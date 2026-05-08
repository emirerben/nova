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
import dataclasses
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC

import redis as redis_lib
import structlog
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, TemplateRecipeVersion, VideoTemplate
from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    GeminiRefusalError,
    TemplateRecipe,
    analyze_clip,
    analyze_template,
    gemini_upload_and_wait,
)
from app.pipeline.template_matcher import TemplateMismatchError, consolidate_slots, match
from app.storage import copy_object_signed_url, download_to_file, upload_public_read
from app.worker import celery_app

log = structlog.get_logger()

# Consolidated label configuration — all visual tuning for text overlays.
# Gemini doesn't return text_size/font_style/text_color reliably,
# so we override per label type to match TikTok template aesthetics.
#
# "prefix" = fixed text like "Welcome to"
#            (detected by _is_subject_placeholder returning False)
# "subject" = variable text like city name
#            (detected by _is_subject_placeholder returning True)
_LABEL_CONFIG: dict[str, dict] = {
    "prefix": {
        "text_size": "small",       # 48px — small italic serif, subordinate to subject
        "start_s": 2.0,             # appears at absolute second 2
    },
    "subject": {
        "text_size": "xxlarge",     # 250px — fills ~75% width like reference
        "font_style": "sans",       # Montserrat ExtraBold
        "text_color": "#F4D03F",    # warm maize/gold
        "start_s": 3.0,             # appears 1s after prefix
        "effect": "font-cycle",     # forced font cycling
        "accel_at_s": 8.0,          # font cycle accelerates after second 8
    },
}


# Routing-only keys that live on `recipe_cached` JSON but are NOT valid
# TemplateRecipe constructor kwargs. Migration 0010 backfilled `template_kind`
# onto every existing recipe; future routing/dispatch fields go here.
_ROUTING_ONLY_RECIPE_KEYS: frozenset[str] = frozenset({"template_kind"})


def _build_recipe(recipe_data: dict) -> TemplateRecipe:
    """Construct a TemplateRecipe from a DB `recipe_cached` blob, stripping
    routing-only fields the dataclass doesn't accept as kwargs."""
    kwargs = {k: v for k, v in recipe_data.items() if k not in _ROUTING_ONLY_RECIPE_KEYS}
    return TemplateRecipe(**kwargs)


# ── analyze_template_task ─────────────────────────────────────────────────────


@celery_app.task(
    name="tasks.analyze_template_task", bind=True, max_retries=0,
    soft_time_limit=840, time_limit=900,
)
def analyze_template_task(self, template_id: str) -> None:
    """Download template video, analyze with Gemini, cache recipe in DB."""
    log.info("analyze_template_start", template_id=template_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "template.mp4")

        # Requeue guard: bail if this template has been retried too many times
        # (prevents infinite SIGKILL → requeue loops on templates that always timeout)
        _redis = redis_lib.from_url(settings.redis_url)
        attempt_key = f"analyze_attempts:{template_id}"
        attempts = _redis.incr(attempt_key)
        _redis.expire(attempt_key, 3600)  # TTL 1 hour

        if attempts > 3:
            log.error("analyze_template_max_attempts", template_id=template_id, attempts=attempts)
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = (
                        f"Exceeded max analysis attempts ({attempts}). "
                        "Template may be too large or trigger safety filters."
                    )
                    db.commit()
            _redis.close()
            return

        try:
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template is None:
                    log.error("template_not_found", template_id=template_id)
                    return
                gcs_path = template.gcs_path
                existing_audio_gcs = template.audio_gcs_path
                # Clear stale error from prior failed run
                template.error_detail = None
                db.commit()

            download_to_file(gcs_path, local_path)

            # Run blackdetect on template video for interstitial detection
            from app.pipeline.interstitials import (  # noqa: PLC0415
                classify_black_segment_type,
                detect_black_segments,
            )
            black_segments = detect_black_segments(local_path)
            black_segments = classify_black_segment_type(local_path, black_segments)

            file_ref = gemini_upload_and_wait(local_path)
            recipe = analyze_template(
                file_ref, analysis_mode="two_pass", black_segments=black_segments,
            )

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

                    # Determine trigger: reanalysis if recipe already exists
                    is_reanalysis = template.recipe_cached is not None
                    trigger = "reanalysis" if is_reanalysis else "initial_analysis"

                    recipe_dict = {
                        "shot_count": recipe.shot_count,
                        "total_duration_s": recipe.total_duration_s,
                        "hook_duration_s": recipe.hook_duration_s,
                        "slots": enriched_slots,
                        "copy_tone": recipe.copy_tone,
                        "caption_style": recipe.caption_style,
                        "beat_timestamps_s": merged_beats,
                        "creative_direction": recipe.creative_direction,
                        "transition_style": recipe.transition_style,
                        "color_grade": recipe.color_grade,
                        "pacing_style": recipe.pacing_style,
                        "sync_style": recipe.sync_style,
                        "interstitials": recipe.interstitials,
                    }

                    # Save recipe version before updating cached recipe
                    version = TemplateRecipeVersion(
                        template_id=template_id,
                        recipe=recipe_dict,
                        trigger=trigger,
                    )
                    db.add(version)

                    template.recipe_cached = recipe_dict
                    template.recipe_cached_at = datetime.now(UTC)
                    template.analysis_status = "ready"
                    if audio_gcs and not template.audio_gcs_path:
                        template.audio_gcs_path = audio_gcs
                    db.commit()
                    log.info("recipe_version_created", template_id=template_id, trigger=trigger)

            # Success — clear requeue guard counter
            _redis.delete(attempt_key)
            log.info("analyze_template_done", template_id=template_id, slots=len(recipe.slots))

        except SoftTimeLimitExceeded:
            log.error("analyze_template_timeout", template_id=template_id)
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = "Analysis timed out (exceeded 840s soft limit)"
                    db.commit()
        except Exception as exc:
            log.error("analyze_template_failed", template_id=template_id, error=str(exc))
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = str(exc)[:1000]
                    db.commit()
        finally:
            _redis.close()


# ── orchestrate_template_job ──────────────────────────────────────────────────


@celery_app.task(
    name="tasks.orchestrate_template_job", bind=True, max_retries=0,
    soft_time_limit=1740, time_limit=1800,
)
def orchestrate_template_job(self, job_id: str) -> None:
    """Full template-mode pipeline. Never raises — all errors go to processing_failed."""
    log.info("template_job_start", job_id=job_id)

    # Defensive: validate job_id BEFORE entering try/except. A stale Redis
    # queue message (worker restart picking up an old enqueue) can deliver
    # job_id=None or a non-UUID string. Without this guard, both the body
    # and the exception handler call uuid.UUID(None) and the task ends in
    # an "unexpected raise" rather than a clean drop.
    try:
        job_uuid = uuid.UUID(str(job_id))
    except (ValueError, TypeError, AttributeError):
        log.error("template_job_invalid_id", job_id=repr(job_id))
        return

    try:
        _run_template_job(job_id)
    except SoftTimeLimitExceeded:
        log.error("template_job_timeout", job_id=job_id)
        with _sync_session() as db:
            job = db.get(Job, job_uuid)
            if job:
                job.status = "processing_failed"
                job.error_detail = "Template job timed out (exceeded 1740s soft limit)"
                db.commit()
    except Exception as exc:
        log.error("template_job_fatal", job_id=job_id, error=str(exc))
        with _sync_session() as db:
            job = db.get(Job, job_uuid)
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

        # Fast path: locked re-render skips Gemini entirely
        if isinstance(job.assembly_plan, dict) and job.assembly_plan.get("locked"):
            _run_rerender(job_id, job)
            return

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
        voiceover_gcs_path = template.voiceover_gcs_path  # may be None
        template_gcs_path = template.gcs_path  # for locked-slot rendering

    # Route on template_kind discriminator. Existing rows without template_kind
    # are backfilled to "multiple_videos" by migration 0010, so .get() with
    # a default is just a belt-and-suspenders guard.
    template_kind = recipe_data.get("template_kind", "multiple_videos")
    if template_kind == "single_video":
        _run_single_video_job(
            job_id=job_id,
            template_id=template_id,
            recipe_data=recipe_data,
            clip_paths_gcs=clip_paths_gcs,
            audio_gcs_path=audio_gcs_path,
            voiceover_gcs_path=voiceover_gcs_path,
            user_subject=user_subject,
            selected_platforms=selected_platforms,
        )
        return

    try:
        recipe = _build_recipe(recipe_data)
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

        # [4] Analyze all clips in parallel — pass per-template filter_hint so
        # Gemini biases best_moments toward the desired content (e.g. ball-in-frame).
        log.info("gemini_analyze_clips_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(
            file_refs,
            local_clip_paths,
            probe_map,
            filter_hint=getattr(recipe, "clip_filter_hint", "") or "",
        )

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

        # [5] Template match.
        # Pin clip 0 to slot 1 when the template's first slot is "hook"-typed —
        # this keeps user-uploaded face/intro clips locked to position 1
        # (where the recipe expects a closeup) regardless of how Gemini scores
        # the other clips' opening moments.
        log.info("template_match_start", job_id=job_id)
        pinned_assignments: dict[int, str] = {}
        slot_1 = next((s for s in recipe.slots if s.get("position") == 1), None)
        if (
            slot_1
            and slot_1.get("slot_type") in ("hook", "face")
            and file_refs
        ):
            pinned_assignments[1] = file_refs[0].name
            log.info("matcher_pin", slot=1, clip_id=file_refs[0].name)
        try:
            recipe = consolidate_slots(recipe, clip_metas)
            assembly_plan = match(
                recipe, clip_metas,
                pinned_assignments=pinned_assignments,
                filter_hint=getattr(recipe, "clip_filter_hint", "") or "",
            )
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        # Build mapping: Gemini file ref name → GCS path for re-render
        clip_id_to_gcs = {ref.name: gcs for ref, gcs in zip(file_refs, clip_paths_gcs)}

        # Persist assembly plan
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

        # [6] FFmpeg assemble
        log.info("ffmpeg_assemble_start", job_id=job_id)
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        base_path = os.path.join(tmpdir, "base_no_overlays.mp4")
        clip_id_to_local = {
            ref.name: path for ref, path in zip(file_refs, local_clip_paths)
        }
        _add_locked_template_source(
            recipe_data, template_gcs_path, tmpdir,
            clip_id_to_local, probe_map,
        )
        _assemble_clips(
            assembly_plan.steps, clip_id_to_local, probe_map,
            assembled_path, tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=clip_metas,
            global_color_grade=recipe.color_grade,
            job_id=job_id,
            user_subject=user_subject,
            interstitials=recipe.interstitials,
            base_output_path=base_path,
            output_fit=getattr(recipe, "output_fit", None) or "crop",
        )

        # [6b] Mix template audio if available
        if audio_gcs_path:
            final_path = os.path.join(tmpdir, "final.mp4")
            _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)
            output_path = final_path
            # Also mix audio into the base video for editor preview
            base_final = os.path.join(tmpdir, "base_final.mp4")
            _mix_template_audio(base_path, audio_gcs_path, base_final, tmpdir)
            base_path = base_final
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

        # [8] Upload assembled video + base video to GCS
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        video_url = upload_public_read(output_path, gcs_output_path)
        gcs_base_path = f"jobs/{job_id}/template_base.mp4"
        base_video_url = upload_public_read(base_path, gcs_base_path)

        # [9] Finalize
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "template_ready"
                job.assembly_plan = {
                    **plan_data,
                    "output_url": video_url,
                    "base_output_url": base_video_url,
                    "platform_copy": platform_copy.model_dump(),
                    "copy_status": copy_status,
                }
                db.commit()

        log.info("template_job_done", job_id=job_id, output_url=video_url)


# ── Locked re-render fast path ─────────────────────────────────────────────────


def _run_rerender(job_id: str, job: Job) -> None:
    """Re-render with locked clip-to-slot assignments. Skips Gemini entirely.

    Reads the locked assembly plan, downloads only the used clips,
    and runs FFmpeg render + audio mix + copy generation + upload.
    """
    from app.pipeline.agents.gemini_analyzer import AssemblyStep  # noqa: PLC0415

    plan = job.assembly_plan
    steps_data = plan.get("steps", [])
    template_id = job.template_id
    selected_platforms = job.selected_platforms or ["tiktok", "instagram", "youtube"]
    user_subject = (job.all_candidates or {}).get("subject", "")

    # Load current recipe from DB (reflects user edits)
    with _sync_session() as db:
        template = db.get(VideoTemplate, template_id)
        if template is None:
            raise ValueError(f"Template {template_id} not found")
        if not template.recipe_cached:
            raise ValueError(f"Template {template_id} has no recipe")

        recipe_data = template.recipe_cached
        audio_gcs_path = template.audio_gcs_path
        template_gcs_path = template.gcs_path

    try:
        recipe = _build_recipe(recipe_data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Template recipe in DB is malformed: {exc}") from exc

    # Sort current recipe slots by position for index-based matching
    current_slots = sorted(
        recipe_data.get("slots", []),
        key=lambda s: s.get("position", 0),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        # Collect unique GCS paths from locked steps
        gcs_paths_unique = list(dict.fromkeys(
            step["clip_gcs_path"] for step in steps_data
        ))

        # Download only the used clips
        local_clip_paths = _download_clips_parallel(gcs_paths_unique, tmpdir)
        gcs_to_local = dict(zip(gcs_paths_unique, local_clip_paths))

        # Probe all clips
        probe_map = _probe_clips(local_clip_paths)

        # Reconstruct AssemblyStep objects with CURRENT recipe slots
        assembly_steps = []
        clip_id_to_local: dict[str, str] = {}
        for i, step_data in enumerate(steps_data):
            # Use current slot (user-edited) matched by index
            if i >= len(current_slots):
                raise ValueError(
                    f"Slot index {i} out of range ({len(current_slots)} slots in recipe)"
                )
            slot = current_slots[i]
            clip_gcs = step_data["clip_gcs_path"]
            clip_id = step_data["clip_id"]
            local_path = gcs_to_local[clip_gcs]

            assembly_steps.append(AssemblyStep(
                slot=slot,
                clip_id=clip_id,
                moment=step_data["moment"],
            ))
            clip_id_to_local[clip_id] = local_path

        _add_locked_template_source(
            recipe_data, template_gcs_path, tmpdir,
            clip_id_to_local, probe_map,
        )

        # FFmpeg assemble (clip_metas=None is safe — text comes from recipe)
        log.info("rerender_assemble_start", job_id=job_id, steps=len(assembly_steps))
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        base_path = os.path.join(tmpdir, "base_no_overlays.mp4")
        _assemble_clips(
            assembly_steps, clip_id_to_local, probe_map,
            assembled_path, tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=None,
            global_color_grade=recipe.color_grade,
            job_id=job_id,
            user_subject=user_subject,
            interstitials=recipe.interstitials,
            base_output_path=base_path,
            output_fit=getattr(recipe, "output_fit", None) or "crop",
        )

        # Mix template audio if available
        if audio_gcs_path:
            final_path = os.path.join(tmpdir, "final.mp4")
            _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)
            output_path = final_path
            base_final = os.path.join(tmpdir, "base_final.mp4")
            _mix_template_audio(base_path, audio_gcs_path, base_final, tmpdir)
            base_path = base_final
        else:
            output_path = assembled_path

        # Generate copy
        from app.pipeline.agents.copy_writer import generate_copy  # noqa: PLC0415
        platform_copy, copy_status = generate_copy(
            hook_text="",
            transcript_excerpt="",
            platforms=selected_platforms,
            has_transcript=False,
            template_tone=recipe.copy_tone,
        )

        # Upload
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        video_url = upload_public_read(output_path, gcs_output_path)
        gcs_base_path = f"jobs/{job_id}/template_base.mp4"
        base_video_url = upload_public_read(base_path, gcs_base_path)

        # Finalize — keep locked steps plus output metadata
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "template_ready"
                job.assembly_plan = {
                    **plan,
                    "output_url": video_url,
                    "base_output_url": base_video_url,
                    "platform_copy": platform_copy.model_dump(),
                    "copy_status": copy_status,
                }
                db.commit()

        log.info("rerender_done", job_id=job_id, output_url=video_url)


# ── Parallelism helpers ────────────────────────────────────────────────────────


def _add_locked_template_source(
    recipe_data: dict,
    template_gcs_path: str | None,
    tmpdir: str,
    clip_id_to_local: dict[str, str],
    probe_map: dict,
) -> None:
    """If recipe has any locked slot, download the template video and register
    it under LOCKED_TEMPLATE_CLIP_ID so _plan_slots/_render_planned_slot can
    trim segments from it like any other clip.
    """
    from app.pipeline.probe import VideoProbe, probe_video  # noqa: PLC0415
    from app.pipeline.template_matcher import LOCKED_TEMPLATE_CLIP_ID  # noqa: PLC0415

    has_locked = any(
        s.get("locked", False) for s in recipe_data.get("slots", [])
    )
    if not has_locked:
        return
    if not template_gcs_path:
        log.warning("locked_slot_skipped_no_template_path")
        return

    local_path = os.path.join(tmpdir, "template_source.mp4")
    download_to_file(template_gcs_path, local_path)
    clip_id_to_local[LOCKED_TEMPLATE_CLIP_ID] = local_path
    try:
        probe_map[local_path] = probe_video(local_path)
    except Exception as exc:
        log.warning("template_probe_failed_using_default", error=str(exc))
        probe_map[local_path] = VideoProbe(
            duration_s=60.0, fps=30.0, width=1920, height=1080,
            aspect_ratio="16:9",
        )


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
    file_refs: list,
    local_paths: list[str],
    probe_map: dict | None = None,
    filter_hint: str = "",
) -> tuple[list[ClipMeta], int]:
    """Analyze all clips with Gemini in parallel.

    Returns (successful_metas, failed_count).
    Per-clip failures are logged but don't raise — caller applies threshold.
    probe_map is used in the Whisper fallback path to get clip duration without
    a second FFprobe call.
    filter_hint biases best_moments selection per-template (e.g. "ball in frame"
    for football templates).
    """
    clip_metas: list[ClipMeta] = []
    failed_count = 0

    def _analyze_one(args: tuple[int, object, str]) -> tuple[ClipMeta | None, str | None]:
        idx, ref, path = args
        try:
            meta = analyze_clip(ref, filter_hint=filter_hint)
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
#   _plan_slots()  → list[SlotPlan]  (sequential: cursor, beat-snap, speed ramp)
#   ThreadPool     → render each SlotPlan via FFmpeg (parallel, max_workers=3)
#   post-render    → curtain-close tails, interstitials, transitions (sequential)
#   _join_or_concat() → assembled.mp4
#
# _plan_slots (sequential math) + _render_planned_slot (parallel FFmpeg)
# replaced the old _render_slot function.
# ──────────────────────────────────────────────────────────────────────────────

_PARALLEL_RENDER_WORKERS = 3  # Fly.io has 2 shared CPUs; 3 overlaps I/O waits


@dataclasses.dataclass
class SlotPlan:
    """Pre-computed plan for a single slot render (pure arithmetic, no I/O)."""

    slot_idx: int
    clip_path: str
    start_s: float
    end_s: float
    speed_factor: float
    slot_target_dur: float
    cumulative_s: float
    aspect_ratio: str
    color_hint: str
    reframed_path: str  # output path for this slot
    output_fit: str = "crop"  # "crop" (default) | "letterbox" (preserve full frame + blurred bg)


def _plan_slots(
    steps: list,
    clip_id_to_local: dict[str, str],
    clip_probe_map: dict,
    beats: list[float],
    clip_metas: list | None,
    global_color_grade: str,
    tmpdir: str,
    output_fit: str = "crop",
) -> tuple[list[SlotPlan], dict[str, float], float]:
    """Phase 1: sequential arithmetic to plan all slot renders.

    Returns (plans, clip_cursors, final_cumulative_s).
    No I/O — only cursor tracking, beat-snap, and speed ramp math.
    """
    clip_cursors: dict[str, float] = {}
    cumulative_s = 0.0
    plans: list[SlotPlan] = []

    for i, step in enumerate(steps):
        clip_id = step.clip_id
        local_path = clip_id_to_local.get(clip_id)
        if not local_path or not os.path.exists(local_path):
            raise ValueError(f"Local path not found for clip_id {clip_id}")

        probe = clip_probe_map.get(local_path)
        moment = step.moment
        slot_target_dur = float(step.slot.get("target_duration_s", 5.0))
        slot_target_dur = max(slot_target_dur, 0.5)

        is_locked = bool(step.slot.get("locked", False))

        if is_locked:
            # Locked source range is exact — bypass beat-snap, cursor sharing,
            # and speed ramp so the segment matches the template audio frame-by-frame.
            start_s = float(moment.get("start_s", 0.0))
            end_s = float(moment.get("end_s", start_s + slot_target_dur))
            slot_target_dur = max(0.5, end_s - start_s)
            speed_factor = 1.0
            cumulative_s += slot_target_dur
            # Force letterbox path for locked sources — the template video may be
            # 16:9 (e.g. Morocco's 1024x576) and the default crop-to-fill chops
            # the baked-in hook text on the sides. With aspect_ratio="9:16" the
            # reframe filter scales-to-fit and pads black bars, preserving text.
            locked_aspect_override = "9:16"
        else:
            # Beat-snap
            if beats:
                expected_end = cumulative_s + slot_target_dur
                snapped_end = _snap_to_beat(expected_end, beats)
                slot_target_dur = max(0.5, snapped_end - cumulative_s)
                cumulative_s = snapped_end
            else:
                cumulative_s += slot_target_dur

            # Speed ramp
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
        if is_locked:
            aspect_ratio = locked_aspect_override
        slot_color = step.slot.get("color_hint") or global_color_grade or "none"

        log.debug(
            "slot_planned",
            position=step.slot.get("position"),
            target_s=slot_target_dur,
            actual_s=round(end_s - start_s, 2),
            speed_factor=speed_factor,
        )

        plans.append(SlotPlan(
            slot_idx=i,
            clip_path=local_path,
            start_s=start_s,
            end_s=end_s,
            speed_factor=speed_factor,
            slot_target_dur=slot_target_dur,
            cumulative_s=cumulative_s,
            aspect_ratio=aspect_ratio,
            color_hint=slot_color,
            reframed_path=os.path.join(tmpdir, f"slot_{i}.mp4"),
            output_fit=output_fit,
        ))

    return plans, clip_cursors, cumulative_s


def _render_planned_slot(plan: SlotPlan) -> str:
    """Phase 2: render a single planned slot via FFmpeg. Thread-safe."""
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

    reframe_and_export(
        input_path=plan.clip_path,
        start_s=plan.start_s,
        end_s=plan.end_s,
        aspect_ratio=plan.aspect_ratio,
        ass_subtitle_path=None,
        output_path=plan.reframed_path,
        color_hint=plan.color_hint,
        speed_factor=plan.speed_factor,
        output_fit=plan.output_fit,
    )
    return plan.reframed_path


def _assemble_clips(
    steps: list,
    clip_id_to_local: dict[str, str],
    clip_probe_map: dict,
    output_path: str,
    tmpdir: str,
    beat_timestamps_s: list[float] | None = None,
    clip_metas: list[ClipMeta] | None = None,
    global_color_grade: str = "none",
    job_id: str | None = None,
    user_subject: str = "",
    interstitials: list[dict] | None = None,
    base_output_path: str | None = None,
    output_fit: str = "crop",
) -> None:
    """Assemble clips in slot order: plan, parallel-render, then join with transitions.

    Phase 1: _plan_slots() computes cursor, beat-snap, speed ramp (sequential math).
    Phase 2: ThreadPoolExecutor renders each SlotPlan via FFmpeg (parallel, max_workers=3).
    Phase 3: Apply curtain-close tails, insert interstitials, collect transitions.
    Then joins with xfade transitions or concat demuxer fallback.

    Interstitials (curtain-close, fade-black-hold, etc.) are rendered as separate
    clips and inserted between slots before joining.

    When EVAL_HARNESS_ENABLED, preserves per-slot .mp4 files in GCS for visual QA.
    """
    from app.pipeline.transitions import translate_transition  # noqa: PLC0415

    beats = beat_timestamps_s or []

    # User-provided subject takes priority over auto-detected
    subject = user_subject or _consensus_subject(clip_metas)
    if subject:
        log.info("subject_resolved", subject=subject, source="user" if user_subject else "auto")

    # Build lookup of interstitials by after_slot position
    interstitial_map: dict[int, dict] = {}
    for inter in (interstitials or []):
        slot_pos = inter["after_slot"]
        if slot_pos in interstitial_map:
            log.warning(
                "interstitial_duplicate_after_slot",
                after_slot=slot_pos,
                existing_type=interstitial_map[slot_pos]["type"],
                new_type=inter["type"],
            )
        interstitial_map[slot_pos] = inter

    # ── Phase 1: plan all slots (sequential, pure math) ───────────────────
    plans, _clip_cursors, cumulative_s = _plan_slots(
        steps, clip_id_to_local, clip_probe_map, beats,
        clip_metas, global_color_grade, tmpdir,
        output_fit=output_fit,
    )

    # ── Phase 2: render all slots in parallel via FFmpeg ──────────────────
    render_errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=_PARALLEL_RENDER_WORKERS) as pool:
        futures = {pool.submit(_render_planned_slot, p): p for p in plans}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                plan = futures[future]
                log.error("parallel_render_failed", slot=plan.slot_idx, error=str(exc))
                render_errors.append(exc)

    if render_errors:
        for p in plans:
            if os.path.exists(p.reframed_path):
                os.unlink(p.reframed_path)
        raise render_errors[0]

    log.info("parallel_render_complete", slots=len(plans))

    # ── Phase 3: post-render (curtain-close, interstitials, transitions) ──
    reframed_paths: list[str] = []
    slot_durations: list[float] = []
    transition_types: list[str] = []
    # Original per-slot durations (excluding interstitials) for overlay timing.
    original_slot_durations: list[float] = []
    # When True, the previous slot had an interstitial after it, so the
    # transition INTO this slot must be hard-cut (the interstitial IS the
    # transition effect — adding an xfade from a solid color looks broken).
    prev_had_interstitial = False

    for plan, step in zip(plans, steps):
        i = plan.slot_idx
        reframed = plan.reframed_path

        # Check if this slot has an interstitial AFTER it (curtain-close tail effect)
        slot_position = int(step.slot.get("position", i + 1))
        inter = interstitial_map.get(slot_position)
        if inter and inter["type"] == "curtain-close":
            # ── PRE-BURN text onto slot BEFORE curtain ─────────────────
            # Tiki-style: text appears on video, curtain closes OVER both.
            # We burn text onto the reframed clip first, then apply curtain
            # on top, so the black bars cover the text as they sweep down.
            reframed = _pre_burn_curtain_slot_text(
                reframed, step, plan.slot_target_dur, clip_metas, subject, i, tmpdir, inter,
            )

            # Apply curtain-close animation to the tail of this slot
            from app.pipeline.interstitials import (  # noqa: PLC0415
                MIN_CURTAIN_ANIMATE_S,
                apply_curtain_close_tail,
            )
            tail_output = os.path.join(tmpdir, f"slot_{i}_curtain.mp4")
            try:
                # Use recipe animate_s directly when explicitly set;
                # only apply MIN_CURTAIN_ANIMATE_S as default fallback.
                recipe_animate = inter.get("animate_s")
                curtain_anim = (
                    recipe_animate if recipe_animate is not None
                    else MIN_CURTAIN_ANIMATE_S
                )
                apply_curtain_close_tail(
                    reframed, tail_output,
                    animate_s=curtain_anim,
                )
                reframed = tail_output
            except Exception as exc:
                log.warning("curtain_close_tail_failed", slot=i, error=str(exc))

        reframed_paths.append(reframed)
        slot_durations.append(plan.slot_target_dur)
        original_slot_durations.append(plan.slot_target_dur)

        # Collect transition types for boundaries (skip first slot).
        # Force hard-cut if the previous slot had an interstitial — the
        # interstitial clip IS the transition, so xfade from a solid color
        # frame (whip-pan, crossfade) would look broken.
        if i > 0:
            if prev_had_interstitial:
                transition_types.append("none")
            else:
                raw_transition = str(step.slot.get("transition_in", "none"))
                transition_types.append(translate_transition(raw_transition))

        # Insert interstitial clip AFTER this slot if one is defined.
        # When hold_s=0 the curtain animation (already baked into the slot
        # above) IS the full transition — skip the colour-hold clip so
        # nothing sits between the two slots and beat+scene align perfectly.
        prev_had_interstitial = bool(inter)
        if inter:
            inter_hold = float(inter.get("hold_s", 1.0))
            if inter_hold > 0:
                _insert_interstitial(
                    inter, i, reframed_paths, slot_durations,
                    transition_types, tmpdir,
                )
            else:
                log.info("interstitial_skip_zero_hold", type=inter["type"], slot=i)
            # Keep beat-snap clock in sync — interstitial hold time is real
            # video duration that must be accounted for when snapping later
            # slot boundaries to musical beats.
            cumulative_s += inter_hold

    if not reframed_paths:
        raise ValueError("No slots to assemble")

    # Join slots (transitions or concat)
    joined_path = os.path.join(tmpdir, "joined.mp4")
    if len(reframed_paths) == 1:
        shutil.copy2(reframed_paths[0], joined_path)
    else:
        _join_or_concat(
            reframed_paths, transition_types, slot_durations,
            joined_path, tmpdir,
            has_interstitials=bool(interstitial_map),
        )

    # Debug: probe durations at each stage to trace truncation
    _joined_dur = _probe_duration(joined_path)
    log.info(
        "debug_stage_durations",
        joined_dur=_joined_dur,
        slot_count=len(reframed_paths),
        slot_durations_sum=round(sum(slot_durations), 2),
    )

    # ── Save base video (pre-overlay) for editor preview ────────────
    if base_output_path:
        shutil.copy2(joined_path, base_output_path)

    # ── Post-join text overlays (absolute timestamps, deduplicated) ────
    # Collect overlays across ALL slots, convert to absolute timestamps,
    # and deduplicate so "Welcome to" appears once and persists.
    abs_overlays = _collect_absolute_overlays(
        steps, original_slot_durations, clip_metas, subject,
        interstitial_map=interstitial_map,
    )
    if abs_overlays:
        _burn_text_overlays(joined_path, abs_overlays, output_path, tmpdir)
        _burned_dur = _probe_duration(output_path)
        log.info("debug_post_burn_duration", burned_dur=_burned_dur)
    else:
        shutil.copy2(joined_path, output_path)

    # Eval harness: upload per-slot files for visual comparison
    if settings.eval_harness_enabled and job_id:
        _upload_eval_slots(reframed_paths, slot_durations, steps, job_id)


def _insert_interstitial(
    inter: dict,
    slot_idx: int,
    reframed_paths: list[str],
    slot_durations: list[float],
    transition_types: list[str],
    tmpdir: str,
) -> None:
    """Render an interstitial clip and insert it into the assembly lists.

    The interstitial is added after the current slot's position in the lists.
    Transition types around the interstitial are set to "none" (hard cut)
    because the interstitial itself IS the transition effect.
    """
    from app.pipeline.interstitials import (  # noqa: PLC0415
        InterstitialError,
        render_color_hold,
    )

    inter_type = inter["type"]
    hold_s = float(inter.get("hold_s", 1.0))
    hold_color = inter.get("hold_color", "#000000")
    # Map hold_color hex to FFmpeg color name
    ffmpeg_color = "white" if hold_color == "#FFFFFF" else "black"
    inter_path = os.path.join(tmpdir, f"interstitial_{slot_idx}_{inter_type}.mp4")

    try:
        if inter_type in ("curtain-close", "fade-black-hold", "flash-white"):
            render_color_hold(inter_path, hold_s=hold_s, color=ffmpeg_color)
        else:
            log.warning("unknown_interstitial_type", type=inter_type)
            return
    except InterstitialError as exc:
        log.warning("interstitial_render_failed", type=inter_type, error=str(exc))
        return

    # Insert interstitial clip into assembly lists
    reframed_paths.append(inter_path)
    slot_durations.append(hold_s)
    # Hard cut into and out of the interstitial
    transition_types.append("none")

    log.info(
        "interstitial_inserted",
        type=inter_type,
        after_slot=inter.get("after_slot"),
        hold_s=hold_s,
    )


def _join_or_concat(
    reframed_paths: list[str],
    transition_types: list[str],
    slot_durations: list[float],
    output_path: str,
    tmpdir: str,
    has_interstitials: bool = False,
) -> None:
    """Join slots with xfade transitions, falling back to concat demuxer on error.

    When has_interstitials is True but no visual transitions exist, use the
    re-encoding concat demuxer instead of xfade. Chaining 17+ xfade filters
    with duration=0.001 (hard-cut simulation) causes FFmpeg to drop frames
    and truncate the output to ~4s. The concat demuxer with re-encode handles
    mixed audio/no-audio streams correctly.
    """
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

    # Concat demuxer re-encodes (handles mixed audio/no-audio from
    # interstitials) and doesn't suffer from xfade offset drift that
    # occurs when slot_durations diverge from actual file durations.
    _concat_demuxer(reframed_paths, output_path, tmpdir)


def _concat_demuxer(
    reframed_paths: list[str], output_path: str, tmpdir: str,
) -> None:
    """FFmpeg concat demuxer — re-encode to handle mixed audio/no-audio slots.

    Stream copy (-c copy) silently truncates when some slots lack an audio
    track. Re-encoding ensures all inputs are normalized to the same layout.
    """
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in reframed_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        # ultrafast: shared-CPU workers crawl on preset=fast (24-slot Morocco
        # was hitting ~9 min for transitions alone). CRF 18 + 4M bitrate cap
        # still keeps quality in the visually-lossless band; scenecut/keyint
        # tuning is a non-issue at ultrafast since every macroblock is
        # I/P-coded conservatively anyway. Trade ~10-20% bigger file for
        # 3-5x faster wall-clock — wins for iteration speed.
        *_encoding_args(output_path, preset="ultrafast"),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=1200, check=False)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg concat failed: {result.stderr.decode()[:300]}")


def _collect_absolute_overlays(
    steps: list,
    slot_durations: list[float],
    clip_metas: list | None,
    subject: str,
    interstitial_map: dict[int, dict] | None = None,
) -> list[dict]:
    """Collect text overlays across all slots with absolute video timestamps.

    Args:
        slot_durations: Original per-slot durations (excluding interstitials).
        interstitial_map: Lookup of interstitials by after_slot position.
            Used to offset cumulative time for interstitial hold durations.

    Two dedup rules prevent visual glitches:
      1. Same-text merge: if the same text (case-insensitive) appears at the
         same position on adjacent slots with a gap < 2.0s, merge them into
         one continuous overlay. This covers interstitial hold durations
         (typically 1.0s) with margin. Templates report overlays per-shot,
         so "Welcome to" would otherwise restart on every slot boundary.
      2. Same-position overlap prevention: if two overlays share the same
         screen position with overlapping time ranges, truncate the earlier
         one so they never render simultaneously.
    """
    raw: list[dict] = []
    cumulative_s = 0.0
    inter_map = interstitial_map or {}

    for i, step in enumerate(steps):
        slot = step.get("slot", {}) if isinstance(step, dict) else step.slot
        dur = (
            slot_durations[i]
            if i < len(slot_durations)
            else float(slot.get("target_duration_s", 5.0))
        )
        clip_id = step.get("clip_id", "") if isinstance(step, dict) else step.clip_id

        # Look up interstitial for this slot once (used by overlay
        # acceleration and cumulative time accounting below).
        slot_position = int(slot.get("position", i + 1))
        inter = inter_map.get(slot_position)

        # Skip overlays for curtain-close slots — they were already
        # pre-burned onto the slot clip before the curtain effect was
        # applied (tiki-style: curtain closes OVER the text).
        if inter and inter.get("type") == "curtain-close":
            cumulative_s += dur
            cumulative_s += float(inter.get("hold_s", 1.0))
            continue

        for ov in slot.get("text_overlays", []):
            clip_meta = _find_clip_meta_by_id(clip_id, clip_metas)
            text = _resolve_overlay_text(ov.get("role", "label"), clip_meta, ov, subject=subject)
            if not text or not text.strip():
                continue

            # Skip combined prefix+subject overlays (e.g. "Welcome to PERU").
            # Gemini sometimes reports separate "Welcome to" + "PERU" overlays
            # on earlier slots AND a combined "Welcome to PERU" on later slots.
            # The combined form is redundant and breaks the staggered reveal.
            sample = ov.get("sample_text", "")
            if (
                sample
                and " " in sample
                and any(w.isupper() and len(w) > 1 for w in sample.split())
                and sample.lower().startswith("welcome")
            ):
                continue

            # 1. Compute ov_start, ov_end from Gemini values
            ov_start = cumulative_s + float(ov.get("start_s", 0.0))
            ov_end = cumulative_s + float(ov.get("end_s", dur))

            # 2. Apply timing overrides — recipe can specify exact timestamps
            # to correct Gemini's approximate timing (relative to slot start)
            if ov.get("start_s_override") is not None:
                ov_start = cumulative_s + float(ov["start_s_override"])
            if ov.get("end_s_override") is not None:
                ov_end = cumulative_s + float(ov["end_s_override"])
            # Guard against negative timestamps from bad override values
            ov_start = max(0.0, ov_start)
            ov_end = max(ov_start + 0.01, ov_end)

            # 3. Build entry dict with defaults
            entry = {
                "text": text,
                "start_s": ov_start,
                "end_s": ov_end,
                "position": ov.get("position", "center"),
                "effect": ov.get("effect", "none"),
                "font_style": ov.get("font_style", "display"),
                "text_size": ov.get("text_size", "medium"),
                "text_size_px": ov.get("text_size_px"),
                "text_color": ov.get("text_color", "#FFFFFF"),
                "position_y_frac": ov.get("position_y_frac"),
            }
            # Pass through font_family from admin recipe (overrides font_style)
            if ov.get("font_family"):
                entry["font_family"] = ov["font_family"]
            # Pass through spans for rich inline formatting
            if ov.get("spans"):
                entry["spans"] = ov["spans"]
            # Pass through player-card fields so the renderer receives
            # jersey_no + player_name (the special-effect path needs both).
            if ov.get("jersey_no"):
                entry["jersey_no"] = ov["jersey_no"]
            if ov.get("player_name"):
                entry["player_name"] = ov["player_name"]
            # Pass through stroke_width (TikTok-style outline)
            if ov.get("stroke_width") is not None:
                entry["stroke_width"] = ov["stroke_width"]

            # 4. Apply label config based on subject vs prefix detection.
            # Gemini inconsistently returns role="hook" for text that should
            # be role="label", so we also detect by sample_text content.
            role = ov.get("role", "")
            sample_text = ov.get("sample_text", "")
            is_subject = _is_subject_placeholder(sample_text)
            is_label_like = (
                role == "label" or is_subject or sample_text.lower().startswith("welcome")
            )
            if is_label_like:
                config = _LABEL_CONFIG["subject" if is_subject else "prefix"]
                # Only apply non-styling keys from config. Visual properties
                # (text_size, font_style, text_color, effect) come from the
                # recipe so the admin editor preview matches the render.
                _STYLING_KEYS = {
                    "text_size", "text_size_px", "font_style",
                    "font_family", "text_color", "effect",
                }
                entry.update(
                    {k: v for k, v in config.items()
                     if k not in ("start_s", "accel_at_s") and k not in _STYLING_KEYS}
                )

                # Timing override for first-slot labels only
                # Guard: don't push start past end (e.g. short overlay)
                if (
                    cumulative_s == 0.0
                    and "start_s" in config
                    and config["start_s"] < entry["end_s"]
                ):
                    entry["start_s"] = config["start_s"]

                # Font-cycle acceleration for subject labels
                if is_subject and "accel_at_s" in config:
                    entry["font_cycle_accel_at_s"] = config["accel_at_s"]

            # 5. Inject font_cycle_accel_at_s with CLAMPED animate_s
            # so font cycling syncs with the visual curtain animation
            if (
                entry["effect"] == "font-cycle"
                and inter
                and inter.get("type") == "curtain-close"
            ):
                from app.pipeline.interstitials import (  # noqa: PLC0415
                    _CURTAIN_MAX_RATIO,
                    MIN_CURTAIN_ANIMATE_S,
                )
                # Use recipe animate_s directly when explicitly set
                recipe_animate = inter.get("animate_s")
                animate_s = (
                    float(recipe_animate) if recipe_animate is not None
                    else MIN_CURTAIN_ANIMATE_S
                )
                # Apply same 60% clamp as apply_curtain_close_tail
                animate_s = min(animate_s, dur * _CURTAIN_MAX_RATIO)
                slot_end_abs = cumulative_s + dur
                accel_at = slot_end_abs - animate_s
                # Clamp: accel must be within the overlay's time range;
                # use min() so curtain-derived accel can override to earlier
                if accel_at > entry["start_s"]:
                    entry["font_cycle_accel_at_s"] = min(
                        entry.get("font_cycle_accel_at_s", accel_at),
                        accel_at,
                    )

            # 6. Clamp overlay end to curtain completion — text disappears
            # when bars fully close, not after. Runs AFTER timing overrides
            # so the clamp always wins (text on black is never correct).
            if inter and inter.get("type") == "curtain-close":
                slot_end_abs = cumulative_s + dur
                if entry["end_s"] > slot_end_abs:
                    entry["end_s"] = slot_end_abs

            raw.append(entry)

        cumulative_s += dur

        # Account for interstitial hold time after this slot
        if inter:
            cumulative_s += float(inter.get("hold_s", 1.0))

    if not raw:
        return []

    # ── Dedup 1: merge adjacent same-text overlays ──────────────────────
    # Templates report overlays per-shot, so "Welcome to" or "PERU" may
    # appear on multiple consecutive slots. Instead of dropping duplicates,
    # MERGE overlays that share the same text AND position and have a
    # small gap (< 2.0s, covers interstitial hold + margin).
    # 2.0s covers the default interstitial hold (1.0s) plus margin for
    # frame-boundary rounding.  A gap > 2.0s likely means a long black
    # hold where the text should legitimately restart.
    _MERGE_GAP_THRESHOLD_S = 2.0
    raw.sort(key=lambda o: (o["text"].lower().strip(), o["position"], o["start_s"]))
    unique: list[dict] = []
    for ov in raw:
        key = ov["text"].lower().strip()
        merged = False
        # Check if we can merge with an existing entry
        for prev in unique:
            prev_key = prev["text"].lower().strip()
            if (
                prev_key == key
                and prev["position"] == ov["position"]
                and prev.get("font_family") == ov.get("font_family")
                and not prev.get("spans") and not ov.get("spans")
                and ov["start_s"] - prev["end_s"] < _MERGE_GAP_THRESHOLD_S
            ):
                # Merge: extend previous overlay's end time
                prev["end_s"] = max(prev["end_s"], ov["end_s"])
                # If current has font-cycle effect, upgrade previous
                if ov.get("effect") == "font-cycle":
                    prev["effect"] = "font-cycle"
                # If current has accel, copy it to previous
                if "font_cycle_accel_at_s" in ov:
                    prev["font_cycle_accel_at_s"] = ov["font_cycle_accel_at_s"]
                merged = True
                break
        if not merged:
            unique.append(ov)

    # ── Dedup 2: prevent same-position time overlap ───────────────────────
    # Sort by position then start time. If two overlays share a position
    # and overlap in time, truncate the earlier one to end before the
    # later one starts (with a small gap for visual clarity).
    unique.sort(key=lambda o: (o["position"], o["start_s"]))
    for i in range(len(unique) - 1):
        if unique[i]["position"] == unique[i + 1]["position"]:
            if unique[i]["end_s"] > unique[i + 1]["start_s"]:
                unique[i]["end_s"] = unique[i + 1]["start_s"] - 0.1
                # Keep font-cycle accel timestamp within the truncated range
                accel = unique[i].get("font_cycle_accel_at_s")
                if accel is not None and accel >= unique[i]["end_s"]:
                    del unique[i]["font_cycle_accel_at_s"]

    # Remove any that became invalid after truncation
    result = [o for o in unique if o["end_s"] > o["start_s"]]

    log.info("text_overlays_collected", raw=len(raw), deduped=len(result))
    return result


def _pre_burn_curtain_slot_text(
    clip_path: str,
    step,
    slot_dur: float,
    clip_metas: list | None,
    subject: str,
    slot_idx: int,
    tmpdir: str,
    inter: dict | None = None,
) -> str:
    """Burn text overlays onto a slot clip BEFORE curtain-close is applied.

    This ensures the curtain closes OVER the text (tiki-style), rather than
    the text floating on top of the curtain bars.  Uses slot-relative
    timestamps (0 … slot_dur) instead of absolute joined-video timestamps.

    Returns the path to the text-burned clip (or original if no overlays).
    """
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415
    from app.pipeline.text_overlay import generate_text_overlay_png  # noqa: PLC0415

    slot = step.slot if hasattr(step, "slot") else step.get("slot", {})
    overlays = slot.get("text_overlays", [])
    if not overlays:
        return clip_path

    clip_id = step.clip_id if hasattr(step, "clip_id") else step.get("clip_id", "")
    clip_meta = _find_clip_meta_by_id(clip_id, clip_metas)

    # Build overlay entries with slot-relative timestamps
    slot_overlays: list[dict] = []
    for ov in overlays:
        text = _resolve_overlay_text(ov.get("role", "label"), clip_meta, ov, subject=subject)
        if not text or not text.strip():
            continue

        # Skip combined prefix+subject overlays
        sample = ov.get("sample_text", "")
        if (
            sample
            and " " in sample
            and any(w.isupper() and len(w) > 1 for w in sample.split())
            and sample.lower().startswith("welcome")
        ):
            continue

        ov_start = float(ov.get("start_s", 0.0))
        ov_end = float(ov.get("end_s", slot_dur))
        # Clamp end to slot duration
        ov_end = min(ov_end, slot_dur)

        # Apply label config styling
        entry = {
            "text": text,
            "start_s": ov_start,
            "end_s": ov_end,
            "position": ov.get("position", "center"),
            "effect": ov.get("effect", "none"),
            "font_style": ov.get("font_style", "display"),
            "text_size": ov.get("text_size", "medium"),
            "text_size_px": ov.get("text_size_px"),
            "text_color": ov.get("text_color", "#FFFFFF"),
            "position_y_frac": ov.get("position_y_frac"),
        }

        role = ov.get("role", "")
        sample_text = ov.get("sample_text", "")
        is_subject = _is_subject_placeholder(sample_text)
        is_label_like = role == "label" or is_subject or sample_text.lower().startswith("welcome")
        if is_label_like:
            config = _LABEL_CONFIG["subject" if is_subject else "prefix"]
            # Recipe styling takes priority — only apply non-styling keys
            # from _LABEL_CONFIG so admin editor preview matches render.
            _STYLING_KEYS = {
                "text_size", "text_size_px", "font_style",
                "font_family", "text_color", "effect",
            }
            entry.update(
                {k: v for k, v in config.items()
                 if k not in ("start_s", "accel_at_s") and k not in _STYLING_KEYS}
            )

            # Font-cycle acceleration synced with curtain animation
            if is_subject and entry.get("effect") == "font-cycle" and inter:
                from app.pipeline.interstitials import (  # noqa: PLC0415
                    _CURTAIN_MAX_RATIO,
                    MIN_CURTAIN_ANIMATE_S,
                )
                recipe_animate = inter.get("animate_s")
                animate_s = (
                    float(recipe_animate) if recipe_animate is not None
                    else MIN_CURTAIN_ANIMATE_S
                )
                animate_s = min(animate_s, slot_dur * _CURTAIN_MAX_RATIO)
                accel_at = slot_dur - animate_s
                if accel_at > ov_start:
                    entry["font_cycle_accel_at_s"] = accel_at
                # Extend font-cycle to cover entire slot so cycling runs
                # through the curtain-close animation until bars fully
                # cover the text. Without this, Gemini's shorter end_s
                # stops cycling before the curtain finishes.
                entry["end_s"] = slot_dur

        slot_overlays.append(entry)

    if not slot_overlays:
        return clip_path

    # Generate PNGs with slot-relative timestamps
    png_configs = generate_text_overlay_png(slot_overlays, slot_dur, tmpdir, slot_index=slot_idx)
    if not png_configs:
        return clip_path

    # Burn PNGs onto clip
    burned_path = os.path.join(tmpdir, f"slot_{slot_idx}_textburned.mp4")
    cmd = ["ffmpeg", "-i", clip_path]
    for cfg in png_configs:
        cmd.extend(["-i", cfg["png_path"]])

    fc_parts = ["[0:v]null[base]"]
    prev = "base"
    for j, cfg in enumerate(png_configs):
        out = f"txt{j}"
        fc_parts.append(
            f"[{prev}][{j + 1}:v]overlay=0:0"
            f":enable='between(t,{cfg['start_s']:.3f},{cfg['end_s']:.3f})'"
            f"[{out}]"
        )
        prev = out

    cmd.extend([
        "-filter_complex", ";".join(fc_parts),
        "-map", f"[{prev}]",
        "-map", "0:a?",
        *_encoding_args(burned_path, preset="ultrafast"),
    ])

    log.info("pre_burn_curtain_text", slot=slot_idx, overlays=len(png_configs))
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        log.warning(
            "pre_burn_curtain_text_failed",
            slot=slot_idx,
            rc=result.returncode,
            stderr=result.stderr[-500:] if result.stderr else b"",
        )
        return clip_path  # fallback: no text, curtain still applied

    return burned_path


def _burn_text_overlays(
    input_path: str,
    overlays: list[dict],
    output_path: str,
    tmpdir: str,
) -> None:
    """Burn text overlays onto the joined video using FFmpeg overlay filter.

    Generates styled PNGs for each unique overlay, then composites them
    onto the video with absolute enable timing.
    """
    from app.pipeline.text_overlay import generate_text_overlay_png  # noqa: PLC0415

    # Generate PNGs for each overlay. Overlays already carry absolute
    # timestamps from _collect_absolute_overlays, so the PNGs come back
    # with correct timing. Font-cycle overlays expand to many PNGs per
    # overlay, so do NOT reassign timing with a 1:1 overlay index.
    png_configs = generate_text_overlay_png(overlays, 999.0, tmpdir, slot_index=99)
    if not png_configs:
        shutil.copy2(input_path, output_path)
        return

    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    # Build FFmpeg command with overlay filter
    cmd = ["ffmpeg", "-i", input_path]
    for cfg in png_configs:
        cmd.extend(["-i", cfg["png_path"]])

    # filter_complex: overlay each PNG with absolute enable timing
    fc_parts = ["[0:v]null[base]"]
    prev = "base"
    for i, cfg in enumerate(png_configs):
        out = f"txt{i}"
        fc_parts.append(
            f"[{prev}][{i + 1}:v]overlay=0:0"
            f":enable='between(t,{cfg['start_s']:.3f},{cfg['end_s']:.3f})'"
            f"[{out}]"
        )
        prev = out

    cmd.extend([
        "-filter_complex", ";".join(fc_parts),
        "-map", f"[{prev}]",
        "-map", "0:a?",
        # ultrafast: matches the speed bump made in _concat_demuxer. The
        # joined input already has slot-boundary I-frames from the concat
        # step, and the overlay filter chain dominates burn cost anyway —
        # CRF 18 keeps quality high. preset=fast was timing out at 600s
        # on 24-slot recipes; ultrafast finishes in well under a minute.
        *_encoding_args(output_path, preset="ultrafast"),
    ])

    log.info("burn_text_overlays", count=len(png_configs))
    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    if result.returncode != 0:
        log.warning(
            "burn_text_failed",
            rc=result.returncode,
            stderr=result.stderr.decode(errors="replace")[-500:],
        )
        shutil.copy2(input_path, output_path)
    else:
        log.info("burn_text_done")


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
    sample = overlay.get("sample_text", "") or overlay.get("text", "")

    if role == "cta":
        return ""  # CTA text generated later by copy_writer

    # Subject substitution: if the template text looks like a variable
    # place/topic name and we have a subject, substitute it.
    if subject and _is_subject_placeholder(sample):
        return subject.upper() if sample.isupper() else subject

    # If the template already has text for this overlay, USE IT.
    # "Welcome to" means "Welcome to" — not clip hook_text.
    if sample:
        return sample

    # Empty sample_text with hook role: use clip analysis hook text.
    if role == "hook" and clip_meta and clip_meta.hook_text:
        return clip_meta.hook_text

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


def _probe_duration(path: str) -> float:
    """Return the duration in seconds of a media file via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, timeout=10, check=False,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 30.0  # safe fallback


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

    # Sync video ending with music ending — but only when they're close.
    #
    # Intent: when the assembled video slightly exceeds the template audio
    # (the original bug: video 24.2s vs audio 21.4s), trim video to the
    # audio length so the music ends naturally instead of looping into a
    # restarted rhythm.
    #
    # BUT: if audio is catastrophically short (e.g., bad extract yielding
    # 3s for a 30s video), trimming video would delete 27s of content.
    # In that case, keep video at natural length and accept that audio
    # ends early (silence tail) — silent data loss beats catastrophic
    # content loss.
    #
    # Defensive: probe failures return 0.0 sentinel. Fall back gracefully.
    video_dur = _probe_duration(video_path)
    audio_dur = _probe_duration(audio_local)

    # Only trim video when audio is within this many seconds of video.
    _AUDIO_SYNC_MAX_GAP_S = 5.0

    if video_dur <= 0 and audio_dur <= 0:
        log.warning("audio_mix_probe_both_failed", falling_back_to="natural-length")
        use_duration = 0.0  # will skip -t below
    elif video_dur <= 0:
        log.warning("audio_mix_video_probe_failed", audio_dur=audio_dur)
        use_duration = audio_dur
    elif audio_dur <= 0:
        log.warning("audio_mix_audio_probe_failed", video_dur=video_dur)
        use_duration = video_dur
    else:
        gap = video_dur - audio_dur
        if 0 < gap <= _AUDIO_SYNC_MAX_GAP_S:
            # Slight overhang (the target bug): trim video to audio length.
            use_duration = audio_dur
        elif gap > _AUDIO_SYNC_MAX_GAP_S:
            # Audio catastrophically short — keep video, accept silent tail.
            log.warning(
                "audio_much_shorter_than_video_keeping_video",
                video_dur=round(video_dur, 2),
                audio_dur=round(audio_dur, 2),
                gap_s=round(gap, 2),
            )
            use_duration = video_dur
        else:
            # Audio >= video: cut at video end (ffmpeg handles natural).
            use_duration = video_dur
    fade_start = max(0, use_duration - 0.5)

    log.info(
        "audio_mix_durations",
        video_dur=round(video_dur, 2),
        audio_dur=round(audio_dur, 2),
        use_duration=round(use_duration, 2),
        trimming_video=video_dur > audio_dur,
    )

    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-stream_loop", "-1",
        "-i", audio_local,
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-af",
        f"afade=t=out:st={fade_start}:d=0.5,loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",
        *(["-t", f"{use_duration:.3f}"] if use_duration > 0 else []),
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


# ── Fixed-intro / dynamic-body template family ─────────────────────────────────


# Map seed-script font_family values to ASS-registered font names. The seed
# uses file-root names ("DMSans-Bold") for clarity in code, but libass needs
# the family name registered in the font file ("DM Sans").
_FIXED_INTRO_FONT_TO_ASS: dict[str, str] = {
    "DMSans-Bold": "DM Sans",
    "Montserrat-ExtraBold": "Montserrat",
    "Outfit-Bold": "Outfit",
    "PlayfairDisplay-Bold": "Playfair Display",
}

# Output spec for the intro and body. Mirrors interstitials._WIDTH/_HEIGHT/_FPS
# but kept local to avoid coupling to that private API.
_FIXED_INTRO_WIDTH = 1080
_FIXED_INTRO_HEIGHT = 1920
_FIXED_INTRO_FPS = 30


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert a float seconds value to ASS H:MM:SS.cc format."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _build_fixed_intro_ass(
    captions: list[dict],
    style: dict,
    out_path: str,
) -> None:
    """Write an ASS subtitle file with one Dialogue per caption.

    Style notes:
    - PlayResX/Y match the render canvas (1080x1920) so positioning math is 1:1.
    - BorderStyle=1 + Shadow=2 produces a soft drop shadow (no hard outline)
      to match the reference TikTok aesthetic.
    - Alignment=5 = center-anchored, MarginV=0 puts the caption vertically
      centered in the frame.
    """
    font_family_seed = style.get("font_family", "DMSans-Bold")
    fontname = _FIXED_INTRO_FONT_TO_ASS.get(font_family_seed, font_family_seed)
    font_size = int(style.get("font_size", 64))
    # ASS uses &HBBGGRR (no alpha for primary). Strip leading '#'.
    color_hex = (style.get("color") or "#FFFFFF").lstrip("#")
    # ASS expects BGR; flip RGB.
    primary_bgr = f"{color_hex[4:6]}{color_hex[2:4]}{color_hex[0:2]}".upper()
    # Shadow color (back). Default black.
    shadow_hex = (style.get("shadow_color") or "#000000").lstrip("#")
    shadow_bgr = f"{shadow_hex[4:6]}{shadow_hex[2:4]}{shadow_hex[0:2]}".upper()
    shadow_blur = int(style.get("shadow_blur", 8))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {_FIXED_INTRO_WIDTH}\n"
        f"PlayResY: {_FIXED_INTRO_HEIGHT}\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{fontname},{font_size},"
        f"&H00{primary_bgr},&H00{primary_bgr},"
        f"&H00{shadow_bgr},&H80{shadow_bgr},"
        "-1,0,0,0,100,100,0,0,1,0,"
        f"{shadow_blur},5,80,80,0,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    lines: list[str] = [header]
    for cap in captions:
        start = _seconds_to_ass_time(float(cap["start_s"]))
        end = _seconds_to_ass_time(float(cap["end_s"]))
        # ASS-escape order matters: backslash first, then override braces,
        # then newline. `{...}` is libass override syntax (e.g. `{\fs100}`)
        # so unescaped braces from JSONB-stored or admin-edited captions
        # would silently mutate rendering. Comma escape is unnecessary —
        # Text is the trailing field, commas inside it are not collisions.
        text = (
            str(cap["text"])
            .replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\n", "\\N")
        )
        lines.append(
            f"Dialogue: 0,{start},{end},Caption,,0,0,0,,{text}\n"
        )

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _render_intro_with_captions(
    captions: list[dict],
    style: dict,
    intro_duration_s: float,
    background_color: str,
    out_path: str,
    tmpdir: str,
) -> None:
    """Render the silent intro: black hold + ASS captions burned on top.

    Single FFmpeg pass: lavfi color generator → subtitles filter → libx264.
    No intermediate files, no audio stream.
    """
    from app.pipeline.text_overlay import FONTS_DIR  # noqa: PLC0415

    ass_path = os.path.join(tmpdir, "intro_captions.ass")
    _build_fixed_intro_ass(captions, style, ass_path)

    # FFmpeg subtitles filter requires escaped path on macOS/Linux.
    # Single-quote the path inside the filter to be safe with special chars.
    ass_filter_path = ass_path.replace(":", "\\:").replace("'", "\\'")
    fonts_dir_escaped = FONTS_DIR.replace(":", "\\:").replace("'", "\\'")

    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", (
            f"color=c={background_color}:"
            f"s={_FIXED_INTRO_WIDTH}x{_FIXED_INTRO_HEIGHT}:"
            f"d={intro_duration_s:.3f}:r={_FIXED_INTRO_FPS}"
        ),
        "-vf", f"subtitles='{ass_filter_path}':fontsdir='{fonts_dir_escaped}'",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        # Pin color metadata so the concat-copy boundary with body.mp4
        # doesn't produce a sudden color shift on QuickTime/Safari/iOS.
        "-color_range", "tv",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-r", str(_FIXED_INTRO_FPS),
        "-t", f"{intro_duration_s:.3f}",
        "-an",
        "-movflags", "+faststart",
        "-y",
        out_path,
    ]

    log.info("fixed_intro_render_start", duration_s=intro_duration_s, captions=len(captions))
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"intro render failed: {stderr}")
    log.info("fixed_intro_render_done", out=out_path)


def _cut_body_segment(
    src_video: str,
    start_s: float,
    duration_s: float,
    out_path: str,
) -> None:
    """Cut a body segment from src_video, scaled+padded to 9:16, silent.

    Forces CFR @ 30fps so the body matches the intro's frame timebase.
    Strips audio (-an) — audio is added later by the mixer.
    """
    # scale: fit within 1080x1920, preserving aspect
    # pad: center on a 1080x1920 black canvas
    vf = (
        f"scale=w={_FIXED_INTRO_WIDTH}:h={_FIXED_INTRO_HEIGHT}:"
        "force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={_FIXED_INTRO_WIDTH}:{_FIXED_INTRO_HEIGHT}:"
        "(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1"
    )
    cmd = [
        "ffmpeg",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", src_video,
        "-vf", vf,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        # Match intro's color metadata so concat-copy doesn't produce a
        # sudden color shift at the boundary on QuickTime/Safari/iOS.
        "-color_range", "tv",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-r", str(_FIXED_INTRO_FPS),
        "-vsync", "cfr",
        "-an",
        "-movflags", "+faststart",
        "-y",
        out_path,
    ]
    log.info("body_cut_start", start_s=start_s, duration_s=duration_s)
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"body cut failed: {stderr}")


def _concat_silent(segments: list[str], out_path: str, tmpdir: str) -> None:
    """Concat already-encoded mp4 segments via concat demuxer (-c copy).

    All segments must share codec/fps/timebase. Output is silent (-an).
    """
    list_path = os.path.join(tmpdir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for seg in segments:
            # concat demuxer needs single-quoted paths; escape any single quotes.
            escaped = seg.replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-an",
        "-movflags", "+faststart",
        "-y",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"concat failed: {stderr}")


def _audio_energy_curve(
    user_video_path: str,
    bucket_s: float = 1.0,
) -> list[float]:
    """Return per-bucket linear RMS energy for the audio of `user_video_path`.

    Each bucket is `bucket_s` seconds wide. Returns an empty list if the
    video has no audio stream or ffmpeg fails — caller falls back to the
    midpoint heuristic.

    Linear (not dB) RMS lets the caller sum windows directly to find the
    loudest contiguous range. Soccer celebrations, drum drops, and "loud
    reaction" content all map cleanly to peaks; quiet talking-head content
    degrades gracefully (curve is flat → midpoint wins).
    """
    samples_per_bucket = int(bucket_s * 44100)
    cmd = [
        "ffmpeg", "-i", user_video_path, "-vn",
        "-af",
        f"asetnsamples={samples_per_bucket},"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        log.warning("audio_energy_probe_failed", path=user_video_path)
        return []

    rms_db: list[float] = []
    for line in result.stderr.decode(errors="replace").splitlines():
        if "RMS_level" in line:
            try:
                val = line.rsplit("=", 1)[-1].strip()
                # Skip "-inf" sentinels — silent buckets become very-low energy.
                if val == "-inf":
                    rms_db.append(-90.0)
                else:
                    rms_db.append(float(val))
            except (ValueError, IndexError):
                continue

    # dB → linear amplitude. Sums are meaningful in linear space.
    return [10 ** (db / 20) for db in rms_db]


def _select_body_window_peak_anchored(
    user_video_path: str,
    *,
    pre_roll_s: float,
    max_body_s: float,
    min_body_s: float,
) -> tuple[float, float]:
    """Peak-anchored body picker (the v2 algorithm — replaces midpoint heuristic).

    Centers the body around the user clip's loudest moment, with a small
    pre-roll so the buildup-to-action is included. Trails as far as the
    available music allows so the celebration plays out naturally.

    Returns (start_s, end_s). Body length is variable, NOT fixed: it adapts
    to the user's clip length, respecting `min_body_s` and `max_body_s`.

    Algorithm:
      peak_t   = argmax of per-second audio energy (e.g. crowd noise peak)
      start    = max(0, peak_t - pre_roll_s)
      end      = min(user_dur, start + max_body_s)
      // if too short to be useful, drag start earlier
      if end - start < min_body_s:
          start = max(0, end - min_body_s)
    """
    from app.pipeline.probe import probe_video  # noqa: PLC0415

    probe = probe_video(user_video_path)
    user_dur = probe.duration_s
    if user_dur < min_body_s:
        raise ValueError(
            f"User video too short: have {user_dur:.2f}s, "
            f"need at least {min_body_s:.2f}s of body"
        )

    energy = _audio_energy_curve(user_video_path, bucket_s=1.0)
    if energy:
        peak_t = float(energy.index(max(energy)))
        log.info("body_v2_audio_peak", peak_t=peak_t, buckets=len(energy))
    else:
        # No audio → assume action is in the back half of the clip.
        peak_t = max(0.0, user_dur * 0.6)
        log.info("body_v2_no_audio_curve", fallback_peak_t=peak_t)

    start = max(0.0, peak_t - pre_roll_s)
    end = min(user_dur, start + max_body_s)

    if end - start < min_body_s:
        # Drag start earlier so we always render at least min_body_s.
        start = max(0.0, end - min_body_s)

    log.info(
        "body_v2_window_chosen",
        peak_t=round(peak_t, 2),
        start=round(start, 2),
        end=round(end, 2),
        dur=round(end - start, 2),
        user_dur=round(user_dur, 2),
    )
    return start, end


def _extract_intro_from_template_video(
    template_video_path: str,
    intro_duration_s: float,
    out_path: str,
    width: int = _FIXED_INTRO_WIDTH,
    height: int = _FIXED_INTRO_HEIGHT,
    fps: int = _FIXED_INTRO_FPS,
) -> None:
    """Cut the first `intro_duration_s` of the template reference video.

    Scales + pads to (width × height) at `fps`. KEEPS audio unchanged so the
    intro is byte-faithful to the original (VO + any background bleed all in
    one shot — no synthetic ASS captions, no ducked-music logic).
    """
    vf = (
        f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1"
    )
    cmd = [
        "ffmpeg",
        "-ss", "0",
        "-t", f"{intro_duration_s:.3f}",
        "-i", template_video_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-color_range", "tv",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-r", str(fps),
        "-vsync", "cfr",
        # Audio: pass through at source quality. Re-encoding here was
        # the first of TWO encode passes degrading the intro VO. The
        # composer below mixes it once more — that's the only encode
        # the intro audio should suffer, not two.
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-y",
        out_path,
    ]
    log.info("intro_extract_start", duration_s=intro_duration_s)
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"intro extract failed: {stderr}")


def _compose_intro_body_with_music(
    intro_path: str,
    body_path: str,
    music_path: str,
    output_path: str,
    intro_duration_s: float,
    output_lufs: float = -14.0,
) -> None:
    """Concat intro+body video AND mix audio in a single ffmpeg pass.

    Audio composition:
      - intro audio passes through unchanged for [0..intro_duration_s)
      - silence from intro_duration_s onward (apad on intro)
      - music starts AT intro_duration_s (delayed via adelay), no loop
      - amix the two streams; intro dominates first segment, music the rest
      - final loudnorm + 300ms fade-out

    Music must be at LEAST (total_duration - intro_duration_s) seconds long.
    Caller is responsible for asset prep — this function does NOT loop.
    """
    intro_dur = _probe_duration_for_compose(intro_path)
    body_dur = _probe_duration_for_compose(body_path)
    music_dur = _probe_duration_for_compose(music_path)
    total_dur = intro_dur + body_dur

    log.info(
        "compose_start",
        intro_dur=round(intro_dur, 3),
        body_dur=round(body_dur, 3),
        music_dur=round(music_dur, 3),
        total_dur=round(total_dur, 3),
    )

    if music_dur > 0 and music_dur < (body_dur - 0.1):
        log.warning(
            "music_shorter_than_body",
            music_dur=round(music_dur, 3),
            body_dur=round(body_dur, 3),
            note="body tail will be silent — caller should provide longer music",
        )

    intro_end_ms = int(round(intro_duration_s * 1000))
    fade_out_start = max(0.0, total_dur - 0.5)

    # Transition timing: intro audio fades out 300ms before the drop, music
    # fades in 300ms before the drop with a longer 800ms ramp. The two
    # streams overlap during the silence region at the end of intro (most
    # TikTok intros end with a brief breath/silence after the punchline)
    # so neither clashes with active speech.
    intro_fadeout_d = 0.3
    music_fadein_d = 0.8
    intro_fade_start = max(0.0, intro_duration_s - intro_fadeout_d)
    music_fade_start = max(0.0, intro_duration_s - intro_fadeout_d)

    filter_complex = (
        # Video: concat intro + body
        "[0:v][1:v]concat=n=2:v=1:a=0[v_out];"
        # Intro audio: pass through at native quality (NO loudnorm — that
        # compressor was killing the VO dynamics and the user heard "kalite
        # bozuk"). Just pad to total duration, fade out before the drop.
        f"[0:a]apad=pad_dur={total_dur:.3f},atrim=0:{total_dur:.3f},"
        "asetpts=PTS-STARTPTS,"
        f"afade=t=out:st={intro_fade_start:.3f}:d={intro_fadeout_d}[a_intro];"
        # Music: delayed to start at drop, padded, faded in. Loudnorm is
        # applied ONLY to the music branch — keeps body music consistent
        # across templates without touching the intro VO.
        f"[2:a]adelay={intro_end_ms}|{intro_end_ms},"
        f"apad=pad_dur={total_dur:.3f},atrim=0:{total_dur:.3f},"
        "asetpts=PTS-STARTPTS,"
        f"afade=t=in:st={music_fade_start:.3f}:d={music_fadein_d}[a_music];"
        # Mix. CRITICAL: normalize=0 disables amix's automatic gain reduction
        # (default normalize=1 divides by sum-of-weights, dropping each input
        # by ~6dB and making the whole output sound quiet). With normalize=0
        # each stream keeps its native level — clipping prevented by the
        # final loudnorm below. Weights stay 1:1 because intro and music
        # don't overlap in time except for the 300ms transition.
        "[a_intro][a_music]amix=inputs=2:duration=longest:weights=1 1:normalize=0[mix];"
        # Final loudness pass on the MIX. Removed the per-branch loudnorm
        # on music so the intro VO and music end up in the same dynamic
        # space. LRA=14 gives more headroom than the default 11, leaving
        # the VO dynamics intact while still hitting the target loudness.
        # aresample AFTER loudnorm pins output to 48kHz (otherwise the AAC
        # encoder lands on 96kHz = "weird pitch" playback bug).
        f"[mix]loudnorm=I={output_lufs}:TP=-1.5:LRA=14,"
        f"aresample=48000,"
        f"afade=t=out:st={fade_out_start:.3f}:d=0.5[a_out]"
    )

    cmd = [
        "ffmpeg",
        "-i", intro_path,
        "-i", body_path,
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "[v_out]",
        "-map", "[a_out]",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-color_range", "tv",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-r", str(_FIXED_INTRO_FPS),
        "-vsync", "cfr",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{total_dur:.3f}",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-800:]
        log.warning("compose_failed", stderr=stderr)
        # Last-resort fallback: at least ship a video without music.
        shutil.copy2(intro_path, output_path)
        return

    log.info("compose_done", output=output_path)


def _probe_duration_for_compose(path: str) -> float:
    """Same as _probe_duration in intro_voiceover_mix; redeclared locally to
    avoid pulling that whole module into orchestrator scope just for one helper."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, timeout=10, check=False,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def _select_body_window(
    user_video_path: str,
    target_duration_s: float,
    tolerance_s: float,
    snap_start_to_scene: bool,
) -> tuple[float, float]:
    """Pick the best `target_duration_s` window from the user's single clip.

    Algorithm (in priority order):
    1. Compute per-second audio energy. Find the contiguous N-second window
       with the highest summed energy — this is the "action peak" (crowd
       celebration in sports, drop in music, dramatic line in talking head).
    2. If audio energy is unavailable (no audio stream, probe failure),
       fall back to the natural midpoint.
    3. If `snap_start_to_scene` is True, snap the chosen start to the
       nearest scene cut within `tolerance_s` so the cut doesn't land mid-
       motion. Snap is best-effort; no eligible cut → keep the audio pick.

    End is ALWAYS `start_s + target_duration_s` — total budget guaranteed.
    """
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.scene_detect import detect_scenes  # noqa: PLC0415

    probe = probe_video(user_video_path)
    user_dur = probe.duration_s
    if user_dur < target_duration_s:
        raise ValueError(
            f"User video too short for body: have {user_dur:.2f}s, "
            f"need ≥ {target_duration_s:.2f}s"
        )

    natural_start = max(0.0, (user_dur - target_duration_s) / 2.0)
    max_start = max(0.0, user_dur - target_duration_s)

    # ── Step 1: audio-energy peak detection ─────────────────────────────
    energy = _audio_energy_curve(user_video_path, bucket_s=1.0)
    window_buckets = max(1, int(round(target_duration_s)))
    energy_chosen: float | None = None
    if len(energy) >= window_buckets:
        # Sliding window sum over linear energy; highest sum wins.
        best_sum = -1.0
        best_idx = 0
        running = sum(energy[:window_buckets])
        if running > best_sum:
            best_sum = running
            best_idx = 0
        for i in range(1, len(energy) - window_buckets + 1):
            running += energy[i + window_buckets - 1] - energy[i - 1]
            if running > best_sum:
                best_sum = running
                best_idx = i
        # Don't let the window slide past the end of the video.
        energy_chosen = float(min(best_idx, max_start))
        log.info(
            "body_window_audio_peak",
            chosen_start=round(energy_chosen, 3),
            natural_start=round(natural_start, 3),
            buckets=len(energy),
            window_buckets=window_buckets,
        )
    else:
        log.info(
            "body_window_audio_unavailable",
            buckets=len(energy),
            falling_back="midpoint",
        )

    # ── Step 2: choose base start (audio peak if available, else midpoint) ─
    base_start = energy_chosen if energy_chosen is not None else natural_start

    if not snap_start_to_scene:
        return base_start, base_start + target_duration_s

    # ── Step 3: scene-snap (best-effort, doesn't override audio pick if
    # no eligible cut is nearby) ────────────────────────────────────────
    cuts = detect_scenes(user_video_path)
    if not cuts:
        log.info("body_window_no_scene_cuts", chosen_start=round(base_start, 3))
        return base_start, base_start + target_duration_s

    eligible = [c.timestamp_s for c in cuts if 0.0 <= c.timestamp_s <= max_start]
    if not eligible:
        return base_start, base_start + target_duration_s

    nearby = [c for c in eligible if abs(c - base_start) <= tolerance_s]
    chosen = (
        min(nearby, key=lambda c: abs(c - base_start)) if nearby else base_start
    )

    log.info(
        "body_window_chosen",
        natural_start=round(natural_start, 3),
        audio_pick=round(energy_chosen, 3) if energy_chosen is not None else None,
        chosen_start=round(chosen, 3),
        snapped=chosen != base_start,
    )
    return chosen, chosen + target_duration_s


def _run_single_video_job(
    *,
    job_id: str,
    template_id: str,
    recipe_data: dict,
    clip_paths_gcs: list[str],
    audio_gcs_path: str | None,
    voiceover_gcs_path: str | None,  # kept for backward compat; unused in v2
    user_subject: str,
    selected_platforms: list[str],
) -> None:
    """Render path for `template_kind="single_video"` — v2.

    Pipeline (v2):
        download template reference video (= the original TikTok)
            → cut first `intro_duration_s` of it WITH AUDIO (no synth, no ASS)
        download user clip
            → pick peak-anchored body window (audio peak + pre-roll)
            → cut body, scale+pad to 9:16, silent
        compose: concat (intro_with_audio + body_silent)
                 + mix (intro audio passthrough + body music delayed to drop)
        upload + persist + generate copy

    The v1 path (synth black hold + ASS captions + dueled VO mix) is gone.
    The intro is now byte-faithful to the original TikTok.
    """
    if len(clip_paths_gcs) != 1:
        raise ValueError(
            "single_video templates expect exactly 1 user clip; "
            f"got {len(clip_paths_gcs)}"
        )

    intro_duration_s = float(recipe_data.get("intro_duration_s", 10.5))
    body_cfg = recipe_data.get("body") or {}
    pre_roll_s = float(body_cfg.get("pre_roll_s", 4.0))
    max_body_s = float(body_cfg.get("max_duration_s", 14.0))
    min_body_s = float(body_cfg.get("min_duration_s", 8.0))

    # Look up the template's reference video (the original TikTok).
    # gcs_path on VideoTemplate IS the source we cut the intro from.
    with _sync_session() as db:
        template = db.get(VideoTemplate, template_id)
        if template is None:
            raise ValueError(f"Template {template_id} not found")
        template_video_gcs = template.gcs_path

    audio_health: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        # [1] Download template's reference video (intro source).
        ref_video_local = os.path.join(tmpdir, "template_ref.mp4")
        try:
            download_to_file(template_video_gcs, ref_video_local)
        except Exception as exc:
            log.error(
                "fixed_intro_template_ref_download_failed",
                gcs_path=template_video_gcs, error=str(exc),
            )
            raise

        # [2] Download user clip.
        user_local = os.path.join(tmpdir, "user_clip.mp4")
        download_to_file(clip_paths_gcs[0], user_local)

        # [3] Pick body window (peak-anchored, variable length).
        try:
            body_start, body_end = _select_body_window_peak_anchored(
                user_local,
                pre_roll_s=pre_roll_s,
                max_body_s=max_body_s,
                min_body_s=min_body_s,
            )
        except ValueError as exc:
            user_msg = (
                f"Video too short for this template. "
                f"Need at least {min_body_s + 1:.1f}s. ({exc})"
            )
            log.warning(
                "fixed_intro_video_too_short",
                job_id=job_id, min_required_s=min_body_s, error=str(exc),
            )
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job:
                    job.status = "failed"
                    job.error_detail = user_msg
                    db.commit()
            return
        body_dur = body_end - body_start

        # [4] Extract intro from template reference (KEEPS audio).
        intro_path = os.path.join(tmpdir, "intro.mp4")
        _extract_intro_from_template_video(
            ref_video_local, intro_duration_s, intro_path,
        )

        # [5] Cut body segment (silent, 9:16).
        body_path = os.path.join(tmpdir, "body.mp4")
        _cut_body_segment(user_local, body_start, body_dur, body_path)

        # [6] Compose final: concat video + mix audio (intro audio + body music).
        if not audio_gcs_path:
            # Music missing — produce video with silent body. Better than failure.
            log.warning("fixed_intro_no_music_asset", template_id=template_id)
            audio_health.append("music_path_unset")
            final_path = os.path.join(tmpdir, "final.mp4")
            _concat_silent([intro_path, body_path], final_path, tmpdir)
        else:
            music_local = os.path.join(tmpdir, "music.m4a")
            try:
                download_to_file(audio_gcs_path, music_local)
            except Exception as exc:
                log.warning("fixed_intro_music_download_failed", error=str(exc))
                audio_health.append("music_download_failed")
                final_path = os.path.join(tmpdir, "final.mp4")
                _concat_silent([intro_path, body_path], final_path, tmpdir)
            else:
                final_path = os.path.join(tmpdir, "final.mp4")
                _compose_intro_body_with_music(
                    intro_path=intro_path,
                    body_path=body_path,
                    music_path=music_local,
                    output_path=final_path,
                    intro_duration_s=intro_duration_s,
                )

        # [7] Upload primary + base output (same file for this template family —
        # there are no slot-level overlays to strip for an "editor preview")
        # template_output.mp4 and template_base.mp4 are byte-identical for
        # this template family (no slot-level overlays to strip for the
        # editor's "without overlays" preview). Upload once, server-side
        # copy to the second key — saves egress + re-upload bandwidth.
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        video_url = upload_public_read(final_path, gcs_output_path)
        gcs_base_path = f"jobs/{job_id}/template_base.mp4"
        base_video_url = copy_object_signed_url(gcs_output_path, gcs_base_path)

        # [8] Generate copy. Hook text = the template's signature punchline.
        # We hardcode the well-known punchline rather than reading from a
        # captions list (v2 doesn't render captions any more — they live in
        # the source TikTok we copied from).
        hook_text = "How do you enjoy your life?"
        from app.pipeline.agents.copy_writer import generate_copy  # noqa: PLC0415
        platform_copy, copy_status = generate_copy(
            hook_text=hook_text,
            transcript_excerpt="",
            platforms=selected_platforms,
            has_transcript=False,
            template_tone="provocative",
        )

        # [9] Persist
        plan_data = {
            "template_kind": "single_video",
            "intro_duration_s": intro_duration_s,
            "body_window": {"start_s": body_start, "end_s": body_end},
            "output_url": video_url,
            "base_output_url": base_video_url,
            "platform_copy": platform_copy.model_dump(),
            "copy_status": copy_status,
            # Audio health: empty list means VO + music both rendered;
            # any non-empty list means the user got a degraded output.
            # Surfaces silent-video failures that would otherwise be invisible
            # outside worker logs.
            "audio_health": audio_health,
        }
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "template_ready"
                job.assembly_plan = plan_data
                if audio_health:
                    job.error_detail = (
                        f"audio assets unavailable: {','.join(audio_health)}"
                    )
                db.commit()

        log.info(
            "fixed_intro_template_done",
            job_id=job_id,
            output_url=video_url,
            body_start=round(body_start, 3),
            body_end=round(body_end, 3),
        )
