"""Best-effort persistence of agent_run rows for the admin debug view.

Called from ``Agent._log_outcome`` after every agent invocation. All
errors are swallowed: a DB hiccup must never break an in-flight job.
The structlog ``agent_run`` event remains the source of truth for
observability; this layer adds queryable per-job rows on top.

When ``job_id`` is missing or not a UUID (e.g. eval harness, track-level
analysis using ``"track:<id>"``), the row is dropped silently.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

log = structlog.get_logger()

# 100KB soft cap. Real-world agent responses run 2-20KB; the cap is a
# safety net for runaway models, not a routine truncation.
_RAW_TEXT_MAX = 100_000


def _parse_job_uuid(job_id: str | None) -> uuid.UUID | None:
    if not job_id:
        return None
    try:
        return uuid.UUID(job_id)
    except (ValueError, AttributeError):
        # "track:<id>" prefix (music track analysis) and other non-job
        # contexts fall through here. No persistence.
        return None


def _truncate_raw_text(raw_text: str | None) -> str | None:
    if raw_text is None:
        return None
    if len(raw_text) <= _RAW_TEXT_MAX:
        return raw_text
    return raw_text[:_RAW_TEXT_MAX] + f"\n...[truncated {len(raw_text) - _RAW_TEXT_MAX} chars]"


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        # Non-serializable input shouldn't break persistence; record the
        # error in place of the payload so the debug view still gets a row.
        return json.dumps({"__serialize_error__": str(exc), "__repr__": repr(value)[:1000]})


def persist_agent_run(
    *,
    job_id: str | None,
    segment_idx: int | None,
    agent_name: str,
    prompt_version: str,
    model: str,
    outcome: str,
    attempts: int,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    input_dict: dict[str, Any] | None,
    output_dict: dict[str, Any] | None,
    raw_text: str | None,
    error: str | None = None,
) -> None:
    """Insert one agent_run row. Swallows all errors. Uses the sync engine
    directly to stay decoupled from any caller-owned session/transaction.
    """
    job_uuid = _parse_job_uuid(job_id)
    if job_uuid is None:
        return

    try:
        # Lazy import: keeps agent import path light (eval harness, tests).
        from sqlalchemy import text  # noqa: PLC0415

        from app.database import sync_engine  # noqa: PLC0415

        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_run (
                        job_id, segment_idx, agent_name, prompt_version, model,
                        input_json, raw_text, output_json, outcome, attempts,
                        tokens_in, tokens_out, cost_usd, latency_ms, error_message
                    ) VALUES (
                        :job_id, :segment_idx, :agent_name, :prompt_version, :model,
                        CAST(:input_json AS JSONB), :raw_text,
                        CAST(:output_json AS JSONB), :outcome, :attempts,
                        :tokens_in, :tokens_out, :cost_usd, :latency_ms, :error_message
                    )
                    """
                ),
                {
                    "job_id": str(job_uuid),
                    "segment_idx": segment_idx,
                    "agent_name": agent_name,
                    "prompt_version": prompt_version,
                    "model": model,
                    "input_json": _json_dumps(input_dict),
                    "raw_text": _truncate_raw_text(raw_text),
                    "output_json": _json_dumps(output_dict),
                    "outcome": outcome,
                    "attempts": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": round(cost_usd, 6),
                    "latency_ms": latency_ms,
                    "error_message": error,
                },
            )
    except Exception as exc:  # noqa: BLE001 — never break an agent call
        log.warning(
            "agent_run_persist_failed",
            agent=agent_name,
            job_id=job_id,
            error=str(exc),
        )
