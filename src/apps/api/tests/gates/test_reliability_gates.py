"""The reliability-gate manifest must point at real code, real tests and real flags.

KRI-577: October's incidents were fixed one symptom at a time, and several fixes were
verified only by tests that faked an internal boundary. This manifest is the shared
ledger of those gates. These tests keep it honest: every path and test it names exists,
every fix commit is on this branch, every kill switch is a real setting, and the CI
journey (scripts/ci/journey-manifest.json) carries exactly the union the manifest
derives, so a gate cannot be added without CI running it for its paths.
"""

from __future__ import annotations

import ast
import json
import subprocess

import pytest

from app.config import Settings
from tests.gates.manifest import API_ROOT, REPO_ROOT, ci_union, load_manifest

MANIFEST = load_manifest()
MERGED = [gate for gate in MANIFEST.gates if gate.status == "merged"]
JOURNEY_ID = "kri-577-reliability-gates"


def _gates(gates):
    return [pytest.param(gate, id=gate.id) for gate in gates]


def test_manifest_covers_every_boundary_class_from_the_ticket() -> None:
    boundaries = {gate.boundary for gate in MANIFEST.gates}
    for wanted in ("parser", "dispatch", "publication", "reply", "persistence", "model_budget"):
        assert wanted in boundaries, wanted
    assert len(MANIFEST.gates) >= 20
    assert any(gate.status == "gap" for gate in MANIFEST.gates), "open gaps stay listed"


@pytest.mark.parametrize("gate", _gates(MANIFEST.gates))
def test_paths_and_tests_exist(gate) -> None:
    for path in gate.paths:
        assert (REPO_ROOT / path).is_file(), f"{gate.id}: missing app path {path}"
    for test in gate.regression_tests:
        file_part, _, node = test.partition("::")
        target = API_ROOT / file_part
        assert target.is_file(), f"{gate.id}: missing test file {file_part}"
        if node:
            names = {
                item.name
                for item in ast.walk(ast.parse(target.read_text(encoding="utf-8")))
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
            assert node.split("::")[-1] in names, f"{gate.id}: {test} names no such test"


@pytest.mark.parametrize("gate", _gates([g for g in MANIFEST.gates if g.kill_switch]))
def test_kill_switches_are_real_settings(gate) -> None:
    assert gate.kill_switch in Settings.model_fields, f"{gate.id}: {gate.kill_switch}"
    assert Settings.model_fields[gate.kill_switch].annotation is bool


def _is_ancestor(commit: str) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode in (0, 1):
        return result.returncode == 0
    return None  # not a git checkout (e.g. a source tarball)


@pytest.mark.parametrize("gate", _gates(MERGED))
def test_fix_commits_are_on_this_branch(gate) -> None:
    ancestor = _is_ancestor(gate.fix_commit)
    if ancestor is None:
        pytest.skip("git history unavailable")
    assert ancestor, f"{gate.id}: fix commit {gate.fix_commit} is not an ancestor of HEAD"


def test_ci_journey_carries_the_manifest_union() -> None:
    manifest = json.loads(
        (REPO_ROOT / "scripts/ci/journey-manifest.json").read_text(encoding="utf-8")
    )
    journeys = {journey["id"]: journey for journey in manifest["journeys"]}
    assert JOURNEY_ID in journeys, "the kri-577 journey is missing from scripts/ci"
    journey = journeys[JOURNEY_ID]
    paths, tests = ci_union(MANIFEST)
    assert sorted(journey["paths"]) == paths, (
        "journey paths drifted from the gate manifest; "
        "run: python3 scripts/reliability-gates.py journey"
    )
    assert journey["api_tests"].split() == tests, (
        "journey api_tests drifted from the gate manifest; "
        "run: python3 scripts/reliability-gates.py journey"
    )


def test_gates_owned_by_another_journey_name_an_existing_one() -> None:
    manifest = json.loads(
        (REPO_ROOT / "scripts/ci/journey-manifest.json").read_text(encoding="utf-8")
    )
    ids = {journey["id"] for journey in manifest["journeys"]}
    for gate in MANIFEST.gates:
        if gate.journey:
            assert gate.journey in ids, f"{gate.id}: journey {gate.journey} does not exist"


def test_runner_cli_agrees_with_the_loader() -> None:
    """The stdlib runner must select the same tests the pydantic loader would."""
    result = subprocess.run(
        ["python3", str(REPO_ROOT / "scripts/reliability-gates.py"), "tests", "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ci_union(MANIFEST)[1]
