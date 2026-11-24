"""Story sequencer: ranks the clips nobody placed into the order that tells the story (KRI-1593).

A separate, small agent on purpose (owner direction on KRI-1586: "the AI decides what fits the
story; if it can't, upload order"): the Main Creator's prompt and its fingerprint do not change.
It reads the clips' stored records and the creator's words, and returns ONE thing: the open
clips' ids in play order, or ``decided: false`` when the clips give no reason to prefer an order.

It decides nothing else. Which clips are open, which stretch each may move within, and whether
the answer is used at all are the server's (`app.services.story_order`): a clip the creator
placed is never in the open set, so no answer can move it. The ``reason`` is for the admin trace
only and is never shown to the creator.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt


class StorySequencerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The creator's message and their live asks: what the video is for. Bounded by the caller.
    request: str = Field(default="", max_length=2000)
    asks: list[str] = Field(default_factory=list, max_length=12)
    # The clips in the order they play now (`story_order.sequencer_clips`): alias ``id``, the
    # ``span`` it may move within (None = the creator placed it), and its stored evidence.
    clips: list[dict[str, Any]] = Field(min_length=2, max_length=200)

    @model_validator(mode="after")
    def clips_are_addressable(self) -> StorySequencerInput:
        ids = [clip.get("id") for clip in self.clips]
        if not all(isinstance(item, str) and item for item in ids):
            raise ValueError("every clip needs an id")
        if len(set(ids)) != len(ids):
            raise ValueError("clip ids must be unique")
        if not any(clip.get("span") for clip in self.clips):
            raise ValueError("no clip is open to order")
        return self

    def open_ids(self) -> list[str]:
        return [str(clip["id"]) for clip in self.clips if clip.get("span")]


class StorySequencerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided: bool = False
    order: list[str] = Field(default_factory=list, max_length=200)
    reason: str = Field(default="", max_length=240)


class StorySequencerAgent(Agent[StorySequencerInput, StorySequencerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.kria.story_sequencer",
        prompt_id="story_sequencer",
        prompt_version="2026-11-24-kri1593-v1",
        model="gemini-2.5-flash",
        thinking_budget=512,
        # One bounded call inside the draft turn: a slow or failed run means upload order, never
        # a retry storm in front of the creator.
        timeout_s=20.0,
        max_attempts=2,
        backoff_s=(2.0,),
        enable_clarification_retries=False,
        enable_json_repair=True,
        sensitive_io=True,
    )
    max_output_tokens = 2048
    Input = StorySequencerInput
    Output = StorySequencerOutput

    def required_fields(self) -> list[str]:
        return ["order"]

    def render_prompt(self, input: StorySequencerInput) -> str:  # noqa: A002
        return load_prompt(
            "story_sequencer",
            request=input.request.strip() or "(nothing beyond making a video from these clips)",
            asks_json=json.dumps(input.asks, ensure_ascii=False),
            clips_json=json.dumps(input.clips, ensure_ascii=False),
        )

    def parse(self, raw_text: str, input: StorySequencerInput) -> StorySequencerOutput:  # noqa: A002
        try:
            output = StorySequencerOutput.model_validate(json.loads(raw_text))
        except (TypeError, ValueError, ValidationError) as exc:
            raise SchemaError(f"story_sequencer: malformed output: {exc}") from exc
        open_ids = input.open_ids()
        if not output.decided:
            # "Cannot decide" is an answer: the clips stay as they are, whatever else it wrote.
            return output.model_copy(update={"order": []})
        known = set(open_ids)
        unknown = [item for item in output.order if item not in known]
        if unknown:
            raise SchemaError(
                "story_sequencer: order names clips that are not open: "
                + ", ".join(sorted(set(unknown))[:8])
            )
        if len(set(output.order)) != len(output.order):
            raise SchemaError("story_sequencer: order repeats a clip")
        missing = known - set(output.order)
        if missing:
            raise SchemaError(
                "story_sequencer: order leaves out open clips: " + ", ".join(sorted(missing)[:8])
            )
        return output
