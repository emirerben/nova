"""Best-effort persistence of agent_run rows for the admin debug view.

Called from ``Agent._log_outcome`` after every agent invocation. All
errors are swallowed: a DB hiccup must never break an in-flight job.
The structlog ``agent_run`` event remains the source of truth for
observability; this layer adds queryable per-job rows on top.

``RunContext.job_id`` may be a job UUID, ``"template:<uuid>"`` (template
analysis), or ``"track:<uuid>"`` (music-track analysis). Each prefix routes
to the appropriate FK column on ``agent_run``. Anything else (eval harness,
plain strings) is dropped silently.
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


_TEMPLATE_PREFIX = "template:"
_TRACK_PREFIX = "track:"


def _parse_owner(
    job_id: str | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    """Route a ``RunContext.job_id`` to ``(job_uuid, template_uuid, track_uuid)``.

    Exactly zero or one of the three values will be non-null; an all-None tuple
    means the row should be dropped (eval harness, malformed prefix).
    """
    if not job_id:
        return (None, None, None)

    if job_id.startswith(_TEMPLATE_PREFIX):
        try:
            return (None, uuid.UUID(job_id[len(_TEMPLATE_PREFIX) :]), None)
        except (ValueError, AttributeError):
            return (None, None, None)

    if job_id.startswith(_TRACK_PREFIX):
        try:
            return (None, None, uuid.UUID(job_id[len(_TRACK_PREFIX) :]))
        except (ValueError, AttributeError):
            return (None, None, None)

    try:
        return (uuid.UUID(job_id), None, None)
    except (ValueError, AttributeError):
        return (None, None, None)


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
    job_uuid, template_uuid, track_uuid = _parse_owner(job_id)
    if job_uuid is None and template_uuid is None and track_uuid is None:
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
                        job_id, template_id, music_track_id,
                        segment_idx, agent_name, prompt_version, model,
                        input_json, raw_text, output_json, outcome, attempts,
                        tokens_in, tokens_out, cost_usd, latency_ms, error_message
                    ) VALUES (
                        :job_id, :template_id, :music_track_id,
                        :segment_idx, :agent_name, :prompt_version, :model,
                        CAST(:input_json AS JSONB), :raw_text,
                        CAST(:output_json AS JSONB), :outcome, :attempts,
                        :tokens_in, :tokens_out, :cost_usd, :latency_ms, :error_message
                    )
                    """
                ),
                {
                    "job_id": str(job_uuid) if job_uuid else None,
                    "template_id": str(template_uuid) if template_uuid else None,
                    "music_track_id": str(track_uuid) if track_uuid else None,
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
