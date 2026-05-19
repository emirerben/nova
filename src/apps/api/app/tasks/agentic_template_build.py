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
from sqlalchemy.exc import OperationalError

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
    AGENT_SET_RECIPE_PLUS_TEXT,
    _resolve_text_overlay_version,
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
from app.tasks.template_text_extraction import (
    extract_template_text_overlays,
)
from app.worker import celery_app

log = structlog.get_logger()

# Mirror analyze_template_task's requeue guard — 3 attempts within 1h.
_MAX_ATTEMPTS = 3
_ATTEMPT_TTL_S = 3600


def _classify_overlay(overlay: dict) -> str | None:
    """Return text_designer placeholder_kind for overlays that need styling.

    Label-like overlays (role=label, subject placeholder, welcome prefix) map
    to "subject" or "prefix" — same detection as template_orchestrate._collect_absolute_overlays.

    Layer-2 body overlays (role in {hook, reaction, cta}) map to "body" so
    text_designer can apply conservative size/effect defaults instead of
    letting the renderer fall back to Playfair Bold at default size. The body
    config is applied deterministically (no LLM call) in _run_text_designer_on_slots.

    Role specificity: when a Layer-2 role (hook/reaction/cta) is explicitly set,
    that classification wins over the subject/prefix heuristics. Short capitalized
    Layer-2 phrases like "It's" would otherwise be mis-classified as subject
    placeholders by _is_subject_placeholder, causing them to go through the LLM
    path and lose stage F's classified effect. Explicit role beats heuristics.

    Returns None for overlays that need no styling pass (e.g. raw captions).
    """
    role = overlay.get("role", "")
    # Layer-2 explicit roles take priority — check before subject/prefix heuristics.
    if role in {"hook", "reaction", "cta"}:
        return "body"
    sample_text = overlay.get("sample_text") or overlay.get("text") or ""
    is_subject = _is_subject_placeholder(sample_text)
    is_label_like = role == "label" or is_subject or sample_text.lower().startswith("welcome")
    if is_label_like:
        return "subject" if is_subject else "prefix"
    return None


# Deterministic styling defaults for Layer-2 body overlays (role=hook/reaction/cta).
# Applied without an LLM call — mirrors the _LABEL_CONFIG pattern in template_orchestrate.py.
# These are conservative defaults: stage F's classified `effect` and `font_color_hex` are
# preserved (not overridden) because they reflect the agent's best-guess for that phrase.
# Only `text_size` and `font_style` are set here since stage F does not classify those.
_BODY_CONFIG: dict[str, dict] = {
    # Hook phrases appear in the first 2-3 seconds; large + bold captures attention.
    "hook": {
        "text_size": "large",
        "font_style": "sans",
    },
    # Reaction phrases are mid-template body text; medium keeps them readable not dominant.
    "reaction": {
        "text_size": "medium",
        "font_style": "sans",
    },
    # CTA phrases close the video; large signals the call-to-action without mimicking hook.
    "cta": {
        "text_size": "large",
        "font_style": "sans",
    },
}


