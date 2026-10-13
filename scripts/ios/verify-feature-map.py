#!/usr/bin/env python3
"""Select the skill's mapped journeys and retain CI's real XCTest/file evidence."""

import argparse
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import ui_tests

ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = ROOT / ".cursor/skills/verify-kria-ios/features/ci.json"
OUTPUT = ROOT / "test-results/verify-kria-ios/ci"
SCRIPT_TEST = (
    "KriaUITests/TeleprompterUITests/testScriptIsSavedAndCameraFailureKeepsItEditable"
)
SCRIPT_TEXT = (
    "Today I want to share a simple idea.\nTake your time and enjoy the story."
)


def load_map(path=MAP_PATH):
    data = json.loads(path.read_text())
    entries = data["features"]
    if (
        data["version"] != 1
        or {entry["id"] for entry in entries}
        != {"projects", "creation", "teleprompter", "editor", "slides"}
        or len(entries) != 5
    ):
        raise ValueError("The verification map must cover all five native features")
    inventory = ui_tests.discovered()
    for entry in entries:
        if (
            not entry["paths"]
            or not entry["tests"]
            or len(set(entry["tests"])) != len(entry["tests"])
        ):
            raise ValueError(
                f"Empty/duplicate verification paths or tests: {entry['id']}"
            )
        unknown = {"KriaUITests/" + test for test in entry["tests"]} - inventory
        if unknown:
            raise ValueError(f"Verification map names missing tests: {sorted(unknown)}")
    return entries


def features_for_paths(paths):
    entries = load_map()
    selected = set()
    for path in paths:
        matched = {
            entry["id"]
            for entry in entries
            if any(fnmatchcase(path, pattern) for pattern in entry["paths"])
        }
        # Shared/unknown UI inputs must cover the feature map, not just smoke.
        selected |= matched or {entry["id"] for entry in entries}
    return [entry["id"] for entry in entries if entry["id"] in selected]


def selected_features(value):
    if value in ("", "none"):
        raise ValueError("Mapped features are required for selected iPhone UI checks")
    selected = set(value.split(","))
    known = {entry["id"] for entry in load_map()}
    if selected - known:
        raise ValueError(f"Unknown verification features: {sorted(selected - known)}")
    return selected


def merge_groups(base, features):
    selected = selected_features(features)
    existing = ui_tests.expected_tests(base)
    mapped = {
        "KriaUITests/" + test
        for entry in load_map()
        if entry["id"] in selected
        for test in entry["tests"]
    }
    if base == "full":
        return base
    group, extra = ui_tests.parse_selection(base)
    tests = set(extra) | {
        test.removeprefix("KriaUITests/") for test in mapped - existing
    }
    return ",".join(["smoke", *([group] if group else []), *sorted(tests)])


def query(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def build_identity():
    binary = (
        ROOT
        / "src/apps/ios/.derived-data/Build/Products/Debug-iphonesimulator/Kria.app/Kria"
    )
    return {
        "head": query("git", "rev-parse", "HEAD"),
        "fingerprint": query(
            "python3", str(ROOT / "scripts/ios/cache-inputs.py"), "fingerprint"
        ),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "simulator_id": (ROOT / "test-results/ios/simulator-id.txt")
        .read_text()
        .strip(),
    }


def copy_script_draft(container, output):
    paths = (container / "Library/Application Support/KriaTeleprompter").glob(
        "*/*/draft.json"
    )
    matches = [
        path
        for path in paths
        if json.loads(path.read_text()).get("text") == SCRIPT_TEXT
    ]
    if not matches:
        raise ValueError(
            "No persisted draft contains the exact UI-entered teleprompter script"
        )
    destination = output / "persistence"
    destination.mkdir(exist_ok=True)
    for index, path in enumerate(matches):
        shutil.copy2(path, destination / f"{index}-draft.json")
    return len(matches)


def capture(features, groups):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "failed",
        "features": sorted(selected_features(features)),
        "selection": groups,
        "evidence_level": "offline native UI",
        "model_spend_usd": 0,
        "unverified": [
            "live backend/model",
            "physical iPhone",
            "production",
            "export media content",
        ],
    }
    try:
        built = json.loads((OUTPUT / "build.json").read_text())
        if built != build_identity():
            raise ValueError(
                "Compiled inputs, revision, simulator or binary changed before capture"
            )
        report.update(built)
        bundles = list((ROOT / "test-results/ios").glob("ui.*/ui.xcresult"))
        if len(bundles) != 1:
            raise ValueError("Expected one selected-UI result bundle from this CI job")
        bundle = bundles[0]
        tests_path = Path(str(bundle) + ".tests.json")
        results = json.loads(tests_path.read_text())
        expected = ui_tests.expected_tests(groups)
        mapped = {
            "KriaUITests/" + test
            for entry in load_map()
            if entry["id"] in report["features"]
            for test in entry["tests"]
        }
        if not mapped <= expected:
            raise ValueError(
                "Selected UI execution omitted required mapped feature tests"
            )
        subprocess.run(
            [
                "xcrun",
                "xcresulttool",
                "export",
                "attachments",
                "--path",
                str(bundle),
                "--output-path",
                str(OUTPUT / "attachments"),
            ],
            check=True,
        )
        # Export failure screenshots before coverage validation can reject a red run.
        report["coverage"] = ui_tests.verify_results(results, expected)
        report["flaky_tests"] = json.loads(
            (bundle.parent / "flaky-tests.json").read_text()
        )
        if not any((OUTPUT / "attachments").glob("*.png")):
            raise ValueError("Selected UI execution produced no screenshots")
        if SCRIPT_TEST in expected:
            container = Path(
                query(
                    "xcrun",
                    "simctl",
                    "get_app_container",
                    built["simulator_id"],
                    "com.kria.app.dev",
                    "data",
                )
            )
            report["persisted_script_drafts"] = copy_script_draft(container, OUTPUT)
        report["status"] = "passed_with_retries" if report["flaky_tests"] else "passed"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        save(OUTPUT / "proof.json", report)
        if report["status"] == "failed":
            print(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    groups = commands.add_parser("groups")
    groups.add_argument("base")
    groups.add_argument("features")
    commands.add_parser("snapshot-build")
    evidence = commands.add_parser("capture")
    evidence.add_argument("features")
    evidence.add_argument("groups")
    args = parser.parse_args()
    if args.command == "groups":
        print(merge_groups(args.base, args.features))
    elif args.command == "snapshot-build":
        OUTPUT.mkdir(parents=True, exist_ok=True)
        save(OUTPUT / "build.json", build_identity())
    else:
        capture(args.features, args.groups)


if __name__ == "__main__":
    main()
