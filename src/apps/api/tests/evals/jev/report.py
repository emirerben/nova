"""Deterministic Markdown report and offline CLI for Jev evaluations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .loader import load_cases, load_predictions
from .scorer import score


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_report(
    cases_path: str | Path,
    predictions_path: str | Path,
    *,
    required_threshold: float = 0.5,
    unsupported_threshold: float = 0.5,
    threshold_source: str = "unverified",
    gates: dict[str, float] | None = None,
) -> str:
    dataset = load_cases(cases_path)
    predictions = load_predictions(predictions_path)
    pred_map = {p.case_id: p for p in predictions}
    if len(pred_map) != len(predictions):
        raise ValueError("duplicate prediction case_id")
    expected_ids = {c.case_id for c in dataset.cases}
    extra = sorted(set(pred_map) - expected_ids)
    if extra:
        raise ValueError(f"unexpected prediction case_id: {', '.join(extra)}")
    missing = sorted(expected_ids - set(pred_map))
    held_out = [c for c in dataset.cases if c.split == "held_out"]
    scored_cases = [c for c in held_out if c.case_id in pred_map]
    result = score(
        scored_cases,
        [pred_map[c.case_id] for c in scored_cases],
        required_threshold=required_threshold,
        unsupported_threshold=unsupported_threshold,
    )
    human = sum(c.label_metadata.human_adjudicated for c in dataset.cases)
    derived = sum(c.provenance == "derived" for c in dataset.cases)
    gates = gates or {}
    checks = {
        "required_recall": result["required"]["recall"],
        "required_coverage": result["required"]["coverage"],
        "unsupported_precision": result["unsupported_claims"]["precision"],
        "unsupported_recall": result["unsupported_claims"]["recall"],
        "unsupported_coverage": result["unsupported_claims"]["coverage"],
        "false_flag_rate": result["unsupported_claims"]["false_flag_rate"],
        "missed_violation_rate": result["unsupported_claims"]["missed_violation_rate"],
        "p95_latency_ms": result["operational"]["p95_latency_ms"],
        "error_rate": result["operational"]["error_rate"],
    }
    maxima = {"false_flag_rate", "missed_violation_rate", "p95_latency_ms", "error_rate"}
    failures = [
        name
        for name, limit in sorted(gates.items())
        if checks.get(name) is None
        or (checks[name] > limit if name in maxima else checks[name] < limit)
    ]
    decision_count = sum(
        len(case.required_items) + len(case.unsupported_claims) for case in dataset.cases
    )
    if not gates:
        failures.append("numeric_gates_not_supplied")
    if threshold_source != "calibration":
        failures.append("calibration_threshold_provenance")
    if not 100 <= decision_count <= 200:
        failures.append("decision_set_size")
    if not held_out:
        failures.append("held_out_subset")
    if missing:
        failures.append("missing_provider_predictions")
    if any(not c.label_metadata.human_adjudicated for c in held_out):
        failures.append("held_out_human_adjudication")
    if any(
        c.case_id not in pred_map or not pred_map[c.case_id].success or pred_map[c.case_id].error
        for c in held_out
    ):
        failures.append("held_out_provider_predictions")
    operations = result["operational"]
    threshold_text = (
        f"- Threshold source: {threshold_source}; explicit arguments "
        f"(required={required_threshold:.4f}, unsupported={unsupported_threshold:.4f})"
    )
    unsupported_header = (
        "| TP | FP | FN | TN | Precision | Recall | False-flag rate | "
        "Missed-violation rate | Coverage | Abstention |"
    )
    lines = [
        "# Jev brief verification evaluation",
        "",
        "Deterministic offline report.",
        "",
        "## Dataset",
        "",
        f"- Cases: {len(dataset.cases)}",
        f"- Held-out cases scored: {len(scored_cases)}",
        f"- Human-adjudicated cases: {human}",
        f"- Derived cases: {derived}",
        threshold_text,
        "",
        "## Required elements",
        "",
        "| TP | FP | FN | TN | Precision | Recall | Miss rate | Coverage | Abstention |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        "| "
        + " | ".join(
            _fmt(result["required"][k])
            for k in (
                "tp",
                "fp",
                "fn",
                "tn",
                "precision",
                "recall",
                "miss_rate",
                "coverage",
                "abstention",
            )
        )
        + " |",
        "",
        "## Unsupported claims",
        "",
        unsupported_header,
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        "| "
        + " | ".join(
            _fmt(result["unsupported_claims"][k])
            for k in (
                "tp",
                "fp",
                "fn",
                "tn",
                "precision",
                "recall",
                "false_flag_rate",
                "missed_violation_rate",
                "coverage",
                "abstention",
            )
        )
        + " |",
        "",
        "## Operations",
        "",
        "- Success/error/fallback/retry cases: "
        f"{operations['success']}/{operations['error']}/"
        f"{operations['fallback']}/{operations['retries']}",
        "- p50/p95 latency (ms): "
        f"{_fmt(operations['p50_latency_ms'])}/{_fmt(operations['p95_latency_ms'])}",
        "- Cost total/mean (USD): "
        f"{_fmt(operations['total_cost_usd'])}/{_fmt(operations['mean_cost_usd'])}",
        f"- Input/output tokens: {operations['input_tokens']}/{operations['output_tokens']}",
        "",
        "## Go / no-go",
        "",
        f"- Result: **{'NO-GO' if failures else 'GO'}**",
    ]
    if gates:
        gate_text = ", ".join(
            f"{name} {'<=' if name in maxima else '>='} {limit:.4f}"
            for name, limit in sorted(gates.items())
        )
        lines += [
            f"- Gates: {gate_text}",
            f"- Failed gates: {', '.join(failures) if failures else 'none'}",
        ]
    else:
        lines += [
            "- Gates: none supplied (informational only)",
            f"- Failed checks: {', '.join(failures) if failures else 'none'}",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--required-threshold", type=float, default=0.5)
    parser.add_argument("--unsupported-threshold", type=float, default=0.5)
    parser.add_argument(
        "--threshold-source",
        choices=("unverified", "calibration"),
        default="unverified",
    )
    parser.add_argument("--gates", type=Path, help="JSON object of minimum named gates")
    args = parser.parse_args(argv)
    gates = json.loads(args.gates.read_text(encoding="utf-8")) if args.gates else None
    text = render_report(
        args.cases,
        args.predictions,
        required_threshold=args.required_threshold,
        unsupported_threshold=args.unsupported_threshold,
        threshold_source=args.threshold_source,
        gates=gates,
    )
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
