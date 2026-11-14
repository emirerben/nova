#!/usr/bin/env python3
"""Journey test registry derived from markers in the tests (stdlib only).

A test file joins a journey, and the CI step that runs it, by declaring
`pytestmark = pytest.mark.journey("<journey-id>")` (optionally `step="<name>"`
when one journey has more than one CI step). ci.yml asks this module for a
step's files instead of listing paths, so a new test needs no registry edit and
a renamed test keeps its declaration.

The hand-written part of scripts/ci/journey-manifest.json (source paths and
evidence tests that carry no marker) is validated in both directions by
`check`; `sync` follows git renames into it.

    journey_registry.py step NAME [--repo-relative]
    journey_registry.py tests IDS
    journey_registry.py check
    journey_registry.py sync [--base REV]
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
API = "src/apps/api"
MANIFEST = Path(__file__).with_name("journey-manifest.json")
FIX = "make sync-test-registries"
STEP_CALL = re.compile(r"journey_registry\.py\s+step\s+([\w.-]+)")
LITERAL_TEST_PATH = re.compile(
    r"(?<![\w/.-])(?:src/apps/api/)?tests/(?:[\w.-]+/)*test_\w+\.py"
)
TEST_REFERENCE = re.compile(
    r"(?<![\w/.-])((?:src/apps/api/)?tests/[\w/.-]+\.py)((?:::\w+)*)"
)
SWIFT_TESTS = "src/apps/ios/Tests"
SWIFT_PACKAGE_TESTS = "src/apps/ios/Packages/*/Tests"
ONLY_TESTING = re.compile(r"-only-testing:(\w+)/(\w+)(?:/(test\w+))?")
XCTEST_ARG = re.compile(r"--xctest\s+(\w+)/(test\w+)")
TESTS_CALL = re.compile(r"journey_registry(?:\.py\s+tests\b|\.api_tests\b)")
# Files that name tests by path but are not derived from markers. `check` fails
# when one names a test that no longer exists; `sync` follows git renames in.
HAND_LISTS = (
    "Makefile",
    "scripts/*.sh",
    "scripts/*.py",
    "scripts/ci/select-tests.py",
    "scripts/ios/*.py",
    "scripts/kria/*.py",
    "formal/kria_turns/verify.py",
    ".github/workflows/*.yml",
    "src/apps/api/tests/gates/reliability-gates.json",
)
RENAME_FOLLOWERS = (*HAND_LISTS, "scripts/ci/test_*.py", "scripts/ios/tests/*.py")
NOT_REWRITTEN = ("scripts/ci/test_journey_registry.py",)


def split_tests(value) -> list[str]:
    return value.split() if isinstance(value, str) else list(value or [])


def is_journey_mark(node) -> bool:
    """`pytest.mark.journey` or `mark.journey`, however it is imported or aliased."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "journey"
        and (
            (isinstance(node.value, ast.Attribute) and node.value.attr == "mark")
            or (isinstance(node.value, ast.Name) and node.value.id == "mark")
        )
    )


def module_marks(tree: ast.Module):
    """Nodes inside a module-level `pytestmark = ...` or `pytestmark: T = ...`."""
    inside = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in names):
            inside.update(id(sub) for sub in ast.walk(value))
    return inside


