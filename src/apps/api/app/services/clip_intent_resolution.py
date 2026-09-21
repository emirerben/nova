"""Resolve creator clip intents to clips inside one chat turn (KRI-127).

Contract frozen here; the pipeline is: text resolver over the shared clip
records -> capped, deadline-bounded vision re-query for clips the record cannot
answer -> grounding fence (``app.schemas.clip_intents.ground_label``) -> either
fully resolved intents or ONE question for the creator. Never a silent partial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.clip_intents import ClipIntent, GroundedLabel, ResolvedClipIntent

# Key on a stored clip analysis holding cached vision answers (see IntentResolution).
ANSWERS_KEY = "answers"


def normalize_question(question: str) -> str:
    """Stable cache key for a vision question."""
    return " ".join(question.casefold().split())[:200]


@dataclass(frozen=True)
class IntentClip:
    """One owned clip as the resolver sees it."""

    media_id: str
    kind: str  # "video" | "image"
    analysis: dict[str, Any] | None
    gcs_path: str | None = None
    # Persists a vision answer on the clip's stored analysis so a repeat is free.
    asset_id: str | None = None


@dataclass(frozen=True)
class IntentResolution:
    intents: list[ResolvedClipIntent] = field(default_factory=list)
    # Set when any intent could not be resolved with enough certainty. The chat
    # turn must ask this instead of proposing the strategy.
    question: str | None = None
    # New vision answers from this turn, for the caller to persist on each
    # clip's stored analysis under ANSWERS_KEY so a repeat question is free:
    # {media_id: {normalized_question: {"answer", "confidence", "evidence"}}}.
    vision_answers: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    @property
    def needs_creator(self) -> bool:
        return self.question is not None


def grounded_labels(intents: list[ResolvedClipIntent] | None) -> list[GroundedLabel]:
    """Flatten resolved label intents into the only shape the render lane accepts."""
    labels: list[GroundedLabel] = []
    for intent in intents or []:
        if intent.op != "label" or intent.status != "resolved":
            continue
        for a in intent.assignments:
            if a.value and a.grounding:
                labels.append(
                    GroundedLabel(
                        media_id=a.media_id,
                        text=a.value,
                        grounding=a.grounding,
                        confidence=a.confidence,
                        intent_id=intent.intent_id,
                    )
                )
    return labels


async def resolve_clip_intents_for_turn(
    *,
    intents: list[ClipIntent],
    creator_request: str,
    clips: list[IntentClip],
    run_context: Any,
) -> IntentResolution:
    """Resolve ``intents`` against ``clips``. Implemented by KRI-127 Lane C."""
    raise NotImplementedError
