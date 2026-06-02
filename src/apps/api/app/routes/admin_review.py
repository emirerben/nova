"""Admin endpoints for the autonomous-dev-loop video-review surface (M1 / T6).

GET  /admin/review                 — list of grader escalations awaiting a human eye
POST /admin/review/{run_id}/label  — write a calibration label (the phone tap)

The read side polls the grader's persisted `AgentRun` rows
(`agent_name == "nova.final_video_grader"`) and surfaces ONLY the `escalate`
band — the ~10% the grader deferred to a human (plan M1). Each item carries
the rendered clip's thumbnail (signed for playback), per-dimension scores, the
one-line rationale, and the risk tag, so the founder can triage from the phone.

The write side is the calibration loop: a tap (`agree` / `disagree`, or an
explicit `auto_pass` / `auto_reject` verdict) is persisted as a SECOND
`AgentRun` row (`agent_name == "nova.final_video_grader.label"`) referencing the
same job. Like the grade itself, the label lives in the AgentRun table — no new
table — so it is part of the same calibration dataset the shadow runner
(`grader_calibration.py`) reads, and is visible in `/admin/jobs`.

Auth: X-Admin-Token header (same `_require_admin` gate as the rest of admin.*).

NOTE: handlers use the SYNC session (`sync_session`), matching
`admin_build_tasks.py` and `persist_agent_run`'s sync engine — the review
queries are small + indexed (`idx_agent_run_agent_name`) and FastAPI runs sync
defs in a threadpool, so there's no event-loop blocking at this volume.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.routes.admin import _require_admin

log = structlog.get_logger()

router = APIRouter()

# Keep these in lockstep with app/tasks/grade_final_video.py — the grader writes
# its verdict under GRADER_AGENT_NAME; the calibration label is a sibling row.
GRADER_AGENT_NAME = "nova.final_video_grader"
GRADER_LABEL_AGENT_NAME = "nova.final_video_grader.label"
GRADER_LABEL_PROMPT_VERSION = "2026-06-02"

# Only the escalate band reaches the phone (the grade bands are the SoT in
# app/services/video_grader.GradeBand).
ESCALATE_BAND = "escalate"

# Thumbnail signed-URL TTL. Short — the review surface re-fetches the list each
# poll, so a stale signature self-heals; a leaked URL is useless within the hour.
THUMBNAIL_URL_TTL_MIN = 60


# ── Schemas ────────────────────────────────────────────────────────────────────


class ReviewItem(BaseModel):
    """One grader escalation awaiting a human verdict (the phone card)."""

    run_id: str
    job_id: str | None
    band: str
    avg: float
    confidence: float
    risk_tag: str
    reasoning: str
    summary_line: str
    scores: dict[str, float] = Field(default_factory=dict)
    # Signed playback/thumbnail URLs for the rendered clip (None if not rendered
    # yet or the blob is gone). thumbnail_url is the still; video_url plays it.
    thumbnail_url: str | None = None
    video_url: str | None = None
    created_at: str | None = None
    # True once a calibration label exists for this job (so the UI can show
    # "labeled" without a second round-trip).
    labeled: bool = False


class ListReviewResponse(BaseModel):
    items: list[ReviewItem]
    total: int


class LabelRequest(BaseModel):
    """A calibration tap. `verdict` is the human's call on the escalated video.

    `agree`/`disagree` are relative to the grader's own (escalate) stance, but
    the explicit `auto_pass`/`auto_reject` are clearer for a human triaging on a
    phone, so both forms are accepted and normalized on write.
    """

    verdict: Literal["auto_pass", "auto_reject", "agree", "disagree"]
    note: str | None = None


class LabelResponse(BaseModel):
    run_id: str
    job_id: str | None
    verdict: str
    ok: bool = True


# ── Helpers ────────────────────────────────────────────────────────────────────


def _output(row: Any) -> dict[str, Any]:
    """The grader's persisted output_json (band/scores/...) as a plain dict."""
    out = getattr(row, "output_json", None)
    return out if isinstance(out, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sign_thumbnail(path: str | None) -> str | None:
    """Best-effort signed GET URL for a GCS thumbnail/video path.

    Never raises into the list response — a missing blob or storage hiccup
    yields None and the card renders without a still rather than 500-ing the
    whole queue.
    """
    if not path:
        return None
    try:
        from app.storage import signed_get_url  # noqa: PLC0415

        return signed_get_url(path, expiration_minutes=THUMBNAIL_URL_TTL_MIN)
    except Exception as exc:  # noqa: BLE001
        log.warning("review_thumbnail_sign_failed", path=path, error=str(exc))
        return None


def _clip_paths_for_job(db: Any, job_id: Any) -> tuple[str | None, str | None]:
    """(thumbnail_path, video_path) for the job's best rendered clip, or (None, None)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import JobClip  # noqa: PLC0415

    row = db.execute(
        select(JobClip.thumbnail_path, JobClip.video_path)
        .where(
            JobClip.job_id == job_id,
            JobClip.render_status == "ready",
        )
        .order_by(JobClip.rank.asc())
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


# ── Endpoints ────────────────────────────────────────────────────────────────────


@router.get("", response_model=ListReviewResponse, dependencies=[Depends(_require_admin)])
def list_review(
    limit: int = Query(50, ge=1, le=200),
) -> ListReviewResponse:
    """List grader escalations (newest first) awaiting a human verdict.

    Filters the grader's AgentRun rows to band == "escalate" and joins each to
    its job's rendered clip thumbnail. Already-labeled escalations stay in the
    list (flagged `labeled=True`) so a re-tap is possible and the calibration
    dataset stays append-only — the UI can hide them client-side if desired.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import sync_session  # noqa: PLC0415
    from app.models import AgentRun  # noqa: PLC0415

    with sync_session() as db:
        # Pull the most recent grader rows; band lives in output_json so we
        # filter in Python (the set is small + capped). Only "ok" grades carry
        # a verdict — a `failed` grade row has no band and is skipped.
        rows = (
            db.execute(
                select(AgentRun)
                .where(AgentRun.agent_name == GRADER_AGENT_NAME)
                .order_by(AgentRun.created_at.desc())
                .limit(limit * 4)  # over-fetch: most grades are auto_pass/reject, not escalate
            )
            .scalars()
            .all()
        )

        # Which jobs already have a calibration label (one query, not N).
        labeled_job_ids: set[str] = set()
        label_rows = db.execute(
            select(AgentRun.job_id)
            .where(AgentRun.agent_name == GRADER_LABEL_AGENT_NAME)
            .where(AgentRun.job_id.isnot(None))
        ).all()
        for (jid,) in label_rows:
            if jid is not None:
                labeled_job_ids.add(str(jid))

        items: list[ReviewItem] = []
        for row in rows:
            out = _output(row)
            if out.get("band") != ESCALATE_BAND:
                continue
            job_id = str(row.job_id) if row.job_id else None
            thumb_path, video_path = (None, None)
            if row.job_id is not None:
                thumb_path, video_path = _clip_paths_for_job(db, row.job_id)
            scores_raw = out.get("scores", {})
            scores = (
                {str(k): _safe_float(v) for k, v in scores_raw.items()}
                if isinstance(scores_raw, dict)
                else {}
            )
            items.append(
                ReviewItem(
                    run_id=str(row.id),
                    job_id=job_id,
                    band=str(out.get("band", ESCALATE_BAND)),
                    avg=_safe_float(out.get("avg")),
                    confidence=_safe_float(out.get("confidence")),
                    risk_tag=str(out.get("risk_tag", "")),
                    reasoning=str(out.get("reasoning", "")),
                    summary_line=str(out.get("summary_line", "")),
                    scores=scores,
                    thumbnail_url=_sign_thumbnail(thumb_path),
                    video_url=_sign_thumbnail(video_path),
                    created_at=row.created_at.isoformat() if row.created_at else None,
                    labeled=job_id in labeled_job_ids if job_id else False,
                )
            )
            if len(items) >= limit:
                break

        return ListReviewResponse(items=items, total=len(items))


@router.post(
    "/{run_id}/label",
    response_model=LabelResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
def label_review(run_id: str, req: LabelRequest) -> LabelResponse:
    """Persist a calibration label for one grader escalation (the phone tap).

    Resolves the escalated grade row, normalizes the human verdict to an
    auto_pass/auto_reject prediction, and writes a sibling AgentRun row
    (`GRADER_LABEL_AGENT_NAME`) referencing the same job. That row joins the
    grader's own rows in the calibration dataset (`grader_calibration.py`).
    """
    import uuid as _uuid  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from app.agents._persistence import persist_agent_run  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.models import AgentRun  # noqa: PLC0415

    try:
        run_uuid = _uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Grade run not found"
        ) from exc

    with sync_session() as db:
        grade = db.execute(
            select(AgentRun).where(
                AgentRun.id == run_uuid,
                AgentRun.agent_name == GRADER_AGENT_NAME,
            )
        ).scalar_one_or_none()
        if grade is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grade run not found")

        graded_band = _output(grade).get("band", ESCALATE_BAND)
        human_verdict = _normalize_verdict(req.verdict, graded_band)
        job_id = str(grade.job_id) if grade.job_id else None

    # persist_agent_run owns its own sync engine/txn — call it OUTSIDE the
    # session block (matching how the grader task persists). A non-UUID/None
    # job_id is silently dropped by persist_agent_run, but a grade always has a
    # job_id, so the label always anchors to a job.
    persist_agent_run(
        job_id=job_id,
        segment_idx=None,
        agent_name=GRADER_LABEL_AGENT_NAME,
        prompt_version=GRADER_LABEL_PROMPT_VERSION,
        model="human",
        outcome="ok",
        attempts=1,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=0,
        input_dict={
            "grade_run_id": run_id,
            "graded_band": graded_band,
            "raw_verdict": req.verdict,
        },
        output_dict={
            "verdict": human_verdict,
            "note": req.note,
        },
        raw_text=None,
        error=None,
    )
    log.info(
        "review_label_written",
        run_id=run_id,
        job_id=job_id,
        verdict=human_verdict,
    )
    return LabelResponse(run_id=run_id, job_id=job_id, verdict=human_verdict)


def _normalize_verdict(raw: str, graded_band: str) -> str:
    """Map the human tap to an explicit auto_pass/auto_reject prediction.

    `agree`/`disagree` are interpreted relative to the grader's stance. Since the
    surface only shows ESCALATE items (the grader abstained), `agree` on an
    escalate has no pass/reject meaning, so for the relative forms we anchor to
    the explicit ones the UI sends: the UI maps a 👍 tap → "auto_pass" and a 👎
    tap → "auto_reject" directly. The relative forms are accepted defensively
    and fall back to escalate when they can't be resolved to a side.
    """
    if raw in ("auto_pass", "auto_reject"):
        return raw
    # Relative to a non-escalate graded band (defensive — surface is escalate-only).
    if graded_band == "auto_pass":
        return "auto_pass" if raw == "agree" else "auto_reject"
    if graded_band == "auto_reject":
        return "auto_reject" if raw == "agree" else "auto_pass"
    # graded_band == escalate (the normal case): an agree/disagree on an abstain
    # carries no side, so record the human's deferral as escalate.
    return "escalate"
