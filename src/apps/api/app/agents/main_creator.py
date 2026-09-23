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
from app.agents._schemas.creator_policy import CAPABILITY_DRAFT_GUIDED_PROPOSAL
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

# KRI-118 item 1: story shapes (day_vlog/single_hero) under Montage.
MAIN_CREATOR_PROMPT_VERSION = "2026-09-23-v34"

# Visual instructions are substituted only when the resolver flag is enabled;
# the base prompt independently describes deferred transcript label intents.
# Rendered INSIDE the exact-on-screen-copy rule (the `$described_text_exception`
# slot in prompts/main_creator.txt) when clip intents are on, "" when off.
# Position matters: appended after the rule the model kept asking the creator
# for exact words (6/6 live runs); stated at the rule it proposes (6/6).
_DESCRIBED_TEXT_EXCEPTION = (
    " EXCEPTION -- described text: when the creator asks for on-screen text they DESCRIBE"
    ' rather than dictate ("the name of the dish on each food clip", "a caption about the'
    ' weather on the park clips"), the missing words are not yours to write and NOT a reason'
    " to ask: emit a `clip_intents` entry (see OPEN-VOCABULARY CLIP INTENTS; `op` is exactly"
    " one of label, group, order, include, caption -- a per-clip tag is `label`, one phrase"
    " over a group of clips is `caption`) and propose the strategy; the server reads the"
    " footage to fill in the words and asks the creator only when the footage cannot answer."
    ' For caption/group/order/include, `attribute` says WHICH clips ("the food clips");'
    ' for label it says WHAT to name on each clip ("the dish shown in the clip"); never'
    ' the word "text". Example: \'Say "post match feast" on the food clips, and add a caption'
    ' about the weather on the park clips\' => [{"intent_id": "feast", "op": "caption",'
    ' "attribute": "the food clips", "creator_text": "post match feast", "caption_attribute":'
    ' null}, {"intent_id": "weather", "op": "caption", "attribute": "the park clips",'
    ' "creator_text": null, "caption_attribute": "the weather"}]; "the name of the dish on'
    ' each food clip" => {"op": "label", "attribute": "the dish shown in the clip",'
    ' "creator_text": null}.'
)

# KRI-118 item 1: guidance for the chat-picked "shape" layered on top of
# `edit_format: "montage"` -- rendered INSIDE the strategy-authoring rules
# (the `$story_shape_section` slot) only when `_story_shapes_available`
# below says the shape could actually render this turn; "" otherwise, so the
# rest of the prompt is untouched byte-for-byte.
_STORY_SHAPE_PROMPT_SECTION = """
STORY SHAPE
The Montage card is the only picker entry for this direction, but two shapes are available
UNDER it: set `archetype` (alongside `edit_format: "montage"`) to "day_vlog" when the
request or footage reads as "my day", "morning to night", or "a day at X" -- the edit is
cut in chronological (shooting) order. Set `archetype` to "single_hero" and `hero_media_id`
to the owned media id that should dominate when one clip clearly should carry the edit and
the rest are cutaways ("show off this shot", "make this clip the star"). Never set
`archetype` for any other request; leave it null. Whenever you pick a shape, `summary` MUST
name the choice in plain language (for example "I'm cutting this as a day vlog, in the order
you shot it." or "I'm building this around your clip, with the rest as cutaways.") -- never
pick a shape silently.
""".strip("\n")

_CLIP_INTENTS_PROMPT_SECTION = """
OPEN-VOCABULARY CLIP INTENTS
When the creator asks to label, name, group, order, include, or caption clips by ANY
attribute they describe in their own words -- add `clip_intents` to `strategy`:
a list of at most 6 objects, each
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
creator_text=null, attribute="the beach clips", caption_attribute="the weather").
Use label_source="clip" for footage-derived labels (including the sport being played).
Requests about spoken scores, participant placeholders, or spoken topics use the
transcript source described above instead; never send them through the visual source.
Never put a per-clip
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
Not having that text is NEVER a reason to ask the creator: a described label or caption is
complete as an intent, the server reads each clip's footage to fill in the value, and it asks
the creator itself only when the footage cannot answer. Propose the strategy with the intent.
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
        # KRI-118 item 4: bumped from 2 -> 3 alongside `schema_retry_limit`
        # below -- `_run_on_model`'s loop bounds EVERY retry path (transient/
        # refusal/schema) by `max_attempts`, so a `schema_retry_limit` above
        # `max_attempts - 1` is otherwise unreachable dead configuration.
        max_attempts=3,
        backoff_s=(2.0,),
        timeout_s=35.0,
        # Reserve output capacity for the full source manifest.
        thinking_level="low",
        sensitive_io=True,
        # This agent's output schema (bounded editorial choices across many
        # optional fields, typed evidence, clip intents) is wide enough that
        # one clarification retry sometimes isn't enough headroom to recover
        # from a single missed constraint.
        schema_retry_limit=2,
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
            described_text_exception=(
                _DESCRIBED_TEXT_EXCEPTION if settings.clip_intents_enabled else ""
            ),
            # KRI-118 item 1: only mention story shapes when they could
            # actually render this turn (rollout flag + guided proposal
            # capability + no recorded voiceover) -- defense in depth, since
            # `compile_strategy_to_plan` (`repair_creator_strategy_shape`)
            # repairs an unavailable shape away regardless of whether the
            # model saw this guidance.
            story_shape_section=(
                _STORY_SHAPE_PROMPT_SECTION
                if _story_shapes_available(input.capability_manifest)
                else ""
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


def _story_shapes_available(manifest: ResolvedCreatorManifest) -> bool:
    """KRI-118 item 1: whether the STORY SHAPE prompt guidance is worth
    showing this turn -- rollout flag, guided proposal capability, and no
    recorded voiceover (a shape only ever renders on a guided montage).
    Mirrors, but does not replace, the server-side repair in
    `app.agents._schemas.creator_policy.repair_creator_strategy_shape` --
    this only controls whether the MODEL is invited to propose one; the
    compiler never trusts the model's own choice alone.
    """

    if not settings.creator_montage_shapes_enabled or manifest.has_voiceover:
        return False
    guided = manifest.capabilities.get(CAPABILITY_DRAFT_GUIDED_PROPOSAL)
    return bool(guided is not None and guided.available)


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
