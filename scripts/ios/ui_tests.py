#!/usr/bin/env python3
"""Conservative UI grouping and verification of actual XCTest result coverage."""

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("ui-test-groups.json")
# Median seconds per UI test on GitHub's macos-15 runners; sizes and balances shards.
DURATIONS = Path(__file__).with_name("ui-test-durations.json")
# Each extra native leg repeats ~7 minutes of setup and compilation, so a
# selection splits only when one leg would run more than this much recorded UI
# time: main's full suite gets three legs, an editor-touching PR two.
SHARD_TARGET_SECONDS = 15 * 60
# Main (three legs) plus one two-leg PR fit the account's five concurrent
# macOS runners. ios.yml must define a matrix for every count up to this one.
MAX_SHARDS = 3
FEATURES = ("creation", "projects", "editor")
FOCUSED = tuple(f"smoke,{group}" for group in FEATURES)
UI_TEST_ROOT = "src/apps/ios/Tests/KriaUITests/"
TEST_ID = re.compile(r"[A-Za-z_]\w*/test\w+")
# Members of the single XCTestCase class sit at exactly four spaces. Test bodies
# (including nested helpers) are indented further; file scope is at column 0.
TEST_METHOD = re.compile(r"    (?:@[\w.]+(?:\([^)]*\))?\s+)*func\s+(test\w+)\s*\(")
MEMBER = re.compile(
    r"    (?:@[\w.]+(?:\([^)]*\))?\s+)*"
    r"(?:(?:private|fileprivate|internal|public|open|override|static|final|class"
    r"|nonisolated|mutating|lazy|weak|convenience|required)\s+)*"
    r"(?:func|var|let|init|deinit|subscript|struct|enum|class|actor|typealias)\b"
)


def parse_selection(value):
    """Split a focused value into (feature group or None, changed test ids).

    Grammar: "smoke" [",<feature>"] {",<Class>/<testMethod>"}, with test ids
    sorted and unique so every selection has one canonical spelling.
    """
    tokens = value.split(",") if isinstance(value, str) else []
    if not tokens or tokens[0] != "smoke":
        raise ValueError(f"Invalid iOS UI groups: {value!r}")
    group = tokens[1] if len(tokens) > 1 and tokens[1] in FEATURES else None
    tests = tokens[2:] if group else tokens[1:]
    if any(not TEST_ID.fullmatch(test) for test in tests) or tests != sorted(
        set(tests)
    ):
        raise ValueError(f"Invalid iOS UI groups: {value!r}")
    return group, tuple(tests)


def validate_groups(value):
    """Selector output: none, full, smoke plus one group and/or changed tests."""
    if value in ("none", "full"):
        return value
    group, tests = parse_selection(value)
    if not group and not tests:
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


def changed_tests(source, lines):
    """Test methods ("Class/testName") containing every changed line, else None.

    `lines` are 1-based line numbers of `source` touched by the diff. Any change
    outside a test method body (imports, properties, setUp, private helpers, a
    comment at column 0, file-scope code) or an unfamiliar layout returns None,
    so the caller falls back to the file's whole feature group.
    """
    classes = re.findall(r"\bclass\s+(\w+)\s*:\s*XCTestCase\b", source)
    text = source.splitlines()
    methods = re.findall(r"\bfunc\s+(test\w+)\s*\(", source)
    if (
        len(classes) != 1
        or not lines
        or sum(1 for line in text if TEST_METHOD.match(line)) != len(methods)
    ):
        return None
    owners, owner = [], None
    for line in text:
        if re.match(r"\S", line):
            owner = None
        elif match := TEST_METHOD.match(line):
            owner = match.group(1)
        elif MEMBER.match(line):
            owner = ""
        owners.append(owner)
    tests = set()
    for number in lines:
        if not 1 <= number <= len(owners) or not owners[number - 1]:
            return None
        tests.add(f"{classes[0]}/{owners[number - 1]}")
    return tests


