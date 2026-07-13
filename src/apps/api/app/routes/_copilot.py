"""Shared edit-copilot turn helper.

The plan-items route mounts v1. A future generative-jobs mirror can reuse this
module after it supplies its own ownership/variant guard.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput, EditCopilotOutput

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
    return CopilotTurnResponse(
        intent=output.intent,
        ops=ops,
        confidence=output.confidence,
        reply=output.reply,
        suggestions=output.suggestions,
        needs_clarification=output.needs_clarification,
    )
