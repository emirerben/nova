"""Unit tests for the grader shadow-calibration gate (plan M1 / T2).

All offline — `run_shadow_calibration` takes an injected `grade_fn`, so the
confusion-matrix + gate logic is tested with deterministic stubs and zero
Gemini spend. The live sweep (scripts/run_grader_calibration.py) is the
human-verify step.

Covers (D3): shadow-calibration confusion-matrix gate (≥90% agreement with
near-zero false-pass/false-reject), ground-truth mapping, cost-cap halt.
"""

from __future__ import annotations

from app.services.grader_calibration import (
    MAX_FALSE_RATE,
    MIN_AGREEMENT_PCT,
    MIN_DECIDED_SAMPLES,
    CalibrationReport,
    GroundTruth,
    JobLabel,
    build_confusion_matrix,
    evaluate_gate,
    ground_truth_for,
    run_shadow_calibration,
)

# ── Ground-truth mapping ──────────────────────────────────────────────────────


def test_ground_truth_for_net_thumb() -> None:
    assert ground_truth_for(up=3, down=1) is GroundTruth.LIKED
    assert ground_truth_for(up=1, down=4) is GroundTruth.DISLIKED
    # Ties + no-signal are excluded.
    assert ground_truth_for(up=2, down=2) is None
    assert ground_truth_for(up=0, down=0) is None


# ── Confusion matrix bookkeeping ──────────────────────────────────────────────


def test_confusion_matrix_counts_each_cell() -> None:
    pairs = [
        (GroundTruth.LIKED, "auto_pass"),  # true_pass
        (GroundTruth.LIKED, "auto_reject"),  # false_reject
        (GroundTruth.DISLIKED, "auto_pass"),  # false_pass
        (GroundTruth.DISLIKED, "auto_reject"),  # true_reject
        (GroundTruth.LIKED, "escalate"),  # escalated
        (GroundTruth.DISLIKED, "escalate"),  # escalated
    ]
    m = build_confusion_matrix(pairs)
    cells = (m.true_pass, m.false_reject, m.false_pass, m.true_reject, m.escalated)
    assert cells == (1, 1, 1, 1, 2)
    assert m.decided == 4
    assert m.total == 6
    assert m.band_agreement_pct == 50.0  # 2 correct / 4 decided
    assert m.escalation_rate == round(2 / 6, 4) or abs(m.escalation_rate - 2 / 6) < 1e-9


def test_escalations_abstain_from_agreement() -> None:
    # All escalations → no decided jobs → agreement is 0 (and the gate blocks).
    pairs = [(GroundTruth.LIKED, "escalate")] * 30
    m = build_confusion_matrix(pairs)
    assert m.decided == 0
    assert m.band_agreement_pct == 0.0
    allowed, reasons = evaluate_gate(m)
    assert allowed is False


def test_unknown_band_treated_as_abstain() -> None:
    m = build_confusion_matrix([(GroundTruth.LIKED, "garbage_band")])
    assert m.escalated == 1
    assert m.decided == 0


# ── The gate ──────────────────────────────────────────────────────────────────


def _perfect_pairs(n: int) -> list[tuple[GroundTruth, str]]:
    """n/2 correct passes + n/2 correct rejects → 100% agreement, 0 false rates."""
    half = n // 2
    return [(GroundTruth.LIKED, "auto_pass")] * half + [(GroundTruth.DISLIKED, "auto_reject")] * (
        n - half
    )


def test_gate_allows_high_agreement_zero_errors() -> None:
    m = build_confusion_matrix(_perfect_pairs(40))
    allowed, reasons = evaluate_gate(m)
    assert allowed is True
    assert reasons == []
    assert m.band_agreement_pct == 100.0


def test_gate_blocks_below_agreement_threshold() -> None:
    # 80% agreement over 40 decided (below the 90% bar), but no false pass/reject:
    # 32 correct, 8 wrong split so neither error rate alone trips — agreement does.
    pairs = (
        [(GroundTruth.LIKED, "auto_pass")] * 16
        + [(GroundTruth.DISLIKED, "auto_reject")] * 16
        + [(GroundTruth.LIKED, "auto_reject")] * 4  # 4 false_reject
        + [(GroundTruth.DISLIKED, "auto_pass")] * 4  # 4 false_pass
    )
    m = build_confusion_matrix(pairs)
    assert m.band_agreement_pct == 80.0
    allowed, reasons = evaluate_gate(m)
    assert allowed is False
    assert any("agreement" in r for r in reasons)


