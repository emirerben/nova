"""nova.layout.transition_picker — pick a transition type for a clip-pair.

Refines transitions at the assembly layer. `template_recipe` already returns a
template-level transition vocabulary; the picker adjusts per-pair when the
template's choice doesn't fit the actual clips (e.g., "whip-pan" between two
static talking-head shots).

Input: outgoing clip + incoming clip metadata. Output: transition type + duration.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError

_VALID_TRANSITIONS = ("hard-cut", "whip-pan", "zoom-in", "dissolve", "curtain-close", "none")
TransitionType = Literal["hard-cut", "whip-pan", "zoom-in", "dissolve", "curtain-close", "none"]


class ClipMetaSnapshot(BaseModel):
    description: str = ""
    energy: float = 5.0
    camera_movement: str = "static"


class TransitionPickerInput(BaseModel):
    outgoing: ClipMetaSnapshot
    incoming: ClipMetaSnapshot
    template_default: TransitionType = "hard-cut"
    pacing_style: str = ""


class TransitionPickerOutput(BaseModel):
    transition: TransitionType
    duration_s: float = Field(default=0.3, ge=0, le=2.0)
    rationale: str = ""


class TransitionPickerAgent(Agent[TransitionPickerInput, TransitionPickerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.layout.transition_picker",
        prompt_id="_inline",
        prompt_version="2026-05-14",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = TransitionPickerInput
    Output = TransitionPickerOutput

    def required_fields(self) -> list[str]:
        return ["transition"]

    def render_prompt(self, input: TransitionPickerInput) -> str:  # noqa: A002
        pacing_line = f'Pacing style: "{input.pacing_style}"\n' if input.pacing_style else ""
        return (
            "You are an editor making one micro-decision: which transition connects these "
            "two specific clips. You think in transition grammar — every transition is a "
            "punctuation mark between two shots, not decoration. A hard-cut is a period, "
            "a dissolve is a comma, a curtain-close is a chapter break. Pick the right "
            "punctuation for THIS pair in service of the template's pacing — not to show "
            "off variety.\n\n"
            f"Outgoing clip: {input.outgoing.description!r} "
            f"(energy={input.outgoing.energy:.1f}, camera={input.outgoing.camera_movement})\n"
            f"Incoming clip: {input.incoming.description!r} "
            f"(energy={input.incoming.energy:.1f}, camera={input.incoming.camera_movement})\n"
            f'Template default: "{input.template_default}"\n'
            + pacing_line
            + "\nValid transitions (emit ONLY one of these): "
            + ", ".join(_VALID_TRANSITIONS)
            + "\n\n"
            "Decision principles:\n\n"
            "Rule 0 — Default fidelity (highest priority). If the template default fits "
            "the pair (camera movement compatible, energy delta not extreme), RETURN IT "
            "UNCHANGED. Consistency is the template's signature. Override only when the "
            "pair clearly contradicts the default.\n\n"
            "Energy delta — the primary signal. Compute |incoming.energy − outgoing.energy|:\n"
            "  0–1.5  continuation (same vibe)      → hard-cut; whip-pan if movement matches\n"
            "  1.5–3  step (verse→chorus)           → hard-cut sharp; zoom-in for emphasis\n"
            "  3–5    major shift (chorus→bridge)   → curtain-close; dissolve at the long end\n"
            "  5+     chapter break (scene change)  → curtain-close upper duration; or none\n\n"
            "Camera-movement compatibility:\n"
            "  - whip-pan: ONLY when both clips have lateral movement in compatible "
            "directions (outgoing whips right, incoming enters as if from the right). "
            "Never pick whip-pan for two static shots — it reads as a glitch, not a transition.\n"
            "  - zoom-in: ONLY when the incoming clip has its subject visible from the start. "
            "Avoid if incoming opens on a wide / empty frame; the zoom has nothing to land on.\n"
            "  - dissolve: camera-agnostic but needs breathing room. Never under 0.4s, "
            "never on high-energy pacing.\n"
            "  - hard-cut: works for any camera state. The neutral default.\n"
            "  - curtain-close: independent of camera movement; it's a section break, "
            "not a continuity move.\n"
            "  - none: semantically 'no transition between scenes' (vs. hard-cut's "
            "'the transition is a hard cut'). Use for narrative scene splits.\n\n"
            "Pacing-style modulation (when pacing_style is set):\n"
            "  - high-energy-edm / energetic-pop → prefer hard-cut, whip-pan, zoom-in. "
            "Dissolves only at section breaks. Durations toward the SHORT end of each range.\n"
            "  - mid-tempo-flow → balanced. All transitions in play; durations in their middle.\n"
            "  - slow-cinematic → prefer dissolve and curtain-close over hard-cut. "
            "Durations toward the LONG end of each range.\n\n"
            "Duration envelope:\n"
            "  hard-cut       0.00 (instant)   default; energetic content; beat-aligned cuts\n"
            "  none           0.00 (instant)   scene split with no explicit transition\n"
            "  whip-pan       0.20–0.40s       both clips have compatible lateral movement\n"
            "  zoom-in        0.30–0.50s       emphasis on incoming subject\n"
            "  dissolve       0.40–0.80s       reflective, slow pacing, time-passage feel\n"
            "  curtain-close  0.60–1.00s       section break, scene change, formal punctuation\n\n"
            "Rationale field: ≤80 characters, editor's shorthand. Examples:\n"
            '  "matched template default — pair fits"\n'
            '  "energy 7.8 → 3.1 (delta 4.7), drop→breakdown wants curtain-close"\n'
            '  "both clips pan right; whip-pan reads as continuous motion"\n'
            '  "override: default dissolve, but pacing high-energy → hard-cut"\n'
            '  "override: static + static, can\'t whip-pan; fell back to hard-cut"\n\n'
            "One decision, three values, ≤80 character rationale. No hedging. When the "
            "default fits, ship it.\n\n"
            'Return JSON: {"transition": str, "duration_s": float, "rationale": short str}\n'
            "Return ONLY valid JSON, no markdown."
        )

    def parse(
        self,
        raw_text: str,
        input: TransitionPickerInput,  # noqa: A002, ARG002
    ) -> TransitionPickerOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"transition_picker: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("transition_picker: response is not a JSON object")

        transition = str(data.get("transition", "hard-cut") or "hard-cut")
        if transition not in _VALID_TRANSITIONS:
            raise SchemaError(f"transition_picker: invalid transition {transition!r}")

        # NB: do not use `... or 0.3` here — Python treats 0.0 as falsy, which
        # would silently coerce hard-cut/none (canonically duration 0.0 per
        # the prompt's duration envelope) to 0.3. Caught by
        # tests/evals/test_transition_picker_evals.py::default_hard_cut.
        raw_duration = data.get("duration_s", 0.3)
        if raw_duration is None:
            duration_s = 0.3
        else:
            try:
                duration_s = float(raw_duration)
            except (TypeError, ValueError):
                duration_s = 0.3
        duration_s = max(0.0, min(2.0, duration_s))

        return TransitionPickerOutput(
            transition=transition,  # type: ignore[arg-type]
            duration_s=duration_s,
            rationale=str(data.get("rationale", "") or "")[:200],
        )
