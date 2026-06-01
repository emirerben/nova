"""nova.compose.agentic_style_selector — pick a style SET for an agentic template.

Text-only. Runs once per agentic build AFTER template_text has extracted the
overlays, consuming the on-screen text samples + the template theme, and returns
one `style_set_id` (agentic-eligible). Decoupled from `template_text` on purpose:
making the extraction agent ALSO choose a set perturbed its bbox/effect/color
extraction (the eval regressed), so set selection lives here instead, leaving
`template_text`'s prompt untouched. The chosen set is threaded onto the overlays
and resolved per-role at render (see template_text_extraction + _collect).

Best-effort: the caller wraps `run()` in try/except and falls back to no set
(uniform styling) on failure. `parse()` clamps an out-of-catalog choice to
"default" rather than raising.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt
from app.pipeline.style_sets import list_style_sets

_DEFAULT_SET_ID = "default"


class StyleSetCandidate(BaseModel):
    id: str = Field(min_length=1)
    label: str = ""
    tags: list[str] = Field(default_factory=list)


class AgenticStyleSelectorInput(BaseModel):
    overlay_texts: list[str] = Field(default_factory=list)
    template_theme: str = ""
    available_sets: list[StyleSetCandidate] = Field(default_factory=list)


class AgenticStyleSelectorOutput(BaseModel):
    style_set_id: str = Field(min_length=1)
    rationale: str = ""


def _agentic_candidates() -> list[StyleSetCandidate]:
    return [StyleSetCandidate(**s) for s in list_style_sets(applies_to="agentic")]


class AgenticStyleSelectorAgent(Agent[AgenticStyleSelectorInput, AgenticStyleSelectorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.agentic_style_selector",
        prompt_id="select_agentic_style",
        prompt_version="2026-05-25",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Cap reasoning: picks one style-set id from a fixed list — a pure
        # selection task (like music_matcher, validated at low budget). 512
        # keeps headroom. Lowest-risk of the capped agents.
        thinking_budget=512,
    )
    Input = AgenticStyleSelectorInput
    Output = AgenticStyleSelectorOutput

    def required_fields(self) -> list[str]:
        return ["style_set_id"]

    def render_prompt(self, input: AgenticStyleSelectorInput) -> str:  # noqa: A002
        candidates = input.available_sets or _agentic_candidates()
        set_lines = "\n".join(
            f"- id={c.id} | {c.label} | tags={', '.join(c.tags) or '(none)'}" for c in candidates
        )
        overlay_lines = (
            "\n".join(f"- {t.replace(chr(10), ' ')[:120]}" for t in input.overlay_texts if t)
            or "(none)"
        )
        return load_prompt(
            "select_agentic_style",
            template_theme=(input.template_theme or "").replace("\n", " ")[:160],
            overlay_lines=overlay_lines,
            set_lines=set_lines,
        )

    def parse(
        self,
        raw_text: str,
        input: AgenticStyleSelectorInput,  # noqa: A002
    ) -> AgenticStyleSelectorOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"agentic_style_selector: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("agentic_style_selector: response is not a JSON object")

        valid_ids = {c.id for c in (input.available_sets or _agentic_candidates())}
        chosen = str(data.get("style_set_id", "") or "").strip()
        if chosen not in valid_ids:
            chosen = _DEFAULT_SET_ID
        return AgenticStyleSelectorOutput(
            style_set_id=chosen,
            rationale=str(data.get("rationale", "") or "").strip(),
        )

# (style-set selection decoupled into agentic_style_selector — see CHANGELOG 0.4.46.0)
