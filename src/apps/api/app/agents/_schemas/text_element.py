"""Schema for TextElement — the unified authoring layer for text overlays.

A TextElement represents one timed text block in the editorial/authoring layer.
TextElements are stored per-variant in
``Job.assembly_plan["variants"][i]["text_elements"]`` and compile down to burn dicts
for the existing Skia/Pillow renderers via ``build_overlays_from_text_elements()``
(T2 in the plan-item-timeline plan).

The read adapter ``text_elements_for_variant()`` lazily synthesizes a TextElement
list from whichever legacy shape a variant has (linear intro / word-cluster intro /
transcript-synced sequence) by calling the existing overlay generators and converting
their output.  Old jobs render byte-identically because the render path still reads
the legacy fields; the TextElement snapshot is non-authoritative until Phase 1 when
a user first edits.

Coordinate convention and size units mirror ``text_overlay_skia.py`` and the
existing burn-dict schema so the Phase-0 compiler (T2) can produce byte-identical
output.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allowed font families (same set as existing burn dict; reject unknown per
# eng review A19 — user-controlled strings reach the Skia renderer).
_ALLOWED_FONTS: frozenset[str] = frozenset(
    {
        "PlayfairDisplay-Bold",
        "PlayfairDisplay-Regular",
        "Inter-Bold",
        "Inter-Regular",
    }
)

# Allowed effects in this schema.  The burn dict supports more effects (e.g.
# "pop-in", "typewriter") but the TextElement editor surface only exposes the
# four effects that map cleanly to the Phase-0 burn-dict roles.  Unknown
# effects from legacy burn dicts are coerced to "static" in the adapter.
_ALLOWED_EFFECTS: frozenset[str] = frozenset(
    {
        "static",
        "fade-in",
        "slide-up",
        "karaoke-line",
    }
)

# Valid size_class values (mirrors _SIZE_CLASSES in generative_overlays.py).
_VALID_SIZE_CLASSES: frozenset[str] = frozenset(
    {"small", "medium", "large", "xlarge", "xxlarge", "jumbo"}
)

# Hex color: exactly #RRGGBB (6 hex digits).
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Map from legacy burn-dict effects (which may include richer Skia effects)
# to the TextElement effect enum.  Anything not listed falls back to "static".
_BURN_EFFECT_TO_TEXT_ELEMENT: dict[str, str] = {
    "static": "static",
    "fade-in": "fade-in",
    "slide-up": "slide-up",
    "karaoke-line": "karaoke-line",
}

# Map from burn-dict text_anchor value → TextElement alignment.
_ANCHOR_TO_ALIGNMENT: dict[str, str] = {
    "left": "left",
    "right": "right",
    "center": "center",
}

# Default reveal window when the real video duration isn't available.
# Matches MAX_INTRO_S in generative_build.py.
_ADAPTER_REVEAL_WINDOW_S: float = 3.0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TextElement(BaseModel):
    """One timed text block in the editorial/authoring layer.

    All numeric fields are either silently clamped on parse (size_px,
    stroke_width, x_frac, y_frac) or coerced to None (invalid colors) so a
    stale or slightly-out-of-range client value doesn't 422 a render path.

    ``font_family`` and ``effect`` reject unknown values with ValueError —
    the route layer (T4) converts this to 422 per A19.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Stable uuid hex; auto-generated when absent.",
    )
    text: str = Field(default="", max_length=500, description="Overlay text content.")
    start_s: float = Field(default=0.0, ge=0, description="Start time in seconds.")
    end_s: float = Field(default=3.0, gt=0, description="End time in seconds; must be > start_s.")
    role: Literal["generative_intro", "generative_sequence"] = Field(
        default="generative_intro",
        description="Maps to the existing burn-dict role (renderer dispatch).",
    )
    position: Literal["top", "middle", "bottom", "custom"] = Field(
        default="middle",
        description=("Vertical position preset. 'custom' requires explicit x_frac / y_frac."),
    )
    x_frac: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Horizontal center fraction [0, 1]; None unless position='custom'.",
    )
    y_frac: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Vertical center fraction [0, 1]; None unless position='custom'.",
    )
    font_family: str | None = Field(
        default=None,
        description=(
            "Font family name. Allowed: PlayfairDisplay-Bold, "
            "PlayfairDisplay-Regular, Inter-Bold, Inter-Regular."
        ),
    )
    size_px: float | None = Field(
        default=None,
        description="Font size in pixels; silently clamped to [8, 300] (A18).",
    )
    size_class: Literal["small", "medium", "large", "xlarge", "xxlarge", "jumbo"] | None = Field(
        default=None,
        description=(
            "Font size class bucket (from burn-dict text_size key). "
            "Used by the compiler when size_px is None (A14)."
        ),
    )
    color: str | None = Field(
        default=None,
        description="Text color '#RRGGBB'; invalid strings coerced to None.",
    )
    highlight_color: str | None = Field(
        default=None,
        description="Highlight color '#RRGGBB'; invalid strings coerced to None.",
    )
    stroke_width: float | None = Field(
        default=None,
        description="Stroke width; silently clamped to [0, 20].",
    )
    alignment: Literal["left", "center", "right"] | None = Field(
        default="center",
        description="Text alignment (maps to text_anchor in the burn dict).",
    )
    effect: Literal["static", "fade-in", "slide-up", "karaoke-line"] | None = Field(
        default="static",
        description="Animation effect.",
    )
    fade_out_ms: int | None = Field(
        default=None,
        ge=0,
        le=2000,
        description="Fade-out duration in milliseconds.",
    )
    reveal_s: float | None = Field(
        default=None,
        ge=0,
        description="Reveal window in seconds (for animated effects).",
    )
    z: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Layer z-index; higher = rendered later = on top.",
    )
    word_timings: list[dict] | None = Field(
        default=None,
        description=(
            "Per-word timing dicts stored from the original burn dict; "
            "used verbatim by the compiler for karaoke-line fidelity (A17)."
        ),
    )
    source_params: dict | None = Field(
        default=None,
        description=(
            "Raw params blob from the original generator; stored for round-trip "
            "safety so the compiler can reconstruct byte-identical burn dicts (A2)."
        ),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("id", mode="before")
    @classmethod
    def _ensure_id(cls, v: object) -> str:
        """Auto-generate id when absent or falsy."""
        if not v:
            return uuid.uuid4().hex
        return str(v)

    @field_validator("size_px", mode="before")
    @classmethod
    def _clamp_size_px(cls, v: object) -> float | None:
        """Silently clamp to [8, 300] (A18 — Skia OOM guard)."""
        if v is None:
            return None
        try:
            return max(8.0, min(300.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @field_validator("stroke_width", mode="before")
    @classmethod
    def _clamp_stroke_width(cls, v: object) -> float | None:
        """Silently clamp to [0, 20]."""
        if v is None:
            return None
        try:
            return max(0.0, min(20.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @field_validator("x_frac", "y_frac", mode="before")
    @classmethod
    def _clamp_frac(cls, v: object) -> float | None:
        """Silently clamp to [0, 1]."""
        if v is None:
            return None
        try:
            return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @field_validator("color", "highlight_color", mode="before")
    @classmethod
    def _coerce_color(cls, v: object) -> str | None:
        """Coerce invalid hex color strings to None."""
        if v is None:
            return None
        s = str(v).strip()
        if _HEX_COLOR_RE.match(s):
            return s
        return None

    @field_validator("font_family", mode="before")
    @classmethod
    def _validate_font_family(cls, v: object) -> str | None:
        """Reject unknown font families (A19 security guard)."""
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if s not in _ALLOWED_FONTS:
            raise ValueError(f"Unknown font_family {s!r}; allowed: {sorted(_ALLOWED_FONTS)}")
        return s

    @field_validator("effect", mode="before")
    @classmethod
    def _validate_effect(cls, v: object) -> str | None:
        """Reject unknown effects (A19)."""
        if v is None:
            return "static"
        s = str(v).strip()
        if s not in _ALLOWED_EFFECTS:
            raise ValueError(f"Unknown effect {s!r}; allowed: {sorted(_ALLOWED_EFFECTS)}")
        return s


# ---------------------------------------------------------------------------
# Coerce helper (mirrors coerce_media_overlays in media_overlay.py)
# ---------------------------------------------------------------------------


def coerce_text_elements(raw: list | None) -> list[TextElement] | None:
    """Parse + coerce a raw list into validated TextElement objects.

    Returns None when the list is empty / None so callers can use the clean
    ``if text_elements:`` idiom.  Non-raising on individual bad entries: they
    are silently dropped rather than failing the entire set.
    """
    if not raw:
        return None
    result: list[TextElement] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(TextElement.model_validate(item))
        except Exception:  # noqa: BLE001 — bad text-element entry → skip
            pass
    return result or None


# ---------------------------------------------------------------------------
# Burn-dict → TextElement conversion helpers (used by the read adapter)
# ---------------------------------------------------------------------------


def _burn_dict_position(
    burn_dict: dict,
) -> tuple[str, float | None, float | None]:
    """Return (position, x_frac, y_frac) from a burn dict.

    Cluster / sequence overlays carry explicit ``position_x_frac`` /
    ``position_y_frac``; linear overlays use the ``position`` string preset.
    """
    x_raw = burn_dict.get("position_x_frac")
    y_raw = burn_dict.get("position_y_frac")
    if x_raw is not None or y_raw is not None:
        x = float(x_raw) if x_raw is not None else 0.5
        y = float(y_raw) if y_raw is not None else 0.5
        return "custom", x, y

    burn_pos = burn_dict.get("position", "center")
    if burn_pos == "top":
        return "top", None, None
    if burn_pos == "bottom":
        return "bottom", None, None
    # "center", "center-above", "center-below", "center-label" → "middle"
    return "middle", None, None


def _burn_dict_to_text_element(
    burn_dict: dict,
    *,
    intro_mode: str | None = None,
    intro_layout: str | None = None,
    intro_text_size_px: int | None = None,
) -> TextElement | None:
    """Convert a single burn dict to a TextElement.

    Returns None when the burn dict can't be converted (empty text, invalid
    timing window).

    Font families and effects not in the TextElement allowlists are silently
    mapped to None / "static" respectively (with a debug log) so the adapter
    never raises on unknown style-set values that appear in legacy burn dicts.
    The ``source_params`` blob preserves the key generator params for
    round-trip safety (A2).
    """
    text = (burn_dict.get("text") or "").strip()
    if not text:
        return None

    start_s = float(burn_dict.get("start_s") or 0.0)
    end_s_raw = burn_dict.get("end_s")
    end_s = float(end_s_raw) if end_s_raw is not None else 3.0
    if end_s <= 0:
        return None

    # role
    role_raw = burn_dict.get("role", "generative_intro")
    role = "generative_sequence" if role_raw == "generative_sequence" else "generative_intro"

    # position
    position, x_frac, y_frac = _burn_dict_position(burn_dict)

    # font_family: validate against allowlist; use None if unsupported.
    raw_font = burn_dict.get("font_family")
    if raw_font and raw_font in _ALLOWED_FONTS:
        font_family: str | None = raw_font
    else:
        if raw_font:
            log.debug(
                "text_element_adapter_font_not_in_allowlist",
                font_family=raw_font,
            )
        font_family = None

    # size_class from text_size bucket key
    size_class_raw = burn_dict.get("text_size")
    size_class: str | None = size_class_raw if size_class_raw in _VALID_SIZE_CLASSES else None

    # size_px
    size_px_raw = burn_dict.get("text_size_px")
    size_px: float | None = float(size_px_raw) if size_px_raw is not None else None

    # colors (defensively coerce)
    raw_color = burn_dict.get("text_color")
    color: str | None = raw_color if (raw_color and _HEX_COLOR_RE.match(str(raw_color))) else None
    raw_highlight = burn_dict.get("highlight_color")
    highlight_color: str | None = (
        raw_highlight if (raw_highlight and _HEX_COLOR_RE.match(str(raw_highlight))) else None
    )

    # stroke_width
    stroke_raw = burn_dict.get("stroke_width")
    stroke_width: float | None = float(stroke_raw) if stroke_raw is not None else None

    # alignment from text_anchor
    text_anchor = str(burn_dict.get("text_anchor") or "center")
    alignment = _ANCHOR_TO_ALIGNMENT.get(text_anchor, "center")

    # effect: coerce to allowed set
    raw_effect = str(burn_dict.get("effect") or "static")
    effect = _BURN_EFFECT_TO_TEXT_ELEMENT.get(raw_effect, "static")

    # fade_out_ms / word_timings
    fade_out_ms = burn_dict.get("fade_out_ms")
    word_timings = burn_dict.get("word_timings")

    # source_params: preserve key generator params for round-trip safety (A2)
    source_params: dict = {
        "mode": intro_mode,
        "layout": intro_layout,
        "size_class": burn_dict.get("text_size"),
        "text_size_px": intro_text_size_px,
    }

    try:
        return TextElement(
            text=text,
            start_s=start_s,
            end_s=end_s,
            role=role,  # type: ignore[arg-type]
            position=position,  # type: ignore[arg-type]
            x_frac=x_frac,
            y_frac=y_frac,
            font_family=font_family,
            size_px=size_px,
            size_class=size_class,  # type: ignore[arg-type]
            color=color,
            highlight_color=highlight_color,
            stroke_width=stroke_width,
            alignment=alignment,  # type: ignore[arg-type]
            effect=effect,  # type: ignore[arg-type]
            fade_out_ms=fade_out_ms,
            word_timings=word_timings,
            source_params=source_params,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "text_element_adapter_conversion_failed",
            error=str(exc),
            text=text[:80],
        )
        return None


# ---------------------------------------------------------------------------
# Read adapter: legacy variant dict → list[TextElement]
# ---------------------------------------------------------------------------


def text_elements_for_variant(v: dict) -> list[TextElement]:
    """Lazily synthesize a TextElement list from whichever legacy shape a variant has.

    This is the back-compat READ adapter.  It does not mutate ``v``.

    Returns ``[]`` when:
      - ``text_mode == "none"`` (text removed from this variant)
      - ``text_mode == "lyrics"`` (lyric injector owns the overlays)
      - no ``intro_text`` and no ``scenes`` (footage-only render)

    Shape dispatch:
      - ``intro_mode == "sequence"`` + ``scenes`` → ``build_sequence_overlays``
      - all other ``intro_mode`` values + ``intro_text`` → ``build_persistent_intro_overlays``

    Font families and effects not in the TextElement allowlists are silently
    coerced; no exception is raised so the adapter never breaks a read path
    on a legacy variant with a style-set-assigned font.

    The ``source_params`` blob on each TextElement preserves the key generator
    params so the Phase-0 compiler (T2) can reconstruct byte-identical burn
    dicts for the first edit (A2).
    """
    text_mode = v.get("text_mode", "agent_text")
    if text_mode == "none":
        return []
    if text_mode == "lyrics":
        # lyrics variants are handled by the lyric injector; not text_elements.
        return []

    intro_mode: str | None = v.get("intro_mode")
    intro_text: str | None = v.get("intro_text") or None
    scenes: list[dict] | None = v.get("scenes") or None

    if not intro_text and not scenes:
        return []

    intro_layout: str | None = v.get("intro_layout")
    intro_word_roles: list[str] | None = v.get("intro_word_roles")
    intro_text_size_px: int | None = v.get("intro_text_size_px")
    intro_effect: str = v.get("intro_effect") or "karaoke-line"
    intro_text_color: str = v.get("intro_text_color") or "#FFFFFF"

    # ------------------------------------------------------------------
    # SEQUENCE path (transcript-synced editorial / rhythm mode)
    # ------------------------------------------------------------------
    if intro_mode == "sequence" and scenes:
        base_size_px = int(v.get("sequence_base_size_px") or intro_text_size_px or 60)
        try:
            from app.pipeline.generative_overlays import (  # noqa: PLC0415
                build_sequence_overlays,
            )

            burn_dicts = build_sequence_overlays(
                scenes,
                base_size_px=base_size_px,
                text_color=intro_text_color,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("text_elements_adapter_sequence_failed", error=str(exc))
            return []

        if not burn_dicts:
            return []

        result: list[TextElement] = []
        for bd in burn_dicts:
            elem = _burn_dict_to_text_element(
                bd,
                intro_mode="sequence",
                intro_layout=intro_layout,
                intro_text_size_px=intro_text_size_px,
            )
            if elem is not None:
                result.append(elem)
        return result

    # ------------------------------------------------------------------
    # LINEAR / CLUSTER path (single-block or multi-block intro hook)
    # ------------------------------------------------------------------
    if not intro_text:
        return []

    layout = intro_layout or "linear"

    style_kwargs: dict = {}
    if intro_text_size_px is not None:
        style_kwargs["text_size_px"] = int(intro_text_size_px)

    try:
        from app.pipeline.generative_overlays import (  # noqa: PLC0415
            build_persistent_intro_overlays,
        )

        burn_dicts_list = build_persistent_intro_overlays(
            text=intro_text,
            effect=intro_effect,
            reveal_window_s=_ADAPTER_REVEAL_WINDOW_S,
            text_color=intro_text_color,
            layout=layout,
            word_roles=intro_word_roles,
            **style_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("text_elements_adapter_linear_failed", error=str(exc))
        return []

    if not burn_dicts_list:
        return []

    result_linear: list[TextElement] = []
    for bd in burn_dicts_list:
        elem = _burn_dict_to_text_element(
            bd,
            intro_mode=intro_mode or layout,
            intro_layout=intro_layout,
            intro_text_size_px=intro_text_size_px,
        )
        if elem is not None:
            result_linear.append(elem)
    return result_linear