def marked_files(root: Path = ROOT):
    """Every (file relative to the API root, journey id, step name) declared by a marker.

    pytest accepts a journey mark on a decorator, a class or a call spelled any
    way, but only a module-level `pytestmark` can name a CI step. Raises
    ValueError for any other spelling, so a declaration can never be dropped
    silently.
    """
    api = root / API
    found = []
    for path in sorted((api / "tests").rglob("*.py")):
        text = path.read_text()
        if "journey" not in text:
            continue
        rel = path.relative_to(api).as_posix()
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            raise ValueError(f"{rel}: cannot parse to read its journey marker: {error}")
        inside = module_marks(tree)
        called = {id(n.func): n for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node in ast.walk(tree):
            if not is_journey_mark(node):
                continue
            call = called.get(id(node))
            if id(node) not in inside or call is None:
                raise ValueError(
                    f"{rel}:{node.lineno}: declare the journey with a module-level "
                    'pytestmark = pytest.mark.journey("<id>", step="<name>"); a decorator, '
                    "a class attribute or a bare mark never reaches a CI step"
                )
            args = [a.value for a in call.args if isinstance(a, ast.Constant)]
            steps = {
                k.arg: k.value.value
                for k in call.keywords
                if isinstance(k.value, ast.Constant)
            }
            if (
                len(args) != 1
                or len(call.args) != 1
                or not isinstance(args[0], str)
                or set(steps) - {"step"}
                or len(steps) != len(call.keywords)
            ):
                raise ValueError(
                    f'{rel}:{call.lineno}: write the marker as pytest.mark.journey("<id>"'
                    ', step="<name>") with string literals'
                )
            found.append((rel, args[0], steps.get("step", args[0])))
    return found


def manifest_rows(manifest: Path = MANIFEST) -> list[dict]:
    return json.loads(manifest.read_text())["journeys"]


def journeys(root: Path = ROOT, manifest: Path = MANIFEST) -> list[dict]:
    """Manifest rows plus the tests the markers declare.

    Trigger `paths` stay exactly as written: deriving them from markers would
    make edits to ~50 more test files select journeys (and Mac legs).
    """
    rows = manifest_rows(manifest)
    by_id = {row["id"]: row for row in rows}
    declared = {}
    for file, journey_id, _ in marked_files(root):
        if journey_id not in by_id:
            raise ValueError(
                f"{file} declares unknown journey {journey_id!r}; add the journey to "
                "scripts/ci/journey-manifest.json"
            )
        declared.setdefault(journey_id, set()).add(file)
    derived = []
    for row in rows:
        row = dict(row)
        tests = split_tests(row.get("api_tests"))
        tests += [
            file for file in sorted(declared.get(row["id"], ())) if file not in tests
        ]
        if tests:
            row["api_tests"] = " ".join(tests)
        derived.append(row)
    return derived


def step_files(step: str, root: Path = ROOT) -> list[str]:
    files = sorted({file for file, _, name in marked_files(root) if name == step})
    if not files:
        raise ValueError(f"no test declares journey step {step!r}")
    return files


def api_tests(journey_id: str, root: Path = ROOT) -> list[str]:
    rows = {row["id"]: row for row in journeys(root)}
    if journey_id not in rows:
        raise ValueError(f"unknown journey {journey_id!r}")
    return split_tests(rows[journey_id].get("api_tests"))


def workflow_steps(root: Path = ROOT) -> set[str]:
    return set(STEP_CALL.findall((root / ".github/workflows/ci.yml").read_text()))


def workflow_tests_ids(root: Path, known: set[str]) -> set[str]:
    """Journey ids named in or just above a `tests` call: those steps run the
    marked files through `api_tests` instead of a `step` call."""
    lines = (root / ".github/workflows/ci.yml").read_text().splitlines()
    ids = set()
    for index, line in enumerate(lines):
        if TESTS_CALL.search(line):
            window = "\n".join(lines[max(0, index - 3) : index + 1])
            ids.update(
                token for token in re.findall(r"[\w.-]+", window) if token in known
            )
    return ids


def path_exists(root: Path, path: str) -> bool:
    """A trigger path is a file, a directory or a filename prefix some file starts with."""
    target = root / path
    return target.exists() or (
        target.parent.is_dir()
        and any(child.name.startswith(target.name) for child in target.parent.iterdir())
    )


def test_def_exists(path: Path, name: str) -> bool:
    return bool(
        re.search(
            rf"^\s*(?:async\s+)?def {re.escape(name.split('[')[0])}\(",
            path.read_text(),
            re.M,
        )
    )


def follower_files(root: Path) -> list[Path]:
    """Files whose literal test paths sync rewrites after a git rename."""
    files = {
        path
        for pattern in RENAME_FOLLOWERS
        for path in root.glob(pattern)
        if path.is_file() and path.relative_to(root).as_posix() not in NOT_REWRITTEN
    }
    return sorted(files)


def swift_test_ids(root: Path) -> tuple[set[str], set[str]]:
    """(`Target/Class/test`, `Target/Class`) for every XCTest the iOS sources declare.

    The target is the directory under a `Tests` folder, so `KriaTests` and
    `KriaUITests` stay apart. A test belongs to a class declared above it in its
    file, never to one declared below.
    """
    tests, classes = set(), set()
    roots = [root / SWIFT_TESTS, *sorted(root.glob(SWIFT_PACKAGE_TESTS))]
    for tests_root in roots:
        for path in tests_root.rglob("*.swift"):
            parts = path.relative_to(tests_root).parts
            if len(parts) < 2:
                continue
            target, text = parts[0], path.read_text()
            declared = [
                (m.start(), m.group(1))
                for m in re.finditer(r"\b(?:class|extension|struct)\s+(\w+)", text)
            ]
            classes.update(f"{target}/{name}" for _, name in declared)
            for method in re.finditer(r"\bfunc\s+(test\w+)\s*\(", text):
                tests.update(
                    f"{target}/{name}/{method.group(1)}"
                    for start, name in declared
                    if start < method.start()
                )
    return tests, classes


def hand_list_problems(root: Path) -> list[str]:
    """Test paths named by hand in the Makefile, scripts and non-CI workflows."""
    problems = []
    api = root / API
    for path in follower_files(root):
        rel = path.relative_to(root).as_posix()
        if rel == ".github/workflows/ci.yml" or rel.startswith("scripts/ci/test_"):
            continue
        for match in TEST_REFERENCE.finditer(path.read_text()):
            file, nodes = match.groups()
            target = root / file if file.startswith(f"{API}/") else api / file
            if not target.exists():
                problems.append(f"{rel} names {file!r}, which does not exist")
            elif nodes and not test_def_exists(target, nodes.split("::")[-1]):
                problems.append(f"{rel} names {file}{nodes}, which has no such test")
    return problems


def stale_after_rename(root: Path, base: str | None, manifest: Path) -> list[str]:
    """Hand-kept files that still name a path git renamed since `base`."""
    try:
        base = base or git(root, "merge-base", "HEAD", "origin/main").strip()
        moved = renames(root, base)
    except (OSError, subprocess.CalledProcessError):
        return []
    problems = []
    for path in [manifest, *follower_files(root)]:
        if not path.exists():
            continue
        text = path.read_text()
        for old, new in sorted(moved.items()):
            variants = [(old, new)]
            if old.startswith(f"{API}/") and new.startswith(f"{API}/"):
                variants.append(
                    (old.removeprefix(f"{API}/"), new.removeprefix(f"{API}/"))
                )
            for before, after in variants:
                if rename_pattern(before).search(text):
                    problems.append(
                        f"{path.relative_to(root).as_posix()} still names {before!r}, "
                        f"which git renamed to {after!r}"
                    )
    return problems


def swift_problems(rows: list[dict], root: Path) -> list[str]:
    """Native tests named by the manifest and by ci.yml's native steps must exist."""
    if not (root / SWIFT_TESTS).is_dir():
        return []
    tests, classes = swift_test_ids(root)
    problems = []
    for row in rows:
        named = [
            *row.get("xctests", []),
            *([row["xctest"]] if row.get("xctest") else []),
        ]
        for test in named:
            if test.removesuffix("()") not in tests:
                problems.append(
                    f"{row['id']}: xctest {test!r} names no test under {SWIFT_TESTS}"
                )
    workflow = (root / ".github/workflows/ci.yml").read_text()
    by_class = {test.split("/", 1)[1] for test in tests}
    for target, name, method in ONLY_TESTING.findall(workflow):
        found = (
            f"{target}/{name}/{method}" in tests
            if method
            else f"{target}/{name}" in classes
        )
        if not found:
            problems.append(
                f"ci.yml -only-testing:{target}/{name}{'/' + method if method else ''} "
                "names no native test"
            )
    for name, method in XCTEST_ARG.findall(workflow):
        if f"{name}/{method}" not in by_class:
            problems.append(f"ci.yml --xctest {name}/{method} names no native test")
    return problems


def check(
    root: Path = ROOT,
    manifest: Path = MANIFEST,
    base: str | None = None,
    after_sync: bool = False,
) -> list[str]:
    """Every way the registries and the tests can disagree, named."""
    problems = []
    api = root / API
    try:
        marks = marked_files(root)
        rows = manifest_rows(manifest)
    except (OSError, ValueError) as error:
        return [str(error)]
    ids = {row["id"] for row in rows}

    for file, journey_id, _ in marks:
        if journey_id not in ids:
            problems.append(
                f"{API}/{file} declares unknown journey {journey_id!r}; add the journey "
                "to scripts/ci/journey-manifest.json or fix the id"
            )
    declared_steps = {step for _, _, step in marks}
    run_steps = workflow_steps(root)
    tests_ids = workflow_tests_ids(root, ids)
    for step in sorted(run_steps - declared_steps):
        problems.append(
            f"ci.yml runs journey step {step!r} but no test declares it; add "
            f'pytestmark = pytest.mark.journey("<id>", step="{step}") to its tests'
        )
    for step in sorted(declared_steps - run_steps - tests_ids):
        problems.append(
            f"journey step {step!r} is declared by "
            f"{sorted(f for f, _, s in marks if s == step)} but ci.yml never runs it; "
            f"add `journey_registry.py step {step}` or `journey_registry.py tests <id>` "
            "to a ci.yml step"
        )
    literal = LITERAL_TEST_PATH.findall((root / ".github/workflows/ci.yml").read_text())
    if literal:
        problems.append(
            f"ci.yml lists test paths by hand ({sorted(set(literal))[:3]}...); declare "
            'pytestmark = pytest.mark.journey("<id>") in the test and call '
            "journey_registry.py step <name>"
        )

    declaration_problems = len(problems)
    for row in rows:
        for entry in split_tests(row.get("api_tests")):
            file, _, node = entry.partition("::")
            target = api / file
            if file.startswith(f"{API}/") or not target.exists():
                hint = (
                    " (write it relative to src/apps/api)"
                    if file.startswith(f"{API}/")
                    else ""
                )
                problems.append(
                    f"{row['id']}: api_tests entry {entry!r} does not exist{hint}"
                )
            elif node and not test_def_exists(target, node):
                problems.append(
                    f"{row['id']}: api_tests node {entry!r} has no such test"
                )
        for path in row["paths"]:
            if not path_exists(root, path):
                problems.append(f"{row['id']}: path {path!r} matches no file")
    problems += swift_problems(rows, root)
    problems += hand_list_problems(root)
    problems += stale_after_rename(root, base, manifest)
    if len(problems) > declaration_problems:
        if after_sync:
            problems.append(
                f"`{FIX}` followed every git rename it could and these remain: edit or "
                "delete the named entry by hand in scripts/ci/journey-manifest.json or "
                "the named file (a removed file or test, or a rename sync cannot see)"
            )
        else:
            problems.append(
                f"Fix: run `{FIX}` to follow git renames (stage the rename first) into "
                "scripts/ci/journey-manifest.json, the Makefile, scripts and workflows; "
                "delete an entry whose file or test was removed"
            )
    return problems


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def renames(root: Path, base: str) -> dict[str, str]:
    """old path -> new path for renames between `base` and the working tree."""
    raw = git(root, "diff", "-M", "--name-status", "-z", base).split("\0")
    out, index = {}, 0
    while index < len(raw) and raw[index]:
        status = raw[index]
        if status.startswith("R"):
            out[raw[index + 1]] = raw[index + 2]
            index += 3
        else:
            index += 2
    return out


def rename_pattern(path: str) -> re.Pattern:
    return re.compile(rf"(?<![\w/.-]){re.escape(path)}(?![\w/.-])")


def sync(
    root: Path = ROOT, manifest: Path = MANIFEST, base: str | None = None
) -> list[str]:
    """Rewrite entries whose file git renamed; return what was changed."""
    base = base or git(root, "merge-base", "HEAD", "origin/main").strip()
    moved = renames(root, base)
    changed = []
    for path in [manifest, *follower_files(root)]:
        text = original = path.read_text()
        for old, new in sorted(moved.items()):
            pairs = [(old, new)]
            if old.startswith(f"{API}/") and new.startswith(f"{API}/"):
                pairs.append((old.removeprefix(f"{API}/"), new.removeprefix(f"{API}/")))
            for before, after in pairs:
                text, count = rename_pattern(before).subn(after, text)
                if count:
                    changed.append(
                        f"{path.relative_to(root).as_posix()}: {before} -> {after} ({count})"
                    )
        if text != original:
            path.write_text(text)
    return changed


def main(argv: list[str]) -> int:
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if command == "step" and rest:
            files = step_files(rest[0])
            prefix = f"{API}/" if "--repo-relative" in rest else ""
            print(" ".join(prefix + f for f in files))
        elif command == "tests" and len(rest) == 1:
            ids = rest[0].split(",")
            print(" ".join(dict.fromkeys(t for i in ids for t in api_tests(i))))
        elif command == "check":
            problems = check()
            for problem in problems:
                print(f"journey registry: {problem}", file=sys.stderr)
            return 1 if problems else 0
        elif command == "sync":
            base = rest[rest.index("--base") + 1] if "--base" in rest else None
            changed = sync(base=base)
            print(
                "\n".join(changed)
                if changed
                else "journey registries already follow git renames"
            )
            problems = check(after_sync=True)
            for problem in problems:
                print(f"journey registry: {problem}", file=sys.stderr)
            return 1 if problems else 0
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"journey registry: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
