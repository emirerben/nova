#!/usr/bin/env python3
"""Conservative UI grouping and verification of actual XCTest result coverage."""

import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("ui-test-groups.json")
FOCUSED = ("smoke,creation", "smoke,projects", "smoke,editor")


def validate_groups(value):
    if value not in ("none", "full", *FOCUSED):
        raise ValueError(f"Invalid iOS UI groups: {value!r}")
    return value


def manifest():
    data = json.loads(MANIFEST.read_text())
    if set(data["groups"]) != {"smoke", "creation", "projects", "editor"}:
        raise ValueError("Invalid UI group manifest")
    for group, tests in data["groups"].items():
        if (
            not tests
            or len(tests) != len(set(tests))
            or any(not re.fullmatch(r"KriaUITests/\w+/test\w+", test) for test in tests)
        ):
            raise ValueError(f"Empty or invalid UI group: {group}")
    if not set(data["sources"].values()) <= {"creation", "projects", "editor"}:
        raise ValueError("Invalid source mapping")
    return data


def discovered(root=ROOT):
    """One XCTestCase class per file; unfamiliar source shapes force full coverage."""
    tests = set()
    for path in (root / "src/apps/ios/Tests/KriaUITests").rglob("*.swift"):
        source = path.read_text()
        methods = re.findall(r"\bfunc\s+(test\w+)\s*\(", source)
        if not methods:
            continue
        classes = re.findall(r"\bclass\s+(\w+)\s*:\s*XCTestCase\b", source)
        if len(classes) != 1 or len(methods) != len(set(methods)):
            raise ValueError(f"Unrecognized XCTest layout: {path}")
        tests.update(f"KriaUITests/{classes[0]}/{name}" for name in methods)
    if not tests:
        raise ValueError("No UI tests discovered")
    return tests


def inventory_matches(root=ROOT):
    data = manifest()
    classified = set().union(*(set(v) for v in data["groups"].values()))
    feature_tests = set().union(
        *(set(data["groups"][g]) for g in ("creation", "projects", "editor"))
    )
    return (
        classified == discovered(root) and set(data["groups"]["smoke"]) <= feature_tests
    )


def select_groups(paths, root=ROOT):
    """Caller passes only paths that selected UI coverage, including deleted paths."""
    try:
        if not inventory_matches(root):
            return "full", "UI inventory changed or contains unclassified tests."
        sources = manifest()["sources"]
        if not paths or any(
            path not in sources or not (root / path).is_file() for path in paths
        ):
            return "full", "Shared, deleted, or unmapped UI input changed."
        groups = {sources[path] for path in paths}
        if len(groups) != 1:
            return "full", "Multiple native feature groups changed."
        group = groups.pop()
        return f"smoke,{group}", f"Smoke plus affected {group} UI tests."
    except (OSError, ValueError, KeyError, TypeError):
        return "full", "UI manifest unavailable or invalid."


def expected_tests(value, root=ROOT):
    validate_groups(value)
    if value == "none":
        raise ValueError("UI execution cannot select none")
    if value == "full":
        return discovered(root)
    if not inventory_matches(root):
        raise ValueError("UI inventory changed; run full coverage")
    data = manifest()
    return set().union(*(set(data["groups"][g]) for g in value.split(",")))


def result_tests(report):
    found = {}

    def visit(node, bundle=""):
        if node.get("nodeType") in ("UI test bundle", "Unit test bundle"):
            bundle = node["name"].removesuffix(".xctest")
        if node.get("nodeType") == "Test Case":
            identifier = node.get("nodeIdentifier", "").removesuffix("()")
            if not identifier.startswith(bundle + "/"):
                identifier = bundle + "/" + identifier
            found[identifier] = node.get("result")
        for child in node.get("children", []):
            visit(child, bundle)

    for node in report["testNodes"]:
        visit(node)
    return found


def verify_results(report, expected):
    actual = result_tests(report)
    missing = expected - actual.keys()
    unsuccessful = {
        test for test in expected & actual.keys() if actual[test] != "Passed"
    }
    unexpected = {test for test in actual if test.startswith("KriaUITests/")} - expected
    if missing or unsuccessful or unexpected:
        raise ValueError(
            f"UI coverage mismatch: missing={sorted(missing)}, "
            f"not passed={sorted(unsuccessful)}, unexpected={sorted(unexpected)}"
        )
    return f"Verified {len(expected)} selected UI tests passed."


def main():
    command, value = sys.argv[1:3]
    expected = expected_tests(value)
    if command == "args":
        if value == "full":
            print("-only-testing:KriaUITests")
        else:
            print("\n".join(f"-only-testing:{test}" for test in sorted(expected)))
    elif command == "verify":
        result = subprocess.check_output(
            [
                "xcrun",
                "xcresulttool",
                "get",
                "test-results",
                "tests",
                "--path",
                sys.argv[3],
                "--compact",
            ]
        )
        report = json.loads(result)
        Path(sys.argv[3] + ".tests.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(verify_results(report, expected))
    else:
        raise ValueError("Expected args or verify")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from error
