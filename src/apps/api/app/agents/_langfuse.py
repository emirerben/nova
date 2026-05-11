"""Optional Langfuse tracing for Agent.run() observability.

Lazy-init, fail-open: if `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` env vars
aren't set OR the `langfuse` SDK isn't installed, every call here is a no-op.
Never blocks or breaks an agent run — Langfuse is purely a quality-of-life
observability layer on top of the existing structlog `agent_run` event.

Install:    pip install -e ".[observability]"   (or: pip install langfuse)
Configure:  set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST as Fly secrets.

What gets traced (one trace per Agent.run()):
  - trace name = agent.spec.name (e.g. "nova.compose.template_recipe")
  - session_id = ctx.job_id (so all agents called for one Job cluster in the UI)
  - input  = validated_input.model_dump()
  - output = parsed Output.model_dump()  (None on failure)
  - tags   = [outcome, agent_name]
  - generation child span includes tokens_in/out, cost_usd, latency_ms, prompt_version
"""

from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger()

_client: Any = None
_init_attempted = False


def _get_client() -> Any:
    """Return a Langfuse client if env + SDK are both available, else None."""
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse  # noqa: PLC0415

        _client = Langfuse()
        log.info("langfuse_init", host=os.environ.get("LANGFUSE_HOST", "cloud.langfuse.com"))
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse_init_failed", error=str(exc))
        _client = None
    return _client


def trace_agent_run(
    *,
    agent_name: str,
    prompt_version: str,
    model: str,
    outcome: str,
    input_dict: dict | None = None,
    output_dict: dict | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    attempts: int = 0,
    fallback_used: bool = False,
    job_id: str | None = None,
    segment_idx: int | None = None,
    request_id: str | None = None,
    error: str | None = None,
    source: str = "prod",
    extra_tags: list[str] | None = None,
) -> str | None:
    """Post a Langfuse trace for one Agent.run() invocation.

    Returns the trace_id if a trace was posted, else None. The trace_id can be
    used by `score_trace()` to attach evaluation scores asynchronously (e.g.
    from an online-eval Celery task).

    Fails open: any exception is swallowed and logged. Never blocks Agent.run().
    """
    client = _get_client()
    if client is None:
        return None
    try:
        tags = [outcome, agent_name, f"source:{source}"]
        if extra_tags:
            tags.extend(extra_tags)
        trace = client.trace(
            name=agent_name,
            input=input_dict,
            output=output_dict,
            metadata={
                "prompt_version": prompt_version,
                "outcome": outcome,
                "segment_idx": segment_idx,
                "request_id": request_id,
                "attempts": attempts,
                "fallback_used": fallback_used,
                "source": source,
            },
            tags=tags,
            session_id=job_id,
        )
        trace.generation(
            name=f"{agent_name}/{model}",
            model=model,
            input=input_dict,
            output=output_dict,
            usage={"input": tokens_in, "output": tokens_out, "unit": "TOKENS"},
            metadata={
                "prompt_version": prompt_version,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
            },
            level="ERROR" if error else "DEFAULT",
            status_message=error,
        )
        return getattr(trace, "id", None)
    except Exception as exc:  # noqa: BLE001
        # Tracing must never break agent work. Log + move on.
        log.debug("langfuse_trace_failed", agent=agent_name, error=str(exc))
        return None


def score_trace(
    trace_id: str,
    *,
    name: str,
    value: float,
    comment: str | None = None,
    data_type: str = "NUMERIC",
) -> None:
    """Attach a score to an existing Langfuse trace.

    Used by the eval harness (post structural + judge scores) and by the
    online-eval Celery task (post judge scores against sampled prod traces).
    Fails open: bad trace_ids, network errors, missing client → swallowed.
    """
    client = _get_client()
    if client is None or not trace_id:
        return
    try:
        client.score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment,
            data_type=data_type,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("langfuse_score_failed", trace_id=trace_id, name=name, error=str(exc))
