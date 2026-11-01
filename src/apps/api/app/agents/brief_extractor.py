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
from app.agents.prompt_vocab import render_vocab_tokens
from app.kria.brief import (
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
    bind_updates,
    parse_brief_updates,
    start_fresh_updates,
)
from app.pipeline.prompt_loader import load_prompt

# v9 (KRI-667/KRI-669): shared brief preserves heading and language-neutral ask facts.
# v10 (KRI-700): shared brief records `footage_light` for brighten / darken asks.
# v11 (KRI-674 follow-up): timing examples satisfy the non-empty content contract.
# v12 (KRI-721 live check): shared brief carries unprocessed source-audio intent.
# v13 (KRI-793): shared brief records the words to spell in the captions (`spellings`).
# v14 (KRI-791 + KRI-793): retain spellings and record text `triggers` + `speaker`.
# v14 (KRI-792): named spoken cuts carry cut_lines as well as caption spellings.
# v15 (KRI-789): shared brief records quoted start / end lines as a `timing` trim.
# v16 shares the complete caption/spoken-trigger/quoted-trim/named-cut instruction union.
# v17 (KRI-814): shared brief section teaches the typed `delegates_all_choices` declaration.
# v18 (KRI-814): earlier creator lines never read into the brief are read with the current one.
# v20 combines delegation v18 with subtitle-semantics v19 in the shared brief.
# v21 (KRI-816): shared brief section teaches the typed `starts_fresh` declaration.
# v22 (KRI-827): the shared brief section also records `keep_text_off` facts.
# v21 (KRI-724): shared brief records `speech_remove` {quote, occurrence} and
# `noise_reduction: "engine"`, on top of v20.
# v21 (KRI-840): shared brief records `display_mode` ("fullscreen" / "pip") for a timed photo ask.
# v21 (KRI-841): shared brief records a video added after the talk as `ending_clip` + `keep_audio`.
# v21 (KRI-842): the shared brief records `clip_sound` (one clip's sound under / after a voice).
# v21 (KRI-847): shared brief records a ceiling length as `duration_bound: "at_most"`.
# v21 (KRI-849): shared brief records `ask_if_unclear` and the creator's `unclear_answer`.
# v34 (KRI-881): main's v33 plus the shared brief section typing a research ask
# (`facts.research`).
# v35 (KRI-689): the shared brief section records a clip-speed ask as `facts.playback_rate`.
# v36: the shared brief section records a `timed_card` fact (a photo as a small card at an
# exact second / clip) for phone Montage and Voiceover edits.
# v37 (KRI-942): the shared brief section records clip sound on / off (`clip_sound` "on"/"off").
# v38 (KRI-957): an after-voice clip ask's `triggers` is the line said in that clip, never the
# creator's own last voiceover words.
# v39 (KRI-994): the shared brief section records `only_media_kind` / `include_media_kind` on a
# select ask.
# v41 (29 Ekim lane D, KRI-1038): the shared brief section records words shown a piece at a
# time as a `text_sequence` requirement, not `captions: word_by_word`. (v40 is lane B's.)
# v42 (KRI-1027): the shared brief section records `landscape_fit` ("fill" / "fit") on a style
# ask about how sideways clips sit in a vertical video.
# v45 (prompt fidelity, animation): the shared brief section records an unnamed animation ask
# as a `text` requirement with `animation: "any"`.
BRIEF_EXTRACTOR_PROMPT_VERSION = "2026-10-31-v45"
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
    .replace(
        'ALWAYS add a `timing` requirement ({"kind": "timing", "scope": "global", '
        '"facts": {"duration_s":\nN}})',
        'ALWAYS add a `timing` requirement with `scope`: "global", `literal`: null,\n'
        '`description`: the creator\'s own length request, and `facts`: {"duration_s": N}',
    )
)
# KRI-543: lets a style ask about existing text be checked instead of "can't verify". Extractor
# only: the main planner's brief section (and so its prompt version) stays untouched.
_STYLE_INTENT_SECTION = render_vocab_tokens("""
When a `style` requirement asks to change how existing on-screen text looks, add
`style_intent` to its `facts` so the result can be checked:
{"style_intent": {"set": [{"field": "entrance", "value": "fade"}], "target": "all_text"}}.
`field` and `value` come ONLY from this list: `entrance` ({{TEXT_ANIMATION_EDGES}}; the value
is the id, never the name in brackets),
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
""")
_NARROW_BRIEF_SECTION += _STYLE_INTENT_SECTION
_NARROW_BRIEF_SECTION += """
Every add/change update needs a non-empty `literal` or `description`, including
timing updates that have `duration_s` in `facts`. Keep all independently requested
constraints in each response, including a schema-correction response; correcting
one invalid field must not remove the other requested changes.
"""


class BriefExtractorAgent(Agent[BriefExtractionInput, BriefExtractionOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.creator.brief_extractor",
        prompt_id="brief_extractor",
        prompt_version=BRIEF_EXTRACTOR_PROMPT_VERSION,
        # This step extracts a small ledger update, not a new editorial plan.
        # Choose the interactive model before provider contact: an unknown Pro
        # outcome cannot safely fall through to Flash after its 60s deadline.
        model="gemini-3.6-flash",
        fallback_models=("gemini-3.1-pro-preview",),
        max_attempts=3,
        backoff_s=(2.0,),
        timeout_s=60.0,
        thinking_level="medium",
        sensitive_io=True,
        schema_retry_limit=2,
    )
    Input = BriefExtractionInput
    Output = BriefExtractionOutput
    response_json = True
    # Gemini shares this allowance between reasoning and the visible answer.
    # The 12k-contract/40-message probe used 2,650 + 443 tokens; 2,048 truncated
    # the response and spent another call before the edit could be approved.
    max_output_tokens = 4_096

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
            allowed = {
                "brief_updates",
                "request_scope",
                "clarification",
                "delegates_all_choices",
                "starts_fresh",
            }
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
            updates = parse_brief_updates(data["brief_updates"])
            for update in updates:
                for field in ("preserve_editor_text", "editor_timing_only"):
                    if field in update.facts and not isinstance(update.facts[field], bool):
                        raise ValueError(f"{field} must be a boolean")
            starts_fresh = data.get("starts_fresh") is True
            # Keep old direct callers compatible: only planner-supplied context
            # enables ledger validation.  The planner always supplies the field,
            # including an empty brief, so non-additive updates fail closed.
            if "current_brief" in input.model_fields_set:
                # The model owns semantic targets/content, not concurrency tokens.
                # Bind to the immutable snapshot supplied BEFORE inference (and complete
                # a partial change from it, KRI-805). Never read/rebind to a newer
                # ledger here: apply_updates and the final persistence CAS must still
                # reject genuinely stale work.
                updates = list(bind_updates(input.current_brief, updates))
                if starts_fresh:
                    updates = list(start_fresh_updates(input.current_brief, updates))
                apply_updates(input.current_brief or CreativeBrief(), updates, source_turn_id=None)
            return BriefExtractionOutput(
                brief_updates=updates,
                # KRI-814: a declaration beside stated requirements fails closed.
                delegates_all_choices=data.get("delegates_all_choices") is True and not updates,
                starts_fresh=starts_fresh,
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
