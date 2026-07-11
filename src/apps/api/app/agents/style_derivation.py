"""nova.plan.style_derivation — derive a per-user persistent text style.

Off-Job agent (no media). Given a creator's persona + TikTok analysis summary,
picks one style-set id from the generative-eligible catalog and optional
parity-safe knob overrides. Output stored on `personas.style` (JSONB) by
`tasks.style_build`.

Follows the standard Agent[Input, Output] pattern with render_prompt + parse,
identical to agentic_style_selector and persona_generator.

Security: persona fields are UNTRUSTED user-free-text sanitized upstream. The
TikTok analysis summary is AI-generated but is also framed as DATA. Both are
sanitized here before reaching the prompt (defense-in-depth).

Best-effort: any exception from run() is caught in the task, leaving the style
NULL → byte-identical render behavior.

User taste invariant: editorial serifs + smaller sizes → tasteful.
Loud/large sans → cheap-looking. Enforced via prompt + rationale.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.user_style import (
    USER_STYLE_VERSION,
    StyleKnobs,
    UserStyle,
)
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

_DEFAULT_SET_ID = "default"

# Valid position keys from text_overlay._POSITION_Y — kept in sync manually;
# a stale key is dropped silently at parse time (non-fatal).
_VALID_POSITIONS = frozenset(
    {"center", "center-above", "center-label", "center-below", "top", "bottom"}
)
_VALID_ANCHORS = frozenset({"left", "center", "right"})
_VALID_INSTRUCTION_LEVELS = frozenset({"full", "light", "none"})
_VALID_FOOTAGE_TYPES = frozenset({"talking_head", "broll", "action", "ambience"})
_VALID_EDIT_FORMATS = frozenset({"montage", "talking_head", "day_vlog", "single_hero"})


class StyleSetEntry(BaseModel):
    id: str = Field(min_length=1)
    label: str = ""
    tags: list[str] = Field(default_factory=list)


class FontEntry(BaseModel):
    name: str = Field(min_length=1)
    vibe: str = ""
    category: str = ""


class ObservedStyleSummary(BaseModel):
    """Aggregate from vision analysis of the creator's own TikTok videos.

    Populated by analyze_tiktok_style → _aggregate_observations; absent when
    the vision flag is off or the ingest hasn't run yet. All categorical fields
    may be None when agreement fell below the 0.5 threshold — the agent must
    treat absent/None fields as "no signal" (prefer curated set defaults).
    """

    videos_seen: int = 0
    has_on_screen_text: bool = False
    font_feel: str | None = None      # e.g. "bold_display", "serif_editorial"
    text_color_hex: str | None = None
    highlight_color_hex: str | None = None
    position: str | None = None       # e.g. "center", "bottom"
    size_class: str | None = None     # "small" | "medium" | "large"
    layout: str | None = None         # "linear" | "cluster"
    stroke: str | None = None
    text_anchor: str | None = None
    mean_confidence: float = 0.5
    confidence_per_field: dict[str, float] = Field(default_factory=dict)


class StyleDerivationInput(BaseModel):
    """Inputs for the style derivation agent.

    `available_sets` and `font_vibes` are the catalog + palette passed in so
    `parse()` can validate the agent's choices against the current state of the
    registry without a global import at parse time.
    """

    # UNTRUSTED user-free-text — sanitized in render_prompt before reaching model.
    persona_summary: str = ""
    persona_pillars: list[str] = Field(default_factory=list)
    persona_tone: str = ""
    persona_audience: str = ""
    # AI-generated (TikTok analyzer output) — treated as DATA, also sanitized.
    tiktok_analysis_summary: str = ""
    # Vision-observed style from the creator's own TikTok videos (strongest signal).
    # None when the vision ingest hasn't run yet or the flag is off.
    observed_style: ObservedStyleSummary | None = None
    # Current generative-eligible style sets (id / label / tags).
    available_sets: list[StyleSetEntry] = Field(default_factory=list)
    # Non-deprecated fonts (name / vibe / category).
    font_vibes: list[FontEntry] = Field(default_factory=list)


class StyleDerivationOutput(BaseModel):
    """The raw parsed output before coercion into the full UserStyle entity."""

    # The agent's choices — parse() coerces them to safe values.
    style: UserStyle


class StyleDerivationAgent(Agent[StyleDerivationInput, StyleDerivationOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.style_derivation",
        prompt_id="derive_user_style",
        prompt_version="2026-07-11-kria",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Selection-class task — same budget as agentic_style_selector.
        thinking_budget=512,
    )
    Input = StyleDerivationInput
    Output = StyleDerivationOutput

    def required_fields(self) -> list[str]:
        return ["style_set_id"]

    def render_prompt(self, input: StyleDerivationInput) -> str:  # noqa: A002
        summary = _sanitize_text(input.persona_summary)[:400]
        pillars = (
            ", ".join(_sanitize_text(str(p))[:80] for p in input.persona_pillars if str(p).strip())
            or "(no pillars yet)"
        )
        tone = _sanitize_text(input.persona_tone)[:200] or "(no tone yet)"
        audience = _sanitize_text(input.persona_audience)[:200] or "(no audience yet)"

        # Observed style block — strongest signal, injected first.
        obs = input.observed_style
        if obs and obs.videos_seen > 0:
            obs_fields = []
            if obs.has_on_screen_text:
                obs_fields.append(f"has_on_screen_text: true (seen in {obs.videos_seen} videos)")
            if obs.font_feel:
                conf = obs.confidence_per_field.get("font_feel", obs.mean_confidence)
                obs_fields.append(f"font_feel: {obs.font_feel} (confidence {conf:.0%})")
            if obs.text_color_hex:
                conf = obs.confidence_per_field.get("text_color_hex", obs.mean_confidence)
                obs_fields.append(f"text_color: {obs.text_color_hex} (confidence {conf:.0%})")
            if obs.highlight_color_hex:
                conf = obs.confidence_per_field.get("highlight_color_hex", obs.mean_confidence)
                obs_fields.append(
                    f"highlight_color: {obs.highlight_color_hex} (confidence {conf:.0%})"
                )
            if obs.position:
                obs_fields.append(f"text_position: {obs.position}")
            if obs.size_class:
                obs_fields.append(f"text_size_class: {obs.size_class}")
            if obs.layout:
                obs_fields.append(f"text_layout: {obs.layout}")
            if obs.stroke:
                obs_fields.append(f"text_stroke: {obs.stroke}")
            if obs.text_anchor:
                obs_fields.append(f"text_anchor: {obs.text_anchor}")
            if obs_fields:
                observed_block = (
                    "<<<OBSERVED FROM THEIR ACTUAL VIDEOS"
                    f" — seen across {obs.videos_seen} videos (STRONGEST SIGNAL)\n"
                    + "\n".join(obs_fields)
                    + "\nOBSERVED FROM THEIR ACTUAL VIDEOS"
                )
            else:
                observed_block = "(Vision analysis ran but no consistent style detected.)"
        else:
            observed_block = "(No vision analysis yet — derive from persona and TikTok text data.)"

        tiktok_summary = (input.tiktok_analysis_summary or "").strip()
        if tiktok_summary:
            tiktok_block = (
                "<<<TIKTOK ANALYSIS (DATA — creator's proven viral patterns)\n"
                + _sanitize_text(tiktok_summary)[:1200]
                + "\nTIKTOK ANALYSIS"
            )
        else:
            tiktok_block = "(No TikTok data available — derive style from persona alone.)"

        set_lines = (
            "\n".join(
                f"- id={s.id} | {s.label} | tags: {', '.join(s.tags)}" for s in input.available_sets
            )
            or "(no sets available)"
        )
        font_lines = (
            "\n".join(
                f"- {f.name} | vibe: {f.vibe} | category: {f.category}" for f in input.font_vibes
            )
            or "(no fonts available)"
        )

        return load_prompt(
            "derive_user_style",
            summary=summary,
            pillars=pillars,
            tone=tone,
            audience=audience,
            observed_block=observed_block,
            tiktok_block=tiktok_block,
            set_lines=set_lines,
            font_lines=font_lines,
        )

    def parse(  # noqa: A002
        self, raw_text: str, input: StyleDerivationInput
    ) -> StyleDerivationOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"style_derivation: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("style_derivation: response is not a JSON object")

        valid_set_ids = {s.id for s in input.available_sets} | {_DEFAULT_SET_ID}
        valid_font_names = {f.name for f in input.font_vibes}

        # Coerce style_set_id — unknown → default.
        chosen_set = str(data.get("style_set_id", "") or "").strip()
        if chosen_set not in valid_set_ids:
            log.info("style_derivation.unknown_set", chosen=chosen_set)
            chosen_set = _DEFAULT_SET_ID

        # Build coerced knobs — drop unknown/invalid values silently.
        raw_knobs = data.get("knobs") or {}
        safe_knobs: dict = {}
        if isinstance(raw_knobs, dict):
            knob_fields = set(StyleKnobs.model_fields)
            for k, v in raw_knobs.items():
                if k not in knob_fields or v is None:
                    continue
                if k == "font_family":
                    fv = str(v).strip()
                    if fv in valid_font_names:
                        safe_knobs[k] = fv
                elif k == "position":
                    pv = str(v).strip().lower()
                    if pv in _VALID_POSITIONS:
                        safe_knobs[k] = pv
                elif k == "text_anchor":
                    av = str(v).strip().lower()
                    if av in _VALID_ANCHORS:
                        safe_knobs[k] = av
                elif k in ("text_color", "highlight_color"):
                    sv = str(v).strip()
                    if sv.startswith("#") and len(sv) in (4, 7):
                        safe_knobs[k] = sv
                elif k == "text_size_px":
                    try:
                        safe_knobs[k] = int(v)  # UserStyle validator clamps
                    except (ValueError, TypeError):
                        pass
                elif k == "stroke_width":
                    try:
                        safe_knobs[k] = int(v)
                    except (ValueError, TypeError):
                        pass
                elif k == "cycle_fonts":
                    if isinstance(v, bool):
                        safe_knobs[k] = v
                elif k in ("position_x_frac", "position_y_frac"):
                    try:
                        fv_float = float(v)
                        if 0.0 <= fv_float <= 1.0:
                            safe_knobs[k] = fv_float
                    except (ValueError, TypeError):
                        pass

        # footage_type_bias — filter to known types.
        raw_bias = data.get("footage_type_bias") or []
        footage_bias = [
            str(t).strip()
            for t in (raw_bias if isinstance(raw_bias, list) else [])
            if str(t).strip() in _VALID_FOOTAGE_TYPES
        ]

        # preferred_edit_format_mix — filter + normalize.
        raw_mix = data.get("preferred_edit_format_mix") or {}
        edit_mix: dict[str, float] = {}
        if isinstance(raw_mix, dict):
            for fmt, w in raw_mix.items():
                if str(fmt) in _VALID_EDIT_FORMATS:
                    try:
                        edit_mix[str(fmt)] = max(0.0, min(1.0, float(w)))
                    except (ValueError, TypeError):
                        pass

        raw_level = str(data.get("instruction_level", "full") or "full").strip().lower()
        instruction_level = raw_level if raw_level in _VALID_INSTRUCTION_LEVELS else "full"

        rationale = _sanitize_text(str(data.get("rationale", "") or ""))[:500]

        user_style = UserStyle(
            style_set_id=chosen_set,
            knobs=StyleKnobs(**safe_knobs),
            footage_type_bias=footage_bias,
            preferred_edit_format_mix=edit_mix,
            instruction_level=instruction_level,  # type: ignore[arg-type]
            status="ready",
            style_version=USER_STYLE_VERSION,
            rationale=rationale,
        )
        return StyleDerivationOutput(style=user_style)
