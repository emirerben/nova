"""Run the pinned Jev model against a frozen pilot manifest."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from app.services.jev_brief_shadow import (
    JevPayload,
    build_jev_questions,
    map_jev_evaluation,
)
from app.services.jev_client import JEV_ENDPOINT, JevClient, JevError

from .loader import load_cases
from .models import Dataset, Prediction


def evaluate_cases(
    dataset: Dataset,
    api_key: str,
    *,
    endpoint: str = JEV_ENDPOINT,
    client_factory: Callable[..., JevClient] = JevClient,
) -> list[Prediction]:
    """Evaluate every case; provider failures become explicit fallback rows."""
    dataset.validate_decision_set()
    client = client_factory(api_key, endpoint=endpoint)
    predictions: list[Prediction] = []
    for case in dataset.cases:
        payload = JevPayload.model_validate(case.payload)
        questions = build_jev_questions(payload)
        started = time.monotonic()
        try:
            result = client.evaluate(payload.model_dump(mode="json", exclude_none=True), questions)
            judgment = map_jev_evaluation(payload, result)
        except (JevError, ValueError) as exc:
            attempts = exc.attempts if isinstance(exc, JevError) else 1
            predictions.append(
                Prediction(
                    case_id=case.case_id,
                    success=False,
                    error=True,
                    fallback=True,
                    retries=max(0, attempts - 1),
                    latency_ms=(time.monotonic() - started) * 1000,
                    error_code=exc.code if isinstance(exc, JevError) else type(exc).__name__,
                )
            )
            continue
        predictions.append(
            Prediction(
                case_id=case.case_id,
                required_items=[
                    {"item_id": item.id, "probability": item.probability}
                    for item in judgment.requirements
                ],
                unsupported_claims=[
                    {"claim_id": item.id, "probability": item.probability}
                    for item in judgment.claims
                ],
                latency_ms=result.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                retries=max(0, result.attempts - 1),
            )
        )
    return predictions


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--endpoint", default=os.environ.get("JEV_API_URL", JEV_ENDPOINT))
    args = parser.parse_args(argv)
    api_key = os.environ.get("TYPESAFE_API_KEY", "")
    if not api_key:
        raise SystemExit("TYPESAFE_API_KEY is required")
    predictions = evaluate_cases(load_cases(args.cases), api_key, endpoint=args.endpoint)
    args.out.write_text(
        "".join(
            json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n" for row in predictions
        ),
        encoding="utf-8",
    )
    print(f"wrote {args.out} ({len(predictions)} predictions)")


if __name__ == "__main__":
    main()
