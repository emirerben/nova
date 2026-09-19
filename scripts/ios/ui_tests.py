#!/usr/bin/env python3
"""Conservative UI grouping and verification of actual XCTest result coverage."""

import json
import os
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


def validate_execution(value):
    """Selector output plus "smoke": the PR tripwire when the selector wants full."""
    if value != "smoke":
        validate_groups(value)
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
    validate_execution(value)
    if value == "none":
        raise ValueError("UI execution cannot select none")
    if value == "full":
        return discovered(root)
    if not inventory_matches(root):
        raise ValueError("UI inventory changed; run full coverage")
    data = manifest()
    return set().union(*(set(data["groups"][g]) for g in value.split(",")))


def _iter_test_cases(report):
    """Yield (identifier, node) for every Test Case, bundle-qualified like result_tests."""

    def visit(node, bundle=""):
        if node.get("nodeType") in ("UI test bundle", "Unit test bundle"):
            bundle = node["name"].removesuffix(".xctest")
        if node.get("nodeType") == "Test Case":
            identifier = node.get("nodeIdentifier", "").removesuffix("()")
            if not identifier.startswith(bundle + "/"):
                identifier = bundle + "/" + identifier
            yield identifier, node
        for child in node.get("children", []):
            yield from visit(child, bundle)

    for node in report["testNodes"]:
        yield from visit(node)


def result_tests(report):
    return {
        identifier: node.get("result") for identifier, node in _iter_test_cases(report)
    }


def flaky_tests(report):
    """Test Cases whose rollup passed but needed at least one retry.

    With -retry-tests-on-failure, a Test Case that eventually passed still
    rolls up to "Passed"; retries are otherwise invisible to verify_results.
    Attempts appear as ordered "Repetition" children ("First Run", "Retry N"),
    each with its own result; a first-try pass has no Repetition children.
    """
    flaky = []
    for identifier, node in _iter_test_cases(report):
        if node.get("result") != "Passed":
            continue
        repetitions = [
            child
            for child in node.get("children", [])
            if child.get("nodeType") == "Repetition"
        ]
        if len(repetitions) <= 1 and all(
            rep.get("result") == "Passed" for rep in repetitions
        ):
            continue
        messages = [
            message.get("name")
            for rep in repetitions
            if rep.get("result") != "Passed"
            for message in rep.get("children", [])
            if message.get("nodeType") == "Failure Message"
        ]
        flaky.append(
            {
                "identifier": identifier,
                "attempts": len(repetitions) or 1,
                "failure_messages": messages,
            }
        )
    return flaky


def report_flaky(flaky, bundle_path):
    """Surface pass-on-retry tests without ever turning a green verify red.

    Writes flaky-tests.json next to the xcresult bundle (always, even when
    empty), prints a warning line plus a GitHub annotation per flaky test, and
    appends a job-summary section when GITHUB_STEP_SUMMARY is set. Report I/O
    failures are swallowed: they must never fail an otherwise passing verify.
    """
    try:
        report_path = bundle_path.parent / "flaky-tests.json"
        report_path.write_text(json.dumps(flaky, indent=2) + "\n")
        for test in flaky:
            print(
                f"Flaky (passed on retry): {test['identifier']} "
                f"(passed after {test['attempts']} attempts)"
            )
            print(
                f"::warning title=Flaky UI test::{test['identifier']} passed "
                f"only after {test['attempts']} attempts"
            )
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if flaky and summary_path:
            with open(summary_path, "a") as summary:
                summary.write("## Flaky UI tests (passed on retry)\n\n")
                for test in flaky:
                    message = (
                        test["failure_messages"][0]
                        if test["failure_messages"]
                        else "(no failure message captured)"
                    )
                    summary.write(
                        f"- `{test['identifier']}`: passed after "
                        f"{test['attempts']} attempts. First failure: {message}\n"
                    )
                summary.write(
                    "\nRetries keep main green, but each entry above still "
                    "needs its own Linear issue. See "
                    "docs/runbooks/ios-development.md.\n"
                )
    except OSError as error:
        print(f"Note: failed to write flaky-test report: {error}")


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
        bundle_path = Path(sys.argv[3])
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
        # Compute and report flaky tests before verify_results can raise on a
        # coverage mismatch, so the report exists on red runs too.
        report_flaky(flaky_tests(report), bundle_path)
        print(verify_results(report, expected))
    else:
        raise ValueError("Expected args or verify")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from error
