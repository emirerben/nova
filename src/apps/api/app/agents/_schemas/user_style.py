"""Schema for the per-user persistent text style (Creator Agent M1).

`UserStyle` lives at `personas.style` (JSONB, nullable). NULL = no style derived
yet → current byte-identical render behavior. `status="edited"` means the user
hand-edited; derivation never auto-overwrites it.

`StyleKnobs` uses `extra="forbid"` as the PRIMARY guard for the CLAUDE.md #296
parity-safe invariant: only fields honored by BOTH the Pillow and Skia renderers
are allowed here. Adding a new field here without confirming renderer parity will
raise at parse time (tests + production validation).

Do NOT add `effect` to `StyleKnobs` until effect parity is confirmed across both
renderers — keep it agent/set-owned for now.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.pipeline.overlay_sizing import MAX_INTRO_PX, MIN_INTRO_PX

# Bump when the schema changes in a breaking way.
USER_STYLE_VERSION = "1"


class StyleKnobs(BaseModel):
    """Parity-safe per-user knob overrides. `extra='forbid'` = primary guard.

    Every field here MUST be honored by BOTH text_overlay.py (Pillow) and
    text_overlay_skia.py (Skia). See _STYLE_KEYS in style_sets.py for the
    canonical parity list. These are the knob-override subset that a user can
    persist (excludes `effect` which requires additional parity verification,
    and `text_size`/`text_size_px` bucket which is expressed as px here only).
    """

    model_config = ConfigDict(extra="forbid")

    font_family: str | None = None
    # px value wins over bucket; silently clamped to [MIN_INTRO_PX, MAX_INTRO_PX] at
    # parse time so a stale/oversized agent value never prevents the style loading.
    # Route validators use ge/le to give the user a 422 on bad input.
    text_size_px: int | None = None
    position: str | None = None
    position_x_frac: float | None = None
    position_y_frac: float | None = None
    text_anchor: str | None = None
    text_color: str | None = None
    highlight_color: str | None = None
    stroke_width: int | None = None
    cycle_fonts: bool | None = None

    @field_validator("text_size_px", mode="before")
    @classmethod
    def _clamp_px(cls, v: object) -> object:
        """Silently clamp text_size_px on read so a stale/oversized value is
        safe. We don't raise — the derivation agent may produce a value just
        outside the envelope, and a silent clamp is safer than a validation
        error that prevents the style from loading at all."""
        if v is None:
            return v
        try:
            return max(MIN_INTRO_PX, min(MAX_INTRO_PX, int(v)))
        except (TypeError, ValueError):
            return None


class UserStyle(BaseModel):
    """Per-user persistent style entity stored at personas.style (JSONB)."""

    # Pinned style-set id from the curated catalog (generative-eligible).
    # Coerced to "default" at parse time if not in the current catalog.
    style_set_id: str = "default"
    # User-editable parity-safe knob overrides. Applied AFTER the set resolves.
    knobs: StyleKnobs = Field(default_factory=StyleKnobs)
    # Content preferences — stored now, consumed by the planner in M3.
    footage_type_bias: list[str] = Field(default_factory=list)
    # edit_format → relative weight; e.g. {"talking_head": 0.7, "montage": 0.3}.
    preferred_edit_format_mix: dict[str, float] = Field(default_factory=dict)
    # "no instructions" preference for the content plan (full/light/none).
    # Stored in M1; planner consumes it in M3.
    instruction_level: Literal["full", "light", "none"] = "full"
    # deriving | ready | edited | failed
    status: Literal["deriving", "ready", "edited", "failed"] = "deriving"
    # Provenance: which persona/TikTok data produced this style.
    derived_from: dict = Field(default_factory=dict)
    style_version: str = USER_STYLE_VERSION
    rationale: str = ""


def coerce_user_style(raw: dict | None) -> UserStyle | None:
    """Parse + coerce a raw style JSONB dict into UserStyle.

    Returns None when raw is None/empty so callers can use the
    clean ``if user_style:`` idiom. Non-raising on malformed data
    (unknown knob keys raise on write via extra='forbid', but we
    defend read paths separately).
    """
    if not raw:
        return None
    try:
        return UserStyle.model_validate(raw)
    except Exception:  # noqa: BLE001 — defensive read; bad blob → None → baseline
        return None


def user_style_knobs_dict(style: UserStyle | None) -> dict:
    """Return the knobs as a plain dict with only non-None values.

    Used in the render path: pass the result as `user_style_knobs`
    to `_resolve_intro_overlay_params`. An empty dict means "no overrides".
    """
    if style is None:
        return {}
    return {k: v for k, v in style.knobs.model_dump().items() if v is not None}