def test_gate_blocks_on_false_pass_even_at_high_agreement() -> None:
    # 92% agreement (above the bar) but the misses are ALL false_pass (shipped
    # garbage) → false_pass_rate > 5% must block even though agreement passes.
    pairs = (
        [(GroundTruth.LIKED, "auto_pass")] * 46
        + [(GroundTruth.DISLIKED, "auto_reject")] * 46
        + [(GroundTruth.DISLIKED, "auto_pass")] * 8  # 8/100 = 8% false_pass
    )
    m = build_confusion_matrix(pairs)
    assert m.band_agreement_pct >= MIN_AGREEMENT_PCT
    assert m.false_pass_rate > MAX_FALSE_RATE
    allowed, reasons = evaluate_gate(m)
    assert allowed is False
    assert any("false_pass" in r for r in reasons)


def test_gate_blocks_on_false_reject_even_at_high_agreement() -> None:
    pairs = (
        [(GroundTruth.LIKED, "auto_pass")] * 46
        + [(GroundTruth.DISLIKED, "auto_reject")] * 46
        + [(GroundTruth.LIKED, "auto_reject")] * 8  # 8% false_reject
    )
    m = build_confusion_matrix(pairs)
    assert m.false_reject_rate > MAX_FALSE_RATE
    allowed, reasons = evaluate_gate(m)
    assert allowed is False
    assert any("false_reject" in r for r in reasons)


def test_gate_blocks_on_too_few_samples() -> None:
    # 100% agreement but only a handful of decided jobs → never certify.
    m = build_confusion_matrix(_perfect_pairs(MIN_DECIDED_SAMPLES - 2))
    allowed, reasons = evaluate_gate(m)
    assert allowed is False
    assert any("too few" in r for r in reasons)


# ── run_shadow_calibration: end-to-end with injected grade_fn ─────────────────


def _labels(n_liked: int, n_disliked: int) -> list[JobLabel]:
    labels = [
        JobLabel(job_id=f"like-{i}", truth=GroundTruth.LIKED, up=3, down=0) for i in range(n_liked)
    ]
    labels += [
        JobLabel(job_id=f"dis-{i}", truth=GroundTruth.DISLIKED, up=0, down=3)
        for i in range(n_disliked)
    ]
    return labels


def test_shadow_run_perfect_grader_allows_auto_action() -> None:
    labels = _labels(20, 20)

    def grade_fn(job_id: str) -> tuple[str, float]:
        # A perfect oracle: pass the liked ones, reject the disliked ones.
        band = "auto_pass" if job_id.startswith("like-") else "auto_reject"
        return band, 0.001

    report = run_shadow_calibration(labels=labels, grade_fn=grade_fn, cost_cap_usd=20.0)
    assert isinstance(report, CalibrationReport)
    assert report.auto_action_allowed is True
    assert report.graded_jobs == 40
    assert report.matrix.band_agreement_pct == 100.0
    assert report.skipped_jobs == 0


def test_shadow_run_blocks_a_biased_grader() -> None:
    labels = _labels(20, 20)

    def grade_fn(job_id: str) -> tuple[str, float]:  # noqa: ARG001
        # Always passes → every disliked job is a false_pass.
        return "auto_pass", 0.001

    report = run_shadow_calibration(labels=labels, grade_fn=grade_fn, cost_cap_usd=20.0)
    assert report.auto_action_allowed is False
    assert report.matrix.false_pass == 20


def test_shadow_run_skips_grade_errors_without_aborting() -> None:
    labels = _labels(2, 2)

    def grade_fn(job_id: str) -> tuple[str, float]:
        if job_id == "dis-1":
            raise RuntimeError("render gone / 404")
        return ("auto_pass" if job_id.startswith("like-") else "auto_reject"), 0.001

    report = run_shadow_calibration(labels=labels, grade_fn=grade_fn, cost_cap_usd=20.0)
    assert report.skipped_jobs == 1
    assert report.graded_jobs == 3  # the other 3 still graded


def test_shadow_run_cost_cap_halts_sweep() -> None:
    labels = _labels(50, 50)

    def grade_fn(job_id: str) -> tuple[str, float]:
        # Each grade "costs" $1 — cap at $5 halts after ~5 grades.
        band = "auto_pass" if job_id.startswith("like-") else "auto_reject"
        return band, 1.0

    report = run_shadow_calibration(labels=labels, grade_fn=grade_fn, cost_cap_usd=5.0)
    assert report.cost_capped is True
    assert report.graded_jobs <= 6  # halts at/just after the cap
    # Cost-truncated below the sample floor → never certifies auto-action.
    assert report.auto_action_allowed is False
    assert any("cost-capped" in r for r in report.gate_reasons)