def select_groups(paths, root=ROOT, focused=None):
    """Caller passes only paths that selected UI coverage, including deleted paths.

    `focused` optionally maps a changed UI test source to the test methods its
    diff touched (see changed_tests); such a file selects only those tests.
    """
    try:
        if not inventory_matches(root):
            return "full", "UI inventory changed or contains unclassified tests."
        data = manifest()
        sources = data["sources"]
        inventory = discovered(root)
        if not paths:
            return "full", "Shared, deleted, or unmapped UI input changed."
        groups, tests = set(), set()
        for path in paths:
            changed = (focused or {}).get(path)
            if not (root / path).is_file():
                return "full", "Shared, deleted, or unmapped UI input changed."
            # Method-level focus needs no mapping: it names the tests it changed.
            if changed and {f"KriaUITests/{test}" for test in changed} <= inventory:
                tests |= changed
            elif path in sources:
                groups.add(sources[path])
            else:
                return "full", "Shared, deleted, or unmapped UI input changed."
        if len(groups) > 1:
            return "full", "Multiple native feature groups changed."
        group = groups.pop() if groups else None
        if group:
            covered = {
                test.removeprefix("KriaUITests/") for test in data["groups"][group]
            }
            tests -= covered
        value = ",".join(["smoke", *([group] if group else []), *sorted(tests)])
        reason = f"Smoke plus affected {group} UI tests" if group else "Smoke"
        if tests:
            reason += f" plus {len(tests)} changed UI test method(s)"
        return value, reason + "."
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
    group, tests = parse_selection(value)
    selected = {f"KriaUITests/{test}" for test in tests}
    unknown = selected - discovered(root)
    if unknown:
        raise ValueError(f"Unknown UI tests selected: {sorted(unknown)}")
    return (
        set(data["groups"]["smoke"])
        | (set(data["groups"][group]) if group else set())
        | selected
    )


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


def parse_shard(spec):
    match = re.fullmatch(r"([1-9])/([1-9])", spec or "")
    if not match or int(match.group(1)) > int(match.group(2)):
        raise ValueError(f"Invalid UI shard: {spec!r}")
    return int(match.group(1)), int(match.group(2))


def weights(tests, durations=None):
    """Recorded seconds per test; tests without a recorded duration weigh the median."""
    if durations is None:
        durations = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    known = sorted(float(value) for value in durations.values())
    default = known[len(known) // 2] if known else 30.0
    return {test: float(durations.get(test, default)) for test in tests}


def shard_count(value, root=ROOT, durations=None):
    """Native legs for an executed selection: one per SHARD_TARGET_SECONDS of
    recorded UI time, at most MAX_SHARDS and never more legs than tests."""
    tests = expected_tests(value, root)
    estimate = sum(weights(tests, durations).values())
    return max(1, min(MAX_SHARDS, len(tests), math.ceil(estimate / SHARD_TARGET_SECONDS)))


def shard_tests(tests, spec, durations=None):
    """This shard's part of a deterministic, duration-balanced partition.

    Every shard computes the same partition from the same checkout, so the
    shards are disjoint and their union is exactly `tests`. Longest tests are
    placed first into the least-loaded shard, so new tests still spread out
    evenly. An empty part is an error: it would verify vacuously.
    """
    index, count = parse_shard(spec)
    weight = weights(tests, durations)
    loads, members = [0.0] * count, [[] for _ in range(count)]
    for test in sorted(tests, key=lambda test: (-weight[test], test)):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        loads[target] += weight[test]
        members[target].append(test)
    if not members[index - 1]:
        raise ValueError(f"UI shard {spec} selects no tests")
    return set(members[index - 1])


def main():
    command, value = sys.argv[1:3]
    expected = expected_tests(value)
    # Long selections (main's full suite, an editor PR's group) split across
    # Macs; each shard runs and verifies exactly its part.
    shard = os.environ.get("KRIA_IOS_UI_SHARD", "")
    if shard:
        expected = shard_tests(expected, shard)
    if command == "args":
        if value == "full" and not shard:
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
