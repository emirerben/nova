"""High-level creative planner for the durable Main Creator Agent controller.

The model sees only a descriptive, server-resolved capability manifest and
returns an inert strategy. A deterministic compiler and authenticated route are
the only code allowed to turn that strategy into typed product operations.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    CREATOR_REQUEST_MAX_CHARS,
    CreatorAgentOutput,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import (
    MontageCadenceConstraint,
    recognize_cadence_reuse_policy,
    recognize_explicit_cadence_reuse_policy,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
    rejects_round_robin_cadence,
)

MAIN_CREATOR_PROMPT_VERSION = "2026-09-07-v18"


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
        )

    def parse(self, raw_text: str, input: MainCreatorInput) -> MainCreatorOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            action = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
                _repair_action_envelope(data.get("action"))
            )
            if isinstance(action, ProposeStrategy):
                # Share the compiler's exact policy: guided planning never
                # echoes opaque IDs, while native planning remains bounded to
                # owned non-asset media.
                from app.agents._schemas.creator_policy import (  # noqa: PLC0415
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
                strategy = action.strategy.model_copy(
                    update={
                        "mixed_media_timing": timing,
                        "montage_cadence": cadence,
                        "media_scope": _explicit_media_scope_from_request(combined_request),
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
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"main_creator: invalid output: {exc}") from exc

    def schema_clarification(self) -> str:
        return "\nReturn only the documented JSON envelope with one valid action object."


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
