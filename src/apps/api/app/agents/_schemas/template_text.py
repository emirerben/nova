"""Schemas for nova.compose.template_text — focused text-overlay extraction.

Distinct from `template_recipe`'s slot-embedded `text_overlays` field. The
template-text agent runs as a SECOND focused Gemini pass on the same template
video and returns every visible on-screen text with a required normalized
bounding box, font color, and per-overlay timing.

The output is consumed by `agentic_template_build` which replaces each slot's
`text_overlays` with the slot-indexed overlays from this agent before
`text_designer` runs. Position fidelity rises from the 3-bucket named position
enum (top/center/bottom) to pixel-level normalized coordinates.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# ── Vocabulary ────────────────────────────────────────────────────────────────
# `role` mirrors template_recipe's enum so downstream code that branches on
# role (e.g. _LABEL_CONFIG in template_orchestrate.py) keeps working.
VALID_ROLES: frozenset[str] = frozenset({"hook", "reaction", "cta", "label"})

# `size_class` is a coarse hint used by the renderer when font_size_px is not
# explicitly provided. The bbox's `h_norm` is the precise signal; this is a
# fallback for the rare case where the agent can't measure height confidently.
VALID_SIZE_CLASSES: frozenset[str] = frozenset({"small", "medium", "large", "jumbo"})

# `effect` mirrors template_recipe's overlay-effect enum exactly, so the
# renderer's existing dispatch on effect name works without modification once
# the overlays are merged into slot dicts.
VALID_EFFECTS: frozenset[str] = frozenset(
    {
        "pop-in",
        "fade-in",
        "scale-up",
        "font-cycle",
        "typewriter",
        "glitch",
        "bounce",
        "slide-in",
        "slide-up",
        "giant-title-wipe",
        "stream-in",
        "static",
        "none",
    }
)


# ── Schemas ───────────────────────────────────────────────────────────────────


class TextBBox(BaseModel):
    """Normalized text bounding box, required on every overlay.

    Coordinates are fractions of the frame (1080×1920 for 9:16) where x_norm,
    y_norm is the CENTER of the box and w_norm, h_norm are the dimensions.
    `sample_frame_t` is the global-timeline seconds at which the agent measured
    the bbox — used by the eval harness to cross-check against OCR ground truth
    (OCR returns per-frame detections; the eval matches them by closest frame).
    """

    x_norm: float = Field(ge=0.0, le=1.0)
    y_norm: float = Field(ge=0.0, le=1.0)
    w_norm: float = Field(gt=0.0, le=1.0)
    h_norm: float = Field(gt=0.0, le=1.0)
    sample_frame_t: float = Field(ge=0.0)

    @field_validator("x_norm", "y_norm", "w_norm", "h_norm")
    @classmethod
    def _validate_in_frame(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        # Pydantic Field constraints handle individual range. Horizontal/vertical
        # extent overflow (center ± half-width outside [0, 1]) is validated in
        # the model_validator below where we have access to all four fields.
        return v

    @classmethod
    def model_validate_with_extent(cls, data: dict) -> TextBBox:  # noqa: D401
        """Helper for callers that want the extent check raised as ValueError."""
        bbox = cls.model_validate(data)
        if (bbox.x_norm - bbox.w_norm / 2.0) < 0.0 or (bbox.x_norm + bbox.w_norm / 2.0) > 1.0:
            raise ValueError(
                f"bbox horizontal extent out of frame (x={bbox.x_norm}, w={bbox.w_norm})"
            )
        if (bbox.y_norm - bbox.h_norm / 2.0) < 0.0 or (bbox.y_norm + bbox.h_norm / 2.0) > 1.0:
            raise ValueError(
                f"bbox vertical extent out of frame (y={bbox.y_norm}, h={bbox.h_norm})"
            )
        return bbox


class TemplateTextOverlay(BaseModel):
    """One on-screen text overlay extracted from the template video.

    Timings are GLOBAL (relative to the start of the template), not slot-relative.
    The orchestrator splits overlays into slots based on `slot_index` reported by
    the agent; if a single overlay spans a slot boundary (e.g. a hook that holds
    across an interstitial), it appears in the list with the slot it primarily
    belongs to and the renderer's `_collect_absolute_overlays` cross-slot merge
    extends it as needed.
    """

    slot_index: int = Field(ge=1, description="1-indexed slot this overlay primarily belongs to.")
    sample_text: str = Field(min_length=1, description="The literal text visible in the template.")
    start_s: float = Field(ge=0.0, description="Global timeline start in seconds.")
    end_s: float = Field(gt=0.0, description="Global timeline end in seconds. > start_s.")
    bbox: TextBBox
    font_color_hex: str = Field(
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Dominant text pixel color at sample_frame_t.",
    )
    effect: str = Field(default="none")
    role: str = Field(default="label")
    size_class: str = Field(default="medium")
    # Horizontal anchor mode for the renderer. Default "center" preserves the
    # historical behavior: lines center on `bbox.x_norm`. "left" anchors the
    # left edge of each line at `bbox.x_norm` so cumulative word-reveal
    # overlays (Layer-2 progressive reveal) can grow rightward without
    # re-centering as new words appear. "right" mirrors left for right-anchored
    # text. Renderer reads this in `_draw_text_png` / `_draw_spans_png`.
    text_anchor: str = Field(default="center")
    # Optional: when `effect="pop-in"`, tags only the newly added word of a
    # cumulative reveal stage as the pop-animation target. The renderer
    # animates just this suffix (scale 30->115->100) while the prefix renders
    # statically. Without it, every cumulative stage would re-pop the whole
    # line. Ignored when effect != "pop-in". None preserves historical behavior.
    pop_animated_suffix: str | None = Field(default=None)
    # Optional gradient text fill.  When present the renderers paint the glyph
    # fill with a smooth multi-colour gradient instead of the solid
    # `font_color_hex`.  The agent is never required to emit this — it is
    # populated by style-set resolution at render time.  Defined here so the
    # schema round-trip keeps the field when an overlay already carries it.
    text_gradient: dict | None = Field(default=None)

    @field_validator("text_anchor")
    @classmethod
    def _validate_text_anchor(cls, v: str) -> str:
        if v not in ("left", "center", "right"):
            raise ValueError(f"text_anchor={v!r} must be one of left/center/right")
        return v

    @field_validator("effect")
    @classmethod
    def _validate_effect(cls, v: str) -> str:
        if v not in VALID_EFFECTS:
            raise ValueError(f"effect={v!r} not in {sorted(VALID_EFFECTS)}")
        return v

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role={v!r} not in {sorted(VALID_ROLES)}")
        return v

    @field_validator("size_class")
    @classmethod
    def _validate_size_class(cls, v: str) -> str:
        if v not in VALID_SIZE_CLASSES:
            raise ValueError(f"size_class={v!r} not in {sorted(VALID_SIZE_CLASSES)}")
        return v


class TemplateTextInput(BaseModel):
    """Input to the template-text agent.

    `slot_boundaries_s` is the list of (start_s, end_s) tuples for each slot in
    the recipe, in temporal order. The agent uses these to attribute each
    extracted overlay to the correct slot_index (1-indexed). Sourced from the
    `template_recipe` output's slots, summed cumulatively from 0.

    `transcript_words` carries per-word timestamps from the audio transcript. It
    is forwarded to stage E (transcript alignment) in the Layer-2 pipeline path
    when `text_overlay_v2_enabled=True`. When empty (the default), stage E skips
    alignment corrections and returns phrases as-is. The Layer-1 single-call path
    ignores this field entirely — it is a no-op for existing callers.

    `force_layer2` is a per-request override that routes this specific build
    through the Layer-2 pipeline even when the global `text_overlay_v2_enabled`
    flag is False. Default False preserves existing behavior for all callers.
    Used by the admin `?use_layer2=true` query param for canary/A-B testing
    without flipping the global flag.
    """

    file_uri: str
    file_mime: str = "video/mp4"
    slot_boundaries_s: list[tuple[float, float]] = Field(default_factory=list)
    # Optional transcript for Layer-2 stage E (alignment). Empty list is valid
    # and causes the alignment stage to pass through phrases unchanged.
    # Layer-1 callers that don't set this field are unaffected.
    transcript_words: list[dict] = Field(
        default_factory=list,
        description="Per-word transcript [{text, start_s, end_s}]. Optional; Layer-2 only.",
    )
    # Per-request Layer-2 override. When True, routes through the Layer-2
    # pipeline regardless of the global text_overlay_v2_enabled setting.
    # Existing callers that omit this field default to False (no change).
    force_layer2: bool = False
    # GCS object path for the Layer-2 video download. Layer-2's ffmpeg-based
    # frame extraction (stage A) needs raw bytes from GCS; `file_uri` is the
    # Gemini File API reference — a different contract. If None when Layer-2
    # is active, _run_layer2 raises TerminalError so the bridge's existing
    # fallback returns (False, 0) and recipe-agent overlays pass through
    # unchanged (loud failure, graceful degradation). Layer-1 callers are
    # unaffected — this field is ignored when the Layer-2 path is not taken.
    gcs_path: str | None = Field(
        default=None,
        description=(
            "GCS object path for Layer-2 video download. "
            "None disables Layer-2 routing (raises TerminalError if Layer-2 is requested)."
        ),
    )


class TemplateTextOutput(BaseModel):
    """Flat list of overlays across all slots.

    Orchestrator groups by `slot_index` and writes back into `recipe.slots[i].text_overlays`,
    replacing whatever `template_recipe` originally emitted.
    """

    overlays: list[TemplateTextOverlay] = Field(default_factory=list)
