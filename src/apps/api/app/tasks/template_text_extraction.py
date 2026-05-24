"""Bridge between TemplateTextAgent and the agentic-template build flow.

`extract_template_text_overlays` runs the agent against an already-uploaded
template video and merges its output back into `recipe.slots[*].text_overlays`,
replacing whatever `template_recipe` originally emitted for the text-overlay
field. Position/timing fidelity is sourced from this agent going forward.

Scoped to the agentic pipeline ONLY (not manual templates, not music jobs).
Manual templates keep the recipe-agent overlays unchanged; music jobs have no
source template to extract text from.

The text-agent overlay dicts include all renderer-required fields plus the new
required `text_bbox` (the dropped-optional version from template_recipe). The
renderer reads bbox via the existing optional code path; consumption of bbox
as the PRIMARY positioning input is a follow-up PR.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.template_text import (
    TemplateTextAgent,
    TemplateTextInput,
    TemplateTextOverlay,
)
from app.config import settings
from app.pipeline.overlay_pacing import normalize_slot_overlay_pacing
from app.pipeline.text_overlay_v2.constants import (
    LAYER2_RENDER_TEXT_SIZE as _LAYER2_UNIFORM_TEXT_SIZE,
)
from app.pipeline.text_overlay_v2.constants import (
    LAYER2_RENDER_TEXT_SIZE_PX as _LAYER2_UNIFORM_TEXT_SIZE_PX,
)

log = structlog.get_logger()


def _build_slot_boundaries(slots: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Convert recipe slot durations to (start_s, end_s) tuples on a global timeline.

    The cumulative sum starts at 0 — interstitial holds between slots are
    ignored. The agent receives the boundaries as a hint for slot attribution;
    a small interstitial-aware offset is not worth the schema noise here.
    """
    boundaries: list[tuple[float, float]] = []
    cursor = 0.0
    for slot in slots:
        try:
            dur = float(slot.get("target_duration_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0:
            # Skip degenerate slots — the agent's clamping logic will assign
            # any overlay landing here to the closest valid slot.
            continue
        boundaries.append((cursor, cursor + dur))
        cursor += dur
    return boundaries


# Uniform Layer-2 render size and left margin. `_LAYER2_UNIFORM_TEXT_SIZE` /
# `_LAYER2_UNIFORM_TEXT_SIZE_PX` are imported at module top from
# `app.pipeline.text_overlay_v2.constants` so the cumulative-emit
# line-overflow split measures widths at the same size this bridge renders.
_LAYER2_UNIFORM_LEFT_MARGIN_FRAC = 0.05


def _overlay_to_recipe_dict(overlay: TemplateTextOverlay) -> dict[str, Any]:
    """Convert a TemplateTextOverlay to the renderer's slot-overlay dict shape.

    Layer-2 overlays render uniformly: every overlay gets the same large font
    size (120 px), a hard 5% left-edge anchor, and `text_anchor="left"`. Only
    the vertical position (`position_y_frac` from `bbox.y_norm`) and per-overlay
    `text` / `start_s` / `end_s` / `font_color_hex` / `effect` vary across
    overlays. This replaces the prior per-overlay `size_class` + role-based
    sizing path that produced visibly inconsistent overlays (different sizes
    per text block, centered text clipping off-screen on long phrases).

    The `_layer2_uniform` sentinel keys downstream agentic styling (text_designer
    and _BODY_CONFIG) to skip these overlays — see `_classify_overlay` in
    `agentic_template_build.py`. Without that gate, text_designer would clobber
    `text_size` with its own per-overlay choice.

    `start_s` / `end_s` / `text_bbox.sample_frame_t` are emitted as raw global
    values here. The caller in `_merge_overlays_into_slots` subtracts the
    slot's start from all three, so the resulting dict is internally
    consistent (slot-relative start <= sample_frame_t <= slot-relative end).
    """
    pop_suffix = getattr(overlay, "pop_animated_suffix", None)

    out: dict[str, Any] = {
        "role": overlay.role,
        "effect": overlay.effect,
        "position": _bbox_to_named_position(overlay.bbox.y_norm),
        "sample_text": overlay.sample_text,
        # Caller writes slot-relative timing for all three fields below.
        "start_s": overlay.start_s,
        "end_s": overlay.end_s,
        "text_bbox": {
            "x_norm": overlay.bbox.x_norm,
            "y_norm": overlay.bbox.y_norm,
            "w_norm": overlay.bbox.w_norm,
            "h_norm": overlay.bbox.h_norm,
            "sample_frame_t": overlay.bbox.sample_frame_t,
        },
        "font_color_hex": overlay.font_color_hex,
        "has_darkening": False,
        "has_narrowing": False,
        # Stage F's per-overlay size_class. Renderer ignores it (the uniform
        # text_size/text_size_px below take effect), but the eval-fixture
        # export script reads this back to reconstruct the agent's original
        # TemplateTextOverlay for round-trip replay.
        "font_size_hint": overlay.size_class,
        # Uniform Layer-2 styling — same for every overlay.
        "text_size": _LAYER2_UNIFORM_TEXT_SIZE,
        "text_size_px": _LAYER2_UNIFORM_TEXT_SIZE_PX,
        "text_anchor": "left",
        "position_x_frac": _LAYER2_UNIFORM_LEFT_MARGIN_FRAC,
        "position_y_frac": overlay.bbox.y_norm,
        # Sentinel: agentic_template_build._classify_overlay returns None when
        # this is set so neither _BODY_CONFIG nor text_designer can override
        # the uniform fields above.
        "_layer2_uniform": True,
        # Preserved for debugging — renderer ignores.
        "_extracted_by": "nova.compose.template_text",
    }

    # `pop_animated_suffix` only emitted when set — None means the renderer
    # uses its default behavior (pop the whole text, not just the suffix).
    if pop_suffix:
        out["pop_animated_suffix"] = pop_suffix

    return out


def _bbox_to_named_position(y_norm: float) -> str:
    """Derive top/center/bottom from y_norm so renderer's named-position
    fallback continues to work until the follow-up PR consumes bbox directly.

    Buckets: y < 0.33 → top, y > 0.67 → bottom, else center. Matches the
    visual intent of template_recipe's 3-bucket enum.
    """
    if y_norm < 0.33:
        return "top"
    if y_norm > 0.67:
        return "bottom"
    return "center"


def _merge_overlays_into_slots(
    slots: list[dict[str, Any]],
    overlays: list[TemplateTextOverlay],
) -> int:
    """Replace each slot's `text_overlays` with the matching extracted overlays.

    Overlays are bucketed by slot_index (1-indexed); slot durations are used to
    convert global timestamps into slot-relative ones (the renderer's existing
    timing math expects slot-relative). Returns the number of overlays merged.

    Empty input replaces with `[]` — this is the intended behavior. The recipe
    agent's overlays are NOT preserved; the text agent is the source of truth
    for the agentic build path.
    """
    by_slot: dict[int, list[TemplateTextOverlay]] = {}
    for ov in overlays:
        by_slot.setdefault(ov.slot_index, []).append(ov)

    merged_count = 0
    cursor = 0.0
    for i, slot in enumerate(slots, start=1):
        try:
            dur = float(slot.get("target_duration_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            dur = 0.0

        slot_overlays = by_slot.get(i, [])
        rendered: list[dict[str, Any]] = []
        for ov in slot_overlays:
            d = _overlay_to_recipe_dict(ov)
            # Convert global → slot-relative timing. Clamp to [0, slot_dur].
            d["start_s"] = max(0.0, ov.start_s - cursor)
            d["end_s"] = max(d["start_s"] + 0.01, ov.end_s - cursor)
            if dur > 0:
                # Don't extend past the slot — the renderer's cross-slot merge
                # handles texts that legitimately span boundaries when the
                # same text+position appears on the neighboring slot.
                d["end_s"] = min(d["end_s"], dur)
            # Convert sample_frame_t to slot-relative too, then clamp into
            # the now-clamped overlay window. Without this clamp, an overlay
            # whose end_s got pulled in by the slot boundary could end up
            # with sample_frame_t outside its own [start_s, end_s], breaking
            # the existing _validate_text_bbox invariant.
            slot_relative_t = max(0.0, ov.bbox.sample_frame_t - cursor)
            d["text_bbox"]["sample_frame_t"] = min(max(slot_relative_t, d["start_s"]), d["end_s"])
            rendered.append(d)

        # Enforce a per-word legibility floor and redistribute within the slot's
        # fixed duration, so cumulative reveals never flash by faster than the
        # eye can read (prod 89cde014: "the work to get there." crammed 5 words
        # into 431 ms; "and good timing so..." showed for 25 ms). No-op for
        # already-readable slots.
        if dur > 0 and rendered:
            rendered, pacing_warns = normalize_slot_overlay_pacing(rendered, slot_duration_s=dur)
            if any(pacing_warns.get(k) for k in ("stages_expanded", "singletons_expanded")):
                log.info(
                    "layer2_legibility_pass", slot_index=i, slot_duration_s=dur, **pacing_warns
                )

        slot["text_overlays"] = rendered
        merged_count += len(rendered)
        if dur > 0:
            cursor += dur

    return merged_count


def extract_template_text_overlays(
    file_ref: Any,
    recipe: Any,
    *,
    job_id: str | None = None,
    force_layer2: bool = False,
    gcs_path: str | None = None,
    transcript_words: list[dict] | None = None,
    transcript_failed: bool = False,
) -> tuple[bool, int]:
    """Run TemplateTextAgent and merge results into recipe.slots in place.

    Returns ``(success, overlay_count)``.

      - ``success=True, count=N`` → agent ran cleanly, N overlays merged
        (N may be 0 for genuinely text-free templates).
      - ``success=False, count=0`` → agent failed or was skipped; the
        recipe's slots still carry whatever overlays template_recipe
        originally returned, which is no worse than today's behavior.

    Callers MUST gate `set_cached_recipe()` on ``success=True``. Caching a
    recipe with ``success=False`` would pin a hybrid state (template-recipe
    overlays under a cache key that promises template-text overlays) for the
    cache TTL, so future hits would serve stale data indefinitely. The
    success flag is the load-bearing signal.

    Cache coherence: with ``success=True``, callers can safely cache. A cache
    hit therefore serves overlays from this agent; a cache miss runs both
    agents and caches the merged result. `TEMPLATE_PROMPT_VERSION` is bumped
    when this agent ships so pre-existing caches (which lack text-agent
    overlays) are invalidated automatically.

    ``transcript_failed`` is the load-bearing failure-mode signal for the
    Layer-2 path: when the caller's transcribe step degraded
    (low_confidence, terminal_refusal, raised exception), Stage E cannot
    ground OCR against ground-truth Whisper words, and the Layer-2 pipeline
    falls back to its music-only passthrough — which emits raw OCR
    artifacts ("luck\\"", "W", duplicated tokens). To prevent that garbage
    from overwriting template_recipe's clean phrases, this function
    short-circuits to ``(False, 0)`` BEFORE running the agent when
    ``transcript_failed`` is True and Layer-2 routing is active. Layer-1
    callers are unaffected (no transcript dependency). Prod incident:
    template 89cde014 on 2026-05-22 (transcript terminal_refusal →
    "luck\\""/"W" rendered to pixels).
    """
    if not file_ref or not getattr(file_ref, "uri", None):
        log.warning(
            "template_text_extraction_skipped_no_file_ref",
            job_id=job_id,
        )
        return False, 0
    if not recipe.slots:
        log.info("template_text_extraction_skipped_no_slots", job_id=job_id)
        return False, 0

    # Hard guard: Layer-2 needs a working transcript to ground Stage E. Without
    # it, OCR garbage passes through and clobbers template_recipe's overlays.
    # Skip the agent entirely so recipe.slots keep template_recipe's text and
    # the caller's `text_success=False` branch skips the cache write.
    if transcript_failed and (force_layer2 or settings.text_overlay_v2_enabled):
        log.warning(
            "template_text_extraction_skipped_transcript_failed",
            job_id=job_id,
            force_layer2=force_layer2,
        )
        return False, 0

    boundaries = _build_slot_boundaries(recipe.slots)
    if not boundaries:
        log.warning(
            "template_text_extraction_skipped_no_boundaries",
            job_id=job_id,
            slot_count=len(recipe.slots),
        )
        return False, 0

    agent = TemplateTextAgent(default_client())
    ctx = RunContext(job_id=job_id)
    # `transcript_words` is what makes Stage E of the Layer-2 pipeline actually
    # run. Without it the alignment LLM short-circuits via its empty-transcript
    # early return and OCR garbage (duplicated tokens, stray characters)
    # passes through unchanged. Caller (agentic_template_build) is responsible
    # for sourcing the transcript; we default to [] so non-Layer-2 callers and
    # tests are unaffected.
    inp = TemplateTextInput(
        file_uri=file_ref.uri,
        file_mime=getattr(file_ref, "mime_type", None) or "video/mp4",
        slot_boundaries_s=boundaries,
        force_layer2=force_layer2,
        gcs_path=gcs_path,
        transcript_words=transcript_words or [],
    )

    try:
        out = agent.run(inp, ctx=ctx)
    except TerminalError as exc:
        log.warning(
            "template_text_extraction_failed",
            job_id=job_id,
            error=str(exc),
        )
        return False, 0

    merged = _merge_overlays_into_slots(recipe.slots, out.overlays)
    log.info(
        "template_text_overlays_merged",
        job_id=job_id,
        overlay_count=merged,
        agent_returned=len(out.overlays),
    )
    return True, merged