def _bake_text_designer_into_overlay(
    overlay: dict,
    designer_output: object,
) -> None:
    """Write text_designer fields into an overlay dict in place.

    These fields override anything template_recipe set, and the job-time
    pipeline's is_agentic branch will skip the _LABEL_CONFIG override block,
    so what the agent decides here is what ships.

    Timing invariant: text_designer proposes a ``start_s`` (e.g. 3.0 for
    subject labels on the first slot) without knowing the source text's
    visible window. If the overlay's ``end_s`` is smaller than the proposed
    ``start_s`` (e.g. end_s=0.9 from text-extraction, start_s=3.0 from
    designer calibration), the write would produce an inverted overlay that
    downstream renderers reject. We clamp ``start_s`` to ``end_s - 0.01``
    after the write so the stored overlay always satisfies
    ``0 <= start_s < end_s``.

    Band-aid note: the underlying semantic gap is that "designer start_s" and
    "source-extraction start_s" are different concepts sharing one field —
    a proper refactor (separate fields, resolved at job time) is tracked as a
    follow-up. This clamp matches the pattern in PR #198/#200
    (template_text.parse) and PR #200 (unconditional sample_frame_t clamp).
    """
    overlay["text_size"] = designer_output.text_size
    overlay["font_style"] = designer_output.font_style
    overlay["text_color"] = designer_output.text_color
    overlay["effect"] = designer_output.effect
    overlay["start_s"] = float(designer_output.start_s)
    # Clamp: designer's suggested start_s must not exceed (or equal) end_s.
    # end_s comes from the text-extraction pass and reflects the actual visible
    # window; text_designer doesn't know it, so inversion is possible.
    end_s = float(overlay.get("end_s", overlay["start_s"] + 0.01))
    if overlay["start_s"] >= end_s:
        clamped_from = overlay["start_s"]
        overlay["start_s"] = max(0.0, end_s - 0.01)
        log.warning(
            "text_designer_bake_start_s_clamped",
            sample_text=overlay.get("sample_text"),
            start_s_clamped_from=clamped_from,
            start_s_clamped_to=overlay["start_s"],
            end_s=end_s,
        )
    if designer_output.accel_at_s is not None:
        overlay["font_cycle_accel_at_s"] = float(designer_output.accel_at_s)


_TEXT_DESIGNER_PARALLEL_CAP = 8


def _apply_body_config_to_overlay(overlay: dict) -> None:
    """Apply _BODY_CONFIG defaults to a Layer-2 body overlay in place.

    Only sets fields that stage F (text_classification) does not already
    classify: text_size and font_style. Preserves stage F's `effect` and
    `font_color_hex` (stored as `text_color` on the overlay) so the agent's
    best-guess for each phrase is respected.

    No LLM call — deterministic, zero latency, consistent with how the manual
    path applies _LABEL_CONFIG in template_orchestrate.py.
    """
    role = overlay.get("role", "reaction")
    config = _BODY_CONFIG.get(role, _BODY_CONFIG["reaction"])
    # Only fill fields stage F doesn't classify; do NOT clobber what it set.
    overlay.setdefault("text_size", config["text_size"])
    overlay.setdefault("font_style", config["font_style"])


