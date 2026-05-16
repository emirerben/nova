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
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC

import redis as redis_lib
import structlog
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, TemplateRecipeVersion, VideoTemplate
from app.pipeline.agentic_matcher import agentic_match_or_fallback as _agentic_match_or_fallback
from app.pipeline.agents.gemini_analyzer import (
    AssemblyPlan,
    AssemblyStep,
    ClipMeta,
    GeminiAnalysisError,
    GeminiRefusalError,
    TemplateRecipe,
    analyze_clip,
    analyze_template,
    gemini_upload_and_wait,
)
from app.pipeline.reframe import ReframeError
from app.pipeline.single_pass import (
    SinglePassInput,
    SinglePassSpec,
    SinglePassUnsupportedError,
    run_single_pass,
)
from app.pipeline.template_matcher import (
    TemplateMismatchError,
    consolidate_slots,
    match,
)
from app.services.job_phases import (
    PHASE_ANALYZE_CLIPS,
    PHASE_ASSEMBLE,
    PHASE_DOWNLOAD_CLIPS,
    PHASE_FINALIZE,
    PHASE_GENERATE_COPY,
    PHASE_MATCH_CLIPS,
    PHASE_MIX_AUDIO,
    PHASE_UPLOAD,
    mark_failed_phase,
    mark_finished,
    mark_started,
    record_phase,
    record_sub_phase,
)
from app.services.template_poster import (
    PosterExtractionError,
)
from app.services.template_poster import (
    generate_and_upload as generate_poster,
)
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
        "text_size": "small",  # 48px — small italic serif, subordinate to subject
        "start_s": 2.0,  # appears at absolute second 2
    },
    "subject": {
        "text_size": "xxlarge",  # 250px — fills ~75% width like reference
        "font_style": "sans",  # Montserrat ExtraBold
        "text_color": "#F4D03F",  # warm maize/gold
        "start_s": 3.0,  # appears 1s after prefix
        "effect": "font-cycle",  # forced font cycling
        "accel_at_s": 8.0,  # font cycle accelerates after second 8
    },
}


# Routing-only keys that live on `recipe_cached` JSON but are NOT valid
# TemplateRecipe constructor kwargs. Migration 0010 backfilled `template_kind`
# onto every existing recipe; future routing/dispatch fields go here.
_ROUTING_ONLY_RECIPE_KEYS: frozenset[str] = frozenset({"template_kind", "has_intro_slot"})


# Failure-reason taxonomy. Persisted on Job.failure_reason for any
# processing_failed job. The frontend maps these to user-friendly copy
# instead of falling back to "Something went wrong".
FAILURE_REASON_TEMPLATE_MISCONFIGURED = "template_misconfigured"
FAILURE_REASON_TEMPLATE_ASSETS_MISSING = "template_assets_missing"
FAILURE_REASON_USER_CLIP_DOWNLOAD_FAILED = "user_clip_download_failed"
FAILURE_REASON_USER_CLIP_UNUSABLE = "user_clip_unusable"
FAILURE_REASON_FFMPEG_FAILED = "ffmpeg_failed"
FAILURE_REASON_GEMINI_ANALYSIS_FAILED = "gemini_analysis_failed"
FAILURE_REASON_COPY_GENERATION_FAILED = "copy_generation_failed"
FAILURE_REASON_OUTPUT_UPLOAD_FAILED = "output_upload_failed"
FAILURE_REASON_TIMEOUT = "timeout"
FAILURE_REASON_UNKNOWN = "unknown"


