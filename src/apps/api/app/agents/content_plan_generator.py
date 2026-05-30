"""nova.plan.content_plan_generator — turn a persona into a day-by-day plan.

Off-Job agent (no media). Input is the editable Persona + optional events +
horizon. Output is a deduped, range-validated list of PlanItemSpec.

`enable_json_repair=True` because this is the long-list truncation case: a
30-item plan can push Gemini near its output-token ceiling and emit a missing
closing brace. Repair fixes punctuation only; genuinely malformed output still
raises. We never persist partial garbage — `parse()` clamps to the valid day
range, drops empty/duplicate-day items, and refuses if nothing valid survives.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.content_plan import (
    CONTENT_PLAN_PROMPT_VERSION,
    ContentPlanInput,
    ContentPlanOutput,
    PlanItemSpec,
)
from app.agents.music_matcher import _sanitize_text
from app.agents.persona_examples import format_ideas_for_pillars, format_success_factors
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


class ContentPlanGeneratorAgent(Agent[ContentPlanInput, ContentPlanOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.content_plan_generator",
        prompt_id="generate_content_plan",
        prompt_version=CONTENT_PLAN_PROMPT_VERSION,
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = ContentPlanInput
    Output = ContentPlanOutput

    def required_fields(self) -> list[str]:
        return ["items"]

    def render_prompt(self, input: ContentPlanInput) -> str:  # noqa: A002
        p = input.persona
        return load_prompt(
            "generate_content_plan",
            summary=_sanitize_text(p.summary),
            content_pillars=_sanitize_text(", ".join(p.content_pillars)),
            tone=_sanitize_text(p.tone),
            audience=_sanitize_text(p.audience),
            posting_cadence=_sanitize_text(p.posting_cadence),
            sample_topics=_sanitize_text(", ".join(p.sample_topics)),
            events=_sanitize_text(input.events) or "(none provided)",
            horizon_days=str(input.horizon_days),
            # Market-research idea bank, ranked toward this creator's pillars.
            idea_bank=format_ideas_for_pillars(p.content_pillars),
            # Codified TikTok success factors for what makes a plan item perform.
            success_factors=format_success_factors("plan"),
        )

    def parse(self, raw_text: str, input: ContentPlanInput) -> ContentPlanOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"content_plan: invalid JSON — {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SchemaError("content_plan: missing/invalid 'items' array")

        horizon = max(1, min(input.horizon_days, 60))
        seen_days: set[int] = set()
        items: list[PlanItemSpec] = []
        for raw in data["items"]:
            if not isinstance(raw, dict):
                continue
            try:
                day = int(raw.get("day_index", 0))
            except (TypeError, ValueError):
                continue
            if not (1 <= day <= horizon) or day in seen_days:
                continue  # out of range or duplicate day — drop, never persist garbage
            theme = _sanitize_text(str(raw.get("theme", "")))
            idea = _sanitize_text(str(raw.get("idea", "")))
            if not theme or not idea:
                continue
            seen_days.add(day)
            items.append(
                PlanItemSpec(
                    day_index=day,
                    theme=theme,
                    idea=idea,
                    filming_suggestion=_sanitize_text(str(raw.get("filming_suggestion", ""))),
                )
            )

        if not items:
            raise RefusalError("content_plan: no valid items after validation")
        items.sort(key=lambda it: it.day_index)
        try:
            return ContentPlanOutput(items=items)
        except ValidationError as exc:
            raise SchemaError(f"content_plan: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            '\n\nIMPORTANT: Return ONLY a JSON object {"items": [...]}. Each item has '
            "day_index (unique, within range), non-empty theme + idea, and a short "
            "filming_suggestion. No markdown, no text outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
