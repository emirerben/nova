"""Narrow requirement extraction for edits that already have a rendered snapshot."""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.brief_extractor import (
    BriefExtractionInput,
    BriefExtractionOutput,
)
from app.agents.main_creator import _BRIEF_PROMPT_SECTION
from app.kria.brief import BriefUpdateBatchError, parse_brief_updates
from app.pipeline.prompt_loader import load_prompt

BRIEF_EXTRACTOR_PROMPT_VERSION = "2026-10-07-v2"
_NARROW_BRIEF_SECTION = (
    _BRIEF_PROMPT_SECTION.replace("In\nADDITION to `action`, return", "Return")
    .replace("in the same\nJSON object as `action`", "in the response\nJSON object")
    .replace(
        "Use that context when proposing `action`, but emit\nno `brief_updates` for it.",
        "Emit no `brief_updates` for incidental context.",
    )
    .replace(
        "Never guess a target; ask one concise question when the request does not name\n"
        "an unambiguous requirement.",
        "Never guess a target; emit no change/remove update for an ambiguous target.",
    )
    .replace(
        "requirements -- propose a full strategy that honours EVERY requirement in the contract.",
        "requirements.",
    )
    # KRI-470: the narrow extractor has no strategy, so drop the `target_duration_s` clauses.
    .replace(
        "Do it even when you also set `target_duration_s`, even for long\nlengths, and even",
        "Do it even for long\nlengths, and even",
    )
    .replace(
        "When the creator\nstates no length, emit NO timing requirement; a length you chose "
        "yourself in `target_duration_s`\nis never the creator's.",
        "When the creator\nstates no length, emit NO timing requirement.",
    )
)


class BriefExtractorAgent(Agent[BriefExtractionInput, BriefExtractionOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.creator.brief_extractor",
        prompt_id="brief_extractor",
        prompt_version=BRIEF_EXTRACTOR_PROMPT_VERSION,
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-3.6-flash",),
        max_attempts=3,
        backoff_s=(2.0,),
        timeout_s=60.0,
        thinking_level="low",
        sensitive_io=True,
        schema_retry_limit=2,
    )
    Input = BriefExtractionInput
    Output = BriefExtractionOutput
    response_json = True
    max_output_tokens = 2_048

    def render_prompt(self, input: BriefExtractionInput) -> str:  # noqa: A002
        return load_prompt(
            "brief_extractor",
            creator_request=input.creator_request or "(none)",
            user_message=input.user_message,
            conversation=json.dumps(input.conversation, ensure_ascii=False),
            brief_section=_NARROW_BRIEF_SECTION,
        )

    def parse(self, raw_text: str, input: BriefExtractionInput) -> BriefExtractionOutput:  # noqa: A002, ARG002
        try:
            data = json.loads(raw_text)
            if not isinstance(data, dict) or set(data) != {"brief_updates"}:
                raise ValueError("response must contain only brief_updates")
            return BriefExtractionOutput(
                brief_updates=parse_brief_updates(data["brief_updates"]),
            )
        except (
            json.JSONDecodeError,
            BriefUpdateBatchError,
            ValidationError,
            TypeError,
            ValueError,
        ) as exc:
            raise SchemaError(f"brief_extractor: invalid output: {exc}") from exc


__all__ = [
    "BRIEF_EXTRACTOR_PROMPT_VERSION",
    "BriefExtractionInput",
    "BriefExtractionOutput",
    "BriefExtractorAgent",
]