class _StageError(Exception):
    """Carries a failure_reason from a stage to the outer task handler.

    Stages raise this (via the `_stage` context manager); the outer Celery
    task catches it and persists both `error_detail` and `failure_reason`
    on the Job in one place.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _clip_id_to_path_map(
    clip_metas_ordered: list,
    paths: list[str],
) -> dict[str, str]:
    """Build clip_id → path mapping, parallel to local_paths by index.

    Used to wire the assembly_plan's `clip_id` (a Gemini file ref name like
    "files/abc123") back to the user's GCS path or the local temp path.

    `clip_metas_ordered` is the canonical index→meta map produced by
    `_analyze_clips_with_cache`. We pull `clip_id` from each meta rather
    than zipping with `file_refs` because on cache hits (PR #87 / 8e4cc52)
    the Gemini upload is skipped and `file_refs[i]` is None — meta.clip_id
    is the surviving identifier and matches what `ref.name` would have been.

    Skips indices where `meta is None` (clip analysis failed). Those clips
    are excluded from the assembly plan upstream so dropping them here is
    consistent and prevents a downstream KeyError.
    """
    return {meta.clip_id: paths[i] for i, meta in enumerate(clip_metas_ordered) if meta is not None}


@contextmanager
def _stage(name: str, on_fail: str, *, job_id: str) -> Iterator[None]:
    """Time + log a pipeline stage; classify uncaught exceptions.

    Wraps each step of `_run_single_video_job` so a hung or failing stage
    is identifiable in `fly logs` (which stage, how long it ran) and the
    failure carries a structured `failure_reason` to the outer handler.
    Re-raises `_StageError` untouched so an inner stage's classification
    survives an outer `_stage` block.
    """
    t0 = time.monotonic()
    log.info("fixed_intro_stage_start", stage=name, job_id=job_id)
    try:
        yield
    except _StageError:
        raise
    except SoftTimeLimitExceeded:
        # Don't reclassify — the outer handler maps this to FAILURE_REASON_TIMEOUT.
        raise
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.error(
            "fixed_intro_stage_failed",
            stage=name,
            failure_reason=on_fail,
            error=str(exc),
            elapsed_ms=elapsed_ms,
            job_id=job_id,
        )
        raise _StageError(on_fail, f"{name}: {exc}") from exc
    else:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "fixed_intro_stage_done",
            stage=name,
            elapsed_ms=elapsed_ms,
            job_id=job_id,
        )


def _build_recipe(recipe_data: dict) -> TemplateRecipe:
    """Construct a TemplateRecipe from a DB `recipe_cached` blob, stripping
    routing-only fields the dataclass doesn't accept as kwargs."""
    kwargs = {k: v for k, v in recipe_data.items() if k not in _ROUTING_ONLY_RECIPE_KEYS}
    return TemplateRecipe(**kwargs)


# ── analyze_template_task ─────────────────────────────────────────────────────


@celery_app.task(
    name="tasks.analyze_template_task",
    bind=True,
    max_retries=0,
    soft_time_limit=840,
    time_limit=900,
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
                file_ref,
                analysis_mode="two_pass",
                black_segments=black_segments,
                job_id=f"template:{template_id}",
            )

            # Note: font identification (PR2, identify_fonts + font_alternatives
            # population) is intentionally scoped to the agentic analysis path
            # only (app/tasks/agentic_template_build.py). Classic templates are
            # human-authored with deliberate font picks; running CLIP matching
            # against the reference video would write font_alternatives onto
            # overlays whose font_family the operator already chose, costing
            # ~250-400MB of resident worker memory and ~5-10s of latency per
            # analysis for no operator value.

            # Extract poster JPEG for the homepage gallery (best-effort: a
            # poster failure must not fail analysis — the gradient fallback
            # still renders).
            poster_gcs: str | None = None
            try:
                poster_gcs = generate_poster(template_id, local_path)
            except PosterExtractionError as exc:
                log.warning(
                    "template_poster_extraction_failed",
                    template_id=template_id,
                    error=str(exc),
                )
            except Exception as exc:  # GCS upload, network, etc.
                log.warning(
                    "template_poster_upload_failed",
                    template_id=template_id,
                    error=str(exc),
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
            ffmpeg_beats = _detect_audio_beats(audio_local) if os.path.exists(audio_local) else []
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
                    if poster_gcs:
                        template.thumbnail_gcs_path = poster_gcs
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
    name="tasks.orchestrate_template_job",
    bind=True,
    max_retries=0,
    soft_time_limit=1740,
    time_limit=1800,
)
def orchestrate_template_job(
    self,
    job_id: str,
    force_single_pass: bool = False,
) -> None:
    """Full template-mode pipeline. Never raises — all errors go to processing_failed.

    ``force_single_pass`` forces the single-pass FFmpeg encode for THIS job
    regardless of the env-level ``settings.single_pass_encode_enabled`` flag.
    Used for staged rollout — ops can enqueue test jobs via
    ``.apply_async(args=[job_id], kwargs={"force_single_pass": True})`` without
    bouncing workers.
    """
    log.info("template_job_start", job_id=job_id, force_single_pass=force_single_pass)

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
        _run_template_job(job_id, force_single_pass=force_single_pass)
    except _StageError as stage_err:
        log.error(
            "template_job_classified_failure",
            job_id=job_id,
            failure_reason=stage_err.reason,
            error=stage_err.message,
        )
        _mark_failed(job_uuid, stage_err.reason, stage_err.message)
    except SoftTimeLimitExceeded:
        log.error("template_job_timeout", job_id=job_id)
        _mark_failed(
            job_uuid,
            FAILURE_REASON_TIMEOUT,
            "Template job timed out (exceeded 1740s soft limit)",
        )
    except (GeminiAnalysisError, GeminiRefusalError) as exc:
        # GeminiRefusalError does NOT subclass GeminiAnalysisError — both
        # extend Exception directly (pipeline/agents/gemini_analyzer.py).
        # Without this combined catch, a refusal falls into the generic
        # `except Exception` below and is misclassified as UNKNOWN, hiding
        # "Gemini refused this clip" behind a meaningless label.
        log.error("template_job_gemini_failed", job_id=job_id, error=str(exc))
        _mark_failed(job_uuid, FAILURE_REASON_GEMINI_ANALYSIS_FAILED, str(exc))
    except TemplateMismatchError as exc:
        log.error(
            "template_job_clip_mismatch",
            job_id=job_id,
            code=exc.code,
            error=exc.message,
        )
        _mark_failed(
            job_uuid,
            FAILURE_REASON_USER_CLIP_UNUSABLE,
            f"{exc.code}: {exc.message}",
        )
    except ReframeError as exc:
        # FFmpeg failure during slot reframe / encode. Without this clause it
        # falls into the generic `except Exception` below and gets the useless
        # FAILURE_REASON_UNKNOWN label — observed in prod on job 2795fa69 where
        # a photo-converted mp4 with primaries=unknown crashed the colorspace
        # filter (rc=234 / EINVAL). Surface as FFMPEG_FAILED so on-call sees
        # the real failure class.
        log.error("template_job_reframe_failed", job_id=job_id, error=str(exc))
        _mark_failed(job_uuid, FAILURE_REASON_FFMPEG_FAILED, str(exc))
    except Exception as exc:
        log.exception("template_job_fatal", job_id=job_id, error=str(exc))
        _mark_failed(job_uuid, FAILURE_REASON_UNKNOWN, str(exc))


def _run_template_job(job_id: str, force_single_pass: bool = False) -> None:
    """Core template job logic. May raise — caller wraps in try/except."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("template_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        db.commit()

        # Stamp started_at + initial current_phase so the frontend shows motion
        # the moment the worker picks the job up — distinct from `created_at`
        # which is the original queue insert time. Inside the session block
        # because the rest of `_run_template_job` continues here.
        mark_started(job_id)

        # Fast path: locked re-render skips Gemini entirely
        if isinstance(job.assembly_plan, dict) and job.assembly_plan.get("locked"):
            _run_rerender(job_id, job, force_single_pass=force_single_pass)
            return

        # Snapshot fields before session closes
        template_id = job.template_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs = all_candidates.get("clip_paths", [])
        user_subject = ((all_candidates.get("inputs") or {}).get("location") or "").strip()
        selected_platforms = job.selected_platforms or ["tiktok", "instagram", "youtube"]
        # Admin test-tab fast preview. Skips visually-expensive interstitials
        # (curtain-close) and the copy-generation LLM call. Set by admin.py's
        # create_test_job from TestJobRequest.preview_mode. Default False.
        preview_mode = bool(all_candidates.get("preview_mode", False))

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
        # is_agentic gates two job-time changes: clip_router replaces the
        # greedy matcher, and the _LABEL_CONFIG override is skipped (the
        # agentic recipe already has per-overlay styling baked in by
        # text_designer at template-build time). Manual templates are
        # untouched — same code path, byte-identical output.
        is_agentic = bool(template.is_agentic)
        # Single-pass allow-list: per-template flag AND env-level kill
        # switch must both be true for the global rollout to fire on this
        # template. force_single_pass=True from a kwarg overrides both —
        # that path is for engineers debugging an individual job via
        # apply_async kwargs.
        template_single_pass = bool(template.single_pass_enabled)

    # Route on template_kind discriminator. Existing rows without template_kind
    # are backfilled to "multiple_videos" by migration 0012, so .get() with
    # a default is just a belt-and-suspenders guard.
    template_kind = recipe_data.get("template_kind", "multiple_videos")

    # Smell test: a multi-video pipeline run with only 1 clip is almost
    # always a routing bug — either the template's recipe was seeded before
    # the kind discriminator was backfilled, or the seed wrote the wrong
    # kind. Don't bail (keeps the failure shape unchanged for genuinely
    # legitimate 1-clip multi-video runs), but make it loud so it's the
    # first thing visible in `fly logs` when this template misbehaves.
    if template_kind == "multiple_videos" and len(clip_paths_gcs) == 1:
        log.error(
            "template_kind_routing_smell",
            template_id=template_id,
            recipe_has_kind=("template_kind" in recipe_data),
            recipe_kind=recipe_data.get("template_kind"),
        )

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
            preview_mode=preview_mode,
        )
        return

    try:
        recipe = _build_recipe(recipe_data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Template recipe in DB is malformed: {exc}") from exc

    with tempfile.TemporaryDirectory() as tmpdir:
        # [2] Download all clip files in parallel
        _phase_t0 = time.monotonic()
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)
        record_phase(
            job_id,
            PHASE_DOWNLOAD_CLIPS,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_ANALYZE_CLIPS,
        )

        mixed_media = _is_mixed_media(recipe.slots)
        _phase_t0 = time.monotonic()
        if mixed_media:
            # Mixed-media template: positional binding (clip[i] → slot[i]).
            # Photo slots skip Gemini entirely; video slots upload+analyze in parallel.
            log.info(
                "template_mixed_media_path",
                job_id=job_id,
                slot_count=len(recipe.slots),
            )
            # [2b] Probe is the only pre-render work we need on this path.
            probe_map = _probe_clips(local_clip_paths)
            slots_in_order = _slots_in_position_order(recipe.slots)
            clip_metas = _build_positional_clip_metas(
                local_clip_paths,
                slots_in_order,
                probe_map,
                job_id=job_id,
            )
            # Mixed-media path is positional 1:1, so the ordered list mirrors
            # the compact list directly.
            clip_metas_ordered = list(clip_metas)
            file_refs = []  # not used in this path
        else:
            # [2b + 3 + 4] Hash → cache lookup → concurrent (probe + upload)
            # → analyze, but only for cache misses. Layering:
            #   - Full cache hit: skip probe and Gemini entirely (~seconds).
            #   - Partial / cold: probe and Gemini upload run concurrently
            #     for the misses (carries forward PR #86's overlap optimization
            #     for the cache-miss path), then analyze.
            log.info("gemini_clip_pipeline_start", job_id=job_id, count=len(local_clip_paths))
            (
                clip_metas,
                clip_metas_ordered,
                file_refs,
                probe_map,
                failed_count,
            ) = _analyze_clips_with_cache(
                local_clip_paths,
                filter_hint=getattr(recipe, "clip_filter_hint", "") or "",
                job_id=job_id,
                record_sub_phases=preview_mode,
            )

            # Threshold check: >50% failure → fatal
            total = len(clip_metas) + failed_count
            if failed_count > total * 0.5:
                raise GeminiAnalysisError(
                    f"{failed_count}/{total} clips failed Gemini analysis — "
                    "too many failures to produce quality output"
                )
            log.info(
                "gemini_clip_pipeline_done",
                job_id=job_id,
                success=len(clip_metas),
                failed=failed_count,
            )

        record_phase(
            job_id,
            PHASE_ANALYZE_CLIPS,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_MATCH_CLIPS,
        )
        _phase_t0 = time.monotonic()

        # [5] Template match.
        # Pin clip 0 to slot 1 when the template's first slot is "hook"-typed —
        # this keeps user-uploaded face/intro clips locked to position 1
        # (where the recipe expects a closeup) regardless of how Gemini scores
        # the other clips' opening moments.
        log.info("template_match_start", job_id=job_id)
        pinned_assignments: dict[int, str] = {}
        slot_1 = next((s for s in recipe.slots if s.get("position") == 1), None)
        # Resolve clip 0's clip_id from the ORDERED list — index 0 is always
        # local_clip_paths[0]'s meta (or None if its analysis failed).
        # `file_refs[0]` is None when clip 0 came from a cache hit, so we
        # can't use it; pinning works off the ClipMeta.clip_id either way.
        # If the only `mixed_media` branch above didn't run, clip_metas_ordered
        # is defined here.
        first_clip_meta = (
            clip_metas_ordered[0]
            if clip_metas_ordered and clip_metas_ordered[0] is not None
            else None
        )
        # Locked slots replay the original template source — no user clip goes
        # there, so pinning is impossible (matcher would raise
        # TEMPLATE_PIN_INVALID_SLOT since it only resolves pins against
        # unlocked slots).
        if (
            slot_1
            and slot_1.get("slot_type") in ("hook", "face")
            and not slot_1.get("locked", False)
            and first_clip_meta is not None
        ):
            pinned_assignments[1] = first_clip_meta.clip_id
            log.info("matcher_pin", slot=1, clip_id=first_clip_meta.clip_id)
        elif slot_1 and slot_1.get("slot_type") in ("hook", "face") and slot_1.get("locked", False):
            log.info("matcher_pin_skipped_locked_slot", slot=1)
        elif slot_1 and slot_1.get("slot_type") in ("hook", "face"):
            log.warning("matcher_pin_skipped_no_first_clip", slot=1)
        try:
            if mixed_media:
                # Positional binding skips consolidate_slots + greedy matcher.
                assembly_plan = _match_positional(recipe, clip_metas)
            elif is_agentic:
                # Agentic match: clip_router picks slot→clip, then we pick the
                # top-scoring moment from each assigned clip. Falls back to the
                # greedy matcher on agent failure so a flaky LLM call doesn't
                # take the whole job down.
                recipe = consolidate_slots(recipe, clip_metas)
                assembly_plan = _agentic_match_or_fallback(
                    recipe,
                    clip_metas,
                    pinned_assignments=pinned_assignments,
                    filter_hint=getattr(recipe, "clip_filter_hint", "") or "",
                    job_id=job_id,
                )
            else:
                recipe = consolidate_slots(recipe, clip_metas)
                assembly_plan = match(
                    recipe,
                    clip_metas,
                    pinned_assignments=pinned_assignments,
                    filter_hint=getattr(recipe, "clip_filter_hint", "") or "",
                )
        except TemplateMismatchError as exc:
            raise _StageError(
                FAILURE_REASON_USER_CLIP_UNUSABLE,
                f"{exc.code}: {exc.message}",
            ) from exc

        # Build mapping: clip_id → GCS path for re-render.
        # On the non-mixed-media path we used to zip `file_refs` with the
        # GCS list and read `ref.name`, but PR #87 (8e4cc52) added a Redis
        # cache that skips the Gemini upload on cache hits — leaving
        # `file_refs[i]` as None. `clip_metas_ordered` is the safe source
        # of truth: `meta.clip_id` equals the original `ref.name` for cache
        # misses and stays valid for hits. None entries are clips whose
        # analysis failed; they're excluded from the assembly plan, so
        # dropping them here is correct.
        if mixed_media:
            clip_id_to_gcs = {f"clip_{i}": gcs for i, gcs in enumerate(clip_paths_gcs)}
        else:
            clip_id_to_gcs = _clip_id_to_path_map(clip_metas_ordered, clip_paths_gcs)

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

        record_phase(
            job_id,
            PHASE_MATCH_CLIPS,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_ASSEMBLE,
        )
        _phase_t0 = time.monotonic()

        # [6] FFmpeg assemble.
        effective_single_pass = _resolve_effective_single_pass(
            force_single_pass,
            template_single_pass,
        )
        _render_path = _resolve_render_path(effective_single_pass)
        log.info(
            "ffmpeg_assemble_start",
            job_id=job_id,
            render_path=_render_path,
            force_single_pass=force_single_pass,
            env_single_pass_enabled=settings.single_pass_encode_enabled,
            template_single_pass=template_single_pass,
            effective_single_pass=effective_single_pass,
        )
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        base_path = os.path.join(tmpdir, "base_no_overlays.mp4")
        if mixed_media:
            clip_id_to_local = {f"clip_{i}": path for i, path in enumerate(local_clip_paths)}
        else:
            # Same cache-hit guard as clip_id_to_gcs above.
            clip_id_to_local = _clip_id_to_path_map(clip_metas_ordered, local_clip_paths)
        _add_locked_template_source(
            recipe_data,
            template_gcs_path,
            tmpdir,
            clip_id_to_local,
            probe_map,
        )
        # Drop curtain-close interstitials under preview_mode. They survive
        # the slot transition (a fade-to-black hold still plays) but skip the
        # PNG-overlay re-encode on the slot tail, which is one of the longest
        # serial steps inside assemble on a cold worker.
        assemble_interstitials = recipe.interstitials
        if preview_mode and assemble_interstitials:
            filtered = [
                i for i in assemble_interstitials if (i or {}).get("type") != "curtain-close"
            ]
            if len(filtered) != len(assemble_interstitials):
                log.info(
                    "interstitials_curtain_close_skipped_preview",
                    job_id=job_id,
                    dropped=len(assemble_interstitials) - len(filtered),
                )
                assemble_interstitials = filtered

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
            user_subject=user_subject,
            interstitials=assemble_interstitials,
            base_output_path=base_path,
            output_fit=getattr(recipe, "output_fit", None) or "crop",
            force_single_pass=effective_single_pass,
            is_agentic=is_agentic,
        )

        record_phase(
            job_id,
            PHASE_ASSEMBLE,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_MIX_AUDIO,
        )
        _phase_t0 = time.monotonic()

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

        record_phase(
            job_id,
            PHASE_MIX_AUDIO,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_GENERATE_COPY,
        )
        _phase_t0 = time.monotonic()

        # [7] Generate copy (with template tone) — skipped under preview_mode
        # since the admin test tab doesn't render copy and the call adds 5-15s.
        if preview_mode:
            from app.pipeline.agents.copy_writer import (  # noqa: PLC0415
                InstagramCopy,
                PlatformCopy,
                TikTokCopy,
                YouTubeCopy,
            )

            platform_copy = PlatformCopy(
                tiktok=TikTokCopy(hook="", caption="", hashtags=[]),
                instagram=InstagramCopy(hook="", caption="", hashtags=[]),
                youtube=YouTubeCopy(title="", description="", tags=[]),
            )
            copy_status = "skipped_preview_mode"
            log.info("copy_generation_skipped_preview", job_id=job_id)
        else:
            hook_text = _extract_hook_text(clip_metas, assembly_plan.steps)
            transcript_excerpt = _extract_transcript(clip_metas, assembly_plan.steps)
            from app.pipeline.agents.copy_writer import generate_copy  # noqa: PLC0415

            platform_copy, copy_status = generate_copy(
                hook_text=hook_text,
                transcript_excerpt=transcript_excerpt,
                platforms=selected_platforms,
                has_transcript=bool(transcript_excerpt),
                template_tone=recipe.copy_tone,
                job_id=job_id,
            )

        record_phase(
            job_id,
            PHASE_GENERATE_COPY,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_UPLOAD,
        )
        _phase_t0 = time.monotonic()

        # [8] Upload assembled video + base video to GCS
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        video_url = upload_public_read(output_path, gcs_output_path)
        gcs_base_path = f"jobs/{job_id}/template_base.mp4"
        base_video_url = upload_public_read(base_path, gcs_base_path)

        record_phase(
            job_id,
            PHASE_UPLOAD,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_FINALIZE,
        )
        _phase_t0 = time.monotonic()

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

        record_phase(
            job_id,
            PHASE_FINALIZE,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
        )
        mark_finished(job_id)

        log.info("template_job_done", job_id=job_id, output_url=video_url)


# ── Locked re-render fast path ─────────────────────────────────────────────────


def _run_rerender(job_id: str, job: Job, force_single_pass: bool = False) -> None:
    """Re-render with locked clip-to-slot assignments. Skips Gemini entirely.

    Reads the locked assembly plan, downloads only the used clips,
    and runs FFmpeg render + audio mix + copy generation + upload.
    """
    from app.pipeline.agents.gemini_analyzer import AssemblyStep  # noqa: PLC0415

    plan = job.assembly_plan
    steps_data = plan.get("steps", [])
    template_id = job.template_id
    selected_platforms = job.selected_platforms or ["tiktok", "instagram", "youtube"]
    user_subject = (((job.all_candidates or {}).get("inputs") or {}).get("location") or "").strip()

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
        # Same allow-list semantics as _run_template_job — the per-template
        # flag AND the env flag must both be true for a rerender to take
        # the single-pass path, unless force_single_pass overrides.
        template_single_pass = bool(template.single_pass_enabled)

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
        gcs_paths_unique = list(dict.fromkeys(step["clip_gcs_path"] for step in steps_data))

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

            assembly_steps.append(
                AssemblyStep(
                    slot=slot,
                    clip_id=clip_id,
                    moment=step_data["moment"],
                )
            )
            clip_id_to_local[clip_id] = local_path

        _add_locked_template_source(
            recipe_data,
            template_gcs_path,
            tmpdir,
            clip_id_to_local,
            probe_map,
        )

        # FFmpeg assemble (clip_metas=None is safe — text comes from recipe)
        effective_single_pass = _resolve_effective_single_pass(
            force_single_pass,
            template_single_pass,
        )
        _render_path = _resolve_render_path(effective_single_pass)
        log.info(
            "rerender_assemble_start",
            job_id=job_id,
            steps=len(assembly_steps),
            render_path=_render_path,
            force_single_pass=force_single_pass,
            env_single_pass_enabled=settings.single_pass_encode_enabled,
            template_single_pass=template_single_pass,
            effective_single_pass=effective_single_pass,
        )
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        base_path = os.path.join(tmpdir, "base_no_overlays.mp4")
        _assemble_clips(
            assembly_steps,
            clip_id_to_local,
            probe_map,
            assembled_path,
            tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=None,
            global_color_grade=recipe.color_grade,
            job_id=job_id,
            user_subject=user_subject,
            interstitials=recipe.interstitials,
            base_output_path=base_path,
            output_fit=getattr(recipe, "output_fit", None) or "crop",
            force_single_pass=effective_single_pass,
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
            job_id=job_id,
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


# ── Mixed-media (video + photo) helpers ──────────────────────────────────────


def _is_mixed_media(slots: list[dict]) -> bool:
    """True when any slot expects a still photo (forces positional binding)."""
    return any(str(s.get("media_type", "video")) == "photo" for s in slots)


def _slots_in_position_order(slots: list[dict]) -> list[dict]:
    return sorted(slots, key=lambda s: int(s.get("position", 0)))


def _build_positional_clip_metas(
    local_paths: list[str],
    slots_in_order: list[dict],
    probe_map: dict,
    *,
    job_id: str | None = None,
) -> list[ClipMeta]:
    """Build one ClipMeta per slot with positional binding (clip[i] → slot[i]).

    Photo slots get a synthetic ClipMeta covering the full clip duration
    (the upload pipeline already produced a 1080x1920 mp4 at slot.target_duration_s).
    Video slots run normal Gemini upload+analyze in parallel.

    clip_id is `clip_{i}` so downstream `clip_id_to_local` can be built positionally.
    """
    if len(local_paths) != len(slots_in_order):
        raise ValueError(
            f"Positional binding needs {len(slots_in_order)} clips, got {len(local_paths)}"
        )

    video_indices = [
        i for i, s in enumerate(slots_in_order) if str(s.get("media_type", "video")) != "photo"
    ]

    video_metas: dict[int, ClipMeta] = {}
    if video_indices:

        def _process(idx: int) -> tuple[int, ClipMeta]:
            path = local_paths[idx]
            slot = slots_in_order[idx]
            try:
                ref = gemini_upload_and_wait(path)
                meta = analyze_clip(ref, job_id=job_id)
            except (GeminiRefusalError, GeminiAnalysisError) as exc:
                # Positional binding pins clip[i] → slot[i] before assembly,
                # so the matcher doesn't need hook_text / best_moments to
                # *route* this clip — they're only consumed later for text
                # placement and beat scoring. A refusal (e.g. Gemini returned
                # empty hook_text for a quiet establishing shot) used to kill
                # the entire mixed-media job. Fall back to a synthetic
                # ClipMeta so the user's job still produces output. Mirrors
                # the non-positional fallback in `_analyze_one`, minus the
                # Whisper transcript (positional binding doesn't need it).
                probe = probe_map.get(path)
                dur = probe.duration_s if probe else float(slot.get("target_duration_s", 5.0))
                log.warning(
                    "positional_video_analysis_fallback",
                    idx=idx,
                    slot_position=slot.get("position"),
                    error=str(exc),
                )
                meta = ClipMeta(
                    clip_id=f"clip_{idx}",
                    transcript="",
                    hook_text="",
                    hook_score=5.0,
                    best_moments=[
                        {
                            "start_s": 0.0,
                            "end_s": float(dur),
                            "energy": 5.0,
                            "description": "fallback (gemini refused)",
                        }
                    ],
                    analysis_degraded=True,
                    clip_path=path,
                )
                return idx, meta
            meta.clip_id = f"clip_{idx}"
            meta.clip_path = path
            return idx, meta

        with ThreadPoolExecutor(
            max_workers=min(len(video_indices), _GEMINI_PARALLEL_CAP),
        ) as pool:
            futures = [pool.submit(_process, i) for i in video_indices]
            for future in as_completed(futures):
                idx, meta = future.result()
                video_metas[idx] = meta

    metas: list[ClipMeta] = []
    for i, slot in enumerate(slots_in_order):
        path = local_paths[i]
        if str(slot.get("media_type", "video")) == "photo":
            probe = probe_map.get(path)
            dur = probe.duration_s if probe else float(slot.get("target_duration_s", 5.0))
            metas.append(
                ClipMeta(
                    clip_id=f"clip_{i}",
                    transcript="",
                    hook_text="",
                    hook_score=5.0,
                    best_moments=[
                        {
                            "start_s": 0.0,
                            "end_s": float(dur),
                            "energy": 5.0,
                            "description": "static photo",
                        }
                    ],
                    clip_path=path,
                )
            )
        else:
            meta = video_metas.get(i)
            if meta is None:
                raise GeminiAnalysisError(
                    f"Video slot {slot.get('position', i + 1)} analysis failed"
                )
            metas.append(meta)
    return metas


def _match_positional(recipe: TemplateRecipe, clip_metas: list[ClipMeta]) -> AssemblyPlan:
    """Direct slot[i] ← clip_metas[i] binding for mixed-media templates.

    Skips the greedy/coverage matcher entirely; ordering is the contract.
    Picks the clip's best_moment closest to slot.target_duration_s.
    """
    sorted_slots = _slots_in_position_order(recipe.slots)
    if len(clip_metas) != len(sorted_slots):
        raise TemplateMismatchError(
            f"Positional match needs {len(sorted_slots)} clips, got {len(clip_metas)}",
            code="TEMPLATE_CLIP_DURATION_MISMATCH",
        )

    steps: list[AssemblyStep] = []
    for slot, meta in zip(sorted_slots, clip_metas):
        target_dur = float(slot.get("target_duration_s", 5.0))
        moments = meta.best_moments or [
            {"start_s": 0.0, "end_s": target_dur, "energy": 5.0, "description": ""}
        ]
        moment = min(
            moments,
            key=lambda m: abs(
                float(m.get("end_s", 0.0)) - float(m.get("start_s", 0.0)) - target_dur
            ),
        )
        steps.append(AssemblyStep(slot=slot, clip_id=meta.clip_id, moment=moment))
    return AssemblyPlan(steps=steps)


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

    has_locked = any(s.get("locked", False) for s in recipe_data.get("slots", []))
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
            duration_s=60.0,
            fps=30.0,
            width=1920,
            height=1080,
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
    """Return {local_path: VideoProbe} for each clip. Raises on probe failure.

    Replaces _get_clip_duration() — probe_video() returns both aspect_ratio
    and duration_s in a single FFprobe call. Probe failure is fatal because
    downstream FFmpeg operations need accurate metadata (trim points, reframe
    geometry, fps); fabricated defaults silently produce wrong output.
    """
    from app.pipeline.probe import probe_video  # noqa: PLC0415

    result: dict = {}
    for path in local_paths:
        try:
            result[path] = probe_video(path)
        except Exception as exc:
            # Re-raise: silently fabricating 1920×1080/30fps/30s defaults makes
            # downstream FFmpeg operate on lies (wrong trim points, wrong
            # reframe geometry). ffprobe is mature; failure here means the file
            # is corrupted or missing — fail loud, don't ship wrong output.
            log.error("clip_probe_failed", path=path, error=str(exc))
            raise RuntimeError(
                f"probe_video failed for {path}; refusing to render with "
                f"fabricated probe defaults: {exc}"
            ) from exc
    return result


def _probe_and_upload_concurrent(
    local_paths: list[str],
) -> tuple[dict, list]:
    """Run ffprobe + Gemini upload concurrently. Returns (probe_map, file_refs).

    The two operations are independent of each other (probe needs only the
    local file; Gemini upload needs only the local file). Running them in
    parallel cuts the slowest of the two out of the critical path —
    typically ffprobe (5-15s) finishes well before Gemini upload (60-180s),
    so this saves the probe time outright.

    Errors from either side propagate via `.result()` re-raise, preserving
    the orchestrator's existing fail-fast contract.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        probe_future = pool.submit(_probe_clips, local_paths)
        upload_future = pool.submit(_upload_clips_parallel, local_paths)
        probe_map = probe_future.result()
        file_refs = upload_future.result()
    return probe_map, file_refs


def _upload_clips_parallel(
    local_paths: list[str],
    *,
    job_id: str | None = None,
    record_sub_phases: bool = False,
) -> list:
    """Upload all clips to Gemini File API in parallel. Returns file refs in order.

    When record_sub_phases is true, each clip's upload+ACTIVE-poll wall time is
    appended to the job's phase_log as a sub_phase under analyze_clips so the
    admin test-tab UI can surface "stuck on clip N" while the job is still
    running. Off by default to keep prod jobs' phase_log untouched — the
    admin test endpoint sets it via the preview_mode flag.
    """
    results: dict[int, object] = {}

    def _upload_one(idx_path: tuple[int, str]) -> tuple[int, object]:
        idx, path = idx_path
        t0 = time.monotonic()
        ref = gemini_upload_and_wait(path)
        if record_sub_phases and job_id is not None:
            record_sub_phase(
                job_id,
                PHASE_ANALYZE_CLIPS,
                "gemini_upload",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                detail={
                    "clip_idx": idx,
                    "clip_path": os.path.basename(path),
                },
            )
        return idx, ref

    with ThreadPoolExecutor(
        max_workers=min(len(local_paths), _GEMINI_PARALLEL_CAP),
    ) as pool:
        futures = {pool.submit(_upload_one, (i, p)): i for i, p in enumerate(local_paths)}
        for future in as_completed(futures):
            idx, ref = future.result()
            results[idx] = ref

    return [results[i] for i in range(len(local_paths))]


def _analyze_clips_with_cache(
    local_paths: list[str],
    filter_hint: str = "",
    *,
    job_id: str | None = None,
    record_sub_phases: bool = False,
) -> tuple[list[ClipMeta], list[ClipMeta | None], list, dict, int]:
    """Hash → cache lookup → concurrent (probe + upload misses) → analyze.

    Returns
        (clip_metas_compact, clip_metas_ordered, file_refs_ordered,
         probe_map, failed_count)

    - `clip_metas_compact` is the failure-filtered list — what the matcher
      consumes. Order within the compact list mirrors successful entries from
      the ordered list.
    - `clip_metas_ordered` is parallel to `local_paths`: index i is the meta
      for local_paths[i], or None if that clip failed analysis. Use this for
      anything that needs a stable index→clip mapping (e.g. slot-1 pinning).
    - `file_refs_ordered` is also parallel to `local_paths`; entries for
      cache hits are None because we skipped the upload.
    - `probe_map` is keyed by local path; produced here because slot planning
      needs it whether or not the cache hit, and on partial cache it overlaps
      with the Gemini upload (per PR #86).

    Why this layout: upload + analyze are the slowest stretch of a multi-clip
    job (~30-120s/clip). On warm cache hits (returning users iterating on the
    same source clips against tweaked recipes) the entire stretch collapses
    to a fingerprint + Redis GET per clip — under a second total. On cold
    runs probe and upload still run concurrently so probing is off the
    critical path.
    """
    from app.pipeline.clip_cache import (  # noqa: PLC0415
        get_cached_meta,
        hash_clips_parallel,
        set_cached_meta,
    )

    n = len(local_paths)
    clip_metas_ordered: list[ClipMeta | None] = [None] * n
    file_refs_ordered: list[object | None] = [None] * n

    # Hash + cache lookup happens before any Gemini I/O so a full hit short-
    # circuits the network entirely. A None hash means the file couldn't be
    # read (deleted between download and analyze, etc.) — treat as a miss and
    # let the upload step surface the real error.
    hashes = hash_clips_parallel(local_paths)
    miss_indices: list[int] = []
    cache_hits = 0
    for i, h in enumerate(hashes):
        cached = get_cached_meta(h, filter_hint) if h else None
        if cached is not None:
            cached.clip_path = local_paths[i]
            clip_metas_ordered[i] = cached
            cache_hits += 1
        else:
            miss_indices.append(i)

    log.info(
        "clip_cache_lookup_done",
        total=n,
        hits=cache_hits,
        misses=len(miss_indices),
    )

    if not miss_indices:
        # Full cache hit: still need probe data for slot planning.
        probe_map = _probe_clips(local_paths)
        compact = [m for m in clip_metas_ordered if m is not None]
        return compact, clip_metas_ordered, file_refs_ordered, probe_map, 0

    # Cache miss path: probe (all clips, needed downstream) and Gemini upload
    # (misses only) run concurrently — the two operations are independent and
    # upload is network-bound while probe is disk+CPU. This is PR #86's
    # optimization, applied after the cache layer instead of replacing it.
    miss_paths = [local_paths[i] for i in miss_indices]
    with ThreadPoolExecutor(max_workers=2) as pool:
        probe_future = pool.submit(_probe_clips, local_paths)
        upload_future = pool.submit(
            _upload_clips_parallel,
            miss_paths,
            job_id=job_id,
            record_sub_phases=record_sub_phases,
        )
        probe_map = probe_future.result()
        miss_refs = upload_future.result()
    for orig_idx, ref in zip(miss_indices, miss_refs):
        file_refs_ordered[orig_idx] = ref

    miss_metas, failed_count = _analyze_clips_parallel(
        miss_refs,
        miss_paths,
        probe_map,
        filter_hint,
        job_id=job_id,
        record_sub_phases=record_sub_phases,
    )

    # Re-align analyzed metas back to their original indices.
    # _analyze_clips_parallel returns metas in completion order with clip_path
    # set, so we use the path → original-index map to slot them back.
    path_to_orig_idx = {miss_paths[k]: orig_i for k, orig_i in enumerate(miss_indices)}
    path_to_hash = {miss_paths[k]: hashes[orig_i] for k, orig_i in enumerate(miss_indices)}
    for meta in miss_metas:
        orig_i = path_to_orig_idx.get(meta.clip_path)
        if orig_i is None:
            log.warning("analyzed_meta_path_not_in_misses", clip_path=meta.clip_path)
            continue
        clip_metas_ordered[orig_i] = meta
        # Persist the fresh analysis to the cache when we have a stable
        # content hash. Degraded entries are filtered inside set_cached_meta.
        h = path_to_hash.get(meta.clip_path)
        if h:
            set_cached_meta(h, filter_hint, meta)

    compact = [m for m in clip_metas_ordered if m is not None]
    return compact, clip_metas_ordered, file_refs_ordered, probe_map, failed_count


def _analyze_clips_parallel(
    file_refs: list,
    local_paths: list[str],
    probe_map: dict | None = None,
    filter_hint: str = "",
    *,
    job_id: str | None = None,
    record_sub_phases: bool = False,
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
        t0 = time.monotonic()
        try:
            meta = analyze_clip(ref, filter_hint=filter_hint, job_id=job_id)
            if record_sub_phases and job_id is not None:
                record_sub_phase(
                    job_id,
                    PHASE_ANALYZE_CLIPS,
                    "gemini_analyze",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    detail={
                        "clip_idx": idx,
                        "clip_path": os.path.basename(path),
                    },
                )
            # Defense-in-depth: empty `best_moments` from a "successful" analysis
            # is unusable downstream (matcher has nothing to match). Treat it the
            # same as an analysis failure so the Whisper fallback engages and the
            # job still produces output.
            if not meta.best_moments:
                log.warning(
                    "clip_analysis_empty_moments",
                    clip_idx=idx,
                    clip_path=path,
                )
                raise GeminiAnalysisError("analyze_clip succeeded but returned 0 best_moments")
            meta.clip_path = path
            return meta, None
        except (GeminiRefusalError, GeminiAnalysisError, Exception) as exc:
            log.warning("clip_analysis_failed", clip_idx=idx, error=str(exc))
            # Whisper heuristic fallback for failed clips
            from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415

            try:
                transcript = transcribe_whisper(path)
                clip_dur = probe_map[path].duration_s if probe_map and path in probe_map else 30.0
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

    with ThreadPoolExecutor(
        max_workers=min(len(file_refs), _GEMINI_PARALLEL_CAP),
    ) as pool:
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

_PARALLEL_RENDER_WORKERS = 2  # Match worker vCPU count (fly.toml: cpus=2).
# Each libx264 ultrafast process saturates one core; running more than the
# vCPU count adds context-switch overhead with no throughput gain. With
# Celery --concurrency=2 the cluster already runs up to 4 ffmpegs at once,
# which is past the sweet spot for shared-CPU machines. If fly.toml cpus
# changes, update this to match.

# Concurrency cap for Gemini-bound work (upload + analyze + mixed-media
# per-clip analysis). Gemini's documented per-project request concurrency
# is well above this, so the bottleneck is request RTT, not quota. Raised
# from 8 → 16 in PR (auto-mode) to halve the wall time of the analyze
# phase on 10+ clip templates without changing failure shape. If we ever
# see 429s, drop this back to 8 and add an adaptive backoff in
# agents/gemini_analyzer.py (deferred to a follow-up PR).
_GEMINI_PARALLEL_CAP = 16


def _phase_done(name: str, t0: float, *, job_id: str | None, **extra: object) -> None:
    """Emit a structured `assemble_phase_done` log for an `_assemble_clips` sub-phase.

    Pure observability — paired manually with `time.monotonic()` at phase
    start instead of a `with` context manager so wrapping the 80-line
    interstitials loop doesn't require re-indenting everything inside it.
    """
    log.info(
        "assemble_phase_done",
        phase=name,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        job_id=job_id,
        **extra,
    )


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
    color_transfer: str = ""  # from probe; drives HLG/PQ tonemap decision in reframe
    color_trc: str = "bt709"  # cleaned probe value (with bt2020+bt709 → HLG fixup)
    # from probe; drives silent-AAC injection in reframe. Safe default is
    # False — if we can't probe, assume no audio so the silent track gets
    # injected, keeping concat-copy safe.
    has_audio: bool = False
    output_fit: str = "crop"  # "crop" (default) | "letterbox" (preserve full frame + blurred bg)
    # Rule-of-thirds grid overlay (sourced from the recipe slot dict via _extract_grid_params).
    has_grid: bool = False
    grid_color: str = "#FFFFFF"
    grid_opacity: float = 0.6
    grid_thickness: int = 3
    grid_highlight_intersection: str | None = None
    grid_highlight_color: str = "#E63946"
    grid_highlight_windows: list[tuple[float, float]] | None = None


_GRID_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_GRID_INTERSECTIONS = {"top-left", "top-right", "bottom-left", "bottom-right"}


def _extract_grid_params(slot: dict) -> dict:
    """Re-validate grid params from a recipe slot dict at the FFmpeg boundary.

    RecipeSlotSchema (admin.py) validates writes via the API, but recipes are
    stored as JSONB and bypass Pydantic at render time. A hand-edited recipe
    with a bad hex value would otherwise reach ffmpeg as a broken filter graph
    (silent failure or cryptic error). Fail fast here with a clear message.
    """
    if not bool(slot.get("has_grid", False)):
        return {"has_grid": False}

    pos = slot.get("position")
    color = slot.get("grid_color", "#FFFFFF")
    if not _GRID_COLOR_RE.fullmatch(color):
        raise ValueError(f"slot {pos}: grid_color {color!r} must match ^#[0-9A-Fa-f]{{6}}$")
    highlight_color = slot.get("grid_highlight_color", "#E63946")
    if not _GRID_COLOR_RE.fullmatch(highlight_color):
        raise ValueError(
            f"slot {pos}: grid_highlight_color {highlight_color!r} must match ^#[0-9A-Fa-f]{{6}}$",
        )

    opacity = max(0.0, min(1.0, float(slot.get("grid_opacity", 0.6))))
    thickness = max(1, min(20, int(slot.get("grid_thickness", 3))))

    intersection = slot.get("grid_highlight_intersection")
    if intersection is not None and intersection not in _GRID_INTERSECTIONS:
        raise ValueError(
            f"slot {pos}: grid_highlight_intersection {intersection!r} must be one of "
            f"{sorted(_GRID_INTERSECTIONS)} or null",
        )

    raw_windows = slot.get("grid_highlight_windows")
    windows: list[tuple[float, float]] | None = None
    if raw_windows is not None:
        windows = []
        for w in raw_windows:
            start, end = float(w[0]), float(w[1])
            if start < 0 or end <= start:
                raise ValueError(
                    f"slot {pos}: grid_highlight_windows entry ({start}, {end}) "
                    "must satisfy 0 <= start < end",
                )
            windows.append((start, end))

    return {
        "has_grid": True,
        "grid_color": color,
        "grid_opacity": opacity,
        "grid_thickness": thickness,
        "grid_highlight_intersection": intersection,
        "grid_highlight_color": highlight_color,
        "grid_highlight_windows": windows,
    }


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
        color_transfer = probe.color_transfer if probe else ""
        color_trc = probe.color_trc if probe else "bt709"
        # Default False on probe failure — pairs with reframe.py's same
        # default. If we can't probe, we can't trust the source has audio,
        # so injecting a silent AAC track makes the slot concat-copy safe.
        # The opposite default (True) would emit `-map 0:a?` against a
        # possibly audioless input → reframed slot has no audio track →
        # downstream concat-copy truncates at that slot.
        has_audio = probe.has_audio if probe else False
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

        grid_params = _extract_grid_params(step.slot)

        plans.append(
            SlotPlan(
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
                color_transfer=color_transfer,
                color_trc=color_trc,
                has_audio=has_audio,
                output_fit=output_fit,
                **grid_params,
            )
        )

    return plans, clip_cursors, cumulative_s


def _render_planned_slot(plan: SlotPlan) -> str:
    """Phase 2: render a single planned slot via FFmpeg. Thread-safe."""
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

    t0 = time.monotonic()
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
        has_grid=plan.has_grid,
        grid_color=plan.grid_color,
        grid_opacity=plan.grid_opacity,
        grid_thickness=plan.grid_thickness,
        grid_highlight_intersection=plan.grid_highlight_intersection,
        grid_highlight_color=plan.grid_highlight_color,
        grid_highlight_windows=plan.grid_highlight_windows,
        color_trc=plan.color_trc,
        has_audio=plan.has_audio,
    )
    log.info(
        "slot_render_done",
        slot_idx=plan.slot_idx,
        target_duration_s=round(plan.slot_target_dur, 3),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    return plan.reframed_path


def _resolve_render_path(force_single_pass: bool) -> str:
    """Return 'single' or 'multi' based on the resolved force flag.

    Callers (_run_template_job, _run_rerender) compute the effective
    single-pass decision via :func:`_resolve_effective_single_pass`. The
    function name is preserved for log-grep compatibility but the
    semantics are now purely "did the caller decide single-pass" —
    not "should we use single-pass."
    """
    return "single" if force_single_pass else "multi"


def _resolve_effective_single_pass(
    force_single_pass: bool,
    template_single_pass: bool,
) -> bool:
    """Compute the effective single-pass decision from three inputs.

    Truth table:
      force=True, anything else                                  → True
      force=False, env=True, template=True                       → True
      any other combination                                      → False

    The AND-gate over (env flag, per-template flag) is the safe rollout
    pattern — flipping either alone has zero render impact until both
    are set. ``force_single_pass=True`` from a job kwarg overrides both
    flags; that path is for engineers debugging an individual job via
    apply_async kwargs.

    Reads :data:`settings.single_pass_encode_enabled` at call time, so a
    runtime env flip takes effect on the next job without a reload.
    """
    return force_single_pass or (settings.single_pass_encode_enabled and template_single_pass)


# Sentinel values passed to generate_text_overlay_png /
# generate_animated_overlay_ass to mark the post-join absolute-overlay pass
# (vs the per-slot pre-burn pass). The generators key several internal
# heuristics off `cumulative_s` and `slot_index`; 999.0 + 99 are far enough
# from any real per-slot value that they don't collide.
# Used by:
#   _burn_text_overlays (multi-pass post-join overlay burn)
#   _generate_single_pass_overlays (single-pass M6 overlay collection)
# Both call sites MUST stay in sync or the parity gate diverges.
ABS_PASS_TIME_S = 999.0
ABS_PASS_SLOT_INDEX = 99


def _generate_single_pass_overlays(
    *,
    steps: list,
    plans: list[SlotPlan],
    interstitial_map: dict[int, dict],
    clip_metas: list | None,
    subject: str,
    tmpdir: str,
    is_agentic: bool = False,
) -> tuple[list[dict], list[str], str]:
    """Build absolute-overlay PNG configs + ASS paths for single-pass M6.

    Wraps the same _collect_absolute_overlays + generate_text_overlay_png +
    generate_animated_overlay_ass calls that multi-pass _burn_text_overlays
    makes, but produces the bare file paths instead of running ffmpeg.

    Returns ``(abs_pngs, abs_ass_paths, fonts_dir)``. ``abs_pngs`` is the
    list of ``{"png_path", "start_s", "end_s"}`` dicts the filter graph
    consumes. ``abs_ass_paths`` is the parallel list of generated .ass
    files for libass-rendered animations. ``fonts_dir`` is the
    ``fontsdir=`` argument the subtitles filter expects.

    Lives in the orchestrator (not single_pass.py) so the spec stays
    filesystem-free for unit tests — mocking PIL / libass at the spec
    boundary is much more expensive than letting the unit tests pass
    pre-built overlay dicts directly.
    """
    from app.pipeline.text_overlay import (  # noqa: PLC0415
        ASS_ANIMATED_EFFECTS,
        FONTS_DIR,
        generate_animated_overlay_ass,
        generate_text_overlay_png,
    )

    slot_durations = [p.slot_target_dur for p in plans]
    overlays = _collect_absolute_overlays(
        steps,
        slot_durations,
        clip_metas,
        subject,
        interstitial_map=interstitial_map,
        is_agentic=is_agentic,
    )
    if not overlays:
        return [], [], ""

    static_overlays = [o for o in overlays if o.get("effect") not in ASS_ANIMATED_EFFECTS]
    animated_overlays = [o for o in overlays if o.get("effect") in ASS_ANIMATED_EFFECTS]

    # ABS_PASS_TIME_S + ABS_PASS_SLOT_INDEX are the post-join sentinels.
    # _burn_text_overlays calls the same generators with the same constants;
    # drift here would split the parity contract silently.
    abs_pngs = (
        generate_text_overlay_png(
            static_overlays,
            ABS_PASS_TIME_S,
            tmpdir,
            slot_index=ABS_PASS_SLOT_INDEX,
        )
        if static_overlays
        else []
    ) or []
    abs_ass_paths = (
        generate_animated_overlay_ass(
            animated_overlays,
            ABS_PASS_TIME_S,
            tmpdir,
            slot_index=ABS_PASS_SLOT_INDEX,
        )
        if animated_overlays
        else []
    ) or []
    return abs_pngs, abs_ass_paths, FONTS_DIR


def _build_single_pass_spec(
    plans: list[SlotPlan],
    interstitial_map: dict[int, dict],
    steps: list,
    *,
    abs_pngs: list[dict] | None = None,
    abs_ass_paths: list[str] | None = None,
    fonts_dir: str = "",
) -> SinglePassSpec:
    """Convert orchestrator plans + interstitials → SinglePassSpec.

    Milestones 2-3 scope — supports:
      - clip slots (with all SlotPlan features: speed, color, aspect, grid)
      - non-curtain color-hold interstitials between slots
      - hard-cut transitions (M2)
      - xfade transitions: crossfade, fade_black, wipe_left, wipe_right (M3)

    Raises SinglePassUnsupportedError if the template needs:
      - curtain-close interstitials (M4 — PNG-bar overlay on slot tail)
      - barn-door-open interstitials (M4 — PNG-bar overlay on slot head)
      - absolute-timestamp text overlays (M6 — post-concat layer)
      - pre-burn PNGs (M5 — slot-relative overlay before bars)

    The caller catches SinglePassUnsupportedError (a NotImplementedError
    subclass) and falls through to multi-pass with a structured warning.

    Transition translation: ``step.slot.transition_in`` is the raw Gemini
    vocabulary (whip-pan, zoom-in, dissolve, hard-cut, etc.). The
    :func:`translate_transition` map collapses it to the internal xfade
    family ({"none", "crossfade", "fade_black", "wipe_left", "wipe_right"}).
    Unknown vocabulary falls back to "none" — silent degrade to hard-cut
    is the right failure mode here (matches multi-pass behavior).
    """
    from app.pipeline.transitions import translate_transition  # noqa: PLC0415

    inputs: list[SinglePassInput] = []
    # ``transitions`` is appended alongside ``inputs`` so the invariant
    # ``len(transitions) == len(inputs) - 1`` holds without a post-pass.
    # Two gap shapes contribute "none":
    #   - clip → color_hold (hard cut into the hold animation)
    #   - color_hold → next clip (hard cut out; matches multi-pass
    #     behavior at template_orchestrate.py:2204-2209 where
    #     prev_had_interstitial forces transition_types[i] = "none")
    transitions: list[str] = []

    for plan, step in zip(plans, steps):
        # Decide the transition INTO this clip. For slot 0 there's nothing
        # before it. If the previous input was a color hold (an
        # interstitial was appended at the end of the prior iteration), the
        # hold IS the transition effect — hard-cut out. Otherwise apply
        # the recipe's transition_in for this slot.
        if inputs:
            if inputs[-1].kind == "color_hold":
                transitions.append("none")
            else:
                raw_transition = str(step.slot.get("transition_in", "none"))
                transitions.append(translate_transition(raw_transition))

        # Append the clip itself.
        inputs.append(
            SinglePassInput(
                kind="clip",
                clip_path=plan.clip_path,
                start_s=plan.start_s,
                end_s=plan.end_s,
                speed_factor=plan.speed_factor,
                aspect_ratio=plan.aspect_ratio,
                output_fit=plan.output_fit,
                color_trc=plan.color_trc,
                color_hint=plan.color_hint,
                has_audio=plan.has_audio,
                has_grid=plan.has_grid,
                grid_params={
                    "grid_color": plan.grid_color,
                    "grid_opacity": plan.grid_opacity,
                    "grid_thickness": plan.grid_thickness,
                    "grid_highlight_intersection": plan.grid_highlight_intersection,
                    "grid_highlight_color": plan.grid_highlight_color,
                    "grid_highlight_windows": plan.grid_highlight_windows,
                }
                if plan.has_grid
                else {},
            )
        )

        # Append color-hold interstitial AFTER this slot, if any.
        slot_position = int(step.slot.get("position", plan.slot_idx + 1))
        inter = interstitial_map.get(slot_position)
        if inter is None:
            continue
        inter_type = inter.get("type", "")
        if inter_type == "curtain-close":
            raise SinglePassUnsupportedError(
                f"slot {plan.slot_idx} has curtain-close interstitial; "
                f"PNG-bar overlay lands in milestone 4"
            )
        if inter_type == "barn-door-open":
            # Multi-pass renders barn-door-open as a color hold AT the
            # interstitial position PLUS a PNG-bar opening animation on the
            # head of the NEXT slot (template_orchestrate.py:2121
            # `apply_barn_door_open_head`). M2 only models the color hold,
            # which would silently drop the slot-head animation and ship
            # output that's missing the hero reveal. No production template
            # uses barn-door-open today, but match-cut + barn-door + speed-
            # ramp shipped in PR #134 (v0.4.12.0) as first-class effects, so
            # this gate is the future-proof. Lift it together with the
            # curtain-close gate when M4 lands.
            raise SinglePassUnsupportedError(
                f"slot {plan.slot_idx} has barn-door-open interstitial; "
                f"slot-head PNG-bar overlay lands in milestone 4"
            )
        # Other types (fade-black-hold, flash-white) → color hold input.
        hold_color_hex = inter.get("hold_color", "#000000")
        ffmpeg_color = "white" if hold_color_hex == "#FFFFFF" else "black"
        # clip → hold transition is always hard cut; the hold animation
        # itself is the effect.
        transitions.append("none")
        inputs.append(
            SinglePassInput(
                kind="color_hold",
                hold_color=ffmpeg_color,
                hold_s=float(inter.get("hold_s", 1.0)),
            )
        )

    # Compute total output duration for parity script bridging. xfade
    # transitions shorten this by their overlap duration, but the M2
    # callsite only uses output_duration_s as a sanity check, not for
    # bridging math (the FFmpeg filter graph computes its own timeline).
    total_dur = 0.0
    for inp in inputs:
        if inp.kind == "clip":
            total_dur += (inp.end_s - inp.start_s) / inp.speed_factor
        else:
            total_dur += inp.hold_s

    return SinglePassSpec(
        inputs=inputs,
        transitions=transitions,
        abs_pngs=abs_pngs or [],
        abs_ass_paths=abs_ass_paths or [],
        fonts_dir=fonts_dir,
        output_duration_s=total_dur,
    )


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
    force_single_pass: bool = False,
    is_agentic: bool = False,
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

    # Subject comes ONLY from explicit user input — never from Gemini's
    # clip-level metadata. Letting `clip_meta.detected_subject` become the
    # substitution input via a majority-vote fallback caused job
    # a1091488-09f6-4ce0-b92e-b1cc52695c9c (Rule of Thirds, 2026-05-13) to
    # render "pilot in cockpit" in place of literal "The"/"Thirds". Templates
    # that need a user subject must surface it through `inputs.location`;
    # templates with literal text render literal.
    subject = user_subject
    if subject:
        log.info("subject_resolved", subject=subject, source="user")

    # Build lookup of interstitials by after_slot position
    interstitial_map: dict[int, dict] = {}
    for inter in interstitials or []:
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
    _phase_t0 = time.monotonic()
    plans, _clip_cursors, cumulative_s = _plan_slots(
        steps,
        clip_id_to_local,
        clip_probe_map,
        beats,
        clip_metas,
        global_color_grade,
        tmpdir,
        output_fit=output_fit,
    )
    _phase_done("plan", _phase_t0, job_id=job_id, slots=len(plans))

    # ── Gate: single-pass branch ──────────────────────────────────────────
    # ``force_single_pass`` is the ONLY gate here — callers
    # (_run_template_job, _run_rerender) have already resolved the env-level
    # ``settings.single_pass_encode_enabled`` AND per-template
    # ``template.single_pass_enabled`` into this kwarg. Unit tests that drive
    # _assemble_clips directly set force_single_pass=True to opt in.
    #
    # When the spec build raises SinglePassUnsupportedError (curtain-close,
    # barn-door-open, pre-burn PNGs), we catch and fall through to the
    # multi-pass loop with a structured warning so engineers opting in via
    # force_single_pass don't get cryptic failures on complex templates.
    if force_single_pass:
        try:
            _phase_t0_sp = time.monotonic()
            # M6: collect absolute-timestamp overlays and pre-generate the
            # PNGs / ASS files BEFORE building the spec, so the spec carries
            # ready-to-consume file paths and the filter graph can reference
            # them by absolute timestamps. This mirrors how multi-pass calls
            # _burn_text_overlays at the post-join stage; single-pass folds
            # the same overlay set into the one filter_complex invocation.
            #
            # The orchestrator-side generation keeps _build_single_pass_spec
            # filesystem-free, so unit tests don't need to mock PIL / libass.
            abs_pngs, abs_ass_paths, fonts_dir = _generate_single_pass_overlays(
                steps=steps,
                plans=plans,
                interstitial_map=interstitial_map,
                clip_metas=clip_metas,
                subject=subject,
                tmpdir=tmpdir,
                is_agentic=is_agentic,
            )
            spec = _build_single_pass_spec(
                plans,
                interstitial_map,
                steps,
                abs_pngs=abs_pngs,
                abs_ass_paths=abs_ass_paths,
                fonts_dir=fonts_dir,
            )
            # Single ffmpeg invocation — when overlays exist AND
            # base_output_path is set, run_single_pass emits a 2-output
            # command that shares the filter graph (one decode, two
            # encodes). When no overlays exist, [base] == [vout]
            # semantically; we copy the file after the render.
            run_single_pass(spec, output_path, base_output_path=base_output_path)
            if base_output_path and not (abs_pngs or abs_ass_paths):
                shutil.copy2(output_path, base_output_path)
            _phase_done(
                "single_pass",
                _phase_t0_sp,
                job_id=job_id,
                slots=len(plans),
                inputs=len(spec.inputs),
                abs_overlays=len(abs_pngs) + len(abs_ass_paths),
            )
            return
        except SinglePassUnsupportedError as exc:
            # Specific catch — only OUR explicit "feature not yet implemented"
            # signals trigger fallback. A real NotImplementedError from some
            # other library propagates up and fails the job loudly, instead
            # of being silently swallowed into the multi-pass branch.
            log.warning(
                "single_pass_unsupported_fallback_to_multi",
                reason=str(exc),
                job_id=job_id,
                slots=len(plans),
            )
            # Fall through to multi-pass.

    # ── Phase 2: render all slots in parallel via FFmpeg ──────────────────
    _phase_t0 = time.monotonic()
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
    _phase_done(
        "render_parallel",
        _phase_t0,
        job_id=job_id,
        slots=len(plans),
        workers=_PARALLEL_RENDER_WORKERS,
    )

    # ── Phase 3: post-render (curtain-close, interstitials, transitions) ──
    _phase_t0 = time.monotonic()
    reframed_paths: list[str] = []
    slot_durations: list[float] = []
    transition_types: list[str] = []
    # Original per-slot durations (excluding interstitials) for overlay timing.
    original_slot_durations: list[float] = []
    # When True, the previous slot had an interstitial after it, so the
    # transition INTO this slot must be hard-cut (the interstitial IS the
    # transition effect — adding an xfade from a solid color looks broken).
    prev_had_interstitial = False
    # When the previous slot had a barn-door-open interstitial after it, the
    # current slot's HEAD gets the opening animation. Tracked separately from
    # the generic `prev_had_interstitial` because the head animation is a
    # type-specific effect, not a shared interstitial concern.
    prev_inter_type: str | None = None
    prev_inter: dict | None = None

    for plan, step in zip(plans, steps):
        i = plan.slot_idx
        reframed = plan.reframed_path

        # Check if this slot has an interstitial AFTER it (curtain-close tail effect)
        slot_position = int(step.slot.get("position", i + 1))
        inter = interstitial_map.get(slot_position)

        # If the PREVIOUS slot's interstitial was barn-door-open, apply the
        # opening animation to the HEAD of this slot before it lands in the
        # assembly. The matching black hold has already been inserted by
        # `_insert_interstitial` in the prior loop iteration; this is the
        # reveal that follows. Same encoder budget as curtain-close — the
        # bars are a 4s PNG overlay on top of the slot's intro footage.
        if prev_inter_type == "barn-door-open":
            from app.pipeline.interstitials import (  # noqa: PLC0415
                MIN_BARN_DOOR_ANIMATE_S,
                apply_barn_door_open_head,
            )

            head_output = os.path.join(tmpdir, f"slot_{i}_barn_door.mp4")
            try:
                recipe_animate = (prev_inter or {}).get("animate_s")
                door_anim = (
                    recipe_animate if recipe_animate is not None else MIN_BARN_DOOR_ANIMATE_S
                )
                apply_barn_door_open_head(
                    reframed,
                    head_output,
                    animate_s=door_anim,
                )
                reframed = head_output
            except Exception as exc:
                # Same re-raise rationale as curtain-close: a missing hero
                # animation paired with the black hold reads as a hard cut
                # to black, which is worse than a hard failure.
                log.error("barn_door_open_head_failed", slot=i, error=str(exc))
                raise RuntimeError(
                    f"barn-door-open failed on slot {i}; refusing to ship a "
                    f"video with a missing hero animation: {exc}"
                ) from exc
        if inter and inter["type"] == "curtain-close":
            # ── PRE-BURN text onto slot BEFORE curtain ─────────────────
            # Tiki-style: text appears on video, curtain closes OVER both.
            # We burn text onto the reframed clip first, then apply curtain
            # on top, so the black bars cover the text as they sweep down.
            reframed = _pre_burn_curtain_slot_text(
                reframed,
                step,
                plan.slot_target_dur,
                clip_metas,
                subject,
                i,
                tmpdir,
                inter,
                is_agentic=is_agentic,
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
                    recipe_animate if recipe_animate is not None else MIN_CURTAIN_ANIMATE_S
                )
                apply_curtain_close_tail(
                    reframed,
                    tail_output,
                    animate_s=curtain_anim,
                )
                reframed = tail_output
            except Exception as exc:
                # Re-raise: a swallowed failure here leaves the slot in the
                # sequence without curtain bars while the post-slot color-hold
                # interstitial still gets inserted — looks like a hard cut to
                # black instead of the hero animation. Partial output is worse.
                log.error("curtain_close_tail_failed", slot=i, error=str(exc))
                raise RuntimeError(
                    f"curtain-close failed on slot {i}; refusing to ship a "
                    f"video with a missing hero animation: {exc}"
                ) from exc

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
        prev_inter_type = inter["type"] if inter else None
        prev_inter = inter
        if inter:
            inter_hold = float(inter.get("hold_s", 1.0))
            if inter_hold > 0:
                _insert_interstitial(
                    inter,
                    i,
                    reframed_paths,
                    slot_durations,
                    transition_types,
                    tmpdir,
                )
            else:
                log.info("interstitial_skip_zero_hold", type=inter["type"], slot=i)
            # Keep beat-snap clock in sync — interstitial hold time is real
            # video duration that must be accounted for when snapping later
            # slot boundaries to musical beats.
            cumulative_s += inter_hold

    if not reframed_paths:
        raise ValueError("No slots to assemble")

    _phase_done(
        "curtain_and_interstitials",
        _phase_t0,
        job_id=job_id,
        post_render_clip_count=len(reframed_paths),
        interstitial_count=len(interstitial_map),
    )

    # Join slots (transitions or concat)
    _phase_t0 = time.monotonic()
    joined_path = os.path.join(tmpdir, "joined.mp4")
    if len(reframed_paths) == 1:
        shutil.copy2(reframed_paths[0], joined_path)
    else:
        _join_or_concat(
            reframed_paths,
            transition_types,
            slot_durations,
            joined_path,
            tmpdir,
            has_interstitials=bool(interstitial_map),
        )
    _phase_done("join", _phase_t0, job_id=job_id, clips=len(reframed_paths))

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
    _phase_t0 = time.monotonic()
    abs_overlays = _collect_absolute_overlays(
        steps,
        original_slot_durations,
        clip_metas,
        subject,
        interstitial_map=interstitial_map,
        is_agentic=is_agentic,
    )
    if abs_overlays:
        _burn_text_overlays(joined_path, abs_overlays, output_path, tmpdir)
        _burned_dur = _probe_duration(output_path)
        log.info("debug_post_burn_duration", burned_dur=_burned_dur)
    else:
        shutil.copy2(joined_path, output_path)
    _phase_done(
        "text_overlay",
        _phase_t0,
        job_id=job_id,
        overlay_count=len(abs_overlays),
    )

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
        # barn-door-open and curtain-close both insert a black hold here.
        # The actual barn-door animation lives at the HEAD of slot N+1; the
        # hold is the pause between the closed-doors moment (end of slot N)
        # and the open-reveal (start of slot N+1). See the call site in
        # _post_render_phase for the head-animation dispatch.
        if inter_type in (
            "curtain-close",
            "barn-door-open",
            "fade-black-hold",
            "flash-white",
        ):
            render_color_hold(inter_path, hold_s=hold_s, color=ffmpeg_color)
        else:
            log.warning("unknown_interstitial_type", type=inter_type)
            return
    except InterstitialError as exc:
        # Re-raise: silent return drops the recipe-defined interstitial (curtain
        # hold, fade-to-black, flash) from the final video without surfacing
        # the failure. Same pattern as the slot-N curtain-close bug.
        log.error("interstitial_render_failed", type=inter_type, error=str(exc))
        raise RuntimeError(
            f"interstitial '{inter_type}' failed to render; refusing to ship a "
            f"video missing a recipe-defined transition: {exc}"
        ) from exc

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
    """Join slots, splitting hard-cut runs from real visual transitions.

    The 17-slot Dimples Passport recipe (15 hard-cut + 1 dissolve) hit a bug
    where chaining xfade=fade:duration=0.001 for hard cuts caused FFmpeg to
    drop frames and truncate output to ~3-4s. Fix: use the concat demuxer
    (re-encode) for runs of "none" transitions and only invoke xfade for the
    real visual boundaries. brazil.mp4 (21.4s, ffprobe-confirmed) is the
    expected output shape for that recipe.

    Pipeline:

        slots:        [s1, s2, s3, s4, s5, s6, ..., s17]
        transitions:        [n,  n,  n,  n,  X,  n,  ..., n]    (X = visual)
                            └──── group A ─────┘  └── group B ──┘
        ┌── concat A ──┐   ┌── concat B ──┐
        │   (5 slots)  │   │  (12 slots)  │
        └──────────────┘   └──────────────┘
                ╲              ╱
                 xfade(X, group_a, group_b)
                       ↓
                  output.mp4

    `has_interstitials` is preserved as a parameter for caller compatibility
    but no longer triggers a separate code path — every all-"none" sequence
    now lands in concat naturally because no group splits get created.
    """
    if not reframed_paths:
        raise ValueError("_join_or_concat called with empty reframed_paths")
    if len(transition_types) != len(reframed_paths) - 1:
        raise ValueError(
            f"transition_types length {len(transition_types)} != "
            f"len(reframed_paths)-1 {len(reframed_paths) - 1}"
        )
    if len(slot_durations) != len(reframed_paths):
        raise ValueError(
            f"slot_durations length {len(slot_durations)} != "
            f"len(reframed_paths) {len(reframed_paths)}"
        )

    if len(reframed_paths) == 1:
        shutil.copy2(reframed_paths[0], output_path)
        return

    # Group slots by visual-transition boundaries. Adjacent slots joined by
    # "none" land in the same group; visual transitions split groups.
    groups: list[tuple[list[str], list[float]]] = []
    boundary_transitions: list[str] = []

    cur_paths = [reframed_paths[0]]
    cur_durs = [slot_durations[0]]
    for i, t in enumerate(transition_types):
        if t == "none":
            cur_paths.append(reframed_paths[i + 1])
            cur_durs.append(slot_durations[i + 1])
        else:
            groups.append((cur_paths, cur_durs))
            boundary_transitions.append(t)
            cur_paths = [reframed_paths[i + 1]]
            cur_durs = [slot_durations[i + 1]]
    groups.append((cur_paths, cur_durs))

    # Concat each group into a single file (1-slot groups pass through).
    group_files: list[str] = []
    group_durs: list[float] = []
    for idx, (paths, durs) in enumerate(groups):
        if len(paths) == 1:
            group_files.append(paths[0])
        else:
            gf = os.path.join(tmpdir, f"group_{idx}.mp4")
            _concat_demuxer(paths, gf, tmpdir, expected_duration_s=sum(durs))
            group_files.append(gf)
        group_durs.append(sum(durs))

    log.info(
        "join_or_concat_groups",
        slots=len(reframed_paths),
        groups=len(group_files),
        visual_boundaries=len(boundary_transitions),
        has_interstitials=has_interstitials,
    )

    # No visual transitions → all slots concat'd into one group, done.
    if len(group_files) == 1:
        shutil.copy2(group_files[0], output_path)
        return

    # Stitch groups via xfade for the real visual transitions only.
    from app.pipeline.transitions import join_with_transitions  # noqa: PLC0415

    try:
        join_with_transitions(
            group_files,
            boundary_transitions,
            group_durs,
            output_path,
        )
    except Exception as exc:
        # Last-resort fallback: drop visual transitions, concat all original
        # slot files. Better to ship a hard-cut version than a 3s truncation.
        log.warning("transition_join_failed_falling_back", error=str(exc))
        _concat_demuxer(
            reframed_paths,
            output_path,
            tmpdir,
            expected_duration_s=sum(slot_durations),
        )


def _concat_demuxer(
    reframed_paths: list[str],
    output_path: str,
    tmpdir: str,
    expected_duration_s: float | None = None,
) -> None:
    """FFmpeg concat demuxer — stream-copy when slot layouts match, else re-encode.

    Stream copy (-c copy) is dramatically faster (mux only, no decode/encode)
    but silently truncates when slots have mismatched codec params or one slot
    lacks an audio track. As of 2026-05, every reframed slot is forced to an
    identical layout in `_encoding_args` (libx264 yuv420p 1080×1920 30fps +
    AAC stereo 44.1k) and `reframe_and_export` injects a silent AAC track
    when the source has no audio — so the truncation precondition no longer
    holds for slots produced by the multi-clip template pipeline.

    Verification strategy:
      - We write the stream-copy output to a temp path so a failed copy
        never overwrites a file the fallback re-encode then half-rewrites.
      - We probe ONLY the output (one ffprobe call), not every input. The
        caller knows slot durations from `SlotPlan` and passes them via
        `expected_duration_s`. If unset, we fall back to per-input probes.
      - Tolerance: 0.05s × N_slots, capped at 0.5s. Tighter than a flat 0.5s
        on small jobs; large jobs accumulate genuine container-rounding
        error per slot boundary.
    """
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in reframed_paths:
            f.write(f"file '{p}'\n")

    # Stream-copy attempt → temp path; promote to output_path on verify pass.
    # Atomicity matters: a partial copy must never linger as the "output"
    # while the re-encode fallback writes to it.
    copy_tmp = output_path + ".copy.mp4"
    copy_cmd = [
        "ffmpeg",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        copy_tmp,
    ]
    copy_result = subprocess.run(copy_cmd, capture_output=True, timeout=300, check=False)

    if copy_result.returncode == 0 and os.path.exists(copy_tmp) and os.path.getsize(copy_tmp) > 0:
        try:
            from app.pipeline.probe import probe_video  # noqa: PLC0415

            actual = probe_video(copy_tmp).duration_s
            if expected_duration_s is None:
                expected = sum(probe_video(p).duration_s for p in reframed_paths)
            else:
                expected = expected_duration_s
            tolerance = min(0.5, 0.05 * len(reframed_paths))
            if abs(expected - actual) <= tolerance:
                os.replace(copy_tmp, output_path)
                log.info(
                    "concat_stream_copy_ok",
                    slots=len(reframed_paths),
                    expected_s=round(expected, 2),
                    actual_s=round(actual, 2),
                    tolerance_s=round(tolerance, 3),
                )
                return
            log.warning(
                "concat_stream_copy_truncated_falling_back",
                expected_s=round(expected, 2),
                actual_s=round(actual, 2),
                tolerance_s=round(tolerance, 3),
                slots=len(reframed_paths),
            )
        except Exception as exc:
            log.warning("concat_stream_copy_verify_failed", error=str(exc))
    else:
        log.warning(
            "concat_stream_copy_failed_falling_back",
            rc=copy_result.returncode,
            stderr_tail=copy_result.stderr.decode("utf-8", errors="replace")[-500:],
        )

    # Drop the rejected copy attempt before the fallback writes its own output.
    if os.path.exists(copy_tmp):
        try:
            os.unlink(copy_tmp)
        except OSError:
            pass

    # Fallback: full re-encode (the historical path).
    encode_cmd = [
        "ffmpeg",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list,
        # preset=fast (not ultrafast): ultrafast disables mb-tree, psy-rd,
        # B-frames and trellis quant, which produces visible 16×16 macroblocking
        # on smooth gradients (the BRAZIL/blue-canopy banding bug). PR #102/#105
        # fixed this for the curtain-close encode (interstitials.py:59) but the
        # same flip was never propagated to this concat fallback or to the
        # downstream burn_text_overlays — so multi-encode banding still compounded
        # into the final output. PR #105's --concurrency=1 (fly.toml) and PNG-
        # overlay curtain (~10× faster) unlocked the CPU budget that previously
        # forced ultrafast here. fast keeps psy-rd + mb-tree on shared-CPU workers
        # without busting the 1200s timeout. Locked by tests/test_encoder_policy.py.
        # Old comment, kept for archaeology: "ultrafast: shared-CPU workers crawl
        # on preset=fast (24-slot Morocco was hitting ~9 min for transitions alone)."
        *_encoding_args(output_path, preset="fast"),
    ]
    result = subprocess.run(encode_cmd, capture_output=True, timeout=1200, check=False)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg concat failed: {result.stderr.decode()[:300]}")


def _collect_absolute_overlays(
    steps: list,
    slot_durations: list[float],
    clip_metas: list | None,
    subject: str,
    interstitial_map: dict[int, dict] | None = None,
    is_agentic: bool = False,
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
                "position_x_frac": ov.get("position_x_frac"),
                "position_y_frac": ov.get("position_y_frac"),
                "stroke_width": int(ov.get("stroke_width", 0)),
                "emoji_prefix": str(ov.get("emoji_prefix", "")),
            }
            # Pass through font_family from admin recipe (overrides font_style)
            if ov.get("font_family"):
                entry["font_family"] = ov["font_family"]
            # Pass through cycle_fonts so per-overlay curated font-cycle lists
            # (e.g. Waka Waka AFRICA's brush-only [Permanent Marker, Caveat
            # Brush]) reach _resolve_cycle_fonts. Without this, the renderer's
            # overlay.get("cycle_fonts") returns None and the cycle falls back
            # to the global cycle_role=contrast pool, producing a sans/serif/
            # script flicker the overlay author specifically opted out of.
            if ov.get("cycle_fonts"):
                entry["cycle_fonts"] = ov["cycle_fonts"]
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
            # Skipped for agentic templates — text_designer baked per-slot
            # timing + accel into the overlay dict at build time, so the
            # static _LABEL_CONFIG would just clobber the agent's choices.
            role = ov.get("role", "")
            sample_text = ov.get("sample_text", "")
            is_subject = _is_subject_placeholder(sample_text)
            is_label_like = (
                role == "label" or is_subject or sample_text.lower().startswith("welcome")
            )
            if is_label_like and not is_agentic:
                config = _LABEL_CONFIG["subject" if is_subject else "prefix"]
                # Only apply non-styling keys from config. Visual properties
                # (text_size, font_style, text_color, effect) come from the
                # recipe so the admin editor preview matches the render.
                _STYLING_KEYS = {
                    "text_size",
                    "text_size_px",
                    "font_style",
                    "font_family",
                    "text_color",
                    "effect",
                }
                entry.update(
                    {
                        k: v
                        for k, v in config.items()
                        if k not in ("start_s", "accel_at_s") and k not in _STYLING_KEYS
                    }
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
            if entry["effect"] == "font-cycle" and inter and inter.get("type") == "curtain-close":
                from app.pipeline.interstitials import (  # noqa: PLC0415
                    _CURTAIN_MAX_RATIO,
                    MIN_CURTAIN_ANIMATE_S,
                )

                # Use recipe animate_s directly when explicitly set
                recipe_animate = inter.get("animate_s")
                animate_s = (
                    float(recipe_animate) if recipe_animate is not None else MIN_CURTAIN_ANIMATE_S
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
    # The "screen slot" key is position + position_y_frac. Two overlays sharing
    # only `position="center"` but stacked at different y_frac (e.g. "The" at
    # 0.45, "Rule of" at 0.50, "Thirds" at 0.56) occupy DIFFERENT screen slots
    # and must not dedup against each other — Dedup 2 below would otherwise
    # truncate the earlier ones to invalid timestamps and filter them out.
    def _slot_key(o: dict) -> tuple:
        # position_y_frac is optional — overlays without an explicit vertical
        # anchor (e.g. legacy hooks that rely on the renderer's default y) have
        # y_frac=None. Mixing None with floats in this tuple crashes Python's
        # lexicographic sort below (`None < 0.45` is a TypeError), so map None
        # to a sentinel float that sorts BEFORE any real y_frac value (which
        # are all in [0.0, 1.0]). Two None-y_frac overlays still share the
        # same screen slot — the sentinel preserves that equality.
        # position_x_frac added for Waka Waka where "This" (x_frac=0.18) and
        # "is" (x_frac=0.82) share position="bottom"+y_frac=0.85 but live in
        # different screen halves; without including x_frac, dedup 2 below
        # truncated one and the rendered output only showed one of the two.
        y_frac = o.get("position_y_frac")
        x_frac = o.get("position_x_frac")
        return (
            o["position"],
            y_frac if y_frac is not None else -1.0,
            x_frac if x_frac is not None else -1.0,
        )

    _MERGE_GAP_THRESHOLD_S = 2.0
    raw.sort(key=lambda o: (o["text"].lower().strip(), _slot_key(o), o["start_s"]))
    unique: list[dict] = []
    for ov in raw:
        key = ov["text"].lower().strip()
        merged = False
        # Check if we can merge with an existing entry
        for prev in unique:
            prev_key = prev["text"].lower().strip()
            if (
                prev_key == key
                and _slot_key(prev) == _slot_key(ov)
                and prev.get("font_family") == ov.get("font_family")
                # Different colors at the same text+slot are an intentional
                # color transition (e.g. white "Thirds" 0.4-1.4s → red "Thirds"
                # 1.4-3.0s on the beat). Merging them would drop the second
                # phase entirely — the white overlay's end_s would extend
                # over the red, and the renderer never gets the red value.
                and prev.get("text_color") == ov.get("text_color")
                and not prev.get("spans")
                and not ov.get("spans")
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

    # ── Dedup 2: prevent same-screen-slot time overlap ──────────────────
    # Sort by screen slot (position + y_frac) then start time. If two overlays
    # share a screen slot and overlap in time, truncate the earlier one to end
    # before the later one starts (with a small gap for visual clarity).
    # Stacked overlays sharing only the position string (e.g. center, but at
    # different y_frac values) are NOT considered the same screen slot and are
    # never truncated against each other.
    unique.sort(key=lambda o: (_slot_key(o), o["start_s"]))
    for i in range(len(unique) - 1):
        if _slot_key(unique[i]) == _slot_key(unique[i + 1]):
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


def _concat_escape(path: str) -> str:
    """Escape a path for inclusion inside single-quoted `file '...'` directives
    in the ffmpeg concat demuxer list. Per the concat-demuxer spec, a literal
    single quote inside the quoted value is escaped by closing the quote,
    emitting an escaped single quote, then reopening (`'\\''`). This is the
    same trick POSIX shell quoting uses. Hardens us against any future tmpdir
    or asset path that happens to contain an apostrophe — the legacy per-`-i`
    path didn't have this risk because paths went straight through argv."""
    return path.replace("'", "'\\''")


def _build_preburn_inputs_and_graph(
    clip_path: str,
    png_configs: list[dict],
    burned_path: str,
    tmpdir: str,
) -> tuple[list[str], list[str], str]:
    """Build the input list and filter_complex parts for the pre-burn ffmpeg cmd.

    Returns ``(ffmpeg_inputs, filter_parts, last_label)``. The caller composes
    the final argv by prepending ``ffmpeg`` and appending ``-filter_complex
    <joined> -map [last_label] -map 0:a? <_encoding_args(...)>``. _encoding_args
    stays at the call site so the encoder-policy lock in
    tests/test_encoder_policy.py keeps finding a single authoritative
    invocation for `_pre_burn_curtain_slot_text`.

    Two PNG-feeding strategies, selected per-call:

    1. Font-cycle frames (filenames containing "fontcycle"): when there are
       2+ of them, they stream through the **concat demuxer** as a single -i
       input. Per-frame durations are copied verbatim from each config's
       ``end_s - start_s``, which carries the slow-then-fast cadence from
       ``_compute_font_cycle_frame_specs`` (recipe ``accel_at_s`` → slow
       0.15 s/frame → fast 0.07 s/frame transition) into ffmpeg without
       interpretation.

       Why concat demuxer instead of one -i per frame: a 5.5 s curtain-close
       slot with font-cycle emits ~57 PNGs at 170 px. Feeding all 57 as
       separate -i inputs plus a 57-link overlay chain peaks at ~487 MB of
       RGBA working set, which the 2 GB Fly worker SIGKILLs (verified on
       machine 4d89590eb44587 with the production image: rc=-9 before, rc=0
       after). The concat demuxer streams one frame at a time, so the working
       set stays constant regardless of frame count. Job b6540038 (Dimples
       Passport, location=Morocco) shipped without the MOROCCO font-cycle
       because the previous all-inputs path OOMed and the silent fallback
       below swallowed the failure.

    2. Static overlays (single-frame PNGs from generate_text_overlay_png's
       non-cycle branch) AND any single-frame "cycle" fallback (which happens
       when ``_resolve_cycle_fonts`` returned <2 fonts): one -i per PNG with
       its own ``enable='between(t,…)'`` clamp. Byte-identical to the pre-fix
       path. Templates that never trigger the cycle branch see no change in
       their ffmpeg invocation.
    """
    cycle = sorted(
        (c for c in png_configs if "fontcycle" in os.path.basename(c["png_path"])),
        key=lambda c: c["start_s"],
    )
    static = [c for c in png_configs if "fontcycle" not in os.path.basename(c["png_path"])]

    ffmpeg_inputs: list[str] = ["-i", clip_path]
    fc_parts: list[str] = ["[0:v]null[base]"]
    prev = "base"
    next_input_idx = 1  # ffmpeg input index for the next -i we add

    # The concat demuxer requires 2+ entries to avoid the "single-frame
    # duration collapses to 0" footgun documented at:
    # https://trac.ffmpeg.org/wiki/Slideshow#Concatdemuxer
    # If _render_font_cycle fell back to a single static PNG (insufficient
    # fonts), it lands in the per-PNG branch below.
    if len(cycle) >= 2:
        concat_list_path = os.path.join(
            tmpdir,
            os.path.basename(burned_path) + ".cycle_concat.txt",
        )
        with open(concat_list_path, "w") as fh:
            for c in cycle:
                # Guard against zero/negative durations from a malformed spec
                # — concat requires strictly positive durations.
                dur = max(0.001, c["end_s"] - c["start_s"])
                fh.write(f"file '{_concat_escape(c['png_path'])}'\n")
                fh.write(f"duration {dur:.6f}\n")
            # ffmpeg concat-demuxer quirk: the duration of the LAST listed
            # file is ignored unless that file is re-listed immediately after.
            # Without the re-listing the final cycle frame collapses to 0 ms
            # and the cycle ends one quantum early (e.g. MOROCCO settles
            # 0.07 s before the curtain meets the text).
            fh.write(f"file '{_concat_escape(cycle[-1]['png_path'])}'\n")

        cycle_start = cycle[0]["start_s"]
        cycle_end = cycle[-1]["end_s"]
        ffmpeg_inputs.extend(["-f", "concat", "-safe", "0", "-i", concat_list_path])
        out_lbl = "fc"
        fc_parts.append(
            f"[{prev}][{next_input_idx}:v]overlay=0:0"
            f":enable='between(t,{cycle_start:.3f},{cycle_end:.3f})'[{out_lbl}]"
        )
        prev = out_lbl
        next_input_idx += 1
        per_png_inputs = static
    else:
        # Legacy path. Byte-identical to the pre-fix code: one -i per PNG, one
        # overlay link per PNG. Behavior unchanged for templates that don't
        # produce cycle frames on their curtain-close slot.
        per_png_inputs = png_configs

    for j, c in enumerate(per_png_inputs):
        ffmpeg_inputs.extend(["-i", c["png_path"]])
        out_lbl = f"txt{j}"
        fc_parts.append(
            f"[{prev}][{next_input_idx}:v]overlay=0:0"
            f":enable='between(t,{c['start_s']:.3f},{c['end_s']:.3f})'[{out_lbl}]"
        )
        prev = out_lbl
        next_input_idx += 1

    return ffmpeg_inputs, fc_parts, prev


def _pre_burn_curtain_slot_text(
    clip_path: str,
    step,
    slot_dur: float,
    clip_metas: list | None,
    subject: str,
    slot_idx: int,
    tmpdir: str,
    inter: dict | None = None,
    is_agentic: bool = False,
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
            "position_x_frac": ov.get("position_x_frac"),
            "position_y_frac": ov.get("position_y_frac"),
            # Carry recipe-side font_cycle_accel_at_s through to the renderer.
            # Without this, `_compute_font_cycle_frame_specs` falls back to its
            # default "cycle for first 70%, settle to STATIC for last 30%"
            # behavior — which on curtain-close slots means the cycling FREEZES
            # right when the curtain starts closing (settle phase overlaps
            # with curtain duration). Worker log proved the symptom:
            # `font_cycle_rendered accelerated=False` despite recipe setting it.
            "font_cycle_accel_at_s": ov.get("font_cycle_accel_at_s"),
        }
        # Pass through font_family + cycle_fonts so per-overlay curated font
        # selection reaches the renderer. (Both fields are in _STYLING_KEYS
        # below so the label-config update doesn't overwrite them — but they
        # also need to be COPIED here first, since the literal entry dict
        # above doesn't include them.)
        if ov.get("font_family"):
            entry["font_family"] = ov["font_family"]
        if ov.get("cycle_fonts"):
            entry["cycle_fonts"] = ov["cycle_fonts"]

        role = ov.get("role", "")
        sample_text = ov.get("sample_text", "")
        is_subject = _is_subject_placeholder(sample_text)
        is_label_like = role == "label" or is_subject or sample_text.lower().startswith("welcome")
        # Agentic templates already have per-overlay styling baked in by
        # text_designer at build time; skip the static override.
        if is_label_like and not is_agentic:
            config = _LABEL_CONFIG["subject" if is_subject else "prefix"]
            # Recipe styling takes priority — only apply non-styling keys
            # from _LABEL_CONFIG so admin editor preview matches render.
            _STYLING_KEYS = {
                "text_size",
                "text_size_px",
                "font_style",
                "font_family",
                "text_color",
                "effect",
            }
            entry.update(
                {
                    k: v
                    for k, v in config.items()
                    if k not in ("start_s", "accel_at_s") and k not in _STYLING_KEYS
                }
            )

            # Font-cycle acceleration synced with curtain animation
            if is_subject and entry.get("effect") == "font-cycle" and inter:
                from app.pipeline.interstitials import (  # noqa: PLC0415
                    _CURTAIN_MAX_RATIO,
                    MIN_CURTAIN_ANIMATE_S,
                )

                recipe_animate = inter.get("animate_s")
                animate_s = (
                    float(recipe_animate) if recipe_animate is not None else MIN_CURTAIN_ANIMATE_S
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

    # Burn PNGs onto clip. The input list and filter graph are built by a
    # dedicated helper so font-cycle frames take the concat-demuxer path
    # (constant-RAM, survives the 2 GB Fly worker) while statics keep the
    # per-PNG path (byte-identical to the pre-fix code for templates that
    # don't trigger cycle frames). See `_build_preburn_inputs_and_graph` for
    # the OOM history and why the split is by filename.
    burned_path = os.path.join(tmpdir, f"slot_{slot_idx}_textburned.mp4")
    ffmpeg_inputs, fc_parts, last_label = _build_preburn_inputs_and_graph(
        clip_path,
        png_configs,
        burned_path,
        tmpdir,
    )
    cmd = [
        "ffmpeg",
        *ffmpeg_inputs,
        "-filter_complex",
        ";".join(fc_parts),
        "-map",
        f"[{last_label}]",
        "-map",
        "0:a?",
        # preset=fast (not ultrafast): this slot is about to be re-encoded ONE
        # MORE TIME by apply_curtain_close_tail_overlay (which already uses
        # preset=fast + tune=film). If we encode here at ultrafast, the mb-tree/
        # psy-rd losses are baked in BEFORE curtain-close runs, and the curtain
        # encoder can't undo them — visible as banding on smooth gradients under
        # the title text (Dimples slot 5 BRAZIL on the blue canopy). Locked by
        # tests/test_encoder_policy.py. Per-slot wall-clock cost is small because
        # the slot is short (typically 2-5s) and the overlay filter chain
        # dominates encode time anyway.
        *_encoding_args(burned_path, preset="fast"),
    ]

    log.info("pre_burn_curtain_text", slot=slot_idx, overlays=len(png_configs))
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        # Hard fail. Mirrors curtain_close_tail_failed (above) and
        # animated_overlay_failed (text_overlay.py) — recipe-defined hero
        # overlays are not a "best effort" surface. Job b6540038 (Dimples
        # Passport, location=Morocco) shipped without the MOROCCO font-cycle
        # because the previous silent fallback returned `clip_path` here,
        # then apply_curtain_close_tail ran on a textless slot. A failed job
        # with `failure_reason=ffmpeg_failed` is strictly better than a
        # "successful" job that lost the slot's hero word.
        log.error(
            "pre_burn_curtain_text_failed",
            slot=slot_idx,
            rc=result.returncode,
            stderr=(result.stderr.decode(errors="replace")[-1500:] if result.stderr else ""),
        )
        raise RuntimeError(
            f"pre-burn curtain text failed on slot {slot_idx} (rc={result.returncode}); "
            f"refusing to ship a video with a missing recipe-defined hero overlay"
        )

    return burned_path


def _burn_text_overlays(
    input_path: str,
    overlays: list[dict],
    output_path: str,
    tmpdir: str,
) -> None:
    """Burn text overlays onto the joined video.

    Two-pass overlay model:
      1. Static + font-cycle overlays → PNG composites via overlay filter.
      2. ASS-animated overlays (fade-in, typewriter, slide-up, slide-down,
         pop-in, bounce) → libass-rendered .ass files chained via the
         subtitles filter.

    Both layers stack on top of the base video. Overlays already carry
    absolute timestamps from _collect_absolute_overlays; PNG timing flows
    through enable='between(t,...)' and ASS timing flows through each
    Dialogue line's Start/End fields written by _write_animated_ass.

    Splitting overlays by effect class prevents the static PNG fallback
    from masking the ASS animation — without this split, a slide-down
    overlay produces both a static "This" at its target Y *and* an ASS
    animation moving the same glyph from off-canvas, and the static layer
    wins visually.
    """
    from app.pipeline.text_overlay import (  # noqa: PLC0415
        ASS_ANIMATED_EFFECTS,
        FONTS_DIR,
        generate_animated_overlay_ass,
        generate_text_overlay_png,
    )

    static_overlays = [o for o in overlays if o.get("effect") not in ASS_ANIMATED_EFFECTS]
    animated_overlays = [o for o in overlays if o.get("effect") in ASS_ANIMATED_EFFECTS]

    png_configs = (
        generate_text_overlay_png(
            static_overlays,
            ABS_PASS_TIME_S,
            tmpdir,
            slot_index=ABS_PASS_SLOT_INDEX,
        )
        if static_overlays
        else None
    ) or []
    ass_files = (
        generate_animated_overlay_ass(
            animated_overlays,
            ABS_PASS_TIME_S,
            tmpdir,
            slot_index=ABS_PASS_SLOT_INDEX,
        )
        if animated_overlays
        else None
    ) or []

    if not png_configs and not ass_files:
        shutil.copy2(input_path, output_path)
        return

    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    cmd = ["ffmpeg", "-i", input_path]
    for cfg in png_configs:
        cmd.extend(["-i", cfg["png_path"]])

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

    # Chain subtitles filter for each ASS file. libass uses absolute times
    # from the Dialogue lines themselves, so no enable= clamp needed. The
    # subtitles filter requires escaped paths on macOS/Linux for both the
    # ASS file and fontsdir, matching the _build_fixed_intro_ass pattern.
    fonts_dir_escaped = FONTS_DIR.replace(":", "\\:").replace("'", "\\'")
    for ass_path in ass_files:
        ass_filter_path = ass_path.replace(":", "\\:").replace("'", "\\'")
        out = f"ass{ass_files.index(ass_path)}"
        fc_parts.append(
            f"[{prev}]subtitles='{ass_filter_path}':fontsdir='{fonts_dir_escaped}'[{out}]"
        )
        prev = out

    cmd.extend(
        [
            "-filter_complex",
            ";".join(fc_parts),
            "-map",
            f"[{prev}]",
            "-map",
            "0:a?",
            # preset=fast (not ultrafast): this is the FINAL encode pass —
            # the bytes that ship to the user. ultrafast disables mb-tree,
            # psy-rd, B-frames and trellis quant, which is exactly what
            # protects smooth gradients (sky, dark canopy) from visible
            # 16×16 macroblocking. The previous comment claimed "CRF 18 keeps
            # quality high" but CRF only controls the rate target — preset
            # controls which encoder features run. Without psy-rd + mb-tree,
            # ultrafast produces blocky output regardless of CRF.
            # The previous 600s timeout pressure on 24-slot recipes was at
            # --concurrency=2; PR #105 dropped to --concurrency=1 (fly.toml)
            # which gives ~2× more CPU per worker. Combined with the PNG-
            # overlay curtain (10× faster than geq), per-slot encode budget
            # absorbs preset=fast easily. Locked by tests/test_encoder_policy.py.
            # Old comment, kept for archaeology: "ultrafast: matches the speed
            # bump made in _concat_demuxer. preset=fast was timing out at 600s
            # on 24-slot recipes; ultrafast finishes in well under a minute."
            *_encoding_args(output_path, preset="fast"),
        ]
    )

    # png_count is already a per-frame count: font-cycle overlays append one
    # cfg per frame (up to MAX_FONT_CYCLE_FRAMES). A high png_count relative
    # to static_overlay_count means a font-cycle overlay is exploding.
    log.info(
        "burn_text_overlays",
        png_count=len(png_configs),
        ass_count=len(ass_files),
        static_overlay_count=len(static_overlays),
        animated_overlay_count=len(animated_overlays),
    )
    burn_t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    burn_elapsed_ms = int((time.monotonic() - burn_t0) * 1000)
    if result.returncode != 0:
        log.warning(
            "burn_text_failed",
            rc=result.returncode,
            elapsed_ms=burn_elapsed_ms,
            stderr=result.stderr.decode(errors="replace")[-500:],
        )
        shutil.copy2(input_path, output_path)
    else:
        log.info(
            "burn_text_done",
            elapsed_ms=burn_elapsed_ms,
            png_count=len(png_configs),
            ass_count=len(ass_files),
        )


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


def _embedded_allcaps_token(sample: str) -> str | None:
    """Return the single all-caps token embedded in mixed-case text, or None.

    Matches the slot 6 "Welcome to PERU" pattern: one all-caps word (length > 1,
    alpha) inside a phrase that has at least one lowercase word. Returns None
    when the text is fully all-caps, fully title-case, has no all-caps token,
    or has more than one (ambiguous → don't guess).
    """
    words = sample.split()
    if not (2 <= len(words) <= 5):
        return None
    if sample.isupper():
        return None
    allcaps = [w for w in words if w.isupper() and len(w) > 1 and w.isalpha()]
    if len(allcaps) != 1:
        return None
    return allcaps[0]


def _is_subject_placeholder(sample: str) -> bool:
    """Return True if sample_text looks like a variable place/topic name.

    Heuristic: short ALL-CAPS text (e.g. "PERU", "NYC"), a short
    title-cased phrase where every word is capitalized (e.g. "New York"),
    or a mixed-case phrase containing a single all-caps token (e.g.
    "Welcome to PERU") is likely a template variable that should be
    replaced with the user's subject. Fixed phrases like "Welcome to"
    (no all-caps token) or "discovering a hidden river" (long, lowercase)
    do NOT match.
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
    # Mixed-case phrase with a single embedded all-caps token: "Welcome to PERU"
    if _embedded_allcaps_token(sample) is not None:
        return True
    return False


def _substitute_subject(sample: str, subject: str) -> str:
    """Replace the placeholder portion of sample with the user's subject.

    - "PERU" + "Tokyo"            → "TOKYO"   (whole-text caps)
    - "Peru" + "Tokyo"            → "Tokyo"   (whole-text title-case, subject as-is)
    - "Welcome to PERU" + "Tokyo" → "Welcome to TOKYO" (token-only swap)

    Caller must check `_is_subject_placeholder(sample)` first; this function
    assumes the placeholder rules already matched.
    """
    if sample.isupper():
        return subject.upper()
    token = _embedded_allcaps_token(sample)
    if token is not None:
        return " ".join(subject.upper() if w == token else w for w in sample.split())
    return subject


def _match_casing(text: str, sample: str) -> str:
    """Cast `text` to mirror the casing pattern of `sample`.

    Used by the `subject_part` branch to keep an aesthetic intact when
    substituting a user input into a fragment placeholder. If the original
    placeholder was lowercase ("lon"), we want the substituted value to
    also be lowercase ("par" for user input "Paris").

    Returns text unchanged when sample carries no signal (empty / no cased
    chars / mixed case). The heuristic branch uses `_substitute_subject`
    instead — it handles the embedded-allcaps case this helper cannot.
    """
    if not text or not sample:
        return text
    if not any(c.isalpha() for c in sample):
        return text
    if sample.isupper():
        return text.upper()
    if sample.islower():
        return text.lower()
    if sample.istitle():
        return text.title()
    return text


def _split_subject(subject: str, part: str) -> str:
    """Slice `subject` according to `part` ("first_half"/"second_half"/"full").

    Midpoint with ceil for first half so the first overlay is the larger
    half on odd lengths (mirrors the original "lon"/"don" 3+3 split).
    Unknown values fall through to the full subject.
    """
    if part == "full":
        return subject
    if not subject:
        return ""
    n = (len(subject) + 1) // 2  # ceil
    if part == "first_half":
        return subject[:n]
    if part == "second_half":
        return subject[n:]
    return subject


def _resolve_overlay_text(
    role: str,
    clip_meta: "ClipMeta | None",
    overlay: dict,
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

    `subject_part` on the overlay (one of "first_half", "second_half", "full")
    is an explicit opt-in for subject substitution that bypasses the casing
    heuristic. This lets templates split a single user input across two
    overlays (e.g. "lon" + "don" → "par" + "is" for subject "Paris") that
    the lowercase-fragment heuristic would never match.

    `subject_template` on the overlay is a format string with a `{subject}`
    placeholder, used for typewriter-style sentences that embed the city in a
    longer phrase (e.g. "that one trip to {subject}"). Optional companion
    `subject_chars` slices the user input to the first N characters before
    substitution, so a partial typewriter beat reveals only "Mor" instead of
    the full "Morocco". Empty subject falls back to the literal text so admin
    previews still render.

    `subject_substitute: False` on the overlay is an explicit opt-OUT that
    pins the overlay to its literal `sample_text` and bypasses every
    substitution path (subject_template, subject_part, AND the casing
    heuristic). Used for templates with literal title-cased or all-caps
    intros — e.g. Waka Waka's "This" / "is" / "AFRICA" — that the casing
    heuristic would otherwise rewrite to the user's subject. Default
    (None / missing / True) preserves existing behavior.

    ⚠️  DO NOT READ FROM `clip_meta` IN THIS FUNCTION ⚠️
    `clip_meta` is kept in the signature only because the two call sites
    (`_collect_absolute_overlays`, `_pre_burn_curtain_slot_text`) thread it
    through for legacy reasons. The earlier `clip_meta.hook_text` fallback
    was removed after job a1091488 (Rule of Thirds, 2026-05-13) leaked
    Gemini's per-clip description into rendered hook text. Reading ANY
    field off `clip_meta` here re-opens that bug. `TestNoGeminiTextLeaks`
    in tests/tasks/test_template_orchestrate.py pins this contract; if you
    need a Gemini-derived field downstream, add an explicit per-overlay
    opt-in (like `subject_template` / `subject_part`) so the leak surface
    is declared in the recipe, not inferred from clip metadata.
    """
    sample = overlay.get("sample_text", "") or overlay.get("text", "")

    if role == "cta":
        return ""  # CTA text generated later by copy_writer

    # Explicit literal opt-out beats every substitution path. Used by
    # templates whose intro words are literal title-cased / all-caps text
    # that the casing heuristic would otherwise misread as placeholders.
    if overlay.get("subject_substitute") is False:
        return sample

    # Explicit subject_template (typewriter/embedded) beats both subject_part
    # and the heuristic. Used for slots whose text is a sentence wrapping the
    # subject (e.g. "that one trip to london").
    subject_template = overlay.get("subject_template")
    if isinstance(subject_template, str) and "{subject}" in subject_template:
        if not subject:
            return overlay.get("text") or sample
        chars = overlay.get("subject_chars")
        try:
            chars = int(chars) if chars is not None else None
        except (TypeError, ValueError):
            chars = None
        sliced = subject[:chars] if (chars is not None and chars > 0) else subject
        return subject_template.format(subject=sliced)

    # Explicit subject_part beats the heuristic: the template author has
    # tagged this overlay as a slot for the user's subject. Empty subject
    # falls back to sample_text so admin previews and dry runs still
    # render the placeholder.
    subject_part = overlay.get("subject_part")
    if subject_part in ("first_half", "second_half", "full"):
        if not subject:
            return sample
        return _match_casing(_split_subject(subject, subject_part), sample)

    # Subject substitution: if the template text looks like a variable
    # place/topic name and we have a subject, substitute it.
    if subject and _is_subject_placeholder(sample):
        return _substitute_subject(sample, subject)

    # The template defines the rendered text. Period. There is no fallback to
    # `clip_meta.hook_text` for empty hook overlays — that path leaked Gemini's
    # per-clip description into the rendered video (e.g. "pilot in cockpit"
    # over Rule of Thirds). A template author that wants the clip's hook
    # description as text must opt in via subject_template/subject_part, the
    # same explicit-intent surface Brazil uses for user-provided locations.
    return sample


# Removed: _consensus_subject(clip_metas).
# Took a majority vote across clips' Gemini `detected_subject` fields and
# fed the result to the placeholder-substitution path when the user didn't
# provide a subject. That fallback turned Gemini's per-clip analysis into
# user-visible rendered text — caught when job a1091488 rendered "pilot in
# cockpit" in place of "The"/"Thirds" on Rule of Thirds. Subject is now
# user-input-only at every call site. Do NOT reintroduce; see the sentinel
# test in tests/tasks/test_template_orchestrate.py.


# ── Beat detection helpers ─────────────────────────────────────────────────────

BEAT_SNAP_TOLERANCE_S = 0.4


def _detect_audio_beats(audio_path: str, threshold_db: float = -35.0) -> list[float]:
    """Detect energy onset timestamps in audio using FFmpeg silencedetect.

    Parses silence_end markers from FFmpeg stderr — each silence_end marks a
    transition from quiet→loud, which corresponds to a beat or energy onset.

    Non-fatal: returns [] on any error.
    """
    cmd = [
        "ffmpeg",
        "-i",
        audio_path,
        "-af",
        f"silencedetect=noise={threshold_db}dB:d=0.1",
        "-f",
        "null",
        "-",
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
            float(m.group(1)) for m in re.finditer(r"silence_end:\s*([\d.]+)", stderr_text)
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
        "ffmpeg",
        "-i",
        local_video_path,
        "-vn",  # no video
        "-acodec",
        "aac",
        "-b:a",
        "192k",
        "-f",
        "mp4",  # force mp4 container (more compatible than raw ADTS .aac)
        "-y",
        output_path,
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
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        timeout=10,
        check=False,
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
        "-i",
        video_path,
        "-stream_loop",
        "-1",
        "-i",
        audio_local,
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-af",
        f"afade=t=out:st={fade_start}:d=0.5,loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",  # noqa: E501
        *(["-t", f"{use_duration:.3f}"] if use_duration > 0 else []),
        "-y",
        output_path,
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
        meta.transcript for meta in clip_metas if meta.clip_id in used_clip_ids and meta.transcript
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

# Floor below which a user clip is considered unusable (corrupt / near-empty
# file). Shorter-than-target clips above this floor render at their full
# available duration instead of failing.
_HARD_MIN_BODY_S = 0.5


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
        lines.append(f"Dialogue: 0,{start},{end},Caption,,0,0,0,,{text}\n")

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
        "-f",
        "lavfi",
        "-i",
        (
            f"color=c={background_color}:"
            f"s={_FIXED_INTRO_WIDTH}x{_FIXED_INTRO_HEIGHT}:"
            f"d={intro_duration_s:.3f}:r={_FIXED_INTRO_FPS}"
        ),
        "-vf",
        f"subtitles='{ass_filter_path}':fontsdir='{fonts_dir_escaped}'",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        # Pin color metadata so the concat-copy boundary with body.mp4
        # doesn't produce a sudden color shift on QuickTime/Safari/iOS.
        "-color_range",
        "tv",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-r",
        str(_FIXED_INTRO_FPS),
        "-t",
        f"{intro_duration_s:.3f}",
        "-an",
        "-movflags",
        "+faststart",
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
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{duration_s:.3f}",
        "-i",
        src_video,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        # Match intro's color metadata so concat-copy doesn't produce a
        # sudden color shift at the boundary on QuickTime/Safari/iOS.
        "-color_range",
        "tv",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-r",
        str(_FIXED_INTRO_FPS),
        "-vsync",
        "cfr",
        "-an",
        "-movflags",
        "+faststart",
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
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        "-an",
        "-movflags",
        "+faststart",
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
        "ffmpeg",
        "-i",
        user_video_path,
        "-vn",
        "-af",
        f"asetnsamples={samples_per_bucket},"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f",
        "null",
        "-",
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

    `min_body_s` is a soft target, not a hard requirement: clips shorter than
    `min_body_s` render as the full available duration rather than failing.
    Only truly unusable clips (sub-`_HARD_MIN_BODY_S`) raise.
    """
    from app.pipeline.probe import probe_video  # noqa: PLC0415

    probe = probe_video(user_video_path)
    user_dur = probe.duration_s
    if user_dur < _HARD_MIN_BODY_S:
        raise ValueError(
            f"User video unusable: have {user_dur:.2f}s (need at least {_HARD_MIN_BODY_S:.1f}s)"
        )

    if user_dur < min_body_s:
        log.info(
            "body_v2_short_clip_full_use",
            user_dur=round(user_dur, 2),
            min_body_target_s=min_body_s,
        )
        return 0.0, user_dur

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
        "-ss",
        "0",
        "-t",
        f"{intro_duration_s:.3f}",
        "-i",
        template_video_path,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-r",
        str(fps),
        "-vsync",
        "cfr",
        # Audio: pass through at source quality. Re-encoding here was
        # the first of TWO encode passes degrading the intro VO. The
        # composer below mixes it once more — that's the only encode
        # the intro audio should suffer, not two.
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
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
) -> bool:
    """Concat intro+body video AND mix audio in a single ffmpeg pass.

    Returns True on success (output_path written), False on graceful failure
    (timeout or ffmpeg non-zero). On False the caller is expected to fall
    back to a silent-body concat — output_path is NOT written so the caller
    still controls the final artifact.

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
        "-i",
        intro_path,
        "-i",
        body_path,
        "-i",
        music_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v_out]",
        "-map",
        "[a_out]",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        # ultrafast (was: fast). Matches the prior optimization in commit
        # 4c678fc which moved _concat_demuxer and _burn_text_overlays to
        # ultrafast for 3-5x faster encoding. Compose was missed in that
        # pass and was the bottleneck behind the 300s subprocess timeout
        # that bricked job ae43a33c on 2026-05-08.
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-r",
        str(_FIXED_INTRO_FPS),
        "-vsync",
        "cfr",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-t",
        f"{total_dur:.3f}",
        "-movflags",
        "+faststart",
        "-y",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except subprocess.TimeoutExpired:
        # Bug fix: prior to this catch, TimeoutExpired bypassed the non-zero
        # branch below and bubbled to _stage() as ffmpeg_failed → user got
        # "Video rendering failed" with no output. Now: degrade to caller's
        # silent-body fallback (better UX than the previous "ship just the
        # intro" copy).
        log.warning("compose_timeout", timeout_s=300)
        return False
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-800:]
        log.warning("compose_failed", stderr=stderr, returncode=result.returncode)
        return False

    log.info("compose_done", output=output_path)
    return True


def _probe_duration_for_compose(path: str) -> float:
    """Same as _probe_duration in intro_voiceover_mix; redeclared locally to
    avoid pulling that whole module into orchestrator scope just for one helper."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        timeout=10,
        check=False,
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
            f"User video too short for body: have {user_dur:.2f}s, need ≥ {target_duration_s:.2f}s"
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
    chosen = min(nearby, key=lambda c: abs(c - base_start)) if nearby else base_start

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
    preview_mode: bool = False,
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

    Each numbered step runs inside `_stage(name, failure_reason)`. Failures
    raise `_StageError(reason, msg)` and are persisted by the outer task.
    """
    if len(clip_paths_gcs) != 1:
        # Should be impossible — POST /template-jobs validates clip count
        # against the template's required_clips_min/max. If we get here,
        # the template's clip-count config is out of sync with its kind.
        raise _StageError(
            FAILURE_REASON_TEMPLATE_MISCONFIGURED,
            f"single_video templates expect exactly 1 user clip; got {len(clip_paths_gcs)}",
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
            raise _StageError(
                FAILURE_REASON_TEMPLATE_MISCONFIGURED,
                f"Template {template_id} not found",
            )
        template_video_gcs = template.gcs_path

    if not template_video_gcs:
        raise _StageError(
            FAILURE_REASON_TEMPLATE_MISCONFIGURED,
            f"Template {template_id} has no gcs_path (reference video)",
        )

    audio_health: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        _phase_t0 = time.monotonic()
        # [1] Download template's reference video (intro source).
        ref_video_local = os.path.join(tmpdir, "template_ref.mp4")
        with _stage(
            "download_template_ref",
            FAILURE_REASON_TEMPLATE_ASSETS_MISSING,
            job_id=job_id,
        ):
            download_to_file(template_video_gcs, ref_video_local)

        # [2] Download user clip.
        user_local = os.path.join(tmpdir, "user_clip.mp4")
        with _stage(
            "download_user_clip",
            FAILURE_REASON_USER_CLIP_DOWNLOAD_FAILED,
            job_id=job_id,
        ):
            download_to_file(clip_paths_gcs[0], user_local)

        record_phase(
            job_id,
            PHASE_DOWNLOAD_CLIPS,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_ANALYZE_CLIPS,
        )
        _phase_t0 = time.monotonic()

        # [3] Pick body window (peak-anchored, variable length). A ValueError
        # here means the user's clip is unusable (too short, no audio, probe
        # failure) — surface that to the user with the original message.
        try:
            with _stage(
                "select_body_window",
                FAILURE_REASON_USER_CLIP_UNUSABLE,
                job_id=job_id,
            ):
                body_start, body_end = _select_body_window_peak_anchored(
                    user_local,
                    pre_roll_s=pre_roll_s,
                    max_body_s=max_body_s,
                    min_body_s=min_body_s,
                )
        except _StageError as stage_err:
            if stage_err.reason == FAILURE_REASON_USER_CLIP_UNUSABLE:
                # Rewrite to the user-friendly phrasing the previous
                # implementation produced.
                raise _StageError(
                    FAILURE_REASON_USER_CLIP_UNUSABLE,
                    f"Could not read user video: {stage_err.message}",
                ) from stage_err
            raise
        body_dur = body_end - body_start

        record_phase(
            job_id,
            PHASE_ANALYZE_CLIPS,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_ASSEMBLE,
        )
        _phase_t0 = time.monotonic()

        # [4] Extract intro from template reference (KEEPS audio).
        intro_path = os.path.join(tmpdir, "intro.mp4")
        with _stage("extract_intro", FAILURE_REASON_FFMPEG_FAILED, job_id=job_id):
            _extract_intro_from_template_video(
                ref_video_local,
                intro_duration_s,
                intro_path,
            )

        # [5] Cut body segment (silent, 9:16).
        body_path = os.path.join(tmpdir, "body.mp4")
        with _stage("cut_body", FAILURE_REASON_FFMPEG_FAILED, job_id=job_id):
            _cut_body_segment(user_local, body_start, body_dur, body_path)

        # [6] Compose final: concat video + mix audio (intro audio + body music).
        # Music download failures fall back to silent-body — the job still
        # ships, just with `audio_health` populated.
        final_path = os.path.join(tmpdir, "final.mp4")
        if not audio_gcs_path:
            log.warning("fixed_intro_no_music_asset", template_id=template_id)
            audio_health.append("music_path_unset")
            with _stage(
                "concat_silent",
                FAILURE_REASON_FFMPEG_FAILED,
                job_id=job_id,
            ):
                _concat_silent([intro_path, body_path], final_path, tmpdir)
        else:
            music_local = os.path.join(tmpdir, "music.m4a")
            try:
                download_to_file(audio_gcs_path, music_local)
                music_ok = True
            except Exception as exc:
                log.warning("fixed_intro_music_download_failed", error=str(exc))
                audio_health.append("music_download_failed")
                music_ok = False
            if not music_ok:
                with _stage(
                    "concat_silent",
                    FAILURE_REASON_FFMPEG_FAILED,
                    job_id=job_id,
                ):
                    _concat_silent(
                        [intro_path, body_path],
                        final_path,
                        tmpdir,
                    )
            else:
                # compose returns False on timeout / ffmpeg non-zero. We
                # degrade to silent-body concat in that case rather than
                # failing the whole job — a 22.9s intro+body video without
                # music is a better outcome than a 6-minute "Video
                # rendering failed". audio_health surfaces the degradation
                # so admins can spot a pattern.
                composed_with_music = False
                with _stage(
                    "compose_with_music",
                    FAILURE_REASON_FFMPEG_FAILED,
                    job_id=job_id,
                ):
                    composed_with_music = _compose_intro_body_with_music(
                        intro_path=intro_path,
                        body_path=body_path,
                        music_path=music_local,
                        output_path=final_path,
                        intro_duration_s=intro_duration_s,
                    )
                if not composed_with_music:
                    audio_health.append("compose_with_music_failed")
                    with _stage(
                        "concat_silent_fallback",
                        FAILURE_REASON_FFMPEG_FAILED,
                        job_id=job_id,
                    ):
                        _concat_silent(
                            [intro_path, body_path],
                            final_path,
                            tmpdir,
                        )

        record_phase(
            job_id,
            PHASE_ASSEMBLE,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_UPLOAD,
        )
        _phase_t0 = time.monotonic()

        # [7] Upload primary + base output. template_output.mp4 and
        # template_base.mp4 are byte-identical for this template family
        # (no slot-level overlays to strip for the editor's "without
        # overlays" preview). Upload once, server-side copy to the second
        # key — saves egress + re-upload bandwidth.
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        gcs_base_path = f"jobs/{job_id}/template_base.mp4"
        with _stage(
            "upload_outputs",
            FAILURE_REASON_OUTPUT_UPLOAD_FAILED,
            job_id=job_id,
        ):
            video_url = upload_public_read(final_path, gcs_output_path)
            base_video_url = copy_object_signed_url(gcs_output_path, gcs_base_path)

        record_phase(
            job_id,
            PHASE_UPLOAD,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_GENERATE_COPY,
        )
        _phase_t0 = time.monotonic()

        # [8] Generate copy. Hook text = the template's signature punchline.
        # Skipped under preview_mode (admin test tab): saves ~5-15s; the test
        # tab doesn't render copy.
        if preview_mode:
            from app.pipeline.agents.copy_writer import (  # noqa: PLC0415
                InstagramCopy,
                PlatformCopy,
                TikTokCopy,
                YouTubeCopy,
            )

            platform_copy = PlatformCopy(
                tiktok=TikTokCopy(hook="", caption="", hashtags=[]),
                instagram=InstagramCopy(hook="", caption="", hashtags=[]),
                youtube=YouTubeCopy(title="", description="", tags=[]),
            )
            copy_status = "skipped_preview_mode"
            log.info("copy_generation_skipped_preview", job_id=job_id)
        else:
            hook_text = "How do you enjoy your life?"
            from app.pipeline.agents.copy_writer import generate_copy  # noqa: PLC0415

            with _stage(
                "generate_copy",
                FAILURE_REASON_COPY_GENERATION_FAILED,
                job_id=job_id,
            ):
                platform_copy, copy_status = generate_copy(
                    hook_text=hook_text,
                    transcript_excerpt="",
                    platforms=selected_platforms,
                    has_transcript=False,
                    template_tone="provocative",
                    job_id=job_id,
                )

        record_phase(
            job_id,
            PHASE_GENERATE_COPY,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
            next_phase=PHASE_FINALIZE,
        )
        _phase_t0 = time.monotonic()

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
                    job.error_detail = f"audio assets unavailable: {','.join(audio_health)}"
                db.commit()

        record_phase(
            job_id,
            PHASE_FINALIZE,
            elapsed_ms=int((time.monotonic() - _phase_t0) * 1000),
        )
        mark_finished(job_id)

        log.info(
            "fixed_intro_template_done",
            job_id=job_id,
            output_url=video_url,
            body_start=round(body_start, 3),
            body_end=round(body_end, 3),
        )


# ── orchestrate_single_video_job ──────────────────────────────────────────────
#
# Dedicated Celery task for `template_kind="single_video"` templates. The
# pipeline is ~5 ffmpeg subprocess calls + 3 GCS round-trips; happy path
# wall-clock is 30–60s. The 1740s soft timeout on `orchestrate_template_job`
# is sized for 24-slot Morocco templates with text-burning — applied to a
# single_video job, it lets a hung run hold the worker slot for half an hour
# (each worker has `--concurrency=2` slots — see fly.toml).
#
# Tight 240s/300s timeouts here mean a bad job fails fast and frees the
# queue. Multi-video templates keep their longer budget via the original
# `orchestrate_template_job` task.


# Soft limit picked from observed prod P95: a successful single_video run
# took ~311s on 2026-05-08, including worker cold-start and Gemini
# generate_copy. 600s gives ~2x headroom for variance while still killing
# truly hung jobs in 10 min instead of the 29-min generic budget. The
# per-stage timing logs added in `_stage()` will let us tighten further
# once we know where the 311s actually goes.
_SINGLE_VIDEO_SOFT_TIMEOUT_S = 600
_SINGLE_VIDEO_HARD_TIMEOUT_S = 720


@celery_app.task(
    name="tasks.orchestrate_single_video_job",
    bind=True,
    max_retries=0,
    soft_time_limit=_SINGLE_VIDEO_SOFT_TIMEOUT_S,
    time_limit=_SINGLE_VIDEO_HARD_TIMEOUT_S,
)
def orchestrate_single_video_job(self, job_id: str) -> None:
    """single_video pipeline. Never raises — all errors → processing_failed."""
    log.info("single_video_job_start", job_id=job_id)

    try:
        job_uuid = uuid.UUID(str(job_id))
    except (ValueError, TypeError, AttributeError):
        log.error("single_video_job_invalid_id", job_id=repr(job_id))
        return

    try:
        _run_single_video_job_entry(job_id)
    except _StageError as stage_err:
        log.error(
            "single_video_job_classified_failure",
            job_id=job_id,
            failure_reason=stage_err.reason,
            error=stage_err.message,
        )
        _mark_failed(job_uuid, stage_err.reason, stage_err.message)
    except SoftTimeLimitExceeded:
        log.error("single_video_job_timeout", job_id=job_id)
        _mark_failed(
            job_uuid,
            FAILURE_REASON_TIMEOUT,
            f"Single-video job timed out (exceeded {_SINGLE_VIDEO_SOFT_TIMEOUT_S}s soft limit)",
        )
    except TemplateMismatchError as exc:
        log.error(
            "single_video_job_clip_mismatch",
            job_id=job_id,
            code=exc.code,
            error=exc.message,
        )
        _mark_failed(
            job_uuid,
            FAILURE_REASON_USER_CLIP_UNUSABLE,
            f"{exc.code}: {exc.message}",
        )
    except Exception as exc:
        log.exception("single_video_job_fatal", job_id=job_id, error=str(exc))
        _mark_failed(job_uuid, FAILURE_REASON_UNKNOWN, str(exc))


def _run_single_video_job_entry(job_id: str) -> None:
    """Hydrate job state, set processing, then call _run_single_video_job."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("single_video_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        # Clear any previous failure tags from a re-enqueue.
        job.error_detail = None
        job.failure_reason = None
        db.commit()

        template_id = job.template_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs = all_candidates.get("clip_paths", [])
        user_subject = ((all_candidates.get("inputs") or {}).get("location") or "").strip()
        selected_platforms = job.selected_platforms or [
            "tiktok",
            "instagram",
            "youtube",
        ]
        # Same admin test-tab preview flag the multi-video path reads. Default
        # False; set only by admin.py create_test_job.
        preview_mode = bool(all_candidates.get("preview_mode", False))

    mark_started(job_id)

    if not template_id:
        raise _StageError(
            FAILURE_REASON_TEMPLATE_MISCONFIGURED,
            "Job has no template_id",
        )

    with _sync_session() as db:
        template = db.get(VideoTemplate, template_id)
        if template is None:
            raise _StageError(
                FAILURE_REASON_TEMPLATE_MISCONFIGURED,
                f"Template {template_id} not found",
            )
        if template.analysis_status != "ready" or not template.recipe_cached:
            raise _StageError(
                FAILURE_REASON_TEMPLATE_MISCONFIGURED,
                f"Template {template_id} analysis not ready",
            )
        recipe_data = template.recipe_cached
        audio_gcs_path = template.audio_gcs_path
        voiceover_gcs_path = template.voiceover_gcs_path

    _run_single_video_job(
        job_id=job_id,
        template_id=template_id,
        recipe_data=recipe_data,
        clip_paths_gcs=clip_paths_gcs,
        audio_gcs_path=audio_gcs_path,
        voiceover_gcs_path=voiceover_gcs_path,
        user_subject=user_subject,
        selected_platforms=selected_platforms,
        preview_mode=preview_mode,
    )


def _mark_failed(job_uuid: uuid.UUID, reason: str, message: str) -> None:
    """Persist processing_failed + structured failure_reason in one place."""
    with _sync_session() as db:
        job = db.get(Job, job_uuid)
        if job:
            job.status = "processing_failed"
            job.error_detail = message[:1000] if message else None
            job.failure_reason = reason
            db.commit()
    # Clear current_phase and stamp finished_at so the progress UI shows a
    # terminal state instead of frozen mid-phase. Best-effort and idempotent.
    mark_failed_phase(job_uuid)
