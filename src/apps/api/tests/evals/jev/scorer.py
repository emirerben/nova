"""Pure threshold scoring for Jev predictions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from statistics import quantiles

from .models import Case, Dataset, Prediction


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def _matrix(
    truth: list[bool], predicted: list[bool], abstained: list[bool]
) -> dict[str, int | float | None]:
    tp = fp = fn = tn = 0
    for actual, guess, abstain in zip(truth, predicted, abstained, strict=True):
        if abstain:
            continue
        if actual and guess:
            tp += 1
        elif actual:
            fn += 1
        elif guess:
            fp += 1
        else:
            tn += 1
    total = len(truth)
    abstentions = sum(abstained)
    judged = total - abstentions
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _rate(tp, tp + fp),
        "recall": _rate(tp, tp + fn),
        "miss_rate": _rate(fn, tp + fn),
        "false_flag_rate": _rate(fp, fp + tn),
        "missed_violation_rate": _rate(fn, tp + fn),
        "coverage": _rate(judged, total),
        "abstention": _rate(abstentions, total),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[int(percentile) - 1]


def score(
    cases: Dataset | Iterable[Case],
    predictions: Iterable[Prediction],
    *,
    required_threshold: float = 0.5,
    unsupported_threshold: float = 0.5,
    confidence_band: tuple[float, float] | None = None,
    _breakdowns: bool = True,
) -> dict:
    """Score aligned case labels and predictions. Thresholds are never inferred here."""
    if not 0 <= required_threshold <= 1 or not 0 <= unsupported_threshold <= 1:
        raise ValueError("thresholds must be between 0 and 1")
    if confidence_band is not None:
        low, high = confidence_band
        if not 0 <= low <= high <= 1:
            raise ValueError("invalid confidence band")
    case_list = cases.cases if isinstance(cases, Dataset) else list(cases)
    prediction_list = list(predictions)
    pred_map = {p.case_id: p for p in prediction_list}
    if len(pred_map) != len(prediction_list):
        raise ValueError("duplicate prediction case_id")
    expected = {c.case_id for c in case_list}
    if set(pred_map) != expected:
        raise ValueError("predictions must contain exactly one row per case")
    required_truth: list[bool] = []
    required_guess: list[bool] = []
    required_abstain: list[bool] = []
    claim_truth: list[bool] = []
    claim_guess: list[bool] = []
    claim_abstain: list[bool] = []
    by_language: defaultdict[str, list[Case]] = defaultdict(list)
    by_scenario: defaultdict[str, list[Case]] = defaultdict(list)
    latency: list[float] = []
    costs: list[float] = []
    in_tokens = out_tokens = 0
    ops = {"cases": 0, "success": 0, "error": 0, "fallback": 0, "retries": 0}
    for case in case_list:
        pred = pred_map[case.case_id]
        ops["cases"] += 1
        ops["success"] += int(pred.success)
        ops["error"] += int(pred.error)
        ops["fallback"] += int(pred.fallback)
        ops["retries"] += pred.retries
        if pred.latency_ms is not None:
            latency.append(pred.latency_ms)
        if pred.cost_usd is not None:
            costs.append(pred.cost_usd)
        in_tokens += pred.input_tokens or 0
        out_tokens += pred.output_tokens or 0
        req = {x.item_id: x.probability for x in pred.required_items}
        claims = {x.claim_id: x.probability for x in pred.unsupported_claims}
        failed = not pred.success or pred.error or pred.fallback
        for label in case.required_items:
            if failed:
                required_truth.append(label.required)
                required_guess.append(False)
                required_abstain.append(True)
                continue
            if label.item_id not in req:
                raise ValueError(f"missing required prediction {case.case_id}/{label.item_id}")
            p = req[label.item_id]
            abstain = confidence_band is not None and confidence_band[0] <= p <= confidence_band[1]
            required_truth.append(label.required)
            required_guess.append(p >= required_threshold)
            required_abstain.append(abstain)
        for label in case.unsupported_claims:
            if failed:
                claim_truth.append(label.unsupported)
                claim_guess.append(False)
                claim_abstain.append(True)
                continue
            if label.claim_id not in claims:
                raise ValueError(f"missing claim prediction {case.case_id}/{label.claim_id}")
            p = claims[label.claim_id]
            abstain = confidence_band is not None and confidence_band[0] <= p <= confidence_band[1]
            claim_truth.append(label.unsupported)
            claim_guess.append(p >= unsupported_threshold)
            claim_abstain.append(abstain)
        by_language[case.language].append(case)
        by_scenario[case.scenario].append(case)

    def subset(items: list[Case]) -> dict:
        ids = {c.case_id for c in items}
        return score(
            items,
            [pred_map[i] for i in ids],
            required_threshold=required_threshold,
            unsupported_threshold=unsupported_threshold,
            confidence_band=confidence_band,
            _breakdowns=False,
        )

    result = {
        "thresholds": {"required": required_threshold, "unsupported": unsupported_threshold},
        "confidence_band": confidence_band,
        "required": _matrix(required_truth, required_guess, required_abstain),
        "unsupported_claims": _matrix(claim_truth, claim_guess, claim_abstain),
        "operational": {
            **ops,
            "success_rate": _rate(ops["success"], ops["cases"]),
            "error_rate": _rate(ops["error"], ops["cases"]),
            "fallback_rate": _rate(ops["fallback"], ops["cases"]),
            "retry_rate": _rate(ops["retries"], ops["cases"]),
            "p50_latency_ms": _percentile(latency, 50),
            "p95_latency_ms": _percentile(latency, 95),
            "total_cost_usd": sum(costs),
            "mean_cost_usd": _rate(sum(costs), len(costs)),
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
        },
        "by_language": {},
        "by_scenario": {},
    }
    # Avoid recursive operational detail in breakdowns; keep the two confusion summaries.
    for key, groups in (
        (("by_language", by_language), ("by_scenario", by_scenario)) if _breakdowns else ()
    ):
        for name, group in sorted(groups.items()):
            sub = subset(group)
            result[key][name] = {
                "required": sub["required"],
                "unsupported_claims": sub["unsupported_claims"],
            }
    return result


def sweep_thresholds(
    cases: Dataset | Iterable[Case], predictions: Iterable[Prediction], thresholds: Iterable[float]
) -> list[dict]:
    """Evaluate calibration-only thresholds; held-out data is rejected, never tuned."""
    case_list = cases.cases if isinstance(cases, Dataset) else list(cases)
    if any(c.split != "calibration" for c in case_list):
        raise ValueError("threshold sweep accepts calibration cases only")
    prediction_list = list(predictions)
    return [
        {
            "threshold": t,
            "score": score(
                case_list, prediction_list, required_threshold=t, unsupported_threshold=t
            ),
        }
        for t in thresholds
    ]
