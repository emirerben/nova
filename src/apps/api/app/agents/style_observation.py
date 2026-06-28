"""nova.video.style_observation — observe visual text-style choices in a creator's video.

This is the vision-analysis building block for the TikTok-style personalization
pipeline. It watches one video from a creator's own catalog and extracts how
they style on-screen text: font feel, colors, position, size, layout, and stroke.

It deliberately emits **semantic vocabulary** (font_feel, size_class), never raw
font names or pixel values — it can't know Nova's font registry or px envelope.
The class→knob mapping is handled downstream by ``style_derivation`` which already
owns the font catalog, parity coercion, and style-set matching.

The vocabulary used in `VideoStyleObservation` is intentionally constrained:
- Literals are a parity guard (the model literally cannot emit a raw font name
  or an `effect` field, which is the CLAUDE.md #296 renderer-parity invariant).
- ``parse()`` coerces every field to its Literal and drops unknowns defensively.

Usage:
    agent = StyleObservationAgent()
    obs = await agent.run(StyleObservationInput(file_uri="...", caption="..."))
    # obs is a VideoStyleObservation or None on hard failure

Called by ``app.tasks.style_vision_build`` (PR 2) which:
  1. Downloads each TikTok MP4 to a TemporaryDirectory.
  2. Uploads to the Gemini File API via ``gemini_upload_and_wait``.
  3. Calls this agent per video.
  4. Deterministically aggregates N observations into one composite.
  5. Writes the aggregate to ``persona.tiktok_profile["style_observations"]``.
  6. Passes the aggregate as structured DATA into ``style_derivation`` (PR 3).
"""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from app.agents._runtime import (
    Agent,
    AgentSpec,
    SchemaError,
)
from app.pipeline.prompt_loader import load_prompt

# ── Vocabulary constants ───────────────────────────────────────────────────────
# Constrained Literals are a renderer-parity guard: the model cannot emit a raw
# font name or `effect`. Keep in sync with ``prompts/observe_video_style.txt``.

FontFeel = Literal[
    "serif_editorial",   # e.g. Playfair Display, Cormorant, Gloock
    "clean_sans",        # e.g. DM Sans, Inter, Poppins
    "bold_display",      # e.g. Bebas Neue, Anton, Bangers
    "handwritten",       # e.g. Permanent Marker, brush fonts
    "mono",              # e.g. monospaced / typewriter
    "none",              # no on-screen text visible
]
Position = Literal["top", "center", "bottom", "center-above", "center-below"]
SizeClass = Literal["small", "medium", "large"]
Layout = Literal["linear", "cluster"]
Stroke = Literal["none", "thin", "thick"]
TextAnchor = Literal["left", "center", "right"]

# Derived from the Literals so adding a value to the type is the only change needed.
_FONT_FEELS: tuple[str, ...] = get_args(FontFeel)
_POSITIONS: tuple[str, ...] = get_args(Position)
_SIZE_CLASSES: tuple[str, ...] = get_args(SizeClass)
_LAYOUTS: tuple[str, ...] = get_args(Layout)
_STROKES: tuple[str, ...] = get_args(Stroke)
_ANCHORS: tuple[str, ...] = get_args(TextAnchor)

# render_prompt thresholds
_CAPTION_MAX_CHARS = 300
_TOP_PERFORMER_VIEW_INDEX = 2.0
_UNDERPERFORMER_VIEW_INDEX = 0.5
# Default confidence when no confidence field is returned
_CONFIDENCE_DEFAULT_NO_TEXT = 0.9  # absence of text is unambiguous
_CONFIDENCE_DEFAULT_HAS_TEXT = 0.7  # style observation carries perceptual ambiguity


# ── Schemas ───────────────────────────────────────────────────────────────────


