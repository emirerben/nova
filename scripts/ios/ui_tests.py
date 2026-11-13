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
DURATIONS = Path(__file__).with_name("ui-test-measured-durations.json")
LEGACY_DURATIONS = Path(__file__).with_name("ui-test-durations.json")
# Each extra native leg repeats ~7 minutes of setup and compilation, so a
# selection splits only when one leg would run more than this much recorded UI
# time. Nightly/manual full regression gets up to MAX_SHARDS legs; PRs up to PR_MAX_SHARDS.
SHARD_TARGET_SECONDS = 15 * 60
# Bound fan-out. ios.yml must define a matrix for every count up to this one;
# ci.yml gives each PR shard its own `ui-<i>` leg next to the unit leg.
# The full suite records ~261 min of UI time: three legs ran ~90 min each and the
# nightly shards were cancelled at the 75-minute job limit every night (KRI-1379,
# e.g. run 43009921809). Five legs are ~54 min of UI + ~9 min of setup each.
MAX_SHARDS = 5
# Pull requests keep the earlier cap: three Macs per PR already saturated the
# account's five concurrent macOS runners (docs/runbooks/change-based-ci.md), so
# a five-leg PR would queue behind itself and block main's release lane. Only
# nightly/manual full regression may use MAX_SHARDS.
PR_MAX_SHARDS = 3
# A PR UI leg runs only its shard after a cached compile (~8-12 min of setup),
# so it targets more recorded time per Mac than nightly. An editor PR's group
# records 88.6 min (it ran 87 min on #2225): three legs of ~30 min finish in
# ~40-50 min next to a 19-40 min unit leg, instead of 2+ hours on one Mac.
# Short selections (smoke, projects: 10 min) stay on the unit leg's Mac.
PR_SHARD_TARGET_SECONDS = 35 * 60
# Shard 1 of ios.yml also runs the unit suite; PR UI legs pass 0.
UNIT_OVERHEAD_SECONDS = 7 * 60
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


def manifest(root=ROOT):
    """Groups derived from the UI test sources, plus the hand-kept inputs.

    The file holds only what source cannot say: each XCTestCase class's feature
    group (`classes`), the few tests that differ from it (`overrides`), the
    smoke subset, and which app sources map to a group. A test method added,
    renamed or removed inside a classified class needs no edit. A class absent
    from `classes` is not silently skipped: its tests join smoke, which every
    selection runs, until `make sync-test-registries` reports it to classify.
    """
    data = json.loads(MANIFEST.read_text())
    classes, overrides = data["classes"], data["overrides"]
    inventory = discovered(root)
    names = {test.removeprefix("KriaUITests/") for test in inventory}
    if (
        not set(classes.values()) <= set(FEATURES)
        or not set(overrides.values()) <= set(FEATURES)
        or not set(overrides) <= names
        or not set(data["smoke"]) <= inventory
        or len(data["smoke"]) != len(set(data["smoke"]))
    ):
        raise ValueError("Invalid UI group manifest")
    if not set(data["sources"].values()) <= set(FEATURES):
        raise ValueError("Invalid source mapping")
    groups = {group: [] for group in FEATURES}
    unclassified = []
    for test in sorted(inventory):
        name = test.removeprefix("KriaUITests/")
        group = overrides.get(name) or classes.get(name.split("/")[0])
        (groups[group] if group else unclassified).append(test)
    for group, tests in groups.items():
        if not tests:
            raise ValueError(f"Empty or invalid UI group: {group}")
    groups["smoke"] = sorted(data["smoke"]) + unclassified
    return {
        "groups": groups,
        "sources": data["sources"],
        "unclassified": sorted({test.split("/")[1] for test in unclassified}),
    }


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


