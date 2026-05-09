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
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = TransitionPickerInput
    Output = TransitionPickerOutput

    def required_fields(self) -> list[str]:
        return ["transition"]

    def render_prompt(self, input: TransitionPickerInput) -> str:  # noqa: A002
        return (
            "Pick a transition between two adjacent clips in a short-form video.\n\n"
            f"Outgoing clip: {input.outgoing.description!r} "
            f"(energy={input.outgoing.energy:.1f}, camera={input.outgoing.camera_movement})\n"
            f"Incoming clip: {input.incoming.description!r} "
            f"(energy={input.incoming.energy:.1f}, camera={input.incoming.camera_movement})\n"
            f'Template default: "{input.template_default}"\n'
            + (f'Pacing style: "{input.pacing_style}"\n' if input.pacing_style else "")
            + "\nValid transitions: " + ", ".join(_VALID_TRANSITIONS) + "\n\n"
            "Guidance:\n"
            "  - hard-cut: default, fast pacing, energetic content\n"
            "  - whip-pan: 0.2–0.4s, dynamic, only when both clips have movement\n"
            "  - zoom-in: 0.3–0.5s, dramatic emphasis on incoming\n"
            "  - dissolve: 0.4–0.8s, slower pacing, reflective tone\n"
            "  - curtain-close: 0.6–1.0s, scene-break, formal\n"
            "  - none: no transition, hard cut without explicit transition (used between scenes)\n\n"
            "If the template default already fits the pair, return it. Only override "
            "when the pair clearly needs something different.\n\n"
            'Return JSON: {"transition": str, "duration_s": float, "rationale": short str}\n'
            "Return ONLY valid JSON, no markdown."
        )

    def parse(
        self, raw_text: str, input: TransitionPickerInput  # noqa: A002, ARG002
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

        try:
            duration_s = float(data.get("duration_s", 0.3) or 0.3)
        except (TypeError, ValueError):
            duration_s = 0.3
        duration_s = max(0.0, min(2.0, duration_s))

        return TransitionPickerOutput(
            transition=transition,  # type: ignore[arg-type]
            duration_s=duration_s,
            rationale=str(data.get("rationale", "") or "")[:200],
        )