class StyleObservationInput(BaseModel):
    """Per-video input for the style observation agent."""

    file_uri: str           # Gemini File API URI (returned by gemini_upload_and_wait)
    file_mime: str = "video/mp4"
    caption: str = ""       # TikTok caption — optional context for the prompt
    # Relative performance vs the creator's own median (views ÷ median_views).
    # Passed to the prompt so the model can note "this was a top-performer" for
    # display in the provenance card. NOT used for weighting in the aggregator
    # (the deterministic mode/majority-vote aggregator is data-driven, not score-weighted).
    view_index: float | None = None

    @field_validator("view_index")
    @classmethod
    def _validate_view_index(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if math.isnan(v) or math.isinf(v) or v < 0:
            raise ValueError(f"view_index must be a finite non-negative number, got {v}")
        return v


class VideoStyleObservation(BaseModel):
    """Raw per-video style observation — NOT a UserStyle.

    Every field is Optional/Literal — a video with no on-screen text should
    simply return ``has_on_screen_text=False`` with all other fields as None.
    The ``parse()`` method coerces unknown values to None (defensive) so a
    drifted Gemini response never breaks aggregation.

    This is intentionally NOT ``StyleKnobs``/``UserStyle`` — it speaks
    semantic classes that Gemini can observe visually, not Nova's internal
    pixel/font-registry vocabulary. The mapping is done by ``style_derivation``.

    extra="forbid" is the renderer-parity guard: it prevents an `effect` field
    or any other Nova-internal key from being injected (mirrors StyleKnobs).
    """

    model_config = ConfigDict(extra="forbid")

    has_on_screen_text: bool = False
    font_feel: FontFeel = "none"
    # Hex colors, normalized lowercase (e.g. "#ffffff"). None when unclear.
    text_color_hex: str | None = None
    highlight_color_hex: str | None = None
    position: Position | None = None
    size_class: SizeClass | None = None
    layout: Layout | None = None
    stroke: Stroke | None = None
    text_anchor: TextAnchor | None = None
    # Agent's self-assessed confidence (0–1). Used by the provenance card to
    # surface "seen clearly" vs "inferred". NOT used to weight the aggregator.
    confidence: float = 0.5


# ── Coercion helpers (used in parse()) ────────────────────────────────────────


def _coerce_optional_literal(
    v: object, valid: tuple[str, ...], *, default: str | None = None
) -> str | None:
    if isinstance(v, str) and v.strip().lower() in valid:
        return v.strip().lower()
    return default


def _coerce_hex_color(v: object) -> str | None:
    """Normalize a hex color string; return None on junk."""
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    # Accept #rrggbb or #rgb; reject everything else.
    if s.startswith("#") and len(s) in (4, 7):
        rest = s[1:]
        if all(c in "0123456789abcdef" for c in rest):
            return s
    return None


def _coerce_confidence(v: object) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


# ── Agent ─────────────────────────────────────────────────────────────────────


class StyleObservationAgent(Agent[StyleObservationInput, VideoStyleObservation]):
    """Watch a creator's own TikTok video and extract their text-style fingerprint."""

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.style_observation",
        prompt_id="observe_video_style",
        # Bump whenever the prompt or output vocabulary changes.
        # PR 3 (wire into style_derivation) should NOT change this version.
        prompt_version="2026-06-28",
        model="gemini-2.5-flash",
        # Pricing same as clip_metadata (same model).
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Moderate thinking budget — the observation task is visually specific
        # but structurally simple (emit a constrained vocabulary object).
        # 512 matches clip_metadata which has similar visual complexity.
        thinking_budget=512,
        # Per-video best-effort: a refusal or bad JSON on one video just drops
        # that video from the aggregate (like _upload_clips_parallel returning
        # None). Skip the clarification retry that doubles latency on bad
        # responses — the aggregator tolerates missing videos.
        enable_clarification_retries=False,
        # Gemini occasionally truncates near token ceiling on long videos;
        # json-repair salvages punctuation-broken responses.
        enable_json_repair=True,
    )
    Input = StyleObservationInput
    Output = VideoStyleObservation

    # ── Media wiring (like clip_metadata) ─────────────────────────

    def media_uri(self, input: StyleObservationInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: StyleObservationInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        # has_on_screen_text is the only reliable required field.
        # A video with no text is a valid, useful observation (tells the
        # aggregator this creator doesn't use on-screen text in this format).
        return ["has_on_screen_text"]

    # ── Prompt ────────────────────────────────────────────────────

    def render_prompt(self, input: StyleObservationInput) -> str:  # noqa: A002
        caption_block = ""
        if input.caption.strip():
            cap = input.caption.strip()[:_CAPTION_MAX_CHARS]
            caption_block = f'Caption (for context only — do not copy): "{cap}"'

        performance_block = ""
        if input.view_index is not None:
            if input.view_index >= _TOP_PERFORMER_VIEW_INDEX:
                performance_block = (
                    f"This video is a top performer for this creator "
                    f"({input.view_index:.1f}× their median views). "
                    "Weight its style choices more in your observation."
                )
            elif input.view_index < _UNDERPERFORMER_VIEW_INDEX:
                performance_block = (
                    f"This video underperformed for this creator "
                    f"({input.view_index:.1f}× their median views)."
                )

        return load_prompt(
            "observe_video_style",
            caption_block=caption_block,
            performance_block=performance_block,
        )

    # ── Parse ─────────────────────────────────────────────────────

    def parse(  # noqa: A002
        self, raw_text: str, input: StyleObservationInput
    ) -> VideoStyleObservation:
        try:
            data: Any = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"style_observation: invalid JSON — {exc}") from exc

        if not isinstance(data, dict):
            raise SchemaError("style_observation: response is not a JSON object")

        # has_on_screen_text — the only required field; must be a JSON boolean.
        # required_fields() raises RefusalError before parse() when the key is
        # absent — so the None guard below is defense-in-depth (unreachable in
        # normal flow, but cheap insurance if the runtime behavior ever changes).
        has_text_raw = data.get("has_on_screen_text")
        if has_text_raw is None:
            raise SchemaError("style_observation: missing required field 'has_on_screen_text'")
        # Reject string booleans ("false", "true") — bool("false") is True,
        # which would silently corrupt the observation for no-text videos.
        if not isinstance(has_text_raw, bool):
            raise SchemaError(
                f"style_observation: 'has_on_screen_text' must be a JSON boolean, "
                f"got {type(has_text_raw).__name__} ({has_text_raw!r})"
            )
        has_on_screen_text: bool = has_text_raw

        # If no on-screen text, everything else is meaningless — return early.
        # We do NOT raise — "no text" is a valid, useful observation.
        if not has_on_screen_text:
            return VideoStyleObservation(
                has_on_screen_text=False,
                font_feel="none",
                confidence=_coerce_confidence(data.get("confidence", _CONFIDENCE_DEFAULT_NO_TEXT)),
            )

        return VideoStyleObservation(
            has_on_screen_text=True,
            font_feel=_coerce_optional_literal(data.get("font_feel"), _FONT_FEELS, default="none"),  # type: ignore[arg-type]
            text_color_hex=_coerce_hex_color(data.get("text_color_hex")),
            highlight_color_hex=_coerce_hex_color(data.get("highlight_color_hex")),
            position=_coerce_optional_literal(data.get("position"), _POSITIONS),
            size_class=_coerce_optional_literal(data.get("size_class"), _SIZE_CLASSES),
            layout=_coerce_optional_literal(data.get("layout"), _LAYOUTS),
            stroke=_coerce_optional_literal(data.get("stroke"), _STROKES),
            text_anchor=_coerce_optional_literal(data.get("text_anchor"), _ANCHORS),
            confidence=_coerce_confidence(data.get("confidence", _CONFIDENCE_DEFAULT_HAS_TEXT)),
        )