def stale_entries(root=ROOT):
    """Hand-kept entries that name a class, test or source that no longer exists."""
    data = json.loads(MANIFEST.read_text())
    inventory = discovered(root)
    names = {test.removeprefix("KriaUITests/") for test in inventory}
    classes = {name.split("/")[0] for name in names}
    return {
        "classes": sorted(set(data["classes"]) - classes),
        "overrides": sorted(set(data["overrides"]) - names),
        "smoke": sorted(set(data["smoke"]) - inventory),
        "sources": sorted(path for path in data["sources"] if not (root / path).is_file()),
    }


def check(root=ROOT):
    """Problems that would make UI selection wrong or fall back to full, each with its fix."""
    problems = []
    try:
        manifest(root)
        stale = stale_entries(root)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"UI registry unreadable: {error}"]
    for kind, names in stale.items():
        for name in names:
            problems.append(f"ui-test-groups.json {kind} entry {name!r} no longer exists")
    if problems:
        problems.append(
            "Fix: run `make sync-test-registries` to follow renamed UI test files; "
            "delete an entry whose test was removed"
        )
    return problems


def unclassified(root=ROOT):
    return manifest(root)["unclassified"]


def inventory_matches(root=ROOT):
    """Whether the registry still describes this tree well enough to select from."""
    try:
        manifest(root)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def git_renames(base, root=ROOT):
    raw = subprocess.run(
        ["git", "diff", "-M", "--name-status", "-z", base],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode().split("\0")
    renames, index = {}, 0
    while index < len(raw) and raw[index]:
        if raw[index].startswith("R"):
            renames[raw[index + 1]] = raw[index + 2]
            index += 3
        else:
            index += 2
    return renames


def sync(root=ROOT, base=None):
    """Follow git renames into the hand-kept entries; return what changed."""
    if base is None:
        base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    data = json.loads(MANIFEST.read_text())
    changed = []
    for old, new in sorted(git_renames(base, root).items()):
        if old in data["sources"] and new not in data["sources"]:
            data["sources"][new] = data["sources"].pop(old)
            changed.append(f"{old} -> {new}")
        if not (old.startswith(UI_TEST_ROOT) and new.startswith(UI_TEST_ROOT)):
            continue
        before = subprocess.run(
            ["git", "show", f"{base}:{old}"], cwd=root, capture_output=True, text=True
        ).stdout
        before_classes = re.findall(r"\bclass\s+(\w+)\s*:\s*XCTestCase\b", before)
        after_classes = re.findall(
            r"\bclass\s+(\w+)\s*:\s*XCTestCase\b", (root / new).read_text()
        )
        if (
            len(before_classes) == len(after_classes) == 1
            and before_classes != after_classes
            and before_classes[0] in data["classes"]
        ):
            old_class, new_class = before_classes[0], after_classes[0]
            data["classes"][new_class] = data["classes"].pop(old_class)
            for table in ("overrides",):
                data[table] = {
                    (new_class + key[len(old_class) :] if key.startswith(old_class + "/") else key): value
                    for key, value in data[table].items()
                }
            data["smoke"] = [
                test.replace(f"KriaUITests/{old_class}/", f"KriaUITests/{new_class}/")
                for test in data["smoke"]
            ]
            changed.append(f"class {old_class} -> {new_class}")
    data["classes"] = dict(sorted(data["classes"].items()))
    MANIFEST.write_text(json.dumps(data, indent=2) + "\n")
    return changed


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


def file_group(path, data, root=ROOT):
    """Feature group owning every test of a self-contained UI test file, else None.

    The file must hold one XCTestCase class and nothing at file scope but
    imports, that class, its extensions and private declarations: a shared
    helper (a struct or enum other files call) can change tests outside the
    file, so it keeps an explicit mapping or selects full.
    """
    if not path.startswith(UI_TEST_ROOT):
        return None
    source = (root / path).read_text()
    classes = re.findall(r"\bclass\s+(\w+)\s*:\s*XCTestCase\b", source)
    methods = re.findall(r"\bfunc\s+(test\w+)\s*\(", source)
    if len(classes) != 1 or not methods:
        return None
    own = re.compile(
        r"(?:import\s|//|/\*|\*|\}|#(?:if|elseif|else|endif)\b|@\w+(?:\([^)]*\))?\s*$"
        r"|(?:private|fileprivate)\s"
        r"|(?:@\w+(?:\([^)]*\))?\s+)*(?:final\s+)?"
        rf"(?:class\s+{classes[0]}\s*:\s*XCTestCase\b|extension\s+{classes[0]}\b))"
    )
    if any(
        re.match(r"\S", line) and not own.match(line) for line in source.splitlines()
    ):
        return None
    tests = {f"KriaUITests/{classes[0]}/{name}" for name in methods}
    groups = {group for group in FEATURES if tests & set(data["groups"][group])}
    return groups.pop() if len(groups) == 1 else None


def select_groups(paths, root=ROOT, focused=None):
    """Caller passes only paths that selected UI coverage, including deleted paths.

    `focused` optionally maps a changed UI test source to the test methods its
    diff touched (see changed_tests); such a file selects only those tests.
    """
    try:
        data = manifest(root)
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
            elif group := file_group(path, data, root):
                groups.add(group)
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
    data = manifest(root)
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
            if identifier.count("/") == 1 or not identifier.startswith(bundle + "/"):
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


TIME_ALLOWANCE_FAILURE = "exceeded execution time allowance"


def _failure_messages(node):
    """A Test Case's failure messages, whether xcodebuild retried it (Repetition children) or not."""
    messages = []
    for child in node.get("children", []):
        if child.get("nodeType") == "Failure Message":
            messages.append(child.get("name", ""))
        elif child.get("nodeType") == "Repetition" and child.get("result") != "Passed":
            messages += [
                message.get("name", "")
                for message in child.get("children", [])
                if message.get("nodeType") == "Failure Message"
            ]
    return messages


def overrun_tests(report):
    """Test Cases that failed only by running past the execution time allowance.

    xcodebuild restarts the runner and never retries such a test, so it gets one
    attempt where -retry-tests-on-failure gives every other failure three.
    verify.sh reruns exactly these once (KRI-1379).
    """
    overruns = []
    for identifier, node in _iter_test_cases(report):
        messages = _failure_messages(node)
        if (
            node.get("result") != "Passed"
            and messages
            and all(TIME_ALLOWANCE_FAILURE in message for message in messages)
        ):
            overruns.append(identifier)
    return sorted(overruns)


def merged_results(report, rerun=None):
    """Results of the run, with each overrun replaced by its rerun.

    A rerun may only stand in for a test that ran past its allowance: anything
    else in it means the rerun selection was wrong, so it is refused.
    """
    results = result_tests(report)
    if rerun is None:
        return results
    rerun_results = result_tests(rerun)
    stray = rerun_results.keys() - set(overrun_tests(report))
    if stray:
        raise ValueError(
            f"Rerun ran tests that did not run past the time allowance: {sorted(stray)}"
        )
    return results | rerun_results


def flaky_tests(report, rerun=None):
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
    if rerun is not None:
        overruns = set(overrun_tests(report))
        first_run = dict(_iter_test_cases(report))
        for identifier, node in _iter_test_cases(rerun):
            if identifier not in overruns or node.get("result") != "Passed":
                continue
            repetitions = [
                child
                for child in node.get("children", [])
                if child.get("nodeType") == "Repetition"
            ]
            flaky.append(
                {
                    "identifier": identifier,
                    "attempts": 1 + (len(repetitions) or 1),
                    "failure_messages": _failure_messages(first_run[identifier])
                    + _failure_messages(node),
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


def verify_results(report, expected, rerun=None):
    actual = merged_results(report, rerun)
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


def load_durations():
    """Keep new feature timings while applying the bulk measured baseline."""
    durations = {}
    for path in (LEGACY_DURATIONS, DURATIONS):
        if path.exists():
            durations.update(json.loads(path.read_text()))
    return durations


def weights(tests, durations=None):
    """Recorded seconds per test; tests without a recorded duration weigh the median."""
    if durations is None:
        durations = load_durations()
    known = sorted(float(value) for value in durations.values())
    default = known[len(known) // 2] if known else 30.0
    return {test: float(durations.get(test, default)) for test in tests}


def shard_count(
    value,
    root=ROOT,
    durations=None,
    target=SHARD_TARGET_SECONDS,
    max_shards=MAX_SHARDS,
):
    """Native legs for an executed selection: one per `target` seconds of
    recorded UI time, at most `max_shards` and never more legs than tests."""
    tests = expected_tests(value, root)
    estimate = sum(weights(tests, durations).values())
    return max(1, min(max_shards, len(tests), math.ceil(estimate / target)))


def shard_tests(tests, spec, durations=None, overhead=UNIT_OVERHEAD_SECONDS):
    """This shard's part of a deterministic, duration-balanced partition.

    Every shard computes the same partition from the same checkout, so the
    shards are disjoint and their union is exactly `tests`. Longest tests are
    placed first into the least-loaded shard, so new tests still spread out
    evenly. An empty part is an error: it would verify vacuously.
    """
    index, count = parse_shard(spec)
    weight = weights(tests, durations)
    loads, members = [0.0] * count, [[] for _ in range(count)]
    # `overhead` is the unit suite shard 1 also runs. Small focused selections
    # keep their old partition: it would otherwise consume their UI budget.
    if count > 1 and sum(weight.values()) > count * overhead:
        loads[0] = overhead
    for test in sorted(tests, key=lambda test: (-weight[test], test)):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        loads[target] += weight[test]
        members[target].append(test)
    if any(not part for part in members):
        raise ValueError(f"UI shard {spec} selects no tests")
    return set(members[index - 1])


def load_report(bundle):
    result = subprocess.check_output(
        [
            "xcrun",
            "xcresulttool",
            "get",
            "test-results",
            "tests",
            "--path",
            bundle,
            "--compact",
        ]
    )
    report = json.loads(result)
    Path(bundle + ".tests.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    if sys.argv[1:2] in (["check"], ["sync"]):
        problems = []
        if sys.argv[1] == "sync":
            print("\n".join(sync()) or "UI registry already follows git renames")
        problems = check()
        for name in unclassified() if not problems else ():
            print(
                f"UI test class {name} is not in ui-test-groups.json classes: its tests "
                "run in every selection until you add it with a creation, projects or "
                "editor group"
            )
        for problem in problems:
            print(f"ui registry: {problem}", file=sys.stderr)
        raise SystemExit(1 if problems else 0)
    command, value = sys.argv[1:3]
    expected = expected_tests(value)
    # Long selections (main's full suite, an editor PR's group) split across
    # Macs; each shard runs and verifies exactly its part.
    shard = os.environ.get("KRIA_IOS_UI_SHARD", "")
    if shard:
        overhead = os.environ.get("KRIA_IOS_UI_SHARD_OVERHEAD_SECONDS", "")
        expected = shard_tests(
            expected,
            shard,
            overhead=int(overhead) if overhead else UNIT_OVERHEAD_SECONDS,
        )
    if command == "args":
        if value == "full" and not shard:
            print("-only-testing:KriaUITests")
        else:
            print("\n".join(f"-only-testing:{test}" for test in sorted(expected)))
    elif command == "overruns":
        report = load_report(sys.argv[3])
        print(
            "\n".join(
                f"-only-testing:{test}"
                for test in overrun_tests(report)
                if test in expected
            )
        )
    elif command == "verify":
        bundle_path = Path(sys.argv[3])
        report = load_report(sys.argv[3])
        rerun = load_report(sys.argv[4]) if len(sys.argv) > 4 else None
        # Compute and report flaky tests before verify_results can raise on a
        # coverage mismatch, so the report exists on red runs too.
        report_flaky(flaky_tests(report, rerun), bundle_path)
        print(verify_results(report, expected, rerun))
    else:
        raise ValueError("Expected args, overruns or verify")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from error
