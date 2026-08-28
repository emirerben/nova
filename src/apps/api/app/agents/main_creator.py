"""High-level creative planner for the durable Main Creator Agent controller.

The model sees only a descriptive, server-resolved capability manifest and
returns an inert strategy. A deterministic compiler and authenticated route are
the only code allowed to turn that strategy into typed product operations.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    CreatorAgentOutput,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import (
    recognize_mixed_media_timing,
)

MAIN_CREATOR_PROMPT_VERSION = "2026-08-28-v9"


class MainCreatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message: str = Field(min_length=1, max_length=2000)
    creator_context: str = Field(default="", max_length=4000)
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
        thinking_level="medium",
    )
    Input = MainCreatorInput
    Output = MainCreatorOutput
    response_json = True
    max_output_tokens = 3000

    def required_fields(self) -> list[str]:
        return ["action"]

    def render_prompt(self, input: MainCreatorInput) -> str:  # noqa: A002
        return load_prompt(
            "main_creator",
            creator_context=input.creator_context or "(not available)",
            item_context=input.item_context or "(not available)",
            media_context=json.dumps(input.media_context, ensure_ascii=False),
            capability_manifest=input.capability_manifest.model_dump_json(exclude_none=True),
            conversation=json.dumps(input.conversation, ensure_ascii=False),
            user_message=input.user_message,
        )

    def parse(self, raw_text: str, input: MainCreatorInput) -> MainCreatorOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            action = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(data.get("action"))
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
                timing = recognize_mixed_media_timing(
                    "\n".join([*user_messages, input.user_message])
                )
                strategy = action.strategy.model_copy(update={"mixed_media_timing": timing})
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


__all__ = [
    "MAIN_CREATOR_PROMPT_VERSION",
    "MainCreatorAgent",
    "MainCreatorInput",
    "MainCreatorOutput",
]
