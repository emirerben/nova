"""Dependency-neutral saved editor snapshot contract."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DraftSnapshotOut(BaseModel):
    draft_id: str
    item_id: str
    variant_key: str
    draft_revision: int = Field(ge=0)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    etag: str
    base_job_id: str | None = None
    base_generation_id: str | None = None
    snapshot: dict[str, Any]
    can_undo: bool
    created_at: datetime
    # Additive: old clients ignore them. ``base_generation`` is the generation the head's
    # editor payload is built on after healing; ``head_current`` says it is the variant's.
    head_current: bool = True
    base_generation: str | None = None
