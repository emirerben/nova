"""Shadow calibration for the final-video grader (plan M1 / T2).

The grader is ADVISORY-ONLY until it clears a ~90%-agreement bar against the
founder's historical taste. This module runs the grader in SHADOW over the
back-catalog of Jobs that already carry a `VideoFeedback` 👍/👎, treats those
thumbs as ground truth, and emits a confusion matrix + band-agreement % that
GATES auto-action.

No auto-action ever happens here — this is pure measurement. The output
`CalibrationReport.auto_action_allowed` is the boolean a later milestone (M2)
reads before flipping the grader from advisory to acting.

Reuses:
  - the `VideoFeedback` query shape from `services/feedback_summary.py`
    (user-scoped 👍/👎 rollup) — here aggregated per-job into a ground-truth label.
  - the `LIVE_COST_CAP_USD` cost discipline from `tests/evals/conftest.py`
    (a hard ceiling on the shadow run's Gemini spend — surfaced as a kwarg the
    live calibration script passes the conftest constant into).

Ground-truth mapping (👍/👎 only — the gradable signals):
  - a job whose net feedback is positive (more 👍 than 👎) → ground truth = LIKED
  - a job whose net feedback is negative (more 👎 than 👍) → ground truth = DISLIKED
  - ties / note-only / more_like_this-only jobs are EXCLUDED (no clean signal).

Band → predicted-label mapping (how the grader's 3-band verdict is scored
against a binary thumb):
  - auto_pass   → predicts LIKED
  - auto_reject → predicts DISLIKED
  - escalate    → ABSTAINS (no prediction; counts toward escalation rate, NOT
    toward agreement/error — the whole point of the band is to defer the
    ambiguous ones to a human).

The gate (all must hold):
  - band_agreement_pct ≥ MIN_AGREEMENT_PCT over the DECIDED (non-escalate) jobs,
  - false_pass_rate ≤ MAX_FALSE_RATE  (auto_pass on a DISLIKED job — ships garbage),
  - false_reject_rate ≤ MAX_FALSE_RATE (auto_reject on a LIKED job — kills a good one),
  - at least MIN_DECIDED_SAMPLES decided jobs (don't certify on a handful).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

log = structlog.get_logger()

# The plan's ≥90% bar, with near-zero error tolerances.
MIN_AGREEMENT_PCT = 90.0
MAX_FALSE_RATE = 0.05  # ≤5% false-pass and false-reject each
MIN_DECIDED_SAMPLES = 20  # don't certify auto-action on a tiny sample


class GroundTruth(StrEnum):
    LIKED = "liked"
    DISLIKED = "disliked"


@dataclass
class JobLabel:
    """One back-catalog job's ground-truth thumb + (after grading) its band."""

    job_id: str
    truth: GroundTruth
    up: int
    down: int


@dataclass
class ConfusionMatrix:
    """2x2 over DECIDED jobs + an abstain count for escalations.

    Rows = ground truth, columns = grader prediction.
        true_pass     : LIKED    + auto_pass   (correct ship)
        false_reject  : LIKED    + auto_reject (killed a good video — the costly error)
        false_pass    : DISLIKED + auto_pass   (shipped garbage — the costly error)
        true_reject   : DISLIKED + auto_reject (correct kill)
        escalated     : either truth + escalate (abstained — deferred to human)
    """

    true_pass: int = 0
    false_reject: int = 0
    false_pass: int = 0
    true_reject: int = 0
    escalated: int = 0

    @property
    def decided(self) -> int:
        """Jobs the grader took a stance on (auto_pass | auto_reject)."""
        return self.true_pass + self.false_reject + self.false_pass + self.true_reject

    @property
    def total(self) -> int:
        return self.decided + self.escalated

    @property
    def correct(self) -> int:
        return self.true_pass + self.true_reject

    @property
    def band_agreement_pct(self) -> float:
        """Agreement over DECIDED jobs only (escalations abstain, by design)."""
        if self.decided == 0:
            return 0.0
        return 100.0 * self.correct / self.decided

    @property
    def false_pass_rate(self) -> float:
        if self.decided == 0:
            return 0.0
        return self.false_pass / self.decided

    @property
    def false_reject_rate(self) -> float:
        if self.decided == 0:
            return 0.0
        return self.false_reject / self.decided

    @property
    def escalation_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.escalated / self.total

    def as_dict(self) -> dict[str, float | int]:
        return {
            "true_pass": self.true_pass,
            "false_reject": self.false_reject,
            "false_pass": self.false_pass,
            "true_reject": self.true_reject,
            "escalated": self.escalated,
            "decided": self.decided,
            "total": self.total,
            "band_agreement_pct": round(self.band_agreement_pct, 2),
            "false_pass_rate": round(self.false_pass_rate, 4),
            "false_reject_rate": round(self.false_reject_rate, 4),
            "escalation_rate": round(self.escalation_rate, 4),
        }


