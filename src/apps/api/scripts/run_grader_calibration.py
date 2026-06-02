"""Live shadow-calibration run for the final-video grader (plan M1 / T2).

Pulls back-catalog Jobs that carry 👍/👎 `VideoFeedback`, runs the Gemini video
grader against each in SHADOW (no auto-action), treats the thumbs as ground
truth, and prints a confusion matrix + band-agreement % + the auto-action gate
decision (≥90% agreement, near-zero false-pass/false-reject).

Usage (from src/apps/api/, with DATABASE_URL + GEMINI_API_KEY set):

    python -m scripts.run_grader_calibration [--cost-cap-usd 20] [--limit N] [--dry-run]

`--dry-run` collects + reports the labeled-job set WITHOUT any Gemini spend (no
grading) — use it to confirm the back-catalog has enough 👍/👎 signal before
paying for a live sweep. The live sweep needs a real Postgres + GEMINI_API_KEY
and bills Gemini, so it is the human-verify step, not something CI runs.

The cost cap reuses the eval suite's `LIVE_COST_CAP_USD` discipline (default
$20) — the sweep halts once cumulative Gemini spend would exceed it, and a
cost-truncated run that hasn't cleared the sample floor never certifies
auto-action.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


def _build_grade_fn():
    """Closure: job_id → (band_string, grade_cost_usd). Downloads + grades live."""
    from app.database import sync_session
    from app.services.video_grader import (
        DEFAULT_VIDEO_MODEL,
        VideoQualityGrader,
    )
    from app.storage import download_to_file
    from app.tasks.grade_final_video import (
        RUBRIC_PATH,
        _estimate_cost_usd,
        _primary_clip_video_path,
    )

    grader = VideoQualityGrader(RUBRIC_PATH, model=DEFAULT_VIDEO_MODEL)

    def grade_fn(job_id: str) -> tuple[str, float]:
        session = sync_session()
        try:
            video_gcs_path = _primary_clip_video_path(session, job_id)
        finally:
            session.close()
        if not video_gcs_path:
            raise RuntimeError(f"job {job_id} has no ready render")
        with tempfile.TemporaryDirectory(prefix="calib-") as tmpdir:
            local = str(Path(tmpdir) / "final.mp4")
            download_to_file(video_gcs_path, local)
            verdict = grader.grade(local)
        cost = _estimate_cost_usd(verdict.tokens_in, verdict.tokens_out)
        return verdict.band.value, cost

    return grade_fn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shadow-calibrate the final-video grader.")
    parser.add_argument(
        "--cost-cap-usd",
        type=float,
        default=None,
        help="Hard ceiling on Gemini spend (defaults to the eval suite LIVE_COST_CAP_USD).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of jobs graded.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect + report the labeled-job set without any Gemini spend.",
    )
    args = parser.parse_args(argv)

    # Reuse the eval suite's cost cap constant as the default ceiling.
    if args.cost_cap_usd is None:
        try:
            from tests.evals.conftest import LIVE_COST_CAP_USD

            args.cost_cap_usd = LIVE_COST_CAP_USD
        except Exception:  # noqa: BLE001 — conftest import is best-effort
            args.cost_cap_usd = 20.0

    from app.database import sync_session
    from app.services.grader_calibration import (
        collect_labeled_jobs,
        run_shadow_calibration,
    )

    session = sync_session()
    try:
        labels = collect_labeled_jobs(session)
    finally:
        session.close()

    if args.limit is not None:
        labels = labels[: args.limit]

    liked = sum(1 for label in labels if label.truth.value == "liked")
    disliked = len(labels) - liked
    print(f"Collected {len(labels)} labeled jobs (liked={liked}, disliked={disliked}).")

    if args.dry_run:
        print("[dry-run] skipping grading — no Gemini spend.")
        for label in labels:
            print(f"  {label.job_id}  truth={label.truth.value}  up={label.up} down={label.down}")
        return 0

    if not labels:
        print("No gradable 👍/👎 back-catalog jobs — nothing to calibrate.")
        return 1

    report = run_shadow_calibration(
        labels=labels,
        grade_fn=_build_grade_fn(),
        cost_cap_usd=args.cost_cap_usd,
    )

    print("\nConfusion matrix (rows=truth, cols=grader prediction):")
    m = report.matrix
    print(f"  LIKED    -> pass={m.true_pass:3d}  reject={m.false_reject:3d} (false_reject)")
    print(f"  DISLIKED -> pass={m.false_pass:3d} (false_pass)  reject={m.true_reject:3d}")
    print(f"  escalated (abstained) = {m.escalated}")
    print("\n" + report.summary())

    # Exit non-zero when auto-action is NOT yet certified, so a CI/cron caller
    # can gate on the result.
    return 0 if report.auto_action_allowed else 2


if __name__ == "__main__":
    sys.exit(main())
