"""Celery task that grades a Job's final rendered video and persists the verdict.

The output gate of the autonomous dev loop (plan M1 / T1). Clones the
non-blocking pattern of `app/tasks/online_eval.py`: enqueued (fire-and-forget)
when a Job hits a terminal render state, never raises into the producer, and
swallows every failure so a misbehaving judge can't backpressure the render
path.

Flow:
  1. Load the Job + its primary rendered `JobClip.video_path`.
  2. Cost-cap guard — halt before spending if today's grader Gemini spend is at
     the ceiling (reuses the `LIVE_COST_CAP_USD` discipline from the eval suite).
  3. Download the MP4, run `VideoQualityGrader` (Gemini video judge).
  4. Persist the verdict as an `AgentRun` row (agent_name="nova.final_video_grader",
     cost_usd, tokens) — NO new verdict table. AgentRun is the calibration
     dataset AND gives the grade free `/admin/jobs` visibility.

v1 scope (CEO D5): dev-loop test renders only — this is wired to fire from the
builder loop's renders, NOT every production user job. Prod-wide grading is
deferred (unbounded cost).
"""

from __future__ import annotations

import datetime
import os
import tempfile
from pathlib import Path

import structlog

from app.worker import celery_app

log = structlog.get_logger()

# agent_name for the persisted AgentRun rows — the SoT key for both the
# calibration dataset and the admin job-debug filter.
GRADER_AGENT_NAME = "nova.final_video_grader"
GRADER_PROMPT_VERSION = "2026-06-02"

# Gemini 2.5 Flash pricing (matches the house constants on every agent spec:
# input ~$0.075/M, output ~$0.30/M). Used to compute the per-grade cost_usd we
# persist + sum for the daily cap.
COST_PER_1K_INPUT_USD = 0.000075
COST_PER_1K_OUTPUT_USD = 0.0003

# Daily grader-spend ceiling. Mirrors `tests/evals/conftest.LIVE_COST_CAP_USD`
# discipline: a runaway loop hits its OWN cap and stops grading rather than
# 429-ing the live product (plan D4). Override per-deploy via env.
GRADER_DAILY_COST_CAP_USD = float(os.environ.get("GRADER_DAILY_COST_CAP_USD", "5.0"))

# Rubric the grader judges against — sibling to the 20 agent-eval rubrics.
# __file__-relative resolution mirrors `_online_eval._RUBRICS_ROOT`: from
# /app/app/tasks/grade_final_video.py this is /app/tests/evals/rubrics, which
# the prod Dockerfile copies (and .dockerignore negation-allows) for the
# online judge — so final_video.md ships to the worker automatically.
RUBRIC_PATH = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "evals" / "rubrics" / "final_video.md"
)


def _estimate_cost_usd(tokens_in: int, tokens_out: int) -> float:
    return (tokens_in / 1000.0) * COST_PER_1K_INPUT_USD + (
        tokens_out / 1000.0
    ) * COST_PER_1K_OUTPUT_USD


