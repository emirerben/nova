"""Agentic template build — runs the full agent stack to produce a recipe.

Counterpart to `analyze_template_task` for templates with `is_agentic=True`.
Manual templates keep using `analyze_template_task`; the two never interact.

Flow:
  1. download template video from GCS
  2. detect black segments (for interstitial placement)
  3. upload to Gemini, run `analyze_template(analysis_mode="single")` — the
     `TemplateRecipeAgent` produces the structural recipe (slots, transitions,
     overlay placeholders) in a single Gemini call
  4. per slot, per label-like overlay, call `text_designer` and BAKE the
     returned styling into the overlay dict so the job-time pipeline reads
     it directly instead of falling back to the static _LABEL_CONFIG
  5. enrich slots with beat-density energy (same as manual path)
  6. persist recipe_cached + write a TemplateRecipeVersion row

`transition_picker`, `shot_ranker`, and `clip_router` are NOT called here —
they need user-clip metadata which only exists at job time. Those agents are
invoked from the is_agentic branch in `template_orchestrate.py`.

Trigger: this task is enqueued by
  - `POST /admin/templates` when is_agentic=True
  - `POST /admin/templates/from-url` when is_agentic=True
  - `POST /admin/templates/{id}/reanalyze-agentic`
"""

from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import redis as redis_lib
import structlog
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import TemplateRecipeVersion, VideoTemplate
from app.pipeline.agents.gemini_analyzer import (
    GeminiAnalysisError,
    GeminiRefusalError,
    analyze_template,
    gemini_upload_and_wait,
)
from app.pipeline.template_cache import (
    compute_template_hash,
    get_cached_recipe,
    set_cached_recipe,
)
from app.services.template_poster import (
    PosterExtractionError,
)
from app.services.template_poster import (
    generate_and_upload as generate_poster,
)
from app.storage import download_to_file, upload_public_read
from app.tasks.template_orchestrate import (
    _detect_audio_beats,
    _enrich_slots_with_energy,
    _extract_template_audio,
    _is_subject_placeholder,
    _merge_beat_sources,
)
from app.worker import celery_app

log = structlog.get_logger()

# Mirror analyze_template_task's requeue guard — 3 attempts within 1h.
_MAX_ATTEMPTS = 3
_ATTEMPT_TTL_S = 3600


def _classify_overlay(overlay: dict) -> str | None:
    """Return text_designer placeholder_kind for label-like overlays.

    Mirrors the detection logic in template_orchestrate._collect_absolute_overlays
    so an agentic build flags exactly the same overlays the manual path would
    style via _LABEL_CONFIG. Returns None for overlays that are not labels
    (text_designer doesn't apply to body text, captions, etc.).
    """
    role = overlay.get("role", "")
    sample_text = overlay.get("sample_text") or overlay.get("text") or ""
    is_subject = _is_subject_placeholder(sample_text)
    is_label_like = role == "label" or is_subject or sample_text.lower().startswith("welcome")
    if not is_label_like:
        return None
    return "subject" if is_subject else "prefix"


def _bake_text_designer_into_overlay(
    overlay: dict,
    designer_output: object,
) -> None:
    """Write text_designer fields into an overlay dict in place.

    These fields override anything template_recipe set, and the job-time
    pipeline's is_agentic branch will skip the _LABEL_CONFIG override block,
    so what the agent decides here is what ships.
    """
    overlay["text_size"] = designer_output.text_size
    overlay["font_style"] = designer_output.font_style
    overlay["text_color"] = designer_output.text_color
    overlay["effect"] = designer_output.effect
    overlay["start_s"] = float(designer_output.start_s)
    if designer_output.accel_at_s is not None:
        overlay["font_cycle_accel_at_s"] = float(designer_output.accel_at_s)


_TEXT_DESIGNER_PARALLEL_CAP = 8


