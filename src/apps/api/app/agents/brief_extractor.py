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
from app.kria.brief import (
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
    parse_brief_updates,
)
from app.pipeline.prompt_loader import load_prompt

BRIEF_EXTRACTOR_PROMPT_VERSION = "2026-10-08-v7"
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
    .replace(" and `expected_version`", "")
    .replace("`target_requirement_id`, and\n`expected_version`", "`target_requirement_id`")
)
# KRI-543: lets a style ask about existing text be checked instead of "can't verify". Extractor
# only: the main planner's brief section (and so its prompt version) stays untouched.
_STYLE_INTENT_SECTION = """
When a `style` requirement asks to change how existing on-screen text looks, add
`style_intent` to its `facts` so the result can be checked:
{"style_intent": {"set": [{"field": "entrance", "value": "fade"}], "target": "all_text"}}.
`field` and `value` come ONLY from this list: `entrance` (none, fade, pop, slide, typewriter),
`alignment` (left, center, right), `text_case` (none, upper, lower, title), `font_family` (a font
name the creator gave), `color` (a literal #RRGGBB hex the creator gave; a colour word such as
"yellow" is NOT a hex, so omit color). Use one value per field. Add `target` ONLY when the
creator named it: "all texts" / "tüm yazılar" -> "all_text", "the title" / "başlık" -> "title",
"the labels" / "etiketler" -> "labels". For "to all of them", "the others" or any wording that
does not name the text, leave `target` out. Never put ids or clip names in `style_intent`. Omit
`style_intent` entirely for vague asks ("make it feel warmer", "more energetic") and keep the
creator's words in `description`.
Examples: "Add fade in animation to all texts" -> {"style_intent": {"set": [{"field":
"entrance", "value": "fade"}], "target": "all_text"}}. "Tüm yazılara fade in animasyonu ekle" ->
the same. "Center the title" -> {"style_intent": {"set": [{"field": "alignment", "value":
"center"}], "target": "title"}}.
"""
_NARROW_BRIEF_SECTION += _STYLE_INTENT_SECTION


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
            allowed = {"brief_updates", "request_scope", "clarification"}
            if (
                not isinstance(data, dict)
                or not set(data) <= allowed
                or "brief_updates" not in data
            ):
                raise ValueError("response must contain only the supported brief fields")
            scope = data.get("request_scope")
            clarification = data.get("clarification")
            if input.require_request_scope:
                if scope not in {"edit", "rebuild", "clarify"}:
                    raise ValueError("response needs a valid request_scope")
                if scope == "clarify" and not isinstance(clarification, str):
                    raise ValueError("clarify scope needs clarification")
                if scope == "clarify" and not clarification.strip():
                    raise ValueError("clarify scope needs non-empty clarification")
                if scope != "clarify" and clarification is not None:
                    raise ValueError("only clarify scope may include clarification")
            elif scope is not None or clarification is not None:
                if scope not in {"edit", "rebuild", "clarify"}:
                    raise ValueError("request_scope is invalid")
                if scope == "clarify" and (
                    not isinstance(clarification, str) or not clarification.strip()
                ):
                    raise ValueError("clarify scope needs clarification")
                if scope != "clarify" and clarification is not None:
                    raise ValueError("only clarify scope may include clarification")
            raw_updates = data["brief_updates"]
            if "current_brief" in input.model_fields_set and isinstance(raw_updates, list):
                # The model owns semantic targets/content, not concurrency tokens.
                # Bind to the immutable snapshot supplied BEFORE inference. Never
                # read/rebind to a newer ledger here: apply_updates and the final
                # persistence CAS must still reject genuinely stale work.
                version = input.current_brief.version if input.current_brief else 0
                raw_updates = [
                    {**update, "expected_version": version}
                    if isinstance(update, dict) and update.get("operation") in {"change", "remove"}
                    else update
                    for update in raw_updates
                ]
            updates = parse_brief_updates(raw_updates)
            # Keep old direct callers compatible: only planner-supplied context
            # enables ledger validation.  The planner always supplies the field,
            # including an empty brief, so non-additive updates fail closed.
            if "current_brief" in input.model_fields_set:
                apply_updates(input.current_brief or CreativeBrief(), updates, source_turn_id=None)
            return BriefExtractionOutput(
                brief_updates=updates,
                request_scope=scope,
                clarification=clarification,
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
