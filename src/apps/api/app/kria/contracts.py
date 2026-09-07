"""Inert, versioned contracts shared by the Kria planner and runtime.

These models deliberately contain no executable callables, storage paths, or
authorization decisions. The registry and policy layer add those server-side.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KRIA_SCHEMA_VERSION = 2


class _KriaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KriaToolDefinition(_KriaModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    version: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=300)
    argument_schema: dict[str, Any]
    result_schema: dict[str, Any]
    capability: str
    risk: Literal["read", "reversible_draft", "approval_required"]
    execution_mode: Literal["sync", "async"]
    retry: Literal["never", "transient_once", "idempotent"]
    failure_class: str


class KriaToolIntent(_KriaModel):
    intent_id: str = Field(min_length=1, max_length=80)
    tool_name: str
    tool_version: int = Field(ge=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=8)


class KriaTurnPlan(_KriaModel):
    schema_version: Literal[2] = KRIA_SCHEMA_VERSION
    mode: Literal["respond", "act"]
    turn_value: Literal["decision", "question", "action", "progress", "review", "recovery"]
    response: str | None = Field(default=None, max_length=1200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    intents: list[KriaToolIntent] = Field(default_factory=list, max_length=8)

    @field_validator("intents")
    @classmethod
    def _unique_and_backward_dependencies(
        cls, intents: list[KriaToolIntent]
    ) -> list[KriaToolIntent]:
        seen: set[str] = set()
        for intent in intents:
            if intent.intent_id in seen:
                raise ValueError("intent IDs must be unique")
            missing = set(intent.depends_on) - seen
            if missing:
                raise ValueError("dependencies must reference earlier intents")
            seen.add(intent.intent_id)
        return intents

    @model_validator(mode="after")
    def _mode_has_one_authority_path(self) -> KriaTurnPlan:
        if self.mode == "respond":
            if not self.response or not self.response.strip():
                raise ValueError("respond plans require a direct response")
            if self.intents:
                raise ValueError("respond plans cannot contain tool intents")
        else:
            if not self.intents:
                raise ValueError("act plans require at least one tool intent")
            if self.response is not None:
                raise ValueError("act plans cannot claim a response before observation")
        return self


class KriaToolReceipt(_KriaModel):
    intent_id: str
    tool_name: str
    tool_version: int
    status: Literal[
        "pending",
        "running",
        "awaiting_approval",
        "accepted",
        "dispatched",
        "completed",
        "failed",
        "cancelled",
        "stale",
        "duplicate",
        "outcome_unknown",
    ]
    result: dict[str, Any] | None = None
    error: KriaProblem | None = None

    @model_validator(mode="after")
    def _settled_outcome_has_evidence(self) -> KriaToolReceipt:
        if self.status == "completed" and self.result is None:
            raise ValueError("completed receipts require a result")
        if self.status in {"failed", "outcome_unknown"} and self.error is None:
            raise ValueError(f"{self.status} receipts require a typed problem")
        if self.status == "completed" and self.error is not None:
            raise ValueError("completed receipts cannot contain an error")
        return self


class KriaObservedTurnResponse(_KriaModel):
    schema_version: Literal[2] = KRIA_SCHEMA_VERSION
    turn_value: Literal["decision", "question", "action", "progress", "review", "recovery"]
    message: str = Field(min_length=1, max_length=1200)
    receipt_ids: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list, max_length=3)


class KriaProblem(_KriaModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    phase: Literal["accept", "plan", "policy", "tool", "approval", "dispatch", "observe"]
    message: str
    retryable: bool = False
    recovery: Literal["retry", "refresh_replan", "ask_user", "manual", "none"] = "none"
    trace_id: str
    current_revision: int | None = Field(default=None, ge=0)
    target: dict[str, str] = Field(default_factory=dict)


KriaToolReceipt.model_rebuild()
