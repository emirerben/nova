"""nova.layout.text_designer — per-slot label styling.

Replaces (eventually) the `_LABEL_CONFIG` static dict in `template_orchestrate.py`.
For the first 200 production jobs this runs in **shadow mode** alongside the
static dict — both outputs are computed; the dict's output is committed; the
agent's output is logged for divergence analysis.

Bail-out: if shadow output diverges from the dict on >5% of first-slot labels,
kill the agent and keep the dict. Honest assessment: a 14-line static dict
usually beats a per-call LLM here. The shadow mode is a one-way door — if the
agent earns its keep, promote it; if not, delete this file.

For shadow comparison, `_LABEL_CONFIG_shadow` provides a callable that mirrors
the legacy dict semantics with the same Pydantic Output shape.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError


_VALID_TEXT_SIZES = ("small", "medium", "large", "xlarge", "xxlarge")
_VALID_FONT_STYLES = ("serif", "sans", "mono")
_VALID_EFFECTS = (
    "none", "pop-in", "fade-in", "scale-up", "font-cycle",
    "typewriter", "glitch", "bounce", "slide-in", "slide-up", "static",
)


class TextDesignerInput(BaseModel):
    slot_position: int = Field(..., ge=1)
    slot_type: str = "broll"
    placeholder_kind: Literal["prefix", "subject", "other"] = "other"
    copy_tone: str = ""
    creative_direction: str = ""


class TextDesignerOutput(BaseModel):
    text_size: str = "large"
    font_style: str = "sans"
    text_color: str = "#FFFFFF"  # hex
    start_s: float = Field(default=0.0, ge=0)
    effect: str = "none"
    accel_at_s: float | None = None  # seconds; None = no accel


class TextDesignerAgent(Agent[TextDesignerInput, TextDesignerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.layout.text_designer",
        prompt_id="_inline",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        # Single attempt — text_designer runs in shadow mode against the static
        # dict, so retries don't help (the dict result is what gets committed).
        max_attempts=1,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = TextDesignerInput
    Output = TextDesignerOutput

    def render_prompt(self, input: TextDesignerInput) -> str:  # noqa: A002
        return (
            "Design the visual styling for a text overlay on a short-form video clip.\n\n"
            f"Slot position: {input.slot_position} (1 = hook / first slot)\n"
            f"Slot type: {input.slot_type}\n"
            f'Placeholder kind: "{input.placeholder_kind}" '
            f"(prefix = small italic serif setup text; "
            f"subject = large gold sans-serif anchor; other = standard label)\n"
            + (f'Copy tone: "{input.copy_tone}"\n' if input.copy_tone else "")
            + (
                f'Creative direction: "{input.creative_direction}"\n'
                if input.creative_direction
                else ""
            )
            + "\nValid options:\n"
            f"  text_size: {' | '.join(_VALID_TEXT_SIZES)}\n"
            f"  font_style: {' | '.join(_VALID_FONT_STYLES)}\n"
            f"  effect: {' | '.join(_VALID_EFFECTS)}\n"
            "  text_color: hex code, e.g. #FFFFFF or #F4D03F\n"
            "  accel_at_s: float seconds, or null (only used with font-cycle)\n\n"
            "Reference: subject placeholders on first slot are typically:\n"
            '  text_size=xxlarge, font_style=sans, text_color="#F4D03F" (gold), '
            'start_s=3.0, effect="font-cycle", accel_at_s=8.0\n'
            "Prefix placeholders on first slot are typically:\n"
            "  text_size=small, font_style=serif, start_s=2.0, effect=none\n\n"
            'Return JSON: {"text_size": str, "font_style": str, "text_color": str, '
            '"start_s": float, "effect": str, "accel_at_s": float | null}\n'
            "Return ONLY valid JSON, no markdown."
        )

    def parse(
        self, raw_text: str, input: TextDesignerInput  # noqa: A002, ARG002
    ) -> TextDesignerOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"text_designer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("text_designer: response is not a JSON object")

        text_size = str(data.get("text_size", "large") or "large")
        if text_size not in _VALID_TEXT_SIZES:
            text_size = "large"
        font_style = str(data.get("font_style", "sans") or "sans")
        if font_style not in _VALID_FONT_STYLES:
            font_style = "sans"
        effect = str(data.get("effect", "none") or "none")
        if effect not in _VALID_EFFECTS:
            effect = "none"
        text_color = str(data.get("text_color", "#FFFFFF") or "#FFFFFF")
        if not (text_color.startswith("#") and len(text_color) in (4, 7)):
            text_color = "#FFFFFF"

        try:
            start_s = max(0.0, float(data.get("start_s", 0.0) or 0.0))
        except (TypeError, ValueError):
            start_s = 0.0

        accel_raw = data.get("accel_at_s", None)
        accel_at_s: float | None
        if accel_raw is None:
            accel_at_s = None
        else:
            try:
                accel_at_s = float(accel_raw)
            except (TypeError, ValueError):
                accel_at_s = None

        return TextDesignerOutput(
            text_size=text_size,
            font_style=font_style,
            text_color=text_color,
            start_s=start_s,
            effect=effect,
            accel_at_s=accel_at_s,
        )


# ── Shadow-mode comparison helper ─────────────────────────────────────────────


def label_config_shadow(input: TextDesignerInput) -> TextDesignerOutput:  # noqa: A002
    """Replicates the legacy `_LABEL_CONFIG` dict for shadow comparison.

    Used by `run_with_shadow(TextDesignerAgent, label_config_shadow, ...)`. The
    dict's output is the committed primary output; the agent runs in parallel and
    its divergence from this is logged for analysis.
    """
    if input.placeholder_kind == "prefix":
        return TextDesignerOutput(
            text_size="small",
            font_style="serif",
            text_color="#FFFFFF",
            start_s=2.0,
            effect="none",
            accel_at_s=None,
        )
    if input.placeholder_kind == "subject":
        return TextDesignerOutput(
            text_size="xxlarge",
            font_style="sans",
            text_color="#F4D03F",  # warm maize/gold
            start_s=3.0,
            effect="font-cycle",
            accel_at_s=8.0,
        )
    # "other" — standard label fallback
    return TextDesignerOutput(
        text_size="large",
        font_style="sans",
        text_color="#FFFFFF",
        start_s=0.0,
        effect="none",
        accel_at_s=None,
    )
