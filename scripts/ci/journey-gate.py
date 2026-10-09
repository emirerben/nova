#!/usr/bin/env python3
"""Fail-closed evidence contract for the KRI-559 offline creation journey."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REQUIRED_STAGES = {
    "creation_composition",
    "same_chat_retime",
    "guided_validation",
    "persistence_reload",
    "phone_compilation",
    "phone_validation",
    "native_export",
}
INPUT_STAGES = REQUIRED_STAGES - {"native_export"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"evidence {path} must be a JSON object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify_input(fixture: Path, head: str) -> dict:
    proof_path = fixture / "journey-input-proof.json"
    proof = load(proof_path)
    require(proof.get("schema_version") == 1, "input proof schema_version must be 1")
    require(proof.get("head_sha") == head, "input proof HEAD SHA is stale")
    require(
        isinstance(proof.get("fixture_provenance"), dict)
        and proof["fixture_provenance"],
        "input proof lacks fixture provenance",
    )
    stages = proof.get("stages")
    require(isinstance(stages, dict), "input proof stages must be an object")
    for stage in INPUT_STAGES:
        require(
            stages.get(stage) == "passed",
            f"input proof stage {stage} is missing or not passed",
        )
    require(
        isinstance(proof.get("model_prompt_configuration"), dict),
        "input proof lacks model/prompt configuration",
    )
    require(
        proof.get("evidence_level") == "offline_replay_and_production_validation",
        "input proof has unsupported evidence level",
    )
    require(
        proof.get("settled_spend_usd") == 0, "PR journey must not make paid live calls"
    )
    hashes = proof.get("input_hashes")
    require(isinstance(hashes, dict), "input proof input_hashes must be an object")
    expected = {
        "e2e.json": fixture / "e2e.json",
        "status-creation-words-retimed.json": fixture
        / "status-creation-words-retimed.json",
        "editor-draft.json": fixture / "editor-draft.json",
    }
    expected["saved-guided-plan.json"] = fixture / "saved-guided-plan.json"
    for key, actual in expected.items():
        require(actual.is_file(), f"required journey fixture is missing: {actual.name}")
        require(hashes.get(key) == digest(actual), f"input hash mismatch for {key}")
    source = (
        Path(__file__).resolve().parents[2]
        / "src/apps/api/tests/fixtures/prompt_coverage/creation_composition.json"
    )
    require(
        source.is_file() and proof.get("source_fixture_sha256") == digest(source),
        "input hash mismatch for source fixture",
    )
    return proof


def require_passed_test(results: Path) -> None:
    value = load(results)
    target = "DeviceMontageRenderE2ETests/testCapturedCreationWordsRetimedThenExportOnTheIPhone"
    matches = []

    def visit(node):
        if isinstance(node, dict):
            text = " ".join(
                str(item)
                for item in node.values()
                if isinstance(item, (str, int, float))
            )
            if (
                target in text
                or "testCapturedCreationWordsRetimedThenExportOnTheIPhone" in text
            ):
                matches.append(node)
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    require(matches, "native xcresult does not contain the required XCTest")
    passed = {"success", "passed"}
    require(
        any(
            str(node.get(key, "")).lower() in passed
            for node in matches
            for key in ("testStatus", "status", "result")
        ),
        "required XCTest did not pass or was skipped",
    )


def record_native(
    fixture: Path,
    head: str,
    xcresult: Path,
    results: Path,
    render: Path,
    app_build: Path,
    output: Path,
) -> None:
    proof = verify_input(fixture, head)
    for path, label in (
        (xcresult, "xcresult"),
        (results, "xcresult test result"),
        (render, "render artifact"),
        (app_build, "app build provenance"),
    ):
        require(path.exists(), f"native export evidence missing {label}: {path}")
    require(render.stat().st_size > 0, "render artifact is empty")
    require_passed_test(results)
    evidence = {
        "schema_version": 1,
        "head_sha": head,
        "fixture_provenance": proof["fixture_provenance"],
        "model_prompt_configuration": proof["model_prompt_configuration"],
        "evidence_level": proof["evidence_level"],
        "settled_spend_usd": proof["settled_spend_usd"],
        "stages": {
            **{stage: "passed" for stage in INPUT_STAGES},
            "native_export": "passed",
        },
        "stage_details": {
            **proof.get("stage_details", {}),
            "native_export": "named XCTest passed; exported movie is nonempty",
        },
        "input_hashes": proof["input_hashes"],
        "commands": {
            "fixture": "python3 scripts/ios/kri-524-creation-e2e.py test-results/kri-559/fixture",
            "native": (
                "xcodebuild -project Kria.xcodeproj -scheme Kria "
                "-skipPackagePluginValidation -derivedDataPath .derived-data "
                "CODE_SIGNING_ALLOWED=NO -destination "
                '"platform=iOS Simulator,id=$(cat ../../../test-results/kri-559/simulator-id.txt)" '
                "-only-testing:KriaTests/DeviceMontageRenderE2ETests/"
                "testCapturedCreationWordsRetimedThenExportOnTheIPhone "
                "-resultBundlePath ../../../test-results/kri-559/native.xcresult test"
            ),
            "result_inspection": (
                "xcrun xcresulttool get test-results tests --path "
                "../../../test-results/kri-559/native.xcresult"
            ),
        },
        "limitations": [
            "Persistence proof reloads the saved guided-plan JSON; it does not perform a database write."
        ],
        "native": {
            "xcresult": {
                "path": str(xcresult),
                "sha256": digest(xcresult / "Info.plist")
                if (xcresult / "Info.plist").is_file()
                else digest_tree(xcresult),
            },
            "test_result": {"path": str(results), "sha256": digest(results)},
            "render": {
                "path": str(render),
                "sha256": digest(render),
                "bytes": render.stat().st_size,
            },
            "app_build": {"path": str(app_build), "sha256": digest(app_build)},
        },
        "gaps": [],
    }
    output.write_text(json.dumps(evidence, indent=2) + "\n")


def digest_tree(path: Path) -> str:
    value = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            value.update(str(child.relative_to(path)).encode())
            value.update(child.read_bytes())
    return value.hexdigest()


def validate(path: Path, head: str) -> None:
    evidence = load(path)
    require(
        evidence.get("schema_version") == 1, "journey evidence schema_version must be 1"
    )
    require(evidence.get("head_sha") == head, "journey evidence HEAD SHA is stale")
    require(
        evidence.get("gaps") == [],
        "journey evidence declares gaps; it cannot be fully verified",
    )
    stages = evidence.get("stages")
    require(isinstance(stages, dict), "journey evidence stages must be an object")
    for stage in REQUIRED_STAGES:
        require(
            stages.get(stage) == "passed",
            f"required journey stage {stage} is missing or not passed",
        )
    native = evidence.get("native")
    require(isinstance(native, dict), "journey evidence lacks native evidence")
    for item in ("xcresult", "render", "app_build"):
        require(
            isinstance(native.get(item), dict) and native[item].get("sha256"),
            f"native {item} evidence is missing",
        )
    require(
        isinstance(evidence.get("commands"), dict),
        "journey evidence lacks command provenance",
    )
    require(
        isinstance(evidence.get("limitations"), list),
        "journey evidence lacks stated limitations",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify-input", "record-native"):
        command = commands.add_parser(name)
        command.add_argument("--fixture", type=Path, required=True)
        command.add_argument("--head", required=True)
    record = commands.choices["record-native"]
    record.add_argument("--xcresult", type=Path, required=True)
    record.add_argument("--test-results", type=Path, required=True)
    record.add_argument("--render", type=Path, required=True)
    record.add_argument("--app-build", type=Path, required=True)
    record.add_argument("--output", type=Path, required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--evidence", type=Path, required=True)
    validate_parser.add_argument("--head", required=True)
    args = parser.parse_args()
    if args.command == "verify-input":
        verify_input(args.fixture, args.head)
    elif args.command == "record-native":
        record_native(
            args.fixture,
            args.head,
            args.xcresult,
            args.test_results,
            args.render,
            args.app_build,
            args.output,
        )
    else:
        validate(args.evidence, args.head)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(f"KRI-559 journey gate: {error}") from error
