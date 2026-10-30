"""Immutable revision-bound references for derived story rows."""

from dataclasses import dataclass
from typing import Literal

from app.schemas.clip_intents import ClipIntent


@dataclass(frozen=True)
class StoryRowRef:
    row_index: int
    intent_ids: tuple[str, ...] = ()
    kind: Literal["derived", "literal"] = "derived"


@dataclass(frozen=True)
class StoryIntentRefs:
    story_revision: str
    inventory_revision: str
    rows: tuple[StoryRowRef, ...]


@dataclass(frozen=True)
class RejectedClipSelection:
    intent: ClipIntent
    question_id: str
    category_key: str
    selection_sequence: int


@dataclass(frozen=True)
class SelectionReconciliation:
    rejected_selections: tuple[RejectedClipSelection, ...]
    status: Literal["reconciled", "unresolved"]
    unresolved_story_rows: tuple[str, ...] = ()
    story_refs: StoryIntentRefs | None = None
    version: Literal[1] = 1
