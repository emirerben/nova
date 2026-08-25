"""Bounded relevance classifier for creator workspace intake.

This first dark-launched implementation is deliberately deterministic.  It
keeps the proposal/approval boundary useful while a model-backed classifier is
evaluated: the classifier can only select an existing item or a neutral
``new_topic`` bucket and never writes creator preferences or media state.
"""

from __future__ import annotations

import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec


class DetectPlanRelevanceInput(BaseModel):
    media: list[dict] = Field(min_length=1, max_length=50)
    plan_items: list[dict] = Field(default_factory=list, max_length=200)


class DetectPlanRelevanceOutput(BaseModel):
    relevance: Literal["existing_item", "new_topic", "unmatched"]
    target_plan_item_id: str | None = None
    topic: str | None = None
    rationale: str = Field(default="", max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)


def _tokens(value: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", str(value or "").lower())
        if token not in {"this", "that", "with", "from", "your", "video", "clip"}
    }


class DetectPlanRelevanceAgent(Agent[DetectPlanRelevanceInput, DetectPlanRelevanceOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.detect_plan_relevance",
        prompt_id="detect_plan_relevance",
        prompt_version="2026-08-25-rule-based-v1",
        model="rule_based",
        max_attempts=1,
    )
    Input = DetectPlanRelevanceInput
    Output = DetectPlanRelevanceOutput

    def __init__(self, model_client=None) -> None:  # noqa: ANN001
        # Rule-based agents never invoke the client, but the shared Agent base
        # keeps one constructor shape for model-backed siblings.
        super().__init__(model_client)

    def render_prompt(self, input: DetectPlanRelevanceInput) -> str:  # noqa: A002
        raise NotImplementedError("rule-based relevance does not render a prompt")

    def parse(
        self,
        raw_text: str,
        input: DetectPlanRelevanceInput,  # noqa: ARG002, A002
    ) -> DetectPlanRelevanceOutput:
        raise NotImplementedError("rule-based relevance does not parse model output")

    def compute(self, input: DetectPlanRelevanceInput) -> DetectPlanRelevanceOutput:
        media_tokens: set[str] = set()
        for media in input.media:
            media_tokens |= _tokens(media.get("label"))
            media_tokens |= _tokens(media.get("source_filename"))
        best: tuple[int, dict] | None = None
        for item in input.plan_items:
            item_tokens = _tokens(item.get("theme")) | _tokens(item.get("idea"))
            overlap = len(media_tokens & item_tokens)
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, item)
        if best is not None:
            return DetectPlanRelevanceOutput(
                relevance="existing_item",
                target_plan_item_id=str(best[1]["id"]),
                rationale="The uploaded media shares grounded terms with an existing plan item.",
                confidence=min(0.95, 0.7 + 0.1 * best[0]),
            )
        # Do not invent a creator preference or topic from filenames.  The
        # creator can name the neutral proposal explicitly before approval.
        return DetectPlanRelevanceOutput(
            relevance="new_topic",
            topic="New footage",
            rationale="No existing plan item was grounded by the supplied media labels.",
            confidence=0.55,
        )


__all__ = [
    "DetectPlanRelevanceAgent",
    "DetectPlanRelevanceInput",
    "DetectPlanRelevanceOutput",
]
