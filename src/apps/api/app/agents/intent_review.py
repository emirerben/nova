"""nova.kria.intent_review: judge frames of a finished video against what the creator asked for.

Production never sends this agent through `Agent.run`. `run_intent_review` renders the prompt
and parses the reply here but sends it through `OpenAIVisionClient` under an explicit cost
reservation, because the dispatcher rejects media on purpose. The agent class exists for the
prompt contract, the replay evals and the admin catalog. It declares that it needs frames, so a
call through the dispatcher is refused instead of letting a model judge a video it never saw.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._openai_vision import DEFAULT_TIMEOUT_S, MAX_OUTPUT_TOKENS, VISION_MODEL
from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.intent_review import (
    IntentReviewOutput,
    InvalidReviewOutput,
    defects_by_requirement_kind,
    parse_review_output,
)
from app.agents.prompt_vocab import PROMPT_VOCABULARIES
from app.kria.intent_review_catalog import ReviewCatalog
from app.pipeline.prompt_loader import load_prompt
from app.services.ai_cost_control import UsageMeter, calculate_cost_usd

PROMPT_VERSION = "intent_review_v3"
# KRI-1583: the v3 prompt plus the legibility rule and defect kind, sent only for a catalog with
# `text_legibility` (a job stamped for informative text designs). Every other review sends v3.
LEGIBILITY_PROMPT_VERSION = "intent_review_v4"
LEGIBILITY_RULE = (
    "- Text that is on screen but hard to read is not satisfied: dark text on a dark part of "
    "the picture, light text on a light part, or letters lost in a busy background. When a "
    'frame shows a text requirement\'s text that way, return "unmet" with "text_illegible" '
    "and cite that frame. Judge only whether a viewer can read it, not whether you like the "
    "colour or font.\n"
)
FRAMES_REQUIRED = "frames://intent-review"
MAX_REQUEST_CHARS = 2_000


def _usd_per_1k(**usage: int) -> float:
    return calculate_cost_usd(VISION_MODEL, UsageMeter(**usage))


class IntentReviewInput(BaseModel):
    original_request: str = Field(default="")
    catalog: ReviewCatalog


def _line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def prompt_version_for(catalog: ReviewCatalog) -> str:
    return LEGIBILITY_PROMPT_VERSION if catalog.text_legibility else PROMPT_VERSION


def render_review_prompt(input: IntentReviewInput) -> str:  # noqa: A002
    catalog = input.catalog
    request = input.original_request[:MAX_REQUEST_CHARS]
    kinds = sorted({item.kind for item in catalog.requirements})
    legible = catalog.text_legibility
    allowed = defects_by_requirement_kind(legibility=legible)
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
        defects_by_kind="\n".join(f"  - {kind}: {', '.join(allowed[kind])}" for kind in kinds),
        verdicts=PROMPT_VOCABULARIES["INTENT_REVIEW_VERDICTS"].rendered,
        defect_kinds=PROMPT_VOCABULARIES[
            "INTENT_REVIEW_LEGIBILITY_DEFECT_KINDS" if legible else "INTENT_REVIEW_DEFECT_KINDS"
        ].rendered,
        confidence_levels=PROMPT_VOCABULARIES["INTENT_REVIEW_CONFIDENCE"].rendered,
        legibility_rule=LEGIBILITY_RULE if legible else "",
    )


class IntentReviewAgent(Agent[IntentReviewInput, IntentReviewOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.kria.intent_review",
        prompt_id="intent_review",
        prompt_version=PROMPT_VERSION,
        model=VISION_MODEL,
        max_attempts=1,
        timeout_s=DEFAULT_TIMEOUT_S,
        cost_per_1k_input_usd=_usd_per_1k(tokens_in=1000),
        cost_per_1k_output_usd=_usd_per_1k(tokens_out=1000),
        sensitive_io=True,
        enable_clarification_retries=False,
        schema_retry_limit=0,
    )
    Input = IntentReviewInput
    Output = IntentReviewOutput
    max_output_tokens: ClassVar[int | None] = MAX_OUTPUT_TOKENS

    def run(self, input, *, ctx=None):  # noqa: A002, ANN001, ANN201
        # The run record names the prompt this catalog was sent, not the class default.
        try:
            version = prompt_version_for(self._validate_input(input).catalog)
        except Exception:  # noqa: BLE001 - the base run owns the private invalid-input error
            version = self.spec.prompt_version
        if version == self.spec.prompt_version:
            return super().run(input, ctx=ctx)
        sent = copy.copy(self)
        sent.spec = dataclasses.replace(self.spec, prompt_version=version)
        return Agent.run(sent, input, ctx=ctx)

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
