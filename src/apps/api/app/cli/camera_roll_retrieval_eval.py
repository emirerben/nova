"""Compare prerecorded text/semantic results with frozen human labels, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CATEGORIES = {"lit_house", "ferry", "long_take_ending", "late_speech", "occlusion", "no_match"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Protocol(Strict):
    registered_at: AwareDatetime
    labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recall_floor: float = Field(ge=0.9, le=1)
    max_latency_s: float = Field(gt=0)
    total_budget_usd: float = Field(ge=0, le=2)
    app_revision: str = Field(min_length=1, max_length=160)
    server_revision: str = Field(min_length=1, max_length=160)
    scope: Literal["single_creator", "creator_event_holdout"]


class Label(Strict):
    query_key: str = Field(min_length=1, max_length=160)
    categories: list[str] = Field(min_length=1, max_length=10)
    answerable: bool
    accepted_ids: list[str] = Field(max_length=100)
    unusable_ids: list[str] = Field(max_length=100)
    irrelevant_ids: list[str] = Field(max_length=100)
    evidence_ref: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def complete_labels(self):
        values = [*self.accepted_ids, *self.unusable_ids, *self.irrelevant_ids]
        if len(set(values)) != len(values) or bool(self.accepted_ids) != self.answerable:
            raise ValueError("Accepted, unusable and irrelevant labels must be disjoint")
        return self


class Run(Strict):
    query_key: str = Field(min_length=1, max_length=160)
    method: Literal["text", "semantic"]
    ranked_ids: list[str] = Field(max_length=10)
    completed_at: AwareDatetime
    latency_s: float | None = Field(default=None, ge=0)
    settled_spend_usd: float | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    source_evidence_ref: str = Field(min_length=1, max_length=500)


def report(protocol, labels, runs):
    by_key = {label.query_key: label for label in labels}
    if not labels or len(by_key) != len(labels):
        raise ValueError("Labels must have unique query keys")
    expected = {(key, method) for key in by_key for method in ("text", "semantic")}
    if len(runs) != len(expected) or {(run.query_key, run.method) for run in runs} != expected:
        raise ValueError("Each labelled query requires both methods, once")
    if any(run.completed_at < protocol.registered_at for run in runs):
        raise ValueError("Register before collecting results")
    for run in runs:
        label = by_key[run.query_key]
        known = set(label.accepted_ids + label.unusable_ids + label.irrelevant_ids)
        if len(set(run.ranked_ids)) != len(run.ranked_ids) or not set(run.ranked_ids) <= known:
            raise ValueError("Returned intervals require independent labels")
    coverage = CATEGORIES <= {category for label in labels for category in label.categories}
    resource_known = all(
        run.latency_s is not None
        and run.settled_spend_usd is not None
        and run.model_calls is not None
        for run in runs
    )
    total = math.fsum(run.settled_spend_usd or 0 for run in runs)
    resources = (
        resource_known
        and total <= protocol.total_budget_usd
        and all(run.latency_s <= protocol.max_latency_s for run in runs)
    )
    methods = {}
    for method in ("text", "semantic"):
        rows = [run for run in runs if run.method == method]
        answerable = sum(by_key[run.query_key].answerable for run in rows)
        hits = sum(
            bool(set(run.ranked_ids) & set(by_key[run.query_key].accepted_ids)) for run in rows
        )
        predicted_empty = [run for run in rows if not run.ranked_ids]
        true_empty = sum(not by_key[run.query_key].answerable for run in predicted_empty)
        no_match_count = sum(not by_key[run.query_key].answerable for run in rows)
        methods[method] = {
            "queries": len(rows),
            "answerable_queries": answerable,
            "recall_at_10": hits / answerable if answerable else None,
            "unusable_returns": sum(
                len(set(run.ranked_ids) & set(by_key[run.query_key].unusable_ids)) for run in rows
            ),
            "irrelevant_returns": sum(
                len(set(run.ranked_ids) & set(by_key[run.query_key].irrelevant_ids)) for run in rows
            ),
            "no_match_precision": true_empty / len(predicted_empty) if predicted_empty else None,
            "no_match_recall": true_empty / no_match_count if no_match_count else None,
        }
    choice = next(
        (
            method
            for method, value in methods.items()
            if coverage
            and resources
            and value["recall_at_10"] is not None
            and value["recall_at_10"] >= protocol.recall_floor
        ),
        None,
    )
    return {
        "schema_version": 1,
        "protocol_sha256": hashlib.sha256(protocol.model_dump_json().encode()).hexdigest(),
        "labels_sha256": protocol.labels_sha256,
        "scope": protocol.scope,
        "app_revision": protocol.app_revision,
        "server_revision": protocol.server_revision,
        "methods": methods,
        "required_categories_present": coverage,
        "resource_caps_met": resources,
        "known_settled_spend_usd": total,
        "unknown_resource_rows": sum(
            run.latency_s is None or run.settled_spend_usd is None or run.model_calls is None
            for run in runs
        ),
        "recommended_method_for_review": choice,
        "supports_wider_rollout": False,
        "evidence_limit": (
            "Human labels and current-source evidence references require independent review. "
            "This report does not verify their contents or change rollout settings."
        ),
    }


def read(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Input exceeds four MiB")
    return path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("protocol", "labels", "runs", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.resolve() in {
            args.protocol.resolve(),
            args.labels.resolve(),
            args.runs.resolve(),
        }:
            raise ValueError("Output cannot replace frozen input")
        protocol = Protocol.model_validate_json(read(args.protocol))
        raw_labels = read(args.labels)
        if hashlib.sha256(raw_labels).hexdigest() != protocol.labels_sha256:
            raise ValueError("Human labels changed after registration")
        label_data, run_data = json.loads(raw_labels), json.loads(read(args.runs))
        if (
            not isinstance(label_data, list)
            or not isinstance(run_data, list)
            or len(label_data) > 1000
            or len(run_data) > 2000
        ):
            raise ValueError("Expected bounded query/run arrays")
        result = report(
            protocol,
            [Label.model_validate(row) for row in label_data],
            [Run.model_validate(row) for row in run_data],
        )
        with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(json.dumps(result, indent=2) + "\n")
                file.flush()
                temporary.replace(args.output)
            finally:
                temporary.unlink(missing_ok=True)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Invalid retrieval input ({type(exc).__name__}); no report written.\n")


if __name__ == "__main__":
    main()