@dataclass
class CalibrationReport:
    matrix: ConfusionMatrix
    auto_action_allowed: bool
    gate_reasons: list[str] = field(default_factory=list)
    graded_jobs: int = 0
    skipped_jobs: int = 0
    spent_usd: float = 0.0
    cost_capped: bool = False

    def summary(self) -> str:
        m = self.matrix
        verdict = "ALLOWED" if self.auto_action_allowed else "BLOCKED"
        return (
            f"auto_action={verdict} | agreement={m.band_agreement_pct:.1f}% "
            f"(decided={m.decided}) false_pass={m.false_pass_rate:.3f} "
            f"false_reject={m.false_reject_rate:.3f} escalation={m.escalation_rate:.3f} "
            f"| graded={self.graded_jobs} skipped={self.skipped_jobs} "
            f"spent=${self.spent_usd:.4f}"
            + (" [COST-CAPPED]" if self.cost_capped else "")
            + ("" if self.auto_action_allowed else f" | blocked_by={self.gate_reasons}")
        )


def ground_truth_for(up: int, down: int) -> GroundTruth | None:
    """Net-thumb → ground truth. Ties / no thumbs → None (excluded)."""
    if up > down:
        return GroundTruth.LIKED
    if down > up:
        return GroundTruth.DISLIKED
    return None


def evaluate_gate(matrix: ConfusionMatrix) -> tuple[bool, list[str]]:
    """Apply the ≥90%-agreement + near-zero-error gate. Returns (allowed, reasons)."""
    reasons: list[str] = []
    if matrix.decided < MIN_DECIDED_SAMPLES:
        reasons.append(f"too few decided samples ({matrix.decided} < {MIN_DECIDED_SAMPLES})")
    if matrix.band_agreement_pct < MIN_AGREEMENT_PCT:
        reasons.append(f"agreement {matrix.band_agreement_pct:.1f}% < {MIN_AGREEMENT_PCT}%")
    if matrix.false_pass_rate > MAX_FALSE_RATE:
        reasons.append(f"false_pass_rate {matrix.false_pass_rate:.3f} > {MAX_FALSE_RATE}")
    if matrix.false_reject_rate > MAX_FALSE_RATE:
        reasons.append(f"false_reject_rate {matrix.false_reject_rate:.3f} > {MAX_FALSE_RATE}")
    return (len(reasons) == 0, reasons)


def build_confusion_matrix(
    pairs: list[tuple[GroundTruth, str]],
) -> ConfusionMatrix:
    """Fold (ground_truth, predicted_band) pairs into a confusion matrix.

    `predicted_band` is the GradeBand string value: "auto_pass" | "auto_reject"
    | "escalate". Pure function → the determinism + gate tests drive it with
    synthetic pairs, no Gemini call.
    """
    m = ConfusionMatrix()
    for truth, band in pairs:
        if band == "escalate":
            m.escalated += 1
        elif band == "auto_pass":
            if truth is GroundTruth.LIKED:
                m.true_pass += 1
            else:
                m.false_pass += 1
        elif band == "auto_reject":
            if truth is GroundTruth.DISLIKED:
                m.true_reject += 1
            else:
                m.false_reject += 1
        else:  # unknown band → treat as an abstain rather than crash a long run
            m.escalated += 1
    return m


