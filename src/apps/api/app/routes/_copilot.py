"""Shared edit-copilot turn helper.

The plan-items route mounts v1. A future generative-jobs mirror can reuse this
module after it supplies its own ownership/variant guard.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_copilot import (
    CopilotOutcome,
    EditCopilotAgent,
    EditCopilotInput,
    EditCopilotOutput,
)

log = structlog.get_logger()

_MAX_SNAPSHOT_BYTES = 20 * 1024


class CopilotTurnBody(BaseModel):
    message: str = Field(default="", max_length=2000)
    turns: list[dict] = Field(default_factory=list, max_length=12)
    snapshot: dict = Field(default_factory=dict)

    @field_validator("message", mode="before")
    @classmethod
    def _coerce_message(cls, value: object) -> str:
        return str(value or "")


class CopilotTurnResponse(BaseModel):
    intent: str
    ops: list[dict] = []
    confidence: float
    reply: str
    suggestions: list[str] = []
    needs_clarification: bool = False
    outcome: CopilotOutcome = "no_effect"
    rejection_reasons: list[dict[str, str]] = []


_SUCCESS_WORDS = re.compile(
    r"\b(done|stored|changed|updated|applied|edited|trimmed|removed|swapped|made|set)\b",
    re.IGNORECASE,
)
_NEGATED_SUCCESS = re.compile(
    r"\b(already|unchanged|cannot|can't|couldn't|unable|not|no change|nothing)\b",
    re.IGNORECASE,
)


def _claims_success(reply: str) -> bool:
    return bool(_SUCCESS_WORDS.search(reply) and not _NEGATED_SUCCESS.search(reply))


def _honest_outcome(output: EditCopilotOutput, ops: list[dict]) -> tuple[CopilotOutcome, str]:
    """Derive a stable outcome and prevent success prose for empty edits."""
    reasons = output.rejection_reasons
    if ops:
        outcome = "applied"
    elif any(item.get("reason") == "stale_target" for item in reasons):
        outcome = "stale"
    elif output.intent == "reject" or any(
        item.get("reason") in {"capability_unavailable", "unknown_operation"} for item in reasons
    ):
        outcome = "unsupported"
    elif any(item.get("reason") in {"missing_required", "invalid_value"} for item in reasons):
        outcome = "failed"
    elif output.needs_clarification or output.intent == "clarify":
        outcome = "clarification"
    else:
        outcome = "no_effect"

    reply = output.reply.strip()
    if outcome == "applied":
        return outcome, reply
    if outcome == "clarification":
        if reply and not _claims_success(reply):
            return outcome, reply
        return outcome, "I need one detail before changing the draft."
    if outcome == "stale":
        return outcome, "That edit is based on an older draft. Refresh the editor and try again."
    if outcome == "unsupported":
        detail = next((item.get("detail") for item in reasons if item.get("detail")), None)
        return outcome, detail or "That kind of edit isn't available for this draft yet."
    if outcome == "failed":
        return outcome, "I couldn't build a valid draft change for that request. Try again."
    if reply and not _claims_success(reply):
        return outcome, reply
    return outcome, "That change is already reflected in the draft."


def _snapshot_size_bytes(snapshot: dict) -> int:
    try:
        return len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="snapshot must be JSON-serializable",
        ) from exc


async def run_copilot_turn(
    body: CopilotTurnBody,
    *,
    job_id: uuid.UUID,
) -> CopilotTurnResponse:
    """Run one stateless edit-copilot turn.

    Zero writes to variant/job/item rows. The client snapshot is untrusted and is
    never written back to the variant, though it is included in agent_run.input_json
    like every Agent.run input. Returned ops are also untrusted; the editor's
    local applier and the existing Save/editor-commit path enforce again.
    """
    if _snapshot_size_bytes(body.snapshot) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="snapshot exceeds 20KB",
        )

    agent_input = EditCopilotInput(
        utterance=body.message[:500],
        prior_turns=body.turns[:12],
        variant_snapshot=body.snapshot,
    )

    try:
        output: EditCopilotOutput = await asyncio.to_thread(
            EditCopilotAgent(default_client()).run,
            agent_input,
            ctx=RunContext(job_id=str(job_id)),
        )
    except TerminalError as exc:
        log.warning("edit_copilot.agent_failed", job_id=str(job_id), error=str(exc)[:300])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edit_copilot_failed",
        ) from exc

    # Ops ride only genuine edit turns: a disobedient model returning
    # intent="reject"/"describe"/"clarify" WITH ops must not have them applied
    # while the reply text says nothing was done (adversarial review F5).
    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    outcome, reply = _honest_outcome(output, ops)
    return CopilotTurnResponse(
        intent=output.intent,
        ops=ops,
        confidence=output.confidence,
        reply=reply,
        suggestions=output.suggestions,
        needs_clarification=outcome == "clarification",
        outcome=outcome,
        rejection_reasons=output.rejection_reasons,
    )
