"""nova.plan.style_intent — parse a free-text style utterance into a typed intent.

Called on each conversational turn of the style agent (POST /personas/agent/turn).
Takes the raw utterance + conversation history + compact style snapshot and returns
a typed intent, the fields to act on, a reply to show the user, and suggestion chips.

Design:
- Single-shot per turn: no persistent session, full history on each call.
- Hallucination-drop discipline (modeled on clip_plan_matcher):
  * only the 10 parity-safe knob keys may appear in fields.knobs
  * style_set_id coerced against catalog (unknown → needs_clarification)
  * confidence clamped to [0.0, 1.0]
  * unknown fields in output.fields are silently ignored (never raise on drift)
- Low confidence (< 0.55) → needs_clarification=True, no write at route level.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

import structlog
from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

STYLE_INTENT_PROMPT_VERSION = "2026-06-07-v2"

# The exactly 10 parity-safe knob keys (must match StyleKnobs in _schemas/user_style.py)
_PARITY_SAFE_KNOBS: frozenset[str] = frozenset(
    {
        "font_family",
        "text_size_px",
        "position",
        "position_x_frac",
        "position_y_frac",
        "text_anchor",
        "text_color",
        "highlight_color",
        "stroke_width",
        "cycle_fonts",
    }
)

_CONFIDENCE_CLARIFY_THRESHOLD = 0.55


# ── Schemas ───────────────────────────────────────────────────────────────────


class StyleIntentInput(BaseModel):
    """Per-turn input for the style intent agent."""

    utterance: str
    prior_turns: list[dict] = Field(default_factory=list)
    # Compact snapshot of the user's current style (set label + top knobs, not full JSONB).
    current_style_snapshot: dict | None = None


class StyleIntentOutput(BaseModel):
    """Parsed intent from a single user utterance about their style."""

    intent: Literal[
        "style_edit", "persona_preference", "scope_reduction", "clarify", "describe", "unknown"
    ]
    # Intent-specific fields — varies by intent:
    #   style_edit: style_set_id?, knobs?, instruction_level?, footage_type_bias?,
    #               preferred_edit_format_mix?
    #   persona_preference: free_text?
    #   scope_reduction: footage_type_bias? (encoded as bias away from X)
    #   describe: (empty — reply is generated from the snapshot)
    #   clarify/unknown: (empty or minimal)
    fields: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # The reply to show the user — confirmation if applied, clarifying question if not.
    reply: str
    # Follow-up suggestion chips.
    suggestions: list[str] = Field(default_factory=list)
    needs_clarification: bool = False


# ── Formatting ────────────────────────────────────────────────────────────────


def _format_prior_turns(turns: list[dict]) -> str:
    if not turns:
        return "(no prior turns)"
    lines = []
    for t in turns:
        role = t.get("role", "unknown").upper()
        content = str(t.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content[:300]}")
    return "\n".join(lines) if lines else "(no prior turns)"


def _format_style_snapshot(snapshot: dict | None) -> str:
    if not snapshot:
        return "(no style set yet)"
    parts = []
    if snapshot.get("style_set_id"):
        parts.append(f"style_set: {snapshot['style_set_id']}")
    if snapshot.get("instruction_level"):
        parts.append(f"instruction_level: {snapshot['instruction_level']}")
    bias = snapshot.get("footage_type_bias") or []
    if bias:
        parts.append(f"footage_bias: {', '.join(bias)}")
    knobs = snapshot.get("knobs") or {}
    if knobs:
        knob_str = ", ".join(f"{k}={v}" for k, v in knobs.items())
        parts.append(f"knobs: {knob_str}")
    return " | ".join(parts) if parts else "(style exists but no details)"


# ── Agent ─────────────────────────────────────────────────────────────────────


class StyleIntentAgent(Agent[StyleIntentInput, StyleIntentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.style_intent",
        prompt_id="style_intent",
        prompt_version=STYLE_INTENT_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_budget=512,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = StyleIntentInput
    Output = StyleIntentOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["intent", "reply"]

    def render_prompt(self, input: StyleIntentInput) -> str:  # noqa: A002
        return load_prompt(
            "style_intent",
            utterance=input.utterance[:500],
            prior_turns=_format_prior_turns(input.prior_turns),
            style_snapshot=_format_style_snapshot(input.current_style_snapshot),
        )

    def parse(
        self,
        raw_text: str,
        input: StyleIntentInput,  # noqa: A002, ARG002
    ) -> StyleIntentOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"style_intent: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("style_intent: response is not a JSON object")

        # Extract and validate intent.
        intent = data.get("intent", "unknown")
        valid_intents = {
            "style_edit",
            "persona_preference",
            "scope_reduction",
            "clarify",
            "describe",
            "unknown",
        }
        if intent not in valid_intents:
            log.warning("style_intent.unknown_intent", intent=intent)
            intent = "unknown"

        # Clamp confidence to [0.0, 1.0].
        raw_confidence = data.get("confidence", 0.5)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        # Process fields — hallucination-drop discipline.
        raw_fields = data.get("fields") or {}
        if not isinstance(raw_fields, dict):
            raw_fields = {}

        fields: dict = {}

        # Pass through safe top-level fields.
        for safe_key in (
            "style_set_id",
            "instruction_level",
            "footage_type_bias",
            "preferred_edit_format_mix",
            "free_text",
        ):
            if safe_key in raw_fields:
                fields[safe_key] = raw_fields[safe_key]

        # Drop any knob key not in the 10 parity-safe knob names.
        raw_knobs = raw_fields.get("knobs") or {}
        if isinstance(raw_knobs, dict) and raw_knobs:
            safe_knobs = {k: v for k, v in raw_knobs.items() if k in _PARITY_SAFE_KNOBS}
            if safe_knobs:
                fields["knobs"] = safe_knobs
            dropped = set(raw_knobs.keys()) - _PARITY_SAFE_KNOBS
            if dropped:
                log.warning("style_intent.dropped_knobs", dropped=list(dropped))

        # Coerce style_set_id: unknown catalog entries → clarify, do not write.
        if "style_set_id" in fields:
            try:
                from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

                known = style_set_ids()
                if fields["style_set_id"] not in known:
                    log.warning(
                        "style_intent.unknown_style_set",
                        style_set_id=fields["style_set_id"],
                    )
                    del fields["style_set_id"]
                    confidence = min(confidence, 0.4)
            except Exception:  # noqa: BLE001
                pass  # catalog unavailable → accept; fail-open on preview

        # Extract reply and suggestions.
        reply = str(data.get("reply") or "").strip()
        if not reply:
            reply = "Got it. What else would you like to change?"

        suggestions_raw = data.get("suggestions") or []
        if not isinstance(suggestions_raw, list):
            suggestions_raw = []
        suggestions = [str(s).strip() for s in suggestions_raw if str(s).strip()][:5]

        needs_clarification = bool(data.get("needs_clarification", False))
        # Low confidence automatically forces clarification (no write at route level).
        if confidence < _CONFIDENCE_CLARIFY_THRESHOLD:
            needs_clarification = True

        try:
            return StyleIntentOutput(
                intent=intent,  # type: ignore[arg-type]
                fields=fields,
                confidence=confidence,
                reply=reply,
                suggestions=suggestions,
                needs_clarification=needs_clarification,
            )
        except Exception as exc:  # noqa: BLE001
            raise RefusalError(f"style_intent: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        intents = "style_edit|persona_preference|scope_reduction|clarify|describe|unknown"
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: "
            f"intent (one of {intents}), "
            "fields (object, intent-specific), "
            "confidence (float 0-1), "
            "reply (string — confirmation or clarifying question), "
            "suggestions (list of 2-4 short action chips), "
            "needs_clarification (boolean). "
            "No markdown, no prose outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
