"""nova.compose.overlay_format_matcher — pick the FORM of a generative intro overlay.

Given a summary of the user's clip set and the hero clip, this agent chooses the
overlay's visual form (effect / position / size / colors / anchor) by matching the
content against a curated few-shot library (`prompts/overlay_examples.json`). It does
NOT write the words — that's `intro_writer`. Keeping form and copy in separate agents
makes each cheaper to eval and lets the writer condition on the chosen form.

Text-only (consumes `clip_metadata` output, never re-runs vision). Mirrors
`music_matcher` in shape: render the library into the prompt, validate hard in parse().

`effect` is coerced to the Skia-renderer-known set in parse(); a hallucinated effect
becomes `static` rather than producing an un-renderable overlay. (Generative overlays
are injected directly into the recipe, bypassing the `template_text` VALID_EFFECTS
gate — see `app/pipeline/generative_overlays.py`.)
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.music_matcher import ClipSummary, _sanitize_text
from app.agents.overlay_examples import load_overlay_examples
from app.pipeline.prompt_loader import load_prompt

# Source of truth duplicated from generative_overlays for the validator. The renderer
# can draw exactly these for a generated intro overlay.
_SKIA_EFFECTS = ("karaoke-line", "pop-in", "fade-in", "scale-up", "static")
_DEFAULT_EFFECT = "static"
_SIZE_CLASSES = ("small", "medium", "large", "xlarge", "xxlarge", "jumbo")
_POSITIONS = ("center", "center-above", "center-label", "center-below", "top", "bottom")
_ANCHORS = ("left", "right", "center")
# "linear" = one centered block (the historical intro); "cluster" = editorial
# word-cluster (multiple positioned blocks, mixed sizes — intro_cluster.py).
_LAYOUTS = ("linear", "cluster")
_HEX_LEN = (4, 7)  # "#RGB" or "#RRGGBB"


class OverlayFormatMatcherInput(BaseModel):
    clip_set_summary: str = ""
    hero_clip: ClipSummary
    # Target render language. Drives form-bias hint in the prompt — some forms
    # (e.g. dense list-of-3) read awkwardly in agglutinative languages like Turkish.
    language: str = "en"


class OverlayFormatMatcherOutput(BaseModel):
    effect: str
    position: str = "center"
    size_class: str = "jumbo"
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFD24A"
    text_anchor: str = "center"
    layout: str = "linear"
    layout_source: str = "coerced_default"
    matched_example_ids: list[str] = Field(default_factory=list)


def _coerce_choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").strip()
    return v if v in allowed else default


def _coerce_hex(value: object, default: str) -> str:
    v = str(value or "").strip()
    if v.startswith("#") and len(v) in _HEX_LEN:
        try:
            int(v[1:], 16)
            return v.upper()
        except ValueError:
            return default
    return default


def _format_example(e) -> str:
    return (
        f'- id={e.id} | profile="{_sanitize_text(e.content_profile)}" | '
        f"effect={e.effect} | layout={e.layout} | position={e.position} | "
        f"size={e.size_class} | colors={e.text_color}/{e.highlight_color} | "
        f'sample="{_sanitize_text(e.text)}"'
    )


_LANGUAGE_HINTS: dict[str, str] = {
    "en": (
        "Output text will be written in English. Pick the form that best suits the "
        "content profile — no language-specific constraints."
    ),
    "tr": (
        "Output text will be written in TURKISH. Turkish is agglutinative — words "
        "are longer on average than English, so AVOID forms that depend on a short "
        "snappy 2-3 word reveal (e.g. dense karaoke-line on a packed 10-word phrase). "
        "Prefer simpler forms (`fade-in`, `static`) when a Turkish phrasing would "
        "overflow a karaoke reveal. Be conservative with `layout=cluster` — long "
        "Turkish words crowd a word-cluster; pick it only when the hook would be "
        "3-5 SHORT words. Lowercase Turkish is the norm; choose colors "
        "that respect that voice."
    ),
}


class OverlayFormatMatcherAgent(Agent[OverlayFormatMatcherInput, OverlayFormatMatcherOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.overlay_format_matcher",
        prompt_id="match_overlay_format",
        # 2026-06-12 — broadened cluster selection to hook-shape-driven and added
        #              energetic/people/lifestyle cluster examples.
        # 2026-06-10 — added `layout` (linear|cluster) + 3 cluster examples in
        #              overlay_examples.json (editorial word-cluster intro).
        # 2026-05-29 — overlay_examples.json grown with market-research hooks.
        # 2026-05-28 — added $language_hint block (en|tr).
        # 2026-06-14 — weekly research refresh: added professional-ootd-static-01 overlay example.
        prompt_version="2026-07-12",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Cap reasoning: format selection from a fixed example set. A/B on a real
        # clip showed default 13.5s vs ~5s at 512 with the same/larger matched
        # set — no quality loss. See clip_metadata for the validation context.
        thinking_budget=512,
    )
    Input = OverlayFormatMatcherInput
    Output = OverlayFormatMatcherOutput

    def required_fields(self) -> list[str]:
        return ["effect"]

    def render_prompt(self, input: OverlayFormatMatcherInput) -> str:  # noqa: A002
        examples = load_overlay_examples()
        c = input.hero_clip
        hero_line = (
            f'subject="{_sanitize_text(c.subject)}" | '
            f'hook="{_sanitize_text(c.hook_text)}" | '
            f"hook_score={c.hook_score:.1f} | energy={c.energy:.1f} | "
            f'desc="{_sanitize_text(c.description)}"'
        )
        return load_prompt(
            "match_overlay_format",
            clip_set_summary=_sanitize_text(input.clip_set_summary) or "(no summary)",
            hero_clip=hero_line,
            example_lines="\n".join(_format_example(e) for e in examples),
            valid_effects=", ".join(_SKIA_EFFECTS),
            valid_positions=", ".join(_POSITIONS),
            valid_sizes=", ".join(_SIZE_CLASSES),
            valid_anchors=", ".join(_ANCHORS),
            valid_layouts=", ".join(_LAYOUTS),
            language_hint=_LANGUAGE_HINTS.get(input.language, _LANGUAGE_HINTS["en"]),
        )

    def parse(
        self,
        raw_text: str,
        input: OverlayFormatMatcherInput,  # noqa: A002
    ) -> OverlayFormatMatcherOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"overlay_format_matcher: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("overlay_format_matcher: response is not a JSON object")

        valid_ids = {e.id for e in load_overlay_examples()}
        matched = [
            str(i).strip()
            for i in (data.get("matched_example_ids") or [])
            if isinstance(i, str) and str(i).strip() in valid_ids
        ]

        effect = _coerce_choice(data.get("effect"), _SKIA_EFFECTS, _DEFAULT_EFFECT)
        raw_layout = str(data.get("layout") or "").strip()
        layout_source = "model" if raw_layout in _LAYOUTS else "coerced_default"
        layout = raw_layout if layout_source == "model" else "linear"
        # A cluster owns its reveal (per-block staggered fade-in); karaoke's
        # word-by-word sweep is incompatible with multi-block geometry. Keep the
        # layout choice and settle the effect.
        if layout == "cluster" and effect == "karaoke-line":
            effect = "fade-in"

        try:
            return OverlayFormatMatcherOutput(
                effect=effect,
                position=_coerce_choice(data.get("position"), _POSITIONS, "center"),
                size_class=_coerce_choice(data.get("size_class"), _SIZE_CLASSES, "jumbo"),
                text_color=_coerce_hex(data.get("text_color"), "#FFFFFF"),
                highlight_color=_coerce_hex(data.get("highlight_color"), "#FFD24A"),
                text_anchor=_coerce_choice(data.get("text_anchor"), _ANCHORS, "center"),
                layout=layout,
                layout_source=layout_source,
                matched_example_ids=matched,
            )
        except ValidationError as exc:
            raise SchemaError(f"overlay_format_matcher: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY a JSON object with keys effect, position, "
            "size_class, text_color, highlight_color, text_anchor, layout, "
            "matched_example_ids. "
            f"`effect` MUST be one of: {', '.join(_SKIA_EFFECTS)}. "
            f"`layout` MUST be one of: {', '.join(_LAYOUTS)}."
        )
