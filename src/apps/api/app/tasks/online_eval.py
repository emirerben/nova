"""Celery task that runs the LLM judge against a sampled prod Agent.run()
trace and posts per-dimension scores back to Langfuse.

Scheduled from `app/agents/_runtime.py._log_outcome` via
`app/agents/_online_eval.maybe_schedule_judge`. See OBSERVABILITY.md.

Designed to be lightweight (Anthropic HTTP call + Langfuse HTTP call only —
no FFmpeg, no GCS, no DB). Never blocks the producer; failures are logged
and dropped so a misbehaving judge can't backpressure the request path.
"""

from __future__ import annotations

import structlog

from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(
    name="tasks.score_trace_async",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    # Bound the worker so a slow Anthropic call can't tie up a slot indefinitely.
    soft_time_limit=120,
    time_limit=180,
)
def score_trace_async(
    self,
    *,
    trace_id: str,
    agent_name: str,
    input_dict: dict,
    output_dict: dict,
) -> None:
    """Run the LLM judge for one prod trace and post scores back to Langfuse.

    Never raises — every failure path logs and returns. The producer is
    fire-and-forget; nothing observes the return value or exception state of
    this task except the Celery worker logs.
    """
    try:
        from app.agents._online_eval import run_judge_and_score  # noqa: PLC0415

        run_judge_and_score(
            trace_id=trace_id,
            agent_name=agent_name,
            input_dict=input_dict,
            output_dict=output_dict,
        )
    except Exception as exc:  # noqa: BLE001
        # Online eval is best-effort. Don't requeue forever — log and drop.
        log.warning(
            "online_eval_task_failed",
            agent=agent_name,
            trace_id=trace_id,
            error=str(exc),
            attempt=self.request.retries,
        )