def _run_text_designer_on_slots(
    slots: list[dict],
    copy_tone: str,
    creative_direction: str,
    job_id: str,
) -> int:
    """Style every routable overlay; bake results in place.

    Label-like overlays (kind="prefix"/"subject") go through the text_designer
    LLM agent — typography decisions are nuanced and benefit from model judgment.
    The LLM calls are parallelized across a bounded pool (`_TEXT_DESIGNER_PARALLEL_CAP`)
    because on a 20-overlay template a sequential loop spent ~60s of single-threaded
    LLM wait per agentic reanalyze. Workers only issue the LLM call; the main thread
    mutates each overlay dict after `future.result()` so there is no shared-state
    contention. Per-overlay `TerminalError` is isolated: the overlay keeps whatever
    `template_recipe` set, and the rest of the batch still bakes.

    Layer-2 body overlays (kind="body", role in {hook, reaction, cta}) get
    deterministic styling via `_BODY_CONFIG` — no LLM call. Stage F already
    classified `effect` and `font_color_hex` for these; _BODY_CONFIG fills
    only `text_size` and `font_style` which stage F does not produce.

    Returns the total number of overlays styled (LLM + deterministic).
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext, TerminalError  # noqa: PLC0415
    from app.agents.text_designer import (  # noqa: PLC0415
        TextDesignerAgent,
        TextDesignerInput,
    )

    agent = TextDesignerAgent(default_client())
    ctx = RunContext(job_id=job_id)

    # Separate body overlays (deterministic) from label overlays (LLM).
    body_overlays: list[dict] = []
    work_items: list[tuple[dict, int, str, str, TextDesignerInput]] = []

    for slot in slots:
        slot_position = int(slot.get("position", 0)) or 1  # text_designer needs ≥ 1
        slot_type = str(slot.get("slot_type", "broll"))
        for overlay in slot.get("text_overlays", []):
            kind = _classify_overlay(overlay)
            if kind is None:
                continue
            if kind == "body":
                body_overlays.append(overlay)
            else:
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

    # Apply deterministic body config first (no LLM, no latency).
    baked = 0
    for overlay in body_overlays:
        _apply_body_config_to_overlay(overlay)
        baked += 1

    if baked:
        log.debug(
            "text_designer_body_overlays_styled",
            job_id=job_id,
            count=baked,
        )

    if not work_items:
        return baked

    def _call(agent_input: TextDesignerInput) -> object:
        return agent.run(agent_input, ctx=ctx)

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


def _fetch_transcript_words_for_layer2(
    file_ref,
    *,
    template_id: str,
    use_layer2: bool,
) -> list[dict]:
    """Best-effort transcript fetch for Stage E of the Layer-2 OCR pipeline.

    Without per-word timestamps the alignment LLM short-circuits via its
    empty-transcript early return and OCR garbage (duplicated tokens like
    "if you if you put put in" from prod template fdaf3bbc) passes through
    unchanged into the cached recipe.

    Gated on Layer-2 routing — the Gemini transcribe call only fires when
    Layer-2 will actually run (`use_layer2=True` admin override OR
    `settings.text_overlay_v2_enabled=True`). Layer-1 builds skip entirely
    so the Gemini cost isn't wasted on a transcript no consumer will read.

    A transcription failure must NOT abort the agentic build. Returns an
    empty list on any error; Stage E falls back to its existing
    empty-transcript passthrough.
    """
    if not (use_layer2 or settings.text_overlay_v2_enabled):
        return []
    try:
        from app.pipeline.agents.gemini_analyzer import transcribe  # noqa: PLC0415

        transcript = transcribe(file_ref, job_id=f"template:{template_id}:agentic")
        words = [{"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in transcript.words]
        log.info(
            "agentic_build_transcript_ready",
            template_id=template_id,
            word_count=len(words),
        )
        return words
    except (TypeError, AttributeError, ImportError, NameError):
        # Programming errors must not be silently degraded — they indicate
        # a refactor broke the transcribe() contract and every Layer-2 build
        # would invisibly fall back to empty-transcript passthrough (the
        # exact failure mode this helper was added to fix).
        raise
    except Exception as exc:  # noqa: BLE001
        # Transient failures (network, agent runtime, Gemini quota) fall back
        # to empty transcript so the build doesn't abort. Stage E handles
        # the empty case via its existing passthrough. error_type lets
        # log-based alerts distinguish quota errors from other transients.
        log.warning(
            "agentic_build_transcript_failed",
            template_id=template_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return []


@celery_app.task(
    name="tasks.agentic_template_build_task",
    bind=True,
    # Retry on transient Postgres outages (incident 2026-05-18 07:45:57Z).
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,  # deterministic exp backoff — jitter halves avg budget
    # 7 retries × deterministic exp backoff (1+2+4+8+16+32+60 = 123s).
    # retry_jitter=False so the budget is predictable, not half on average.
    # Covers the documented 65s nova-db VM stall (2026-05-18 07:45:57Z)
    # with ~2× safety margin. retry_backoff_max=60 caps any single delay.
    max_retries=7,
    soft_time_limit=1500,  # ~25 min — Big 3 + N text_designer calls
    time_limit=1560,
)
def agentic_template_build_task(self, template_id: str, *, use_layer2: bool = False) -> None:
    """Build a full recipe end-to-end using agents. No human edits.

    Mirrors `analyze_template_task` so the manual path is unchanged; the only
    difference is the extra text_designer pass per slot before persistence.

    `use_layer2` is a per-request override forwarded from the
    `reanalyze-agentic?use_layer2=true` admin endpoint. When True the
    text-extraction pass routes through the Layer-2 pipeline regardless of
    the global `text_overlay_v2_enabled` flag. Default False keeps existing
    behavior; callers that omit the kwarg are unaffected.
    """
    # Captured once at task entry and written to TemplateRecipeVersion.build_started_at
    # at the end of the happy path. Paired with the DB-generated `created_at` (end),
    # gives per-run wall-clock without relying on Langfuse trace aggregation.
    build_started_at = datetime.now(UTC)
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
            #
            # The cache is scoped by `text_overlay_version` so Layer-1 and Layer-2
            # entries coexist without collision. A ?use_layer2=true request that hits
            # a cached Layer-1 entry would otherwise short-circuit and silently
            # discard the override — the namespace separation prevents that.
            _AGENTIC_ANALYSIS_MODE = "single"
            text_overlay_version = _resolve_text_overlay_version(
                force_layer2=use_layer2,
                settings_flag=settings.text_overlay_v2_enabled,
            )
            template_hash = compute_template_hash(local_path)
            recipe = None
            if template_hash is not None:
                cached = get_cached_recipe(
                    template_hash,
                    _AGENTIC_ANALYSIS_MODE,
                    agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
                    text_overlay_version=text_overlay_version,
                )
                if cached is not None:
                    log.info(
                        "agentic_template_recipe_cache_hit",
                        template_id=template_id,
                        template_hash=template_hash[:12],
                        text_overlay_version=text_overlay_version,
                    )
                    recipe = cached
                else:
                    log.info(
                        "agentic_template_recipe_cache_miss",
                        template_id=template_id,
                        template_hash=template_hash[:12],
                        text_overlay_version=text_overlay_version,
                    )

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
                # Focused text-extraction pass — overwrites recipe.slots[*].text_overlays
                # with the dedicated text agent's output (every visible text, required
                # bbox, font color). Runs in agentic build ONLY; manual templates and
                # music jobs keep the recipe-agent overlays. Must run BEFORE the cache
                # write so a future hit serves the merged overlays. Agentic builds use
                # the `recipe+text` cache namespace so this never invalidates manual
                # caches.
                #
                # text_success gates the cache write: a failed text-extraction pass
                # leaves the recipe with recipe-agent overlays under a cache key that
                # promises text-agent overlays. Caching that would pin stale data for
                # the full TTL. Skip the cache on failure; the next reanalyze gets
                # another shot at producing the proper merged recipe.
                transcript_words = _fetch_transcript_words_for_layer2(
                    file_ref, template_id=template_id, use_layer2=use_layer2
                )

                text_success, _text_count = extract_template_text_overlays(
                    file_ref,
                    recipe,
                    job_id=f"template:{template_id}:agentic",
                    force_layer2=use_layer2,
                    gcs_path=gcs_path,
                    transcript_words=transcript_words,
                )
                if template_hash is not None and text_success:
                    set_cached_recipe(
                        template_hash,
                        _AGENTIC_ANALYSIS_MODE,
                        recipe,
                        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
                        text_overlay_version=text_overlay_version,
                    )

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
            # that were previously skipped (role=hook/reaction/cta) now receive
            # _BODY_CONFIG defaults via text_designer's body path, so they too
            # get the CLIP-identified font first and body-config text_size/font_style
            # second. Only baked when font_default is non-empty (font ID found at least
            # one above-floor match across all overlays); otherwise the overlay
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
                    build_started_at=build_started_at,
                )
                db.add(version)

                template.recipe_cached = recipe_dict
                template.recipe_cached_at = datetime.now(UTC)
                # Persist the live AgentSpec.prompt_version map so the admin UI
                # can flag this row as STALE if any agent's prompt rotates later.
                # See app/services/template_staleness.py for the rationale.
                from app.services.template_staleness import (  # noqa: PLC0415
                    capture_recipe_versions,
                )

                template.recipe_cached_versions = capture_recipe_versions(is_agentic=True)
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

        except OperationalError as db_exc:
            # Transient Postgres outage — re-raise for Celery autoretry.
            # Do NOT mark the template failed; that itself would hit the
            # still-down DB. Incident 2026-05-18 07:45:57Z.
            log.warning(
                "agentic_template_build_transient_db_error_retry",
                template_id=template_id,
                error=str(db_exc),
                retry_count=self.request.retries,
            )
            _redis.close()
            raise

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
