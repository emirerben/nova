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
    r"(?<![\w/.-])(?:src/apps/api/)?tests/[\w/.-]+/?test_\w+\.py"
)


def split_tests(value) -> list[str]:
    return value.split() if isinstance(value, str) else list(value or [])


def marked_files(root: Path = ROOT):
    """Every (file relative to the API root, journey id, step name) declared by a marker.

    Raises ValueError for a marker this scanner cannot read, so a declaration
    can never be dropped silently.
    """
    api = root / API
    found = []
    for path in sorted((api / "tests").rglob("*.py")):
        text = path.read_text()
        if "mark.journey" not in text:
            continue
        rel = path.relative_to(api).as_posix()
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            raise ValueError(f"{rel}: cannot parse to read its journey marker: {error}")
        for node in tree.body:
            if not (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "pytestmark"
                    for t in node.targets
                )
            ):
                continue
            for call in ast.walk(node.value):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "journey"
                    and isinstance(call.func.value, ast.Attribute)
                    and call.func.value.attr == "mark"
                ):
                    continue
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


def test_def_exists(path: Path, name: str) -> bool:
    return bool(
        re.search(
            rf"^\s*(?:async\s+)?def {re.escape(name.split('[')[0])}\(",
            path.read_text(),
            re.M,
        )
    )


def check(root: Path = ROOT, manifest: Path = MANIFEST) -> list[str]:
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
            problems.append(f"{API}/{file} declares unknown journey {journey_id!r}")
    declared_steps = {step for _, _, step in marks}
    run_steps = workflow_steps(root)
    for step in sorted(run_steps - declared_steps):
        problems.append(f"ci.yml runs journey step {step!r} but no test declares it")
    for step in sorted(declared_steps - run_steps):
        problems.append(
            f"journey step {step!r} is declared by "
            f"{sorted(f for f, _, s in marks if s == step)} but ci.yml never runs it"
        )
    literal = LITERAL_TEST_PATH.findall((root / ".github/workflows/ci.yml").read_text())
    if literal:
        problems.append(
            f"ci.yml lists test paths by hand ({sorted(set(literal))[:3]}...); declare "
            'pytestmark = pytest.mark.journey("<id>") in the test and call '
            "journey_registry.py step <name>"
        )

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
            if not (root / path).exists():
                problems.append(f"{row['id']}: path {path!r} does not exist")
    if problems:
        problems.append(
            f"Fix: run `{FIX}` to follow git renames into scripts/ci/journey-manifest.json; "
            "delete an entry whose file was removed"
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


def sync(
    root: Path = ROOT, manifest: Path = MANIFEST, base: str | None = None
) -> list[str]:
    """Rewrite manifest entries whose file git renamed; return what was changed."""
    base = base or git(root, "merge-base", "HEAD", "origin/main").strip()
    moved = renames(root, base)
    text = manifest.read_text()
    changed = []
    for old, new in sorted(moved.items()):
        pairs = [(old, new)]
        if old.startswith(f"{API}/") and new.startswith(f"{API}/"):
            pairs.append((old.removeprefix(f"{API}/"), new.removeprefix(f"{API}/")))
        for before, after in pairs:
            pattern = re.compile(rf'(?<=[\s"]){re.escape(before)}(?=[\s":])')
            text, count = pattern.subn(after, text)
            if count:
                changed.append(f"{before} -> {after} ({count})")
    manifest.write_text(text)
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
                else "journey manifest already follows git renames"
            )
            problems = check()
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
