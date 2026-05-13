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
    "none",
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
        prompt_version="2026-05-14",
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
        copy_tone_line = f'Copy tone: "{input.copy_tone}"\n' if input.copy_tone else ""
        cd_line = (
            f'Creative direction: "{input.creative_direction}"\n'
            if input.creative_direction
            else ""
        )
        return (
            "You are a kinetic-typography designer for a top-tier short-form creator.\n"
            "You make per-element typographic decisions — size, font, color, motion,\n"
            "timing — that turn a placeholder slot into the visual anchor of the moment.\n"
            "You think like a title designer (Saul Bass, Apple keynote, Spotify ad),\n"
            "not a slide-deck builder. Every decision serves the slot's role in the\n"
            "template, not your taste.\n\n"
            f"Slot position: {input.slot_position} "
            "(1 = hook / first slot — highest-stakes typography of the template)\n"
            f"Slot type: {input.slot_type}\n"
            f'Placeholder kind: "{input.placeholder_kind}"\n'
            "  - prefix  = small italic serif setup text; the lead-in to the subject. Quiet.\n"
            "  - subject = the visual anchor of the slot; large, bold, branded color.\n"
            "  - other   = standard label; conveys info without demanding the eye.\n"
            + copy_tone_line
            + cd_line
            + "\nValid options (emit ONLY these — invalid values are coerced to defaults):\n"
            f"  text_size:  {' | '.join(_VALID_TEXT_SIZES)}\n"
            f"  font_style: {' | '.join(_VALID_FONT_STYLES)}\n"
            f"  effect:     {' | '.join(_VALID_EFFECTS)}\n"
            "  text_color: hex code (#RRGGBB), e.g. #FFFFFF or #F4D03F\n"
            "  accel_at_s: float seconds, or null (only used with font-cycle)\n\n"
            "Decision principles:\n\n"
            "Hierarchy by placeholder kind:\n"
            "  - subject — dominates the frame. xxlarge on slot 1; xlarge or large later.\n"
            "    sans typically; serif only when creative direction asks for cinematic tone.\n"
            "    Color is the brand anchor — gold #F4D03F is the default; deviate only when\n"
            "    tone or creative direction explicitly calls for a different palette.\n"
            "    Effect marks the entrance: font-cycle for hook-slot subjects, lighter\n"
            "    effects (pop-in, scale-up, bounce, fade-in) for later slots.\n"
            "  - prefix — always supporting. small, serif. Neutral color: white #FFFFFF\n"
            "    or off-white #F4F1ED. Quiet effect: none, fade-in, or typewriter.\n"
            "    Appears BEFORE its companion subject to set up the read.\n"
            "  - other — standard label. medium size. font_style matches the template's\n"
            "    typographic register: sans for energetic, serif for cinematic.\n"
            "    Effect is none or fade-in unless the moment demands emphasis.\n\n"
            "Slot-position awareness:\n"
            "  - Slot 1 (hook) carries the heaviest typography. Attention is most fragile\n"
            "    here — the subject reveal must be deliberate and signature. font-cycle\n"
            "    with accel_at_s timed to a major musical beat is the established treatment.\n"
            "  - Mid-slots lean lighter. Repeat the typographic family from slot 1 for\n"
            "    consistency, but drop size by one step and use quieter effects.\n"
            "  - Final slot (CTA / outro) can return to bolder typography to close, but\n"
            "    don't copy slot 1 — pick a related but distinct effect.\n\n"
            "Tone → typography (when copy_tone is set, treat as a binding constraint).\n"
            "Current template_recipe emits one of: casual, formal, energetic, calm.\n"
            "  casual             → sans, gold/white, fade-in or pop-in\n"
            "  formal             → serif, white/gold/deep-red, scale-up or none\n"
            "  energetic          → bold sans, gold/white/pink, bounce or font-cycle\n"
            "  calm               → serif light, off-white/pink/sage, fade-in or scale-up\n"
            "Aspirational tones (rare today; future template_recipe schema expansion):\n"
            "  energetic-pop      → bold sans, gold/white/pink, bounce or font-cycle\n"
            "  chill-lofi         → serif light, off-white/pink/sage, fade-in or scale-up\n"
            "  dramatic-cinematic → serif, white/gold/deep-red, scale-up or none\n"
            "  melancholic-indie  → serif italic, cream/blue/rose, fade-in only\n"
            "  aggressive-trap    → bold sans, white/magenta/lime, font-cycle\n"
            "  dreamy-synthwave   → sans, magenta/cyan/blue, scale-up or font-cycle\n\n"
            "Creative direction outranks tone defaults when provided. Read it for explicit\n"
            "typographic cues and translate to the closest values in the allowed enums.\n\n"
            "Timing (start_s):\n"
            "  start_s is seconds RELATIVE to the slot's start, not the clip's start.\n"
            "  - prefix appears first — 0.5–2.0s into the slot.\n"
            "  - subject appears after the prefix lands — 2.0–3.0s for the long hook slot;\n"
            "    0.0–1.0s for short later slots.\n"
            "  - other appears when supporting info is needed — 0.5–1.5s typically.\n"
            "  - Never start a text element in the slot's last 0.5s — must be reading time\n"
            "    before the cut.\n\n"
            "accel_at_s — only set when effect == 'font-cycle'. It's the moment (seconds\n"
            "relative to slot start) when the cycling decelerates and locks on the final\n"
            "font. Time it to a major musical beat or visual emphasis. Slot-1 hook subject\n"
            "with a slow build-up: accel_at_s ≈ 8.0 is the known-good landing. For shorter\n"
            "slots, scale proportionally so the lock-in lands on a beat. Set to null for\n"
            "every other effect.\n\n"
            "Calibration patterns — when uncertain, snap to these:\n"
            "  subject on slot 1 (signature hook reveal):\n"
            '    {"text_size": "xxlarge", "font_style": "sans", "text_color": "#F4D03F",\n'
            '     "start_s": 3.0, "effect": "font-cycle", "accel_at_s": 8.0}\n'
            "  prefix on slot 1 (the lead-in):\n"
            '    {"text_size": "small", "font_style": "serif", "text_color": "#FFFFFF",\n'
            '     "start_s": 2.0, "effect": "none", "accel_at_s": null}\n'
            "  subject on a mid-slot (lighter, family-consistent):\n"
            '    {"text_size": "large", "font_style": "sans", "text_color": "#F4D03F",\n'
            '     "start_s": 0.5, "effect": "fade-in", "accel_at_s": null}\n\n'
            "Decisions, not suggestions. Each JSON value is the final answer the renderer\n"
            "will use. No hedging.\n\n"
            'Return JSON: {"text_size": str, "font_style": str, "text_color": str, '
            '"start_s": float, "effect": str, "accel_at_s": float | null}\n'
            "Return ONLY valid JSON, no markdown."
        )

    def parse(
        self,
        raw_text: str,
        input: TextDesignerInput,  # noqa: A002, ARG002
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
