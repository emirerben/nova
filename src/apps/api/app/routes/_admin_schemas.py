"""Shared Pydantic types for admin debug routes.

Extracted so admin.py and admin_jobs.py can both surface agent_run payloads
without a cross-route import.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models import AgentRun


class AgentRunPayload(BaseModel):
    id: str
    segment_idx: int | None
    agent_name: str
    prompt_version: str
    model: str
    outcome: str
    attempts: int
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    latency_ms: int | None
    error_message: str | None
    input_json: Any
    output_json: Any
    raw_text: str | None
    created_at: datetime


def agent_run_to_payload(r: AgentRun) -> AgentRunPayload:
    return AgentRunPayload(
        id=str(r.id),
        segment_idx=r.segment_idx,
        agent_name=r.agent_name,
        prompt_version=r.prompt_version,
        model=r.model,
        outcome=r.outcome,
        attempts=r.attempts,
        tokens_in=r.tokens_in,
        tokens_out=r.tokens_out,
        cost_usd=float(r.cost_usd) if r.cost_usd is not None else None,
        latency_ms=r.latency_ms,
        error_message=r.error_message,
        input_json=r.input_json,
        output_json=r.output_json,
        raw_text=r.raw_text,
        created_at=r.created_at,
    )
