#!/usr/bin/env python3
"""Fail-closed evidence contract for the KRI-559 offline creation journey."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ios"))
import ui_tests


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


def require_passed_test(
    results: Path,
    target: str = "DeviceMontageRenderE2ETests/testCapturedCreationWordsRetimedThenExportOnTheIPhone",
) -> None:
    value = load(results)
    matches = []

    def visit(node):
        if isinstance(node, dict):
            text = " ".join(
                str(item)
                for item in node.values()
                if isinstance(item, (str, int, float))
            )
            if target in text or target.rsplit("/", 1)[-1] in text:
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
            "build": (
                "xcodebuild -project Kria.xcodeproj -scheme Kria "
                "-skipPackagePluginValidation -derivedDataPath .derived-data "
                "CODE_SIGNING_ALLOWED=NO -destination "
                '"platform=iOS Simulator,id=$(cat ../../../test-results/kri-559/simulator-id.txt)" '
                "build-for-testing"
            ),
            "native": (
                "xcodebuild -project Kria.xcodeproj -scheme Kria "
                "-skipPackagePluginValidation -derivedDataPath .derived-data "
                "CODE_SIGNING_ALLOWED=NO -destination "
                '"platform=iOS Simulator,id=$(cat ../../../test-results/kri-559/simulator-id.txt)" '
                "-only-testing:KriaTests/DeviceMontageRenderE2ETests/"
                "testCapturedCreationWordsRetimedThenExportOnTheIPhone "
                "-resultBundlePath ../../../test-results/kri-559/native.xcresult test-without-building"
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


BUNDLE_MANIFEST = "fixture-bundle.json"
PROVENANCE = "selected-journeys.json"
JOURNEY_MANIFEST = Path(__file__).with_name("journey-manifest.json")


def bundle_files(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"fixture bundle contains symlink: {path.name}")
        if path.is_file() and path != root / BUNDLE_MANIFEST:
            files[path.relative_to(root).as_posix()] = digest(path)
    require(files, "fixture bundle is empty")
    return files


def selected_rows(journeys: str) -> list:
    """Manifest rows for a comma-separated selection; unknown ids fail closed."""
    ids = [item for item in journeys.split(",") if item]
    require(ids, "no journeys selected for the fixture bundle")
    rows = {row["id"]: row for row in load(JOURNEY_MANIFEST)["journeys"]}
    unknown = sorted(set(ids) - set(rows))
    require(not unknown, f"unknown journey ids for the fixture bundle: {unknown}")
    return [rows[item] for item in sorted(set(ids))]


def require_declared_fixtures(root: Path, rows: list) -> list:
    """A journey that declares a fixture_dir must have produced files there.

    Journeys without one (for example native-notification-runtime) own no
    offline fixtures. Returns the declared directories.
    """
    declared = []
    for row in rows:
        directory = row.get("fixture_dir")
        if not directory:
            continue
        folder = root / directory
        produced = folder.is_dir() and any(
            item.is_file() for item in folder.rglob("*")
        )
        require(
            produced,
            f"journey {row['id']} declares fixtures in {directory}/ but its "
            "generator produced none",
        )
        declared.append(directory)
    return sorted(declared)


def provenance_record(head: str, run: str, attempt: str, rows: list, declared: list):
    return {
        "head_sha": head,
        "run_id": run,
        "run_attempt": attempt,
        "journeys": [row["id"] for row in rows],
        "fixtures": declared or "none required",
    }


def seal_bundle(root: Path, head: str, run: str, attempt: str, journeys: str) -> None:
    rows = selected_rows(journeys)
    declared = require_declared_fixtures(root, rows)
    (root / PROVENANCE).write_text(
        json.dumps(provenance_record(head, run, attempt, rows, declared), indent=2)
        + "\n"
    )
    manifest = {
        "head_sha": head,
        "run_id": run,
        "run_attempt": attempt,
        "files": bundle_files(root),
    }
    (root / BUNDLE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")


def verify_bundle(root: Path, head: str, run: str, attempt: str, journeys: str) -> None:
    manifest = load(root / BUNDLE_MANIFEST)
    for key, expected in (
        ("head_sha", head),
        ("run_id", run),
        ("run_attempt", attempt),
    ):
        require(manifest.get(key) == expected, f"fixture bundle {key} is stale")
    require(
        manifest.get("files") == bundle_files(root),
        "fixture bundle files are missing or changed",
    )
    rows = selected_rows(journeys)
    declared = require_declared_fixtures(root, rows)
    record = load(root / PROVENANCE)
    for key, expected in (
        ("head_sha", head),
        ("run_id", run),
        ("run_attempt", attempt),
    ):
        require(
            record.get(key) == expected, f"fixture bundle {PROVENANCE} {key} is stale"
        )
    require(
        record == provenance_record(head, run, attempt, rows, declared),
        "fixture bundle was sealed for different journeys",
    )


def verify_named_tests(report: dict, expected: set) -> None:
    actual = ui_tests.result_tests(report)
    missing = {test for test in expected if actual.get(test) != "Passed"}
    require(
        not missing, f"required named tests missing or not passed: {sorted(missing)}"
    )


def native_units(ids: str, require_unit: bool) -> None:
    """Reuse this job's unit receipt, otherwise execute selected cases once."""
    repo = Path(__file__).resolve().parents[2]
    journeys = load(Path(__file__).with_name("journey-manifest.json"))["journeys"]
    selected = {row["id"] for row in journeys} if ids == "all" else set(ids.split(","))
    require(selected <= {row["id"] for row in journeys}, "unknown native journey ids")
    tests = {
        test
        for row in journeys
        if row["id"] in selected
        for test in row.get("xctests", [])
        if test.startswith("KriaTests/")
        and not test.startswith("KriaTests/DeviceMontageRenderE2ETests/")
    }
    if not tests:
        print("No plain unit journey tests selected; fixture exports run separately.")
        return
    result_root = repo / "test-results/ios"
    bundles = list(result_root.glob("*/unit.xcresult"))
    require(len(bundles) <= 1, "ambiguous unit result bundles")
    if bundles:
        bundle = bundles[0]
        print(f"Reusing unit evidence for {len(tests)} named journey tests.")
    else:
        require(not require_unit, "expected unit evidence is missing")
        bundle = repo / "test-results/kri-559/selected-units.xcresult"
        simulator = (result_root / "simulator-id.txt").read_text().strip()
        require(simulator, "native simulator receipt is empty")
        subprocess.run(
            [
                "xcodebuild",
                "-project",
                "Kria.xcodeproj",
                "-scheme",
                "Kria",
                "-skipPackagePluginValidation",
                "-derivedDataPath",
                ".derived-data",
                "CODE_SIGNING_ALLOWED=NO",
                "-destination",
                f"platform=iOS Simulator,id={simulator}",
                "-parallel-testing-enabled",
                "NO",
                *[f"-only-testing:{test}" for test in sorted(tests)],
                "-resultBundlePath",
                str(bundle),
                "test-without-building",
            ],
            cwd=repo / "src/apps/ios",
            check=True,
        )
    report = json.loads(
        subprocess.check_output(
            [
                "xcrun",
                "xcresulttool",
                "get",
                "test-results",
                "tests",
                "--path",
                str(bundle),
            ]
        )
    )
    verify_named_tests(report, tests)
    destination = result_root / "named-journey-test-results.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Verified {len(tests)} named native journey unit tests passed.")


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
    tests_parser = commands.add_parser("verify-tests")
    tests_parser.add_argument("--test-results", type=Path, required=True)
    tests_parser.add_argument("--xctest", action="append", required=True)
    for name in ("seal-bundle", "verify-bundle"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--head", required=True)
        command.add_argument("--run", required=True)
        command.add_argument("--attempt", required=True)
        command.add_argument("--journeys", required=True)
    unit_parser = commands.add_parser("native-units")
    unit_parser.add_argument("--ids", required=True)
    unit_parser.add_argument("--require-unit", action="store_true")
    args = parser.parse_args()
    if args.command == "seal-bundle":
        seal_bundle(args.root, args.head, args.run, args.attempt, args.journeys)
    elif args.command == "verify-bundle":
        verify_bundle(args.root, args.head, args.run, args.attempt, args.journeys)
    elif args.command == "native-units":
        native_units(args.ids, args.require_unit)
    elif args.command == "verify-input":
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
    elif args.command == "verify-tests":
        for xctest in args.xctest:
            require_passed_test(args.test_results, xctest)
    else:
        validate(args.evidence, args.head)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(f"KRI-559 journey gate: {error}") from error
