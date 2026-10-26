"""Validated clip instructions held by one unanswered picker and its media set."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.clip_intent_planner import PlannedClipIntent
from app.schemas.clip_intents import MAX_CLIP_INTENTS, ResolvedClipIntent
from app.services.clip_intent_resolution import IntentClip


class PickerInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_digest: str = Field(min_length=64, max_length=64)
    planned: list[PlannedClipIntent] = Field(max_length=MAX_CLIP_INTENTS)
    resolved: list[ResolvedClipIntent] = Field(max_length=MAX_CLIP_INTENTS)


def media_digest(clips: Sequence[IntentClip]) -> str:
    identities = [(c.media_id, c.kind, c.gcs_path, c.generation) for c in clips]
    return hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()


def answered_inventory(
    events: Iterable[tuple[str, dict[str, Any] | None]], clips: Sequence[IntentClip]
) -> PickerInventory | None:
    questions: dict[str, dict[str, Any]] = {}
    answer: dict[str, Any] | None = None
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(question := payload.get("clip_question"), dict):
            questions[str(question.get("question_id"))] = question
        if role == "user":
            selection = payload.get("clip_selection")
            answer = selection if isinstance(selection, dict) else None
    if answer is None:
        return None
    question = questions.get(str(answer.get("question_id")))
    if question is None or not isinstance(question.get("inventory"), dict):
        return None
    try:
        inventory = PickerInventory.model_validate(question["inventory"])
    except (ValidationError, TypeError, ValueError):
        return None
    if inventory.media_digest != media_digest(clips):
        return None
    planned = {i.intent_id: i for i in inventory.planned}
    owned = {c.media_id for c in clips}
    if len(planned) != len(inventory.planned):
        return None
    for resolved in inventory.resolved:
        intent = planned.get(resolved.intent_id)
        if (
            intent is None
            or resolved.op != intent.op
            or resolved.creator_text != intent.creator_text
            or not set(resolved.media_ids()) <= owned
        ):
            return None
    return inventory