def _grader_spend_today(session) -> float:  # noqa: ANN001 — sync sqlalchemy session
    """Sum cost_usd of today's grader AgentRun rows (UTC day boundary).

    Bounded: filtered to one agent_name + a 1-day window, both indexed
    (`idx_agent_run_agent_name`). Returns 0.0 on any query error so a DB hiccup
    fails OPEN to "spend allowed" — the per-grade cost is tiny and the
    fire-and-forget task already swallows failures; blocking grading on a flaky
    count would be worse than a rare cap overshoot.
    """
    from sqlalchemy import func as safunc  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import AgentRun  # noqa: PLC0415

    start = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        total = session.execute(
            select(safunc.coalesce(safunc.sum(AgentRun.cost_usd), 0)).where(
                AgentRun.agent_name == GRADER_AGENT_NAME,
                AgentRun.created_at >= start,
            )
        ).scalar_one()
        return float(total or 0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("grader_spend_query_failed", error=str(exc))
        return 0.0


def _primary_clip_video_path(session, job_id: str) -> str | None:  # noqa: ANN001
    """The GCS path of the job's best rendered clip (lowest rank = top result)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import JobClip  # noqa: PLC0415

    row = session.execute(
        select(JobClip.video_path)
        .where(
            JobClip.job_id == job_id,
            JobClip.video_path.isnot(None),
            JobClip.render_status == "ready",
        )
        .order_by(JobClip.rank.asc())
        .limit(1)
    ).first()
    return row[0] if row else None


@celery_app.task(
    name="tasks.grade_final_video",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    # Bound the worker: a slow Gemini upload+invoke can't tie up a slot forever.
    soft_time_limit=240,
    time_limit=300,
)
def grade_final_video(self, *, job_id: str) -> None:
    """Grade one Job's final render and persist the verdict as an AgentRun.

    Never raises — every failure path logs and returns. Fire-and-forget:
    nothing observes the return value except the worker logs and the persisted
    AgentRun row.
    """
    try:
        _run_grade(job_id=job_id)
    except Exception as exc:  # noqa: BLE001 — best-effort: log + drop, never requeue forever
        log.warning(
            "grade_final_video_failed",
            job_id=job_id,
            error=str(exc),
            attempt=self.request.retries,
        )


def _run_grade(*, job_id: str) -> None:
    """The grading body. Separated so tests drive it without the Celery wrapper."""
    from app.agents._persistence import persist_agent_run  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.services.video_grader import (  # noqa: PLC0415
        DEFAULT_VIDEO_MODEL,
        VideoGraderError,
        VideoQualityGrader,
    )
    from app.storage import download_to_file  # noqa: PLC0415

    session = sync_session()
    try:
        # 1. Cost-cap guard — halt BEFORE spending if today's budget is gone.
        spent = _grader_spend_today(session)
        if spent >= GRADER_DAILY_COST_CAP_USD:
            log.warning(
                "grade_final_video_cost_cap_halt",
                job_id=job_id,
                spent_usd=round(spent, 4),
                cap_usd=GRADER_DAILY_COST_CAP_USD,
            )
            return

        # 2. Resolve the rendered MP4 path.
        video_gcs_path = _primary_clip_video_path(session, job_id)
        if not video_gcs_path:
            log.warning("grade_final_video_no_render", job_id=job_id)
            return
    finally:
        session.close()

    grader = VideoQualityGrader(RUBRIC_PATH, model=DEFAULT_VIDEO_MODEL)

    with tempfile.TemporaryDirectory(prefix="grade-") as tmpdir:
        local_path = str(Path(tmpdir) / "final.mp4")
        try:
            download_to_file(video_gcs_path, local_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("grade_final_video_download_failed", job_id=job_id, error=str(exc))
            return

        try:
            verdict = grader.grade(local_path)
        except VideoGraderError as exc:
            # Infrastructure failure (Gemini timeout, malformed/empty JSON).
            # Persist a `failed` AgentRun so the broken grade is VISIBLE in
            # /admin/jobs — it must not silently look like an auto_reject.
            persist_agent_run(
                job_id=job_id,
                segment_idx=None,
                agent_name=GRADER_AGENT_NAME,
                prompt_version=GRADER_PROMPT_VERSION,
                model=DEFAULT_VIDEO_MODEL,
                outcome="failed",
                attempts=1,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_ms=0,
                input_dict={"video_path": video_gcs_path, "rubric": RUBRIC_PATH.name},
                output_dict=None,
                raw_text=None,
                error=str(exc),
            )
            log.warning("grade_final_video_grader_error", job_id=job_id, error=str(exc))
            return

    cost_usd = _estimate_cost_usd(verdict.tokens_in, verdict.tokens_out)

    # 3. Persist the verdict as an AgentRun row — the calibration dataset +
    #    free /admin/jobs visibility. No new table for the verdict.
    persist_agent_run(
        job_id=job_id,
        segment_idx=None,
        agent_name=GRADER_AGENT_NAME,
        prompt_version=GRADER_PROMPT_VERSION,
        model=DEFAULT_VIDEO_MODEL,
        outcome="ok",
        attempts=1,
        tokens_in=verdict.tokens_in,
        tokens_out=verdict.tokens_out,
        cost_usd=cost_usd,
        latency_ms=0,
        input_dict={"video_path": video_gcs_path, "rubric": RUBRIC_PATH.name},
        output_dict={
            "band": verdict.band.value,
            "scores": verdict.scores,
            "avg": round(verdict.avg, 4),
            "confidence": round(verdict.confidence, 4),
            "threshold": verdict.threshold,
            "risk_tag": verdict.risk_tag,
            "reasoning": verdict.reasoning,
            "summary_line": verdict.summary_line,
        },
        raw_text=verdict.raw_response,
        error=None,
    )
    log.info(
        "grade_final_video_done",
        job_id=job_id,
        band=verdict.band.value,
        avg=round(verdict.avg, 2),
        confidence=round(verdict.confidence, 2),
        cost_usd=round(cost_usd, 6),
    )
