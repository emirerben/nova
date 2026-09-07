"""HTTP contracts for the gated Kria runtime-v2 control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.kria.contracts import KriaProblem

TurnStatus = Literal[
    "pending",
    "queued",
    "planning",
    "executing",
    "awaiting_approval",
    "observing",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]
ThreadStatus = Literal["active", "archived", "failed"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired", "cancelled", "consumed"]


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitTurnBody(_StrictBody):
    message: str = Field(min_length=1, max_length=4000)
    client_event_id: str = Field(min_length=1, max_length=160)
    expected_thread_revision: int = Field(ge=0)

    @field_validator("message")
    @classmethod
    def _normalize_message(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("message must not be blank")
        return value

    @field_validator("client_event_id")
    @classmethod
    def _opaque_client_event_id(cls, value: str) -> str:
        value = value.strip()
        if not value or any(token in value for token in ("/", "\\", "..")):
            raise ValueError("client_event_id must be opaque")
        return value


class TurnAccepted(BaseModel):
    turn_id: str
    thread_revision: int
    status: TurnStatus
    replayed: bool = False


class TurnCancelBody(_StrictBody):
    expected_thread_revision: int = Field(ge=0)


class TurnCancelled(BaseModel):
    turn_id: str
    thread_revision: int
    status: Literal["cancelled"]
    approval_ids: list[str] = Field(default_factory=list)


class ApprovalDecisionBody(_StrictBody):
    expected_thread_revision: int = Field(ge=0)
    expected_draft_revision: int = Field(ge=0)
    expected_approval_fingerprint: str = Field(min_length=64, max_length=64)

    @field_validator("expected_approval_fingerprint")
    @classmethod
    def _lowercase_sha256(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("expected_approval_fingerprint must be lowercase SHA-256")
        return value


class ApprovalDecisionOut(BaseModel):
    approval_id: str
    turn_id: str
    status: Literal["approved", "denied"]
    thread_revision: int
    # Approval only records explicit consent. A future exact-state executor
    # consumes it and dispatches through the existing Job dispatcher.
    render_dispatched: Literal[False] = False


class ApprovalSnapshotOut(BaseModel):
    approval_id: str
    turn_id: str
    draft_id: str | None = None
    draft_revision: int | None = None
    status: ApprovalStatus
    consequence_summary: str
    cost_summary: str | None = None
    expires_at: datetime
    approval_fingerprint: str = Field(min_length=64, max_length=64)


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


class DraftWriteBody(_StrictBody):
    expected_draft_revision: int = Field(ge=0)
    snapshot: dict[str, Any]


class DraftUndoBody(_StrictBody):
    expected_draft_revision: int = Field(ge=0)


class DeltaEvent(BaseModel):
    id: str
    sequence: int
    revision: int
    role: Literal["user", "assistant", "system"]
    event_type: str
    content: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class ThreadDeltaOut(BaseModel):
    thread_id: str
    runtime_version: Literal[2]
    status: ThreadStatus
    thread_revision: int
    events: list[DeltaEvent]
    after_sequence: int
    next_after_sequence: int
    before_sequence: int | None = None
    previous_before_sequence: int | None = None
    has_more: bool


class KriaProblemOut(BaseModel):
    problem: KriaProblem
