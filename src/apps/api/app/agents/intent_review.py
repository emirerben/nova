"""nova.kria.intent_review: judge frames of a finished video against what the creator asked for.

Production never sends this agent through `Agent.run`. `run_intent_review` renders the prompt
and parses the reply here but sends it through `OpenAIVisionClient` under an explicit cost
reservation, because the dispatcher rejects media on purpose. The agent class exists for the
prompt contract, the replay evals and the admin catalog. It declares that it needs frames, so a
call through the dispatcher is refused instead of letting a model judge a video it never saw.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.intent_review import (
    DEFECTS_BY_REQUIREMENT_KIND,
    IntentReviewOutput,
    InvalidReviewOutput,
    parse_review_output,
)
from app.agents.prompt_vocab import PROMPT_VOCABULARIES
from app.kria.intent_review_catalog import ReviewCatalog
from app.pipeline.prompt_loader import load_prompt

PROMPT_VERSION = "intent_review_v2"
FRAMES_REQUIRED = "frames://intent-review"
MAX_REQUEST_CHARS = 2_000


class IntentReviewInput(BaseModel):
    original_request: str = Field(default="")
    catalog: ReviewCatalog


def _line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def render_review_prompt(input: IntentReviewInput) -> str:  # noqa: A002
    catalog = input.catalog
    request = input.original_request[:MAX_REQUEST_CHARS]
    kinds = sorted({item.kind for item in catalog.requirements})
    return load_prompt(
        "intent_review",
        request_json=json.dumps(request, ensure_ascii=False),
        requirements_json="\n".join(
            _line(
                {
                    "id": item.requirement_id,
                    "kind": item.kind,
                    "label": item.label,
                    "literal": item.literal,
                }
            )
            for item in catalog.requirements
        ),
        frames_json="\n".join(
            _line(
                {
                    "id": frame.frame_id,
                    "t_s": round(frame.t_ms / 1000, 2),
                    **({"centred_on_text_span": True} if frame.anchor_ref else {}),
                }
            )
            for frame in catalog.frames
        ),
        defects_by_kind="\n".join(
            f"  - {kind}: {', '.join(DEFECTS_BY_REQUIREMENT_KIND[kind])}" for kind in kinds
        ),
        verdicts=PROMPT_VOCABULARIES["INTENT_REVIEW_VERDICTS"].rendered,
        defect_kinds=PROMPT_VOCABULARIES["INTENT_REVIEW_DEFECT_KINDS"].rendered,
        confidence_levels=PROMPT_VOCABULARIES["INTENT_REVIEW_CONFIDENCE"].rendered,
    )


class IntentReviewAgent(Agent[IntentReviewInput, IntentReviewOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.kria.intent_review",
        prompt_id="intent_review",
        prompt_version=PROMPT_VERSION,
        model="gpt-6.1-sol",
        max_attempts=1,
        timeout_s=60.0,
        cost_per_1k_input_usd=0.002,
        cost_per_1k_output_usd=0.010,
        sensitive_io=True,
        enable_clarification_retries=False,
        schema_retry_limit=0,
    )
    Input = IntentReviewInput
    Output = IntentReviewOutput
    max_output_tokens: ClassVar[int | None] = 2_500

    def media_uri(self, input: IntentReviewInput) -> str | None:  # noqa: A002
        return FRAMES_REQUIRED

    def media_mime(self, input: IntentReviewInput) -> str:  # noqa: A002
        return "image/jpeg"

    def render_prompt(self, input: IntentReviewInput) -> str:  # noqa: A002
        return render_review_prompt(input)

    def parse(self, raw_text: str, input: IntentReviewInput) -> IntentReviewOutput:  # noqa: A002
        try:
            return parse_review_output(raw_text)
        except (InvalidReviewOutput, ValidationError) as exc:
            raise SchemaError(f"intent_review: {exc}") from exc
