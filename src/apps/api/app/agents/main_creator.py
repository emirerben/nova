"""High-level creative planner for the durable Main Creator Agent controller.

The model sees only a descriptive, server-resolved capability manifest and
returns an inert strategy. A deterministic compiler and authenticated route are
the only code allowed to turn that strategy into typed product operations.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    CREATOR_REQUEST_MAX_CHARS,
    CreatorAgentOutput,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.config import settings
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import (
    MontageCadenceConstraint,
    recognize_cadence_reuse_policy,
    recognize_explicit_cadence_reuse_policy,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
    rejects_round_robin_cadence,
    resolve_video_reuse_policy,
)

# v28 -> v29: added the flag-gated KRI-127 clip_intents section (see
# `_CLIP_INTENTS_PROMPT_SECTION` below). v29 -> v30: KRI-129 added the
# `caption` op (+ `caption_attribute`) to the SAME section.
# `settings.clip_intents_enabled=False` renders the identical v28 prompt text
# byte-for-byte (the new template slot renders to an empty string on the same
# blank line it replaced) -- pinned by
# `test_main_creator_prompt_flag_off_is_byte_identical_to_pre_kri127`. There is
# no repo precedent for a second, flag-conditional prompt_version, so this is a
# single bump covering both prompt states.
MAIN_CREATOR_PROMPT_VERSION = "2026-09-22-v30"

# KRI-127 (flag `clip_intents_enabled`). Kept out of prompts/main_creator.txt's
# unconditional JSON envelope so a flag-off render never differs by even one
# example line; only ever substituted into the one optional template slot.
_CLIP_INTENTS_PROMPT_SECTION = """
OPEN-VOCABULARY CLIP INTENTS
When the creator asks to label, name, group, order, include, or caption clips by ANY
attribute they describe in their own words -- not only a coded sport/participant/score
field -- add `clip_intents` to `strategy`: a list of at most 6 objects, each
{"intent_id": "short-slug", "op": "label|group|order|include|caption", "attribute": "the
creator's described attribute -- WHICH clips this is about, in your own words",
"creator_text": "the creator's exact on-screen words for this intent, or null",
"caption_attribute": "op=\\"caption\\" with no creator_text ONLY -- what the caption should
be ABOUT (e.g. \\"the weather\\"), never which clips; null for every other case",
"position": "first|last (only for op=\\"order\\"), else null"}. Examples this covers
(diverse; treat every similarly-shaped request the same way, not only these): "put the name
of the dish on each food clip" (label), "group these by city" (group), "move the clips
where nobody is on screen to the end" (order, position "last"), "only use the clips with my
dog in them" (include), "put my product's name under the unboxing shots" (label), 'say
"post match feast" on the food clips' (caption; creator_text="post match feast",
attribute="the food clips"), "add a caption about the weather on the beach clips" (caption;
creator_text=null, attribute="the beach clips", caption_attribute="the weather"). Prefer
`clip_intents` over `sport_labels`/`context_label` for any such request: when you use
`clip_intents`, leave `sport_labels` false and `context_label` null. Never put a per-clip
answer, a media id, or label/caption text you invented into `clip_intents` -- the server
matches clips to the described attribute and verifies any on-screen value against the
footage before it can render. `creator_text` may ONLY be the creator's own exact written
words for that intent, copied verbatim; never your paraphrase or an inference from clip
metadata. `label` prints a short tag on EVERY matching clip; `caption` is different -- it is
ONE short on-screen phrase for the WHOLE group of matching clips (a chapter), never a
per-clip value, so its `attribute` still names which clips it's for while
`caption_attribute` (only when there is no `creator_text`) names what the one phrase should
say. `analysis_only_not_copy` evidence may inform which owned clips an attribute or
`caption_attribute` is about, but you never author the label or caption text yourself.
""".strip("\n")


class MainCreatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
    creator_request: str = Field(default="", max_length=CREATOR_REQUEST_MAX_CHARS)
    creator_context: str = Field(default="", max_length=4000)
    creator_direction: str = Field(default="", max_length=4000)
    item_context: str = Field(default="", max_length=4000)
    media_context: list[dict] = Field(default_factory=list, max_length=50)
    conversation: list[dict] = Field(default_factory=list, max_length=20)
    capability_manifest: ResolvedCreatorManifest


class MainCreatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: CreatorAgentOutput


class MainCreatorAgent(Agent[MainCreatorInput, MainCreatorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.creator.main",
        prompt_id="main_creator",
        prompt_version=MAIN_CREATOR_PROMPT_VERSION,
        model="gemini-3.1-pro-preview",
        fallback_models=("gemini-3.6-flash",),
        max_attempts=2,
        backoff_s=(2.0,),
        timeout_s=35.0,
        # Reserve output capacity for the full source manifest.
        thinking_level="low",
        sensitive_io=True,
    )
    Input = MainCreatorInput
    Output = MainCreatorOutput
    response_json = True
    max_output_tokens = 4096

    def required_fields(self) -> list[str]:
        return ["action"]

    def render_prompt(self, input: MainCreatorInput) -> str:  # noqa: A002
        # Storage identity is needed for the confirmation/execution fence but
        # is not useful creative context and must not be shown to the model.
        prompt_manifest = input.capability_manifest.model_dump_json(
            exclude_none=True,
            exclude={"narration": True},
        )
        return load_prompt(
            "main_creator",
            creator_context=input.creator_context or "(not available)",
            creator_direction=input.creator_direction or "(none)",
            item_context=input.item_context or "(not available)",
            media_context=json.dumps(input.media_context, ensure_ascii=False),
            capability_manifest=prompt_manifest,
            conversation=json.dumps(input.conversation, ensure_ascii=False),
            creator_request=input.creator_request or input.user_message,
            user_message=input.user_message,
            # Renders to "" (flag off) on the one blank template line it
            # occupies, so the rest of the prompt is untouched byte-for-byte.
            clip_intents_section=(
                _CLIP_INTENTS_PROMPT_SECTION if settings.clip_intents_enabled else ""
            ),
        )

    def parse(self, raw_text: str, input: MainCreatorInput) -> MainCreatorOutput:  # noqa: A002
        self._schema_feedback = ""
        try:
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            raw_action = _repair_action_envelope(data.get("action"))
            if isinstance(raw_action, dict) and raw_action.get("kind") == "propose_strategy":
                raw_strategy = raw_action.get("strategy")
                if not isinstance(raw_strategy, dict) or "target_duration_s" not in raw_strategy:
                    raise ValueError("propose_strategy must explicitly choose target_duration_s")
                rationale = raw_strategy.get("rationale")
                if not isinstance(rationale, str) or not rationale.strip():
                    raise ValueError("propose_strategy must explain its duration in rationale")
            action = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(raw_action)
            if isinstance(action, ProposeStrategy):
                # Share the compiler's exact policy: guided planning never
                # echoes opaque IDs, while native planning remains bounded to
                # owned non-asset media.
                from app.agents._schemas.creator_policy import (  # noqa: PLC0415
                    explicit_scope_from_stated_media_count,
                    normalize_creator_strategy_media,
                )

                user_messages = [
                    str(turn.get("content") or "")
                    for turn in input.conversation
                    if isinstance(turn, dict) and turn.get("role") == "user"
                ]
                request_contract = input.creator_request or input.user_message
                timing = recognize_mixed_media_timing("\n".join([*user_messages, request_contract]))
                combined_request = "\n".join([*user_messages, request_contract])
                latest_cut_s = recognize_round_robin_cadence(input.user_message)
                cadence_cut_s = (
                    None
                    if rejects_round_robin_cadence(input.user_message)
                    else latest_cut_s or recognize_round_robin_cadence(combined_request)
                )
                videos = [
                    media
                    for media in input.capability_manifest.media
                    if media.kind == "video" and media.duration_s is not None
                ]
                cadence = None
                if cadence_cut_s is not None and len(videos) == 2:
                    reuse_policy = recognize_explicit_cadence_reuse_policy(
                        input.user_message
                    ) or recognize_cadence_reuse_policy(combined_request)
                    cadence = MontageCadenceConstraint(
                        source_media_ids=[media.media_id for media in videos],
                        cut_duration_s=cadence_cut_s,
                        reuse_policy=reuse_policy,
                    )
                reuse = "once"
                for message in [request_contract, *user_messages, input.user_message]:
                    reuse = resolve_video_reuse_policy(message, reuse)
                reuse = resolve_video_reuse_policy(input.user_message, reuse, cadence)
                if reuse == "once":
                    # A numeric cadence structurally requires reusing (or at
                    # least re-cutting between) the same two sources more
                    # than once; "once" only happens here when the creator's
                    # own latest wording explicitly forbade any repeat/loop,
                    # which is incompatible with that cadence. Falling back
                    # to an ordinary montage (not erroring) mirrors the
                    # route's identical rule (creator_agent.py, same "once"
                    # check), so this is existing, intentional policy rather
                    # than a silent drop of unrelated creator intent.
                    cadence = None
                # KRI-129 part C: the regex is evidence FOR an explicit scope,
                # never a veto over what the model itself read from the full
                # conversation. Regex silence must not erase a model-authored
                # "all"/"selected" the creator did state some other way.
                explicit_scope = _explicit_media_scope_from_request(combined_request)
                if explicit_scope is None and explicit_scope_from_stated_media_count(
                    combined_request, input.capability_manifest
                ):
                    # "Continue with 16 clips" naming the whole manifest size
                    # is just as explicit as literally saying "all" -- apply
                    # the same rule the route applies post-hoc, so it holds
                    # even before `_apply_explicit_render_intent` runs.
                    explicit_scope = "all"
                resolved_scope = (
                    explicit_scope if explicit_scope is not None else action.strategy.media_scope
                )
                strategy = action.strategy.model_copy(
                    update={
                        "mixed_media_timing": timing,
                        "montage_cadence": cadence,
                        "video_reuse_policy": reuse,
                        "media_scope": resolved_scope,
                    }
                )
                action = action.model_copy(
                    update={
                        "strategy": normalize_creator_strategy_media(
                            input.capability_manifest,
                            strategy,
                            repair_model_output=True,
                        )
                    }
                )
            return MainCreatorOutput(action=action)
        except ValidationError as exc:
            # Tell the retry which contract fields failed, without echoing
            # private field values or the model's full response into logs.
            self._schema_feedback = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                for error in exc.errors(include_input=False, include_context=False)[:8]
            )[:1000]
            raise SchemaError(f"main_creator: invalid output: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"main_creator: invalid output: {exc}") from exc

    def schema_clarification(self) -> str:
        feedback = getattr(self, "_schema_feedback", "")
        return (
            "\nReturn only the documented JSON envelope with one valid action object."
            + (f"\nCorrect these schema errors: {feedback}." if feedback else "")
            + " Use only documented fields, exact enum values, and #RRGGBB colors."
        )


def _repair_action_envelope(action: object) -> object:
    """Repair only the known harmless nested-summary envelope typo.

    Some model responses put a proposal summary inside ``strategy`` although
    the documented envelope puts it beside ``strategy``. Move only a string
    summary when the destination is absent; all other malformed or unknown
    fields remain subject to the strict adapter and fail closed.
    """

    if not isinstance(action, dict) or action.get("kind") != "propose_strategy":
        return action
    strategy = action.get("strategy")
    if not isinstance(strategy, dict):
        return action
    nested_summary = strategy.get("summary")
    if "summary" in action or not isinstance(nested_summary, str):
        return action
    repaired_strategy = dict(strategy)
    repaired_strategy.pop("summary", None)
    repaired = dict(action)
    repaired["strategy"] = repaired_strategy
    repaired["summary"] = nested_summary
    return repaired


__all__ = [
    "MAIN_CREATOR_PROMPT_VERSION",
    "MainCreatorAgent",
    "MainCreatorInput",
    "MainCreatorOutput",
    "_repair_action_envelope",
]


def _explicit_media_scope_from_request(request: str) -> str | None:
    """Keep the model from turning ordinary editorial selection into all-media scope."""

    normalized = " ".join(str(request or "").casefold().split())
    if re.search(
        r"\b(?:do not|don't|dont|never|without|no)\b.{0,40}"
        r"\b(?:use|include|keep|select)\b.{0,20}\b(?:all|everything|every)\b"
        r"|\b(?:all|everything|every)\b.{0,20}\b(?:not|excluded|omit)\b",
        normalized,
    ):
        return "selected"
    if re.search(
        r"\b(?:all|every|each)\s+(?:the\s+)?(?:images?|photos?|videos?|clips?|media|footage)\b"
        r"|\buse\s+(?:all|everything)\b"
        r"|\b(?:use|include|keep)\s+(?:these|those)(?:\s+\d+)?\s+"
        r"(?:images?|photos?|videos?|clips?|files?|pieces?\s+of\s+media)\b"
        r"|\b(?:all|every)\s+(?:uploaded|provided)\s+(?:media|files?|images?|photos?|videos?)\b",
        normalized,
    ):
        return "all"
    if re.search(
        r"\b(?:only|just)\s+(?:the\s+)?(?:selected|specified|chosen|listed)\b"
        r"|\bselected\s+(?:media|files?|clips?)\b",
        normalized,
    ):
        return "selected"
    return None