def _run_text_designer_on_slots(
    slots: list[dict],
    copy_tone: str,
    creative_direction: str,
    job_id: str,
) -> int:
    """Call text_designer for every label-like overlay; bake results in place.

    Returns the number of overlays styled. Parallelized across a bounded pool
    (`_TEXT_DESIGNER_PARALLEL_CAP`) because on a 20-overlay template a sequential
    loop spent ~60s of single-threaded LLM wait per agentic reanalyze. Workers
    only issue the LLM call; the main thread mutates each overlay dict after
    `future.result()` so there is no shared-state contention. Per-overlay
    `TerminalError` is isolated: the overlay keeps whatever `template_recipe`
    set, and the rest of the batch still bakes.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext, TerminalError  # noqa: PLC0415
    from app.agents.text_designer import (  # noqa: PLC0415
        TextDesignerAgent,
        TextDesignerInput,
    )

    agent = TextDesignerAgent(default_client())
    ctx = RunContext(job_id=job_id)

    work_items: list[tuple[dict, int, str, str, TextDesignerInput]] = []
    for slot in slots:
        slot_position = int(slot.get("position", 0)) or 1  # text_designer needs ≥ 1
        slot_type = str(slot.get("slot_type", "broll"))
        for overlay in slot.get("text_overlays", []):
            kind = _classify_overlay(overlay)
            if kind is None:
                continue
            work_items.append(
                (
                    overlay,
                    slot_position,
                    slot_type,
                    kind,
                    TextDesignerInput(
                        slot_position=slot_position,
                        slot_type=slot_type,
                        placeholder_kind=kind,
                        copy_tone=copy_tone,
                        creative_direction=creative_direction,
                    ),
                )
            )

    if not work_items:
        return 0

    def _call(agent_input: TextDesignerInput) -> object:
        return agent.run(agent_input, ctx=ctx)

    baked = 0
    with ThreadPoolExecutor(
        max_workers=min(len(work_items), _TEXT_DESIGNER_PARALLEL_CAP),
    ) as pool:
        futures = {pool.submit(_call, item[4]): item for item in work_items}
        for future in as_completed(futures):
            overlay, slot_position, _slot_type, kind, _agent_input = futures[future]
            try:
                out = future.result()
            except TerminalError as exc:
                log.warning(
                    "text_designer_failed",
                    job_id=job_id,
                    slot_position=slot_position,
                    placeholder_kind=kind,
                    error=str(exc),
                )
                continue
            _bake_text_designer_into_overlay(overlay, out)
            baked += 1

    return baked


def _apply_font_default_to_overlays(recipe) -> int:
    """Set overlay.font_family to recipe.font_default for any overlay that
    hasn't been assigned a font yet. Returns the count baked.

    Called between identify_fonts (which sets recipe.font_default) and
    _run_text_designer_on_slots (which may override font_family per overlay
    for label-like content). The renderer's resolution chain reads
    `overlay.font_family → overlay.font_style → registry default
    (Playfair Display)` — it does NOT consult `recipe.font_default`. Without
    this baking step, agentic templates with a populated font_default still
    render every overlay in Playfair because nothing wires the template-level
    field down to the per-overlay one.

    Idempotent and side-effect-free when font_default is empty (font ID found
    no above-floor match) or when every overlay already has font_family set.
    Existing font_family values are never overwritten — text_designer's
    downstream per-overlay choice always wins, and any human-authored override
    in the editor JSON stays put on re-analysis.
    """
    font_default = getattr(recipe, "font_default", "") or ""
    if not font_default:
        return 0
    applied = 0
    for slot in getattr(recipe, "slots", []) or []:
        for overlay in slot.get("text_overlays", []) or []:
            if overlay.get("font_family"):
                continue
            overlay["font_family"] = font_default
            applied += 1
    if applied:
        log.info(
            "font_default_baked_into_overlays",
            font_default=font_default,
            overlay_count=applied,
        )
    return applied


@celery_app.task(
    name="tasks.agentic_template_build_task",
    bind=True,
    max_retries=0,
    soft_time_limit=1500,  # ~25 min — Big 3 + N text_designer calls
    time_limit=1560,
)
def agentic_template_build_task(self, template_id: str) -> None:
    """Build a full recipe end-to-end using agents. No human edits.

    Mirrors `analyze_template_task` so the manual path is unchanged; the only
    difference is the extra text_designer pass per slot before persistence.
    """
    log.info("agentic_template_build_start", template_id=template_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "template.mp4")

        # Requeue guard — share key space with analyze_template_task so a
        # template can't bypass the limit by toggling its build path.
        _redis = redis_lib.from_url(settings.redis_url)
        attempt_key = f"analyze_attempts:{template_id}"
        attempts = _redis.incr(attempt_key)
        _redis.expire(attempt_key, _ATTEMPT_TTL_S)

        if attempts > _MAX_ATTEMPTS:
            log.error(
                "agentic_template_build_max_attempts",
                template_id=template_id,
                attempts=attempts,
            )
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = (
                        f"Exceeded max agentic build attempts ({attempts}). "
                        "Template may be too large, trigger safety filters, or "
                        "an agent in the chain is consistently failing."
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
                if not template.is_agentic:
                    # Defensive: an op may have hand-flipped this row via raw
                    # SQL. The orchestrator is agent-only by contract — refuse
                    # rather than silently writing a recipe that the job-time
                    # pipeline will treat as manual (drift).
                    log.error(
                        "agentic_build_on_non_agentic_template",
                        template_id=template_id,
                    )
                    template.analysis_status = "failed"
                    template.error_detail = (
                        "Agentic build invoked on a non-agentic template. "
                        "Set is_agentic=true or use analyze_template_task."
                    )
                    db.commit()
                    return
                gcs_path = template.gcs_path
                existing_audio_gcs = template.audio_gcs_path
                template.analysis_status = "analyzing"
                template.error_detail = None
                db.commit()

            if not gcs_path:
                raise GeminiAnalysisError(
                    "Template has no gcs_path — cannot download source video."
                )

            download_to_file(gcs_path, local_path)

            # Reuse the same black-segment detection as the manual path so
            # interstitial placement is identical between build modes.
            from app.pipeline.interstitials import (  # noqa: PLC0415
                classify_black_segment_type,
                detect_black_segments,
            )

            black_segments = detect_black_segments(local_path)
            black_segments = classify_black_segment_type(local_path, black_segments)

            # Phase 3 perf: content-hash the source video and check Redis before
            # uploading to the Gemini File API. On hit we skip both the upload
            # (~30-60s for a 1080p template — ACTIVE-poll bound) and the actual
            # `analyze_template` LLM call. identify_fonts, text_designer, poster,
            # and audio extraction still run on the local copy.
            _AGENTIC_ANALYSIS_MODE = "single"
            template_hash = compute_template_hash(local_path)
            recipe = None
            if template_hash is not None:
                cached = get_cached_recipe(template_hash, _AGENTIC_ANALYSIS_MODE)
                if cached is not None:
                    log.info(
                        "agentic_template_recipe_cache_hit",
                        template_id=template_id,
                        template_hash=template_hash[:12],
                    )
                    recipe = cached

            if recipe is None:
                file_ref = gemini_upload_and_wait(local_path)
                # Phase 2 perf: single-pass skips the inline `_extract_creative_direction`
                # Gemini call (Pass 1). `recipe.creative_direction` is still populated —
                # it now comes from the structural JSON itself, set by `TemplateRecipeAgent`
                # via `analyze_template_schema.txt` which requires the field. Downstream
                # agents (text_designer, clip_router, shot_ranker) read a real
                # model-generated creative_direction in both regimes; what changes is just
                # the source (recipe-embedded vs separate Pass-1 paragraph). To restore the
                # standalone Pass-1 call set "two_pass" here.
                recipe = analyze_template(
                    file_ref,
                    analysis_mode=_AGENTIC_ANALYSIS_MODE,
                    black_segments=black_segments,
                    job_id=f"template:{template_id}:agentic",
                )
                if template_hash is not None:
                    set_cached_recipe(template_hash, _AGENTIC_ANALYSIS_MODE, recipe)

            # Font identification (PR2). Best-effort: a font-id failure must
            # not abort agentic build. Mutates `recipe.slots[*]["text_overlays"]
            # [*]["font_alternatives"]` and `recipe.font_default` in place.
            # Mirrors the wiring in template_orchestrate.py's manual analysis
            # path — agentic templates were missed in PR #150 and never saw
            # font_alternatives populated until this fix.
            try:
                from app.pipeline.font_identification import identify_fonts  # noqa: PLC0415
                from app.services.clip_font_matcher import get_matcher  # noqa: PLC0415

                identify_fonts(recipe, local_path, get_matcher())
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "font_identification_failed",
                    template_id=template_id,
                    error=str(exc),
                )

            # Bake the template-level `font_default` into every overlay whose
            # `font_family` is not already set. Without this step the renderer's
            # resolution chain (`font_family → font_style → registry default`)
            # falls through to Playfair Display Bold even when font_default is
            # populated on the recipe — `text_overlay.py` never consults the
            # template-level field. Doing this AT BUILD TIME (before
            # text_designer runs) means: (1) text_designer is free to override
            # per overlay if it has a more nuanced choice, and (2) overlays
            # text_designer skips (e.g. non-label content with role=hook) still
            # get the CLIP-identified font instead of Playfair. Only baked when
            # font_default is non-empty (font ID found at least one
            # above-floor match across all overlays); otherwise the overlay
            # stays untouched and the renderer chain handles fallback.
            _apply_font_default_to_overlays(recipe)

            # Per-slot text_designer pass — bakes typography into overlays.
            baked = _run_text_designer_on_slots(
                recipe.slots,
                copy_tone=recipe.copy_tone,
                creative_direction=recipe.creative_direction,
                job_id=f"template:{template_id}:agentic",
            )
            log.info(
                "agentic_text_designer_baked",
                template_id=template_id,
                overlays_styled=baked,
            )

            # Poster + audio extraction — identical to manual path.
            poster_gcs: str | None = None
            try:
                poster_gcs = generate_poster(template_id, local_path)
            except PosterExtractionError as exc:
                log.warning(
                    "template_poster_extraction_failed",
                    template_id=template_id,
                    error=str(exc),
                )
            except Exception as exc:
                log.warning(
                    "template_poster_upload_failed",
                    template_id=template_id,
                    error=str(exc),
                )

            audio_gcs: str | None = existing_audio_gcs
            audio_local = os.path.join(tmpdir, "audio.m4a")
            if not existing_audio_gcs:
                if _extract_template_audio(local_path, audio_local):
                    audio_gcs = f"templates/{template_id}/audio.m4a"
                    upload_public_read(audio_local, audio_gcs)
                    log.info("template_audio_extracted", template_id=template_id)
            elif not os.path.exists(audio_local):
                try:
                    download_to_file(existing_audio_gcs, audio_local)
                except Exception as exc:
                    log.warning("template_audio_redownload_failed", error=str(exc))

            ffmpeg_beats = _detect_audio_beats(audio_local) if os.path.exists(audio_local) else []
            merged_beats = _merge_beat_sources(recipe.beat_timestamps_s, ffmpeg_beats)
            enriched_slots = _enrich_slots_with_energy(recipe.slots, merged_beats)

            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template is None:
                    log.error("template_disappeared", template_id=template_id)
                    return

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
                    "font_default": recipe.font_default,
                }

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
                log.info(
                    "agentic_recipe_version_created",
                    template_id=template_id,
                    trigger=trigger,
                    overlays_styled=baked,
                )

            _redis.delete(attempt_key)
            log.info(
                "agentic_template_build_done",
                template_id=template_id,
                slots=len(recipe.slots),
                overlays_styled=baked,
            )

        except SoftTimeLimitExceeded:
            log.error("agentic_template_build_timeout", template_id=template_id)
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = (
                        "Agentic build timed out. The agent chain (Big 3 + "
                        "text_designer per slot) ran longer than the soft "
                        "time limit."
                    )
                    db.commit()

        except (GeminiRefusalError, GeminiAnalysisError) as exc:
            log.error(
                "agentic_template_build_gemini_error",
                template_id=template_id,
                error=str(exc),
            )
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = f"Agentic build failed: {exc}"
                    db.commit()

        except Exception as exc:
            log.exception(
                "agentic_template_build_unexpected_error",
                template_id=template_id,
            )
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    template.error_detail = (
                        f"Agentic build crashed unexpectedly: {type(exc).__name__}"
                    )
                    db.commit()
            raise

        finally:
            _redis.close()
