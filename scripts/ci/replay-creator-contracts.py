#!/usr/bin/env python3
"""Offline, fail-closed comparison of captured creator-contract jobs.

The candidate is an explicit local Python export.  CI never contacts a model,
storage provider, or paid API.  Baseline outcomes are either produced by a
checked-in base export or supplied as a base-SHA-bound outcome file.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class ReplayError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReplayError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError(f"{path} must contain a JSON object")
    return value


def _outcome(value: object, *, field: str) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != {"declined"} or not isinstance(value["declined"], bool):
        raise ReplayError(f"{field} must be exactly {{'declined': boolean}}")
    return {"declined": value["declined"]}


def validate_snapshot(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if snapshot.get("schema_version") != 1:
        raise ReplayError("snapshot schema_version must be 1")
    if not isinstance(snapshot.get("base_sha"), str) or not snapshot["base_sha"]:
        raise ReplayError("snapshot lacks base_sha")
    captures = snapshot.get("captures")
    if not isinstance(captures, list) or not captures:
        raise ReplayError("snapshot lacks captured jobs")
    available_count = snapshot.get("available_count")
    if isinstance(available_count, bool) or not isinstance(available_count, int) or available_count < 1:
        raise ReplayError("snapshot lacks available_count")
    if snapshot.get("missing_required_inputs_count") != 0:
        raise ReplayError("snapshot has captures with missing required inputs")
    if len(captures) != min(available_count, 100):
        raise ReplayError("snapshot does not replay every available capture up to the 100-job bound")
    ids: set[str] = set()
    for capture in captures:
        if not isinstance(capture, Mapping):
            raise ReplayError("captured job must be an object")
        capture_id = capture.get("id")
        if not isinstance(capture_id, str) or not capture_id or capture_id in ids:
            raise ReplayError("captured job ids must be unique nonempty strings")
        ids.add(capture_id)
        if not isinstance(capture.get("source_provenance"), Mapping) or not capture["source_provenance"]:
            raise ReplayError(f"capture {capture_id} lacks source provenance")
        if not isinstance(capture.get("contract"), Mapping) or not capture["contract"]:
            raise ReplayError(f"capture {capture_id} lacks contract")
        if not isinstance(capture.get("variant"), Mapping) or not capture["variant"]:
            raise ReplayError(f"capture {capture_id} lacks measured variant")
        if not isinstance(capture.get("candidates"), Mapping) or not capture["candidates"]:
            raise ReplayError(f"capture {capture_id} lacks creator candidates")
        has_phone = isinstance(capture.get("phone_recipe"), Mapping) and bool(capture["phone_recipe"])
        has_cloud = isinstance(capture.get("cloud_validation"), Mapping) and bool(capture["cloud_validation"])
        if not (has_phone or has_cloud):
            raise ReplayError(f"capture {capture_id} lacks phone recipe or cloud validation payload")
        destination = capture["variant"].get("render_destination")
        if has_phone and destination not in {"native", "device"}:
            raise ReplayError(f"phone capture {capture_id} is not a device destination")
        if has_cloud and destination in {"native", "device"}:
            raise ReplayError(f"cloud capture {capture_id} cannot claim a device destination")
    return captures


def replay(
    snapshot: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    candidate_sha: str | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    captures = validate_snapshot(snapshot)
    comparison_base_sha = base_sha or snapshot["base_sha"]
    if (
        baseline.get("schema_version") != 1
        or baseline.get("base_sha") != comparison_base_sha
        or baseline.get("evaluator_sha") != comparison_base_sha
        or baseline.get("capture_base_sha", baseline.get("base_sha")) != snapshot["base_sha"]
    ):
        raise ReplayError("baseline outcomes are absent or not bound to comparison and capture base SHAs")
    baseline_outcomes = baseline.get("outcomes")
    if not isinstance(baseline_outcomes, Mapping):
        raise ReplayError("baseline lacks outcomes")
    rows = []
    newly_blocking = []
    baseline_declines = []
    candidate_declines = []
    for capture in captures:
        capture_id = capture["id"]
        before = _outcome(baseline_outcomes.get(capture_id), field=f"baseline outcome {capture_id}")
        after = _outcome(candidate(capture), field=f"candidate outcome {capture_id}")
        # A decline that turns a previously successful captured job into a
        # blocking result is the regression this gate protects against.
        regression = not before["declined"] and after["declined"]
        if before["declined"]:
            baseline_declines.append(capture_id)
        if after["declined"]:
            candidate_declines.append(capture_id)
        if regression:
            newly_blocking.append(capture_id)
        rows.append({"id": capture_id, "baseline": before, "candidate": after, "newly_blocking": regression})
    return {
        "schema_version": 1,
        "base_sha": comparison_base_sha,
        "capture_base_sha": snapshot["base_sha"],
        "candidate_sha": candidate_sha,
        "available_count": snapshot["available_count"],
        "input_count": snapshot["available_count"],
        "replayed_count": len(captures),
        "complete": True,
        "successful": not newly_blocking and not baseline_declines,
        "newly_blocking_declines": len(newly_blocking),
        "newly_blocking_capture_ids": newly_blocking,
        "baseline_declining_capture_ids": baseline_declines,
        "candidate_decline_observations": candidate_declines,
        "results": rows,
    }


def evaluate_candidate(capture: Mapping[str, Any]) -> Mapping[str, Any]:
    """Run the current, real verifier against one sanitized captured shape."""
    from app.kria.recipes_v2 import EditRecipeV2
    from app.services.cloud_render_contract import CloudRenderContractError, verify_cloud_variant
    from app.services.creator_render_contract import (
        COMPOSITION_FIELD,
        CONTRACT_FIELD,
        CreatorRenderContractError,
        CompositionCommitments,
        read_render_contract,
        verify_phone_recipe,
    )

    assembly: dict[str, Any] = {CONTRACT_FIELD: capture["contract"]}
    length_policy = None
    # The base archive predates this module. Its export intentionally uses its
    # legacy verifier; the candidate export stamps duration as enforce.
    try:
        from app.services.length_policy_state import read_length_policy, stamp_length_policy

        pinned = read_render_contract(assembly)
        if pinned is not None:
            assembly = stamp_length_policy(
                assembly,
                capture["candidates"].get("creator_strategy") or {},
                pinned,
                mode="enforce",
            )
            length_policy = read_length_policy(assembly, pinned)
    except ImportError:
        pass
    if isinstance(capture.get("composition"), Mapping):
        # Validate before inserting; malformed replay input must be a hard input
        # failure rather than a decline that could make a broken fixture look safe.
        composition = CompositionCommitments.model_validate(capture["composition"])
        contract = read_render_contract(assembly)
        if contract is not None:
            assembly[COMPOSITION_FIELD] = {
                "contract_digest": contract.digest,
                **composition.model_dump(mode="json"),
            }
    try:
        if isinstance(capture.get("cloud_validation"), Mapping):
            variant = dict(capture["variant"])
            # The live cloud verifier accepts renderer output on the variant.
            from app.pipeline.cloud_render_evidence import CLOUD_EVIDENCE_KEY

            variant.setdefault(CLOUD_EVIDENCE_KEY, capture["cloud_validation"])
            verify_cloud_variant(assembly, variant, candidates=capture["candidates"])
        else:
            contract = read_render_contract(assembly)
            if contract is None:
                raise ReplayError("phone capture contract is unreadable")
            recipe = EditRecipeV2.model_validate(capture["phone_recipe"])
            source_audio = capture.get("source_audio")
            if source_audio is not None and not isinstance(source_audio, Mapping):
                raise ReplayError("phone capture source_audio must be an object")
            composition = (
                CompositionCommitments.model_validate(capture["composition"])
                if isinstance(capture.get("composition"), Mapping)
                else None
            )
            phone_args = {"source_audio": source_audio, "composition": composition}
            if length_policy is not None:
                phone_args["length_policy"] = length_policy
            verify_phone_recipe(contract, recipe, **phone_args)
    except (CreatorRenderContractError, CloudRenderContractError):
        return {"declined": True}
    return {"declined": False}


def export_outcomes(snapshot: Mapping[str, Any], *, evaluator_sha: str) -> dict[str, Any]:
    captures = validate_snapshot(snapshot)
    return {
        "schema_version": 1,
        "base_sha": evaluator_sha,
        "capture_base_sha": snapshot["base_sha"],
        "evaluator_sha": evaluator_sha,
        "outcomes": {
            capture["id"]: _outcome(
                evaluate_candidate(capture), field=f"candidate outcome {capture['id']}"
            )
            for capture in captures
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--export-outcomes", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    snapshot = load_json(args.snapshot)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.export_outcomes:
        if args.baseline is not None:
            raise ReplayError("--export-outcomes cannot use --baseline")
        args.report.write_text(
            json.dumps(
                export_outcomes(snapshot, evaluator_sha=args.base_sha or snapshot["base_sha"]),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return
    if args.baseline is None:
        raise ReplayError("--baseline is required unless exporting outcomes")
    report = replay(
        snapshot,
        load_json(args.baseline),
        evaluate_candidate,
        candidate_sha=args.candidate_sha,
        base_sha=args.base_sha,
    )
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["baseline_declining_capture_ids"]:
        raise SystemExit("KRI-499 replay: base verifier declines a captured successful job")
    if report["newly_blocking_declines"]:
        raise SystemExit("KRI-499 replay: candidate newly declines a successful captured job")


if __name__ == "__main__":
    try:
        main()
    except ReplayError as error:
        raise SystemExit(f"KRI-499 replay: {error}") from error
