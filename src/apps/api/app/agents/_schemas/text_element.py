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

import json
import logging
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allowed font families (same set as existing burn dict; reject unknown per
# eng review A19 — user-controlled strings reach the Skia renderer).
_LEGACY_FONT_ALIASES: frozenset[str] = frozenset(
    {
        "PlayfairDisplay-Bold",
        "PlayfairDisplay-Regular",
        "Inter-Bold",
        "Inter-Regular",
    }
)


def _load_registry_font_names() -> frozenset[str]:
    registry_path = Path(__file__).resolve().parents[3] / "assets" / "fonts" / "font-registry.json"
    try:
        data = json.loads(registry_path.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("text_element_font_registry_load_failed: %s (%s)", exc, registry_path)
        return frozenset()
    fonts = data.get("fonts") if isinstance(data, dict) else None
    if not isinstance(fonts, dict):
        return frozenset()
    return frozenset(str(name) for name in fonts)


_ALLOWED_FONTS: frozenset[str] = _LEGACY_FONT_ALIASES | _load_registry_font_names()

# Allowed effects in the shared TextElement editor glossary. Unknown effects
# from legacy burn dicts are coerced to "static" in the adapter.
_ALLOWED_EFFECTS: frozenset[str] = frozenset(
    {
        "static",
        "none",
        "fade-in",
        "slide-up",
        "slide-down",
        "karaoke-line",
        "pop-in",
        "scale-up",
        "typewriter",
        "stream-in",
        "staggered-slice",
        "bounce",
        "slide-in",
    }
)

# Valid size_class values (mirrors _SIZE_CLASSES in generative_overlays.py).
_VALID_SIZE_CLASSES: frozenset[str] = frozenset(
    {"small", "medium", "large", "xlarge", "xxlarge", "jumbo"}
)

# Hex color: exactly #RRGGBB (6 hex digits).
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Valid text_case transforms (mirror of TEXT_CASES in lib/overlay-layout.ts).
_VALID_TEXT_CASES: frozenset[str] = frozenset({"none", "upper", "lower", "title"})

# Spacing clamps — mirrors LETTER_SPACING_MIN/MAX + LINE_SPACING_MIN/MAX in
# lib/overlay-layout.ts and the resolver clamps in generative_overlays.py.
LETTER_SPACING_MIN_EM = -0.05
LETTER_SPACING_MAX_EM = 0.5
LINE_SPACING_MIN = 0.5
LINE_SPACING_MAX = 3.0
MAX_WIDTH_FRAC_MIN = 0.2
MAX_WIDTH_FRAC_MAX = 1.0

# ---------------------------------------------------------------------------
# Renderer-parity registry (Python mirror of PARITY_VERIFIED_FIELDS in
# src/apps/web/src/lib/parity-verified-fields.ts — decision D9/D17).
#
# A style field may be added here ONLY in the same PR as its shared-fixture
# layout-contract test (tests/fixtures/text-element-parity/<field>.json,
# asserted by BOTH tests/pipeline/test_text_element_parity_contract.py and
# src/apps/web/src/__tests__/lib/text-element-parity-contract.test.ts) and its
# Skia render verification. Keep in lockstep with the TS registry.
# ---------------------------------------------------------------------------

PARITY_VERIFIED_FIELDS: frozenset[str] = frozenset(
    {
        # Base fields both renderers honored before the D17 gate existed:
        "text",
        "start_s",
        "end_s",
        "position",
        "x_frac",
        "y_frac",
        "font_family",
        "size_px",
        "size_class",
        "color",
        "highlight_color",
        "stroke_width",
        "alignment",
        "effect",
        # Gated style fields (T11) — each has a shared parity fixture:
        "text_case",  # tests/fixtures/text-element-parity/text_case.json
        "letter_spacing",  # tests/fixtures/text-element-parity/letter_spacing.json
        "line_spacing",  # tests/fixtures/text-element-parity/line_spacing.json
        "max_width_frac",  # tests/fixtures/text-element-parity/max_width_frac.json
        # `behind_subject` is deliberately NOT registered here: it's a render-only
        # compositing flag (Skia subject-matte occlusion), not a layout field. The
        # browser CSS preview has no subject segmentation, so it can never render
        # this field identically to the burn — there is no parity fixture to write.
    }
)


def apply_text_case(text: str, case: str | None) -> str:
    """Apply a text_case transform. EXACT mirror of `applyTextCase` in
    src/apps/web/src/lib/overlay-layout.ts — the CSS preview and the burn
    must produce identical strings (parity fixture: text_case.json).

    "title" uppercases the FIRST CHARACTER of each whitespace-delimited run
    and lowercases the rest (deliberately not Python's ``str.title()``, which
    capitalizes after apostrophes — "don't" → "Don'T").
    """
    if not case or case == "none":
        return text
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return re.sub(r"\S+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), text)
    return text


# Map from legacy burn-dict effects (which may include richer Skia effects)
# to the TextElement effect enum.  Anything not listed falls back to "static".
_BURN_EFFECT_TO_TEXT_ELEMENT: dict[str, str] = {
    "static": "static",
    "fade-in": "fade-in",
    "slide-up": "slide-up",
    "karaoke-line": "karaoke-line",
    "staggered-slice": "staggered-slice",
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
_ADAPTER_HOLD_TO_END_S: float = 3600.0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ThemeTransition(BaseModel):
    """Transition-layer animation that can compose with a text effect."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["giant-title-wipe"] = Field(
        description="Theme/scene transition applied to the whole title layer."
    )
    target_glyph: str | None = Field(
        default="O",
        max_length=4,
        description="Glyph counter to dive through; v1 renderer targets the O center.",
    )

    @field_validator("target_glyph", mode="before")
    @classmethod
    def _coerce_target_glyph(cls, v: object) -> str | None:
        if v is None:
            return "O"
        s = str(v).strip()
        return s[:4] or "O"


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
    role: Literal["generative_intro", "generative_sequence", "lyric_line"] = Field(
        default="generative_intro",
        description="Maps to the existing burn-dict role (renderer dispatch).",
    )
    visual_block_id: str | None = Field(
        default=None,
        max_length=80,
        description="Optional visual block this text is grouped with in the editor.",
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
    rotation_deg: float | None = Field(
        default=None,
        description="Clockwise text rotation in degrees; silently clamped to [-360, 360].",
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
    shadow_enabled: bool | None = Field(
        default=None,
        description="Explicit soft-shadow toggle. None preserves legacy renderer defaults.",
    )
    glow_color: str | None = Field(
        default=None,
        description="Optional editorial glow color '#RRGGBB'.",
    )
    glow_strength: float | None = Field(
        default=None,
        description="Optional editorial glow intensity, clamped to [0, 1].",
    )
    alignment: Literal["left", "center", "right"] | None = Field(
        default="center",
        description="Text alignment (maps to text_anchor in the burn dict).",
    )
    effect: (
        Literal[
            "static",
            "none",
            "fade-in",
            "slide-up",
            "slide-down",
            "karaoke-line",
            "pop-in",
            "scale-up",
            "typewriter",
            "stream-in",
            "staggered-slice",
            "bounce",
            "slide-in",
        ]
        | None
    ) = Field(
        default="static",
        description="Animation effect.",
    )
    theme_transition: ThemeTransition | None = Field(
        default=None,
        description=(
            "Theme/scene transition layer applied independently from `effect`, "
            "so titles can combine entrance text animations with a transition wipe."
        ),
    )
    text_case: Literal["none", "upper", "lower", "title"] | None = Field(
        default=None,
        description=(
            "Display-case transform applied at compile time (burn dict + CSS "
            "preview receive the TRANSFORMED text; the stored `text` keeps the "
            "user's original casing). 'title' = first character of each "
            "whitespace-delimited word uppercased, rest lowercased."
        ),
    )
    letter_spacing: float | None = Field(
        default=None,
        description=(
            "Extra tracking between characters in EM units (× font size). "
            "Silently clamped to [-0.05, 0.5]. Honored by the Skia renderer "
            "(per-character advance) and the CSS preview (letter-spacing); "
            "px resolution = em × final font size on both sides."
        ),
    )
    line_spacing: float | None = Field(
        default=None,
        description=(
            "Line-height multiplier over the face's natural line height "
            "(ascent+descent). Silently clamped to [0.5, 3.0]; None = the "
            "renderer default 1.15 (Skia _LINE_SPACING / CSS preview)."
        ),
    )
    max_width_frac: float | None = Field(
        default=None,
        description=(
            "Maximum text wrap width as a fraction of frame width. Silently "
            "clamped to [0.2, 1.0]; None = renderer default 0.9."
        ),
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
    removed: bool = Field(
        default=False,
        description=(
            "Explicit tombstone for a generated AI text source the user deleted. "
            "Hidden on GET and skipped by the renderer so projection does not resurrect it."
        ),
    )
    behind_subject: bool = Field(
        default=False,
        description=(
            "Occlude this text behind the moving subject via the Skia subject-matte "
            "compositing hook (text_overlay_skia.py). NOT in PARITY_VERIFIED_FIELDS — "
            "it's a render-only compositing flag, not a layout field, and the browser "
            "CSS preview cannot segment the subject to show it."
        ),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_giant_title_wipe_effect(cls, data: object) -> object:
        """Draft/back-compat adapter: old effect value becomes a transition."""
        if not isinstance(data, dict):
            return data
        if data.get("effect") != "giant-title-wipe":
            return data
        migrated = dict(data)
        migrated["effect"] = "static"
        migrated.setdefault("theme_transition", {"type": "giant-title-wipe"})
        return migrated

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

    @field_validator("letter_spacing", mode="before")
    @classmethod
    def _clamp_letter_spacing(cls, v: object) -> float | None:
        """Silently clamp to [-0.05, 0.5] em (mirrors the TS clamp)."""
        if v is None:
            return None
        try:
            return max(LETTER_SPACING_MIN_EM, min(LETTER_SPACING_MAX_EM, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @field_validator("line_spacing", mode="before")
    @classmethod
    def _clamp_line_spacing(cls, v: object) -> float | None:
        """Silently clamp to [0.5, 3.0] (mirrors the TS clamp)."""
        if v is None:
            return None
        try:
            return max(LINE_SPACING_MIN, min(LINE_SPACING_MAX, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @field_validator("max_width_frac", mode="before")
    @classmethod
    def _clamp_max_width_frac(cls, v: object) -> float | None:
        """Silently clamp to [0.2, 1.0] (mirrors the TS clamp)."""
        if v is None:
            return None
        try:
            return max(MAX_WIDTH_FRAC_MIN, min(MAX_WIDTH_FRAC_MAX, float(v)))  # type: ignore[arg-type]
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

    @field_validator("rotation_deg", mode="before")
    @classmethod
    def _clamp_rotation_deg(cls, v: object) -> float | None:
        """Silently clamp clockwise rotation to one full turn in either direction."""
        if v is None:
            return None
        try:
            return max(-360.0, min(360.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @field_validator("color", "highlight_color", "glow_color", mode="before")
    @classmethod
    def _coerce_color(cls, v: object) -> str | None:
        """Coerce invalid hex color strings to None."""
        if v is None:
            return None
        s = str(v).strip()
        if _HEX_COLOR_RE.match(s):
            return s
        return None

    @field_validator("glow_strength", mode="before")
    @classmethod
    def _clamp_glow_strength(cls, v: object) -> float | None:
        if v is None:
            return None
        try:
            return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
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

    @field_validator("text_case", mode="before")
    @classmethod
    def _coerce_text_case(cls, v: object) -> str | None:
        """Coerce unknown/empty text_case values to None (no transform) so a
        drifted client value degrades to the user's own casing, never a
        dropped element."""
        if v is None:
            return None
        s = str(v).strip()
        if s in _VALID_TEXT_CASES:
            return s
        return None


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
    shadow_enabled_raw = burn_dict.get("shadow_enabled")
    shadow_enabled: bool | None = (
        bool(shadow_enabled_raw) if shadow_enabled_raw is not None else None
    )
    glow_color_raw = burn_dict.get("glow_color")
    glow_color: str | None = (
        glow_color_raw if (glow_color_raw and _HEX_COLOR_RE.match(str(glow_color_raw))) else None
    )
    glow_strength_raw = burn_dict.get("glow_strength")
    glow_strength: float | None = (
        float(glow_strength_raw) if glow_strength_raw is not None else None
    )

    # Parity-gated spacing fields (already clamped by TextElement validators).
    letter_spacing_raw = burn_dict.get("letter_spacing")
    letter_spacing: float | None = (
        float(letter_spacing_raw) if letter_spacing_raw is not None else None
    )
    line_spacing_raw = burn_dict.get("line_spacing")
    line_spacing: float | None = float(line_spacing_raw) if line_spacing_raw is not None else None
    max_width_frac_raw = burn_dict.get("max_width_frac")
    max_width_frac: float | None = (
        float(max_width_frac_raw) if max_width_frac_raw is not None else None
    )
    rotation_deg_raw = burn_dict.get("rotation_deg")
    rotation_deg: float | None = float(rotation_deg_raw) if rotation_deg_raw is not None else None

    # alignment from text_anchor
    text_anchor = str(burn_dict.get("text_anchor") or "center")
    alignment = _ANCHOR_TO_ALIGNMENT.get(text_anchor, "center")

    # effect: coerce to allowed text-effect set. Legacy burn dicts may still
    # carry the transition id in `effect`; expose that as theme_transition.
    raw_effect = str(burn_dict.get("effect") or "static")
    effect = _BURN_EFFECT_TO_TEXT_ELEMENT.get(raw_effect, "static")
    raw_theme_transition = burn_dict.get("theme_transition")
    theme_transition: dict | None = (
        raw_theme_transition if isinstance(raw_theme_transition, dict) else None
    )
    if raw_effect == "giant-title-wipe" and theme_transition is None:
        theme_transition = {"type": "giant-title-wipe"}

    # fade_out_ms / word_timings
    fade_out_ms = burn_dict.get("fade_out_ms")
    word_timings = burn_dict.get("word_timings")

    behind_subject = bool(burn_dict.get("behind_subject"))

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
            rotation_deg=rotation_deg,
            font_family=font_family,
            size_px=size_px,
            size_class=size_class,  # type: ignore[arg-type]
            color=color,
            highlight_color=highlight_color,
            stroke_width=stroke_width,
            shadow_enabled=shadow_enabled,
            glow_color=glow_color,
            glow_strength=glow_strength,
            letter_spacing=letter_spacing,
            line_spacing=line_spacing,
            max_width_frac=max_width_frac,
            alignment=alignment,  # type: ignore[arg-type]
            effect=effect,  # type: ignore[arg-type]
            theme_transition=theme_transition,
            fade_out_ms=fade_out_ms,
            word_timings=word_timings,
            source_params=source_params,
            behind_subject=behind_subject,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "text_element_adapter_conversion_failed",
            error=str(exc),
            text=text[:80],
        )
        return None


def _text_window(v: dict, *, default_end: float = _ADAPTER_HOLD_TO_END_S) -> tuple[float, float]:
    start_s = float(v.get("intro_start_s") or 0.0)
    end_raw = v.get("intro_end_s")
    if end_raw is not None:
        end_s = float(end_raw)
    else:
        duration = v.get("duration_s")
        end_s = float(duration) if duration else default_end
    if end_s <= start_s:
        end_s = start_s + _ADAPTER_REVEAL_WINDOW_S
    return start_s, end_s


def _identity_for_source(source: str, key: str) -> str:
    return f"{source}:{key}"


def _legacy_sequence_scene_key(raw: TextElement | dict) -> str | None:
    """Return the scene index owned by a legacy scene-level projection.

    Sequence projections used to collapse every rendered block in a scene into
    one ``sequence_scene:{index}`` element. New projections are block-exact and
    use ``sequence_scene:{index}:{block}``. A saved legacy element therefore
    owns (and suppresses) every new block for that scene so old user edits never
    duplicate after the projection shape changes.
    """
    params = raw.source_params if isinstance(raw, TextElement) else raw.get("source_params")
    if not isinstance(params, dict) or params.get("source") != "sequence_scene":
        return None
    key = str(params.get("key") or "")
    if not key or ":" in key:
        return None
    return key


def _sequence_block_scene_key(raw: TextElement | dict) -> str | None:
    params = raw.source_params if isinstance(raw, TextElement) else raw.get("source_params")
    if not isinstance(params, dict) or params.get("source") != "sequence_scene":
        return None
    key = str(params.get("key") or "")
    scene_key, separator, _block_key = key.partition(":")
    return scene_key if separator and scene_key else None


def text_element_source_identity(raw: TextElement | dict) -> str | None:
    """Stable identity for generated AI text bars.

    User-created bars do not carry a source identity. Generated bars do; the
    serializer uses it to merge newly projected AI text into an already user-edited
    element list without clobbering the user's saved element ids or edits.
    """
    role = raw.role if isinstance(raw, TextElement) else raw.get("role")
    params = raw.source_params if isinstance(raw, TextElement) else raw.get("source_params")
    if not isinstance(params, dict):
        return None
    identity = params.get("identity")
    if identity:
        return f"{role}:{identity}"
    source = params.get("source")
    key = params.get("key")
    if source and key:
        return f"{role}:{source}:{key}"
    return None


def _tombstone_for(projected: TextElement) -> dict:
    identity = text_element_source_identity(projected)
    params = dict(projected.source_params or {})
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, identity or projected.id).hex,
        "text": "",
        "start_s": projected.start_s,
        "end_s": projected.end_s,
        "role": projected.role,
        "source_params": params,
        "removed": True,
    }


def append_ai_text_tombstones(
    variant: dict, elements: list[dict], *, include_lyric_projection: bool = False
) -> list[dict]:
    """Append removed=true tombstones for generated bars omitted by a save payload."""
    projected = text_elements_for_variant(
        variant, include_lyric_projection=include_lyric_projection
    )
    if not projected:
        return elements
    incoming_ids = {
        ident for raw in elements if (ident := text_element_source_identity(raw)) is not None
    }
    out = list(elements)
    for projected_elem in projected:
        if projected_elem.role == "lyric_line":
            continue
        ident = text_element_source_identity(projected_elem)
        if ident and ident not in incoming_ids:
            out.append(_tombstone_for(projected_elem))
            incoming_ids.add(ident)
    return out


def merge_projected_text_elements_for_variant(
    variant: dict, *, include_lyric_projection: bool = False
) -> list[dict] | None:
    """GET adapter: saved user elements win, missing generated bars are appended.

    This is the read-side single source of truth. It fixes legacy user-edited rows
    that only stored hand-created bars by appending any generated AI bar whose
    source identity has no saved counterpart. Saved tombstones suppress projection.
    """
    projected = text_elements_for_variant(
        variant, include_lyric_projection=include_lyric_projection
    )
    saved = coerce_text_elements(variant.get("text_elements") or []) or []
    if not variant.get("text_elements_user_edited"):
        visible_projected = [e.model_dump() for e in projected if not e.removed]
        if visible_projected:
            return visible_projected
        visible_saved = [e.model_dump() for e in saved if not e.removed]
        return visible_saved or None

    seen: set[str] = set()
    legacy_sequence_scenes = {
        scene_key for elem in saved if (scene_key := _legacy_sequence_scene_key(elem)) is not None
    }
    tombstoned: set[str] = set()
    out: list[dict] = []
    for elem in saved:
        ident = text_element_source_identity(elem)
        if ident:
            seen.add(ident)
            if elem.removed:
                tombstoned.add(ident)
        if not elem.removed:
            out.append(elem.model_dump())

    for elem in projected:
        ident = text_element_source_identity(elem)
        block_scene_key = _sequence_block_scene_key(elem)
        if block_scene_key is not None and block_scene_key in legacy_sequence_scenes:
            continue
        if ident and ident not in seen and ident not in tombstoned:
            out.append(elem.model_dump())
            seen.add(ident)
    return out or None


def _element_from_burn_group(
    burn_dicts: list[dict],
    *,
    text: str,
    start_s: float,
    end_s: float,
    source: str,
    key: str,
    intro_mode: str | None,
    intro_layout: str | None,
    intro_text_size_px: int | None,
) -> TextElement | None:
    if not burn_dicts:
        return None
    first = burn_dicts[0]
    # Grouped reveal+hold bars should expose the animated reveal style in the
    # editor. The hold dict may carry the settled karaoke highlight color, which
    # would make the inspector lie about the authored text color.
    style = first if first.get("effect") != "static" else burn_dicts[-1]
    style = {**style, "text": text, "start_s": start_s, "end_s": end_s}
    elem = _burn_dict_to_text_element(
        style,
        intro_mode=intro_mode,
        intro_layout=intro_layout,
        intro_text_size_px=intro_text_size_px,
    )
    if elem is None:
        return None
    params = dict(elem.source_params or {})
    params.update(
        {
            "source": source,
            "key": key,
            "identity": _identity_for_source(source, key),
            "source_text": text,
            "burn_dicts": deepcopy(burn_dicts),
        }
    )
    elem.source_params = params
    if len(burn_dicts) > 1:
        if first.get("effect") != "static":
            elem.effect = _BURN_EFFECT_TO_TEXT_ELEMENT.get(str(first.get("effect")), elem.effect)
            elem.reveal_s = max(0.0, float(first.get("end_s") or start_s) - start_s)
            elem.word_timings = first.get("word_timings")
    return elem


def _element_from_lyric_snapshot(entry: dict) -> TextElement | None:
    if not isinstance(entry, dict):
        return None
    line_key = str(entry.get("line_key") or "").strip()
    text = str(entry.get("text") or "").strip()
    if not line_key or not text:
        return None
    try:
        start_s = float(entry.get("start_s"))
        end_s = float(entry.get("end_s"))
    except (TypeError, ValueError):
        return None
    if end_s <= start_s:
        return None

    raw_font = entry.get("font_family")
    font_family = raw_font if raw_font in _ALLOWED_FONTS else None
    raw_color = entry.get("color")
    color = raw_color if isinstance(raw_color, str) and _HEX_COLOR_RE.match(raw_color) else None
    raw_highlight = entry.get("highlight_color")
    highlight_color = (
        raw_highlight
        if isinstance(raw_highlight, str) and _HEX_COLOR_RE.match(raw_highlight)
        else None
    )
    raw_effect = str(entry.get("effect") or "karaoke-line")
    effect = raw_effect if raw_effect in _ALLOWED_EFFECTS else "karaoke-line"

    try:
        return TextElement(
            id=f"lyric_{line_key}",
            text=text,
            start_s=start_s,
            end_s=end_s,
            role="lyric_line",
            position="custom",
            x_frac=0.5,
            y_frac=entry.get("y_frac"),
            font_family=font_family,
            size_px=entry.get("size_px"),
            color=color,
            highlight_color=highlight_color,
            effect=effect,  # type: ignore[arg-type]
            word_timings=None,
            source_params={
                "source": "lyric",
                "key": line_key,
                "identity": _identity_for_source("lyric", line_key),
                "source_text": text,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("text_elements_adapter_lyric_failed", line_key=line_key, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Read adapter: legacy variant dict → list[TextElement]
# ---------------------------------------------------------------------------


def text_elements_for_variant(
    v: dict, *, include_lyric_projection: bool = False
) -> list[TextElement]:
    """Read-adapter entry point; appends lyric_line projections when asked.

    Lyric projections come from ``lyric_overlay_snapshot`` (written by every
    lyrics-rendering pass) and surface for ANY text_mode — a song_text/none
    variant with lyrics toggled on must expose its lines as editable blocks
    alongside its normal projected elements.
    """
    lyric_elems: list[TextElement] = []
    if include_lyric_projection:
        snapshot = v.get("lyric_overlay_snapshot")
        if isinstance(snapshot, list) and snapshot:
            lyric_elems = [
                elem for entry in snapshot if (elem := _element_from_lyric_snapshot(entry))
            ]
    return _base_text_elements_for_variant(v) + lyric_elems


def _base_text_elements_for_variant(v: dict) -> list[TextElement]:
    """Lazily synthesize a TextElement list from whichever legacy shape a variant has.

    This is the back-compat READ adapter.  It does not mutate ``v``.

    Returns ``[]`` when:
      - ``text_mode == "none"`` (text removed from this variant)
      - ``text_mode == "lyrics"`` (lyric projections are appended by the wrapper)
      - no AI text source is present (footage-only render)

    Shape dispatch:
      - ``caption_cues`` → one bar per cue
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
        # lyric overlays are projected by the wrapper from lyric_overlay_snapshot.
        return []

    intro_mode: str | None = v.get("intro_mode")
    intro_text: str | None = v.get("intro_text") or None
    scenes: list[dict] | None = v.get("scenes") or None
    caption_cues: list[dict] | None = v.get("caption_cues") or None

    if not intro_text and not scenes and not caption_cues:
        return []

    intro_layout: str | None = v.get("intro_layout")
    intro_word_roles: list[str] | None = v.get("intro_word_roles")
    intro_text_size_px: int | None = v.get("intro_text_size_px")
    intro_effect: str = v.get("intro_effect") or "karaoke-line"
    intro_text_color: str = v.get("intro_text_color") or "#FFFFFF"
    intro_start_s, intro_end_s = _text_window(v)

    # ------------------------------------------------------------------
    # CAPTION path (narrated/subtitled caption cues)
    # ------------------------------------------------------------------
    if caption_cues:
        result_captions: list[TextElement] = []
        caption_font = v.get("voiceover_caption_font") or v.get("caption_font")
        for i, cue in enumerate(caption_cues):
            if not isinstance(cue, dict):
                continue
            text = str(cue.get("text") or "").strip()
            if not text:
                continue
            try:
                start_s = float(cue.get("start_s") or 0.0)
                end_s = float(cue.get("end_s") or 0.0)
            except (TypeError, ValueError):
                continue
            if end_s <= start_s:
                continue
            try:
                result_captions.append(
                    TextElement(
                        text=text,
                        start_s=start_s,
                        end_s=end_s,
                        role="generative_sequence",
                        position="bottom",
                        font_family=caption_font if caption_font in _ALLOWED_FONTS else None,
                        size_px=float(v.get("caption_size_px") or 58),
                        color=v.get("caption_text_color") or "#FFFFFF",
                        stroke_width=float(v.get("caption_stroke_width") or 6),
                        alignment="center",
                        effect="static",
                        word_timings=cue.get("words"),
                        source_params={
                            "source": "caption_cue",
                            "key": str(i),
                            "identity": _identity_for_source("caption_cue", str(i)),
                            "source_text": text,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("text_elements_adapter_caption_failed", error=str(exc))
        if result_captions:
            return result_captions

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
        remaining = list(burn_dicts)
        for i, scene in enumerate(scenes):
            start_s = scene.get("start_s")
            end_s = scene.get("end_s")
            if start_s is None or end_s is None:
                continue
            try:
                scene_start = float(start_s)
                scene_end = float(end_s)
            except (TypeError, ValueError):
                continue
            scene_burns = [
                bd
                for bd in remaining
                if float(bd.get("start_s") or 0.0) >= scene_start
                and float(bd.get("end_s") or 0.0) <= scene_end + 0.001
            ]
            if not scene_burns:
                continue
            remaining = [bd for bd in remaining if bd not in scene_burns]
            # The renderer emits one independently timed/styled burn dict per
            # visible editorial block. Project that exact shape. The previous
            # scene-level grouping replaced every block with the whole phrase
            # and the first block's font/position, so the editor could never be
            # WYSIWYG even though source_params retained the real burn dicts.
            for block_index, burn_dict in enumerate(scene_burns):
                block_text = str(burn_dict.get("text") or "").strip()
                elem = _element_from_burn_group(
                    [burn_dict],
                    text=block_text,
                    start_s=float(burn_dict.get("start_s") or scene_start),
                    end_s=float(burn_dict.get("end_s") or scene_end),
                    source="sequence_scene",
                    key=f"{i}:{block_index}",
                    intro_mode="sequence",
                    intro_layout=intro_layout,
                    intro_text_size_px=intro_text_size_px,
                )
                if elem is not None:
                    elem.role = "generative_sequence"
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
    placement_candidates = v.get("text_placement_candidates") or []
    first_candidate = placement_candidates[0] if placement_candidates else None
    if isinstance(first_candidate, dict):
        style_kwargs.update(
            {
                "position": "center",
                "position_x_frac": first_candidate.get("x_frac"),
                "position_y_frac": first_candidate.get("y_frac"),
                "max_width_frac": first_candidate.get("max_width_frac"),
                "rotation_deg": first_candidate.get("rotation_deg"),
                "text_anchor": "center",
            }
        )

    try:
        from app.pipeline.generative_overlays import (  # noqa: PLC0415
            build_persistent_intro_overlays,
        )

        burn_dicts_list = build_persistent_intro_overlays(
            text=intro_text,
            effect=intro_effect,
            reveal_window_s=min(_ADAPTER_REVEAL_WINDOW_S, max(0.1, intro_end_s - intro_start_s)),
            text_color=intro_text_color,
            layout=layout,
            word_roles=intro_word_roles,
            start_s=intro_start_s,
            end_s=intro_end_s,
            **style_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("text_elements_adapter_linear_failed", error=str(exc))
        return []

    if not burn_dicts_list:
        return []

    if layout == "cluster":
        grouped: list[TextElement] = []
        by_block: dict[tuple[str, float | None, float | None], list[dict]] = {}
        for bd in burn_dicts_list:
            key = (
                str(bd.get("text") or ""),
                bd.get("position_x_frac"),
                bd.get("position_y_frac"),
            )
            by_block.setdefault(key, []).append(bd)
        for i, group in enumerate(by_block.values()):
            elem = _element_from_burn_group(
                group,
                text=str(group[0].get("text") or "").strip(),
                start_s=min(float(bd.get("start_s") or 0.0) for bd in group),
                end_s=max(float(bd.get("end_s") or intro_end_s) for bd in group),
                source="intro_cluster",
                key=str(i),
                intro_mode=intro_mode or layout,
                intro_layout=intro_layout,
                intro_text_size_px=intro_text_size_px,
            )
            if elem is not None:
                grouped.append(elem)
        return grouped

    elem = _element_from_burn_group(
        burn_dicts_list,
        text=intro_text,
        start_s=intro_start_s,
        end_s=intro_end_s,
        source="intro",
        key="intro",
        intro_mode=intro_mode or layout,
        intro_layout=intro_layout,
        intro_text_size_px=intro_text_size_px,
    )
    return [elem] if elem is not None else []
