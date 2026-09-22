"""Extract complete, source-backed clip operations from creator chat text."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import MAX_CLIP_INTENTS, ClipIntent

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?i)(^|[\s.;!?])(system|assistant|user|tool|developer)\s*[:>]")
_FENCE = re.compile(r"```+")


def _sanitize_text(value: str) -> str:
    value = _CONTROL_CHARS.sub(" ", value)
    value = _ROLE_MARKERS.sub(r"\1[role-marker-stripped]", value)
    return _FENCE.sub("'''", value)


class PlannedClipIntent(ClipIntent):
    """A clip operation with its exact creator-written provenance."""

    source_quote: str = Field(min_length=1, max_length=600)


class ClipIntentPlannerInput(BaseModel):
    creator_request: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
    latest_user_message: str | None = Field(default=None, max_length=CREATOR_REQUEST_MAX_CHARS)
    # Strategy-produced candidates can aid recall, but never authorize output.
    candidate_intents: list[ClipIntent] | None = Field(default=None, max_length=MAX_CLIP_INTENTS)


class ClipIntentPlannerOutput(BaseModel):
    intents: list[PlannedClipIntent] = Field(default_factory=list, max_length=MAX_CLIP_INTENTS)
    question: str | None = Field(default=None, max_length=300)


class ClipIntentPlannerAgent(Agent[ClipIntentPlannerInput, ClipIntentPlannerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.clip_intent_planner",
        prompt_id="clip_intent_planner",
        prompt_version="2026-09-22.3",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=384,
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=20.0,
        sensitive_io=True,
    )
    Input = ClipIntentPlannerInput
    Output = ClipIntentPlannerOutput

    def required_fields(self) -> list[str]:
        # Empty is the valid result for a request that only changes style,
        # duration, music, or other non-clip operations.
        return []

    def project_input_for_observability(
        self, input_dict: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(input_dict, dict):
            return None

        def redact(value: object) -> dict[str, object]:
            text = str(value or "")
            return {
                "redacted": True,
                "chars": len(text),
                "sha256": sha256(text.encode()).hexdigest(),
            }

        candidates = input_dict.get("candidate_intents") or []
        return {
            "creator_request": redact(input_dict.get("creator_request")),
            "latest_user_message": redact(input_dict.get("latest_user_message")),
            "candidate_intents_count": len(candidates) if isinstance(candidates, list) else 0,
        }

    def render_prompt(self, input: ClipIntentPlannerInput) -> str:  # noqa: A002
        candidates = [intent.model_dump(mode="json") for intent in (input.candidate_intents or [])]
        return load_prompt(
            "clip_intent_planner",
            creator_request=_sanitize_text(input.creator_request),
            latest_user_message=_sanitize_text(input.latest_user_message or ""),
            candidate_intents=json.dumps(candidates, ensure_ascii=False),
            max_intents=str(MAX_CLIP_INTENTS),
        )

    def parse(self, raw_text: str, input: ClipIntentPlannerInput) -> ClipIntentPlannerOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"clip_intent_planner: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("clip_intent_planner: response is not a JSON object")
        raw_intents = data.get("intents")
        if not isinstance(raw_intents, list):
            raise SchemaError("clip_intent_planner: intents must be a list")
        question = data.get("question")
        if question is not None and (not isinstance(question, str) or not question.strip()):
            raise SchemaError("clip_intent_planner: question must be a non-empty string or null")

        sources = (input.creator_request, input.latest_user_message or "")
        intents: list[PlannedClipIntent] = []
        seen: set[tuple[str, str, str | None, str | None, str, str | None]] = set()
        seen_ids: set[str] = set()
        for index, raw_intent in enumerate(raw_intents):
            if not isinstance(raw_intent, dict):
                raise SchemaError(f"clip_intent_planner: intents[{index}] is not an object")
            try:
                intent = PlannedClipIntent.model_validate(raw_intent)
            except ValidationError as exc:
                raise SchemaError(
                    f"clip_intent_planner: invalid intent at {index} — {exc}"
                ) from exc
            if not any(intent.source_quote in source for source in sources):
                raise SchemaError(
                    f"clip_intent_planner: intent {intent.intent_id!r} "
                    "source_quote is not creator text"
                )
            if intent.creator_text is not None and (
                intent.creator_text not in intent.source_quote
                or not any(intent.creator_text in source for source in sources)
            ):
                raise SchemaError(
                    f"clip_intent_planner: intent {intent.intent_id!r} "
                    "creator_text is not source-backed"
                )
            if intent.op != "caption" and intent.caption_attribute is not None:
                raise SchemaError(
                    f"clip_intent_planner: intent {intent.intent_id!r} "
                    f"has caption_attribute for {intent.op}"
                )
            if intent.op != "order" and intent.position is not None:
                raise SchemaError(
                    f"clip_intent_planner: intent {intent.intent_id!r} has position for {intent.op}"
                )
            if intent.intent_id in seen_ids:
                raise SchemaError(
                    f"clip_intent_planner: duplicate intent_id {intent.intent_id!r} at {index}"
                )
            key = (
                intent.op,
                intent.attribute.casefold(),
                intent.creator_text,
                intent.position,
                intent.label_source,
                intent.transcript_kind,
            )
            if key in seen:
                raise SchemaError(f"clip_intent_planner: duplicate intent at {index}")
            seen.add(key)
            seen_ids.add(intent.intent_id)
            intents.append(intent)
        if question is not None and intents:
            raise SchemaError("clip_intent_planner: question cannot accompany partial intents")
        try:
            return ClipIntentPlannerOutput(
                intents=intents, question=question.strip() if question else None
            )
        except ValidationError as exc:
            raise SchemaError(f"clip_intent_planner: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nReturn only the requested JSON. Every intent needs an exact "
            "source_quote copied from the creator text; do not emit a partial "
            "inventory or invent clip facts."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "ClipIntentPlannerAgent",
    "ClipIntentPlannerInput",
    "ClipIntentPlannerOutput",
    "PlannedClipIntent",
]
