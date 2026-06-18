"""nova.compose.shot_list_writer — generate a filming guide for a single plan item.

Used by the on-demand POST /plan-items/{id}/generate-guide endpoint to retrofit
a structured shot list onto items that were generated with an empty guide (e.g.
instruction_level=none personas). Re-uses _parse_filming_guide from
content_plan_generator so parse/sanitize/cap behavior is identical for the
what/how/duration_s fields; clip_count is parsed in the same loop.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, RefusalError, RunContext, SchemaError
from app.agents._schemas.content_plan import (
    MAX_SHOT_DURATION_S,
    MAX_SHOTS_PER_ITEM,
    MIN_SHOT_DURATION_S,
)
from app.agents.music_matcher import _sanitize_text

SHOT_LIST_WRITER_PROMPT_VERSION = "2026-06-18"

_MAX_CLIP_COUNT = 10
_MIN_CLIP_COUNT = 1


class ShotSpecWithCount(BaseModel):
    """One shot in a generated filming guide, including clip_count."""

    what: str
    how: str = ""
    duration_s: int = MIN_SHOT_DURATION_S
    clip_count: int = 1


class ShotListWriterInput(BaseModel):
    theme: str = ""
    idea: str
    edit_format: str = "montage"


class ShotListWriterOutput(BaseModel):
    shots: list[ShotSpecWithCount] = Field(default_factory=list)


def _parse_shots_with_count(raw: object) -> list[ShotSpecWithCount]:
    """Parse the filming_guide list from a raw LLM response.

    Mirrors _parse_filming_guide from content_plan_generator but also
    extracts clip_count. Best-effort: malformed entries are skipped.
    """
    if not isinstance(raw, list):
        return []
    shots: list[ShotSpecWithCount] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        what_raw = entry.get("what", "")
        if not isinstance(what_raw, str):
            continue
        what = _sanitize_text(what_raw)
        if not what:
            continue
        how_raw = entry.get("how", "")
        how = _sanitize_text(how_raw) if isinstance(how_raw, str) else ""
        try:
            duration_s = int(float(entry.get("duration_s", MIN_SHOT_DURATION_S)))
        except (TypeError, ValueError, OverflowError):
            duration_s = MIN_SHOT_DURATION_S
        duration_s = max(MIN_SHOT_DURATION_S, min(MAX_SHOT_DURATION_S, duration_s))
        try:
            clip_count = int(entry.get("clip_count", 1))
        except (TypeError, ValueError, OverflowError):
            clip_count = 1
        clip_count = max(_MIN_CLIP_COUNT, min(_MAX_CLIP_COUNT, clip_count))
        shots.append(
            ShotSpecWithCount(what=what, how=how, duration_s=duration_s, clip_count=clip_count)
        )
        if len(shots) >= MAX_SHOTS_PER_ITEM:
            break
    return shots


_EDIT_HINTS: dict[str, str] = {
    "montage": "2–4 varied shots covering different moments or angles",
    "talking_head": "1–2 shots — the to-camera take first; add one B-roll if clearly helpful",
    "day_vlog": "2–4 shots in time order, spanning the outing or day",
    "single_hero": "1–2 shots of the hero subject from the best angle",
}


class ShotListWriterAgent(Agent[ShotListWriterInput, ShotListWriterOutput]):
    """Generate a structured filming guide for a single plan item."""

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.shot_list_writer",
        prompt_id="shot_list_writer",
        prompt_version=SHOT_LIST_WRITER_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_budget=256,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = ShotListWriterInput
    Output = ShotListWriterOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["filming_guide"]

    def render_prompt(self, input: ShotListWriterInput) -> str:  # noqa: A002
        hint = _EDIT_HINTS.get(input.edit_format, _EDIT_HINTS["montage"])
        theme_line = f"Theme: {_sanitize_text(input.theme)}\n" if input.theme.strip() else ""
        return (
            "You are a short-form video coach. Given a content idea, produce a concrete "
            "filming guide — a list of shots the creator should capture.\n\n"
            "## Content idea\n\n"
            f"{theme_line}"
            f"Idea: {_sanitize_text(input.idea)}\n"
            f"Edit format: {_sanitize_text(input.edit_format)}\n\n"
            "## Instructions\n\n"
            f"Generate {hint}. For each shot:\n"
            '- "what": one clear sentence describing what to film (subject, action, setting)\n'
            '- "how": optional framing tip (e.g. "handheld, eye level") — brief, one phrase\n'
            '- "duration_s": realistic clip length in seconds (3–15 for most shots)\n'
            '- "clip_count": how many clips to film for this shot '
            "(1 for most; 3–7 for variety/action shots)\n\n"
            'Return ONLY the JSON object with a "filming_guide" array. No commentary.\n\n'
            "Respond with JSON only:\n"
            '{"filming_guide": [{"what": "...", "how": "...", "duration_s": 5, "clip_count": 1}, ...]}'  # noqa: E501
        )

    def parse(
        self,
        raw_text: str,
        input: ShotListWriterInput,  # noqa: A002, ARG002
    ) -> ShotListWriterOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"shot_list_writer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("shot_list_writer: response is not a JSON object")
        try:
            shots = _parse_shots_with_count(data.get("filming_guide", []))
        except Exception as exc:  # noqa: BLE001
            raise RefusalError(f"shot_list_writer: parse failed — {exc}") from exc
        return ShotListWriterOutput(shots=shots)

    def schema_clarification(self) -> str:
        return (
            '\n\nIMPORTANT: return ONLY valid JSON with a "filming_guide" key containing '
            "a list of {what, how, duration_s, clip_count} objects. "
            "No markdown, no prose outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


def run_shot_list_writer(
    inp: ShotListWriterInput, *, client: object | None = None
) -> ShotListWriterOutput:
    """Generate a filming guide for a single plan item.

    Synchronous — the route wraps this in asyncio.to_thread().
    """
    from app.agents._model_client import default_client  # noqa: PLC0415

    agent = ShotListWriterAgent(client or default_client())
    return agent.run(inp, ctx=RunContext(job_id=None))
