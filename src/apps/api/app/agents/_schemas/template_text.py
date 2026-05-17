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
    def model_validate_with_extent(cls, data: dict) -> "TextBBox":  # noqa: D401
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
    """

    file_uri: str
    file_mime: str = "video/mp4"
    slot_boundaries_s: list[tuple[float, float]] = Field(default_factory=list)


class TemplateTextOutput(BaseModel):
    """Flat list of overlays across all slots.

    Orchestrator groups by `slot_index` and writes back into `recipe.slots[i].text_overlays`,
    replacing whatever `template_recipe` originally emitted.
    """

    overlays: list[TemplateTextOverlay] = Field(default_factory=list)