def collect_labeled_jobs(session) -> list[JobLabel]:  # noqa: ANN001 — sync sqlalchemy session
    """Pull back-catalog jobs that have gradable 👍/👎 VideoFeedback.

    Per-job aggregation of the up/down counts (the same `VideoFeedback` table +
    signal grouping `feedback_summary.rollup_user_feedback` uses, here grouped
    by job_id instead of user_id). Jobs with no net thumb (tie / note-only) are
    excluded so the ground truth is clean.
    """
    from sqlalchemy import (
        case,  # noqa: PLC0415
        select,  # noqa: PLC0415
    )
    from sqlalchemy import func as safunc  # noqa: PLC0415

    from app.models import VideoFeedback  # noqa: PLC0415

    rows = session.execute(
        select(
            VideoFeedback.job_id,
            safunc.sum(case((VideoFeedback.signal == "up", 1), else_=0)).label("up"),
            safunc.sum(case((VideoFeedback.signal == "down", 1), else_=0)).label("down"),
        )
        .where(VideoFeedback.job_id.isnot(None))
        .group_by(VideoFeedback.job_id)
    ).all()

    labels: list[JobLabel] = []
    for job_id, up, down in rows:
        up_i, down_i = int(up or 0), int(down or 0)
        truth = ground_truth_for(up_i, down_i)
        if truth is None:
            continue
        labels.append(JobLabel(job_id=str(job_id), truth=truth, up=up_i, down=down_i))
    return labels


def run_shadow_calibration(
    *,
    labels: list[JobLabel],
    grade_fn: Callable[[str], tuple[str, float]],
    cost_cap_usd: float,
) -> CalibrationReport:
    """Run the grader in shadow over `labels`, build the matrix, apply the gate.

    Args:
        labels: gradable back-catalog jobs (from `collect_labeled_jobs`).
        grade_fn: job_id → (band_string, cost_usd_for_this_grade). Injected so
            tests pass a deterministic stub and the live script passes a closure
            that downloads + runs `VideoQualityGrader`. A grade that raises is
            skipped (not fatal) — one broken render can't abort the whole sweep.
        cost_cap_usd: hard ceiling on cumulative Gemini spend (reuse
            `conftest.LIVE_COST_CAP_USD`). When cumulative spend would exceed
            the cap, the sweep HALTS and the report is marked `cost_capped` —
            no auto-action is certified on a partial, cost-truncated run unless
            it already cleared MIN_DECIDED_SAMPLES.

    NO auto-action is taken — this only measures + gates.
    """
    pairs: list[tuple[GroundTruth, str]] = []
    graded = 0
    skipped = 0
    spent = 0.0
    cost_capped = False

    for label in labels:
        if spent >= cost_cap_usd:
            cost_capped = True
            log.warning(
                "calibration_cost_cap_halt",
                spent_usd=round(spent, 4),
                cap_usd=cost_cap_usd,
                graded=graded,
            )
            break
        try:
            band, grade_cost = grade_fn(label.job_id)
        except Exception as exc:  # noqa: BLE001 — one bad render must not abort the sweep
            skipped += 1
            log.warning("calibration_grade_skipped", job_id=label.job_id, error=str(exc))
            continue
        spent += float(grade_cost or 0.0)
        pairs.append((label.truth, band))
        graded += 1

    matrix = build_confusion_matrix(pairs)
    allowed, reasons = evaluate_gate(matrix)
    if cost_capped and matrix.decided < MIN_DECIDED_SAMPLES:
        # A cost-truncated run with too little signal must never certify.
        allowed = False
        if "cost-capped before reaching sample floor" not in reasons:
            reasons.append("cost-capped before reaching sample floor")

    report = CalibrationReport(
        matrix=matrix,
        auto_action_allowed=allowed,
        gate_reasons=reasons,
        graded_jobs=graded,
        skipped_jobs=skipped,
        spent_usd=spent,
        cost_capped=cost_capped,
    )
    log.info("calibration_report", **report.matrix.as_dict(), allowed=allowed)
    return report
