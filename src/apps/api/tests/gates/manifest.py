"""Schema and loader for ``tests/gates/reliability-gates.json`` (KRI-577).

A gate is one production incident mapped to the boundary it crossed, the fix that
closed it, the regression tests that pin it, how real that harness is, the evidence
level each fix reached, and the neighbouring cases still open. The JSON is the source
of truth; ``scripts/reliability-gates.py`` (stdlib only) reads the same file to select
and run gates for changed paths and to run the red control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GATES_PATH = Path(__file__).with_name("reliability-gates.json")
API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[2]

Boundary = Literal[
    "parser",
    "brief",
    "contract",
    "dispatch",
    "planner",
    "compiler",
    "verifier",
    "publication",
    "reply",
    "persistence",
    "model_budget",
    "device",
]
# How much real code the regression tests drive. ``mocked_internal`` means an internal
# business boundary (not just the provider transport) is faked, so the gate cannot
# catch a representation mismatch across that boundary (the KRI-454 class).
Harness = Literal[
    "real_pipeline",
    "real_compiler",
    "real_contract",
    "real_reply",
    "real_parser",
    "real_with_db",
    "mocked_provider",
    "mocked_internal",
    "pending",
]
Status = Literal["merged", "open_pr", "gap"]
RedControl = Literal["pending", "verified", "not_applicable", "weak"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    """One string per evidence level; ``unknown`` is an honest value, never a guess."""

    offline_replay: str
    live_model: str
    native_output: str
    production: str


class Gate(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    tickets: list[str] = Field(min_length=1)
    symptom: str = Field(min_length=10)
    boundary: Boundary
    status: Status = "merged"
    fix_prs: list[int] = []
    fix_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    owner: str | None = None
    gap_reason: str | None = None
    # An existing CI journey (scripts/ci/journey-manifest.json) that already owns this
    # gate's paths; such a gate is not folded into the kri-577 journey union.
    journey: str | None = None
    paths: list[str] = []
    regression_tests: list[str] = []
    harness: Harness
    kill_switch: str | None = None
    evidence: Evidence
    neighbours_covered: list[str] = []
    neighbours_gap: list[str] = []
    follow_ups: list[str] = []
    red_control: RedControl = "pending"

    @model_validator(mode="after")
    def _consistent(self) -> Gate:
        if self.status == "merged":
            if not self.fix_prs or not self.fix_commit:
                raise ValueError(f"{self.id}: a merged gate names its fix PR(s) and commit")
            if not self.regression_tests:
                raise ValueError(f"{self.id}: a merged gate pins at least one regression test")
            if self.harness == "pending":
                raise ValueError(f"{self.id}: a merged gate has a real harness label")
        else:
            if not self.owner:
                raise ValueError(f"{self.id}: an open or gap entry names its owner ticket")
            if self.status == "gap" and not self.gap_reason:
                raise ValueError(f"{self.id}: a gap entry says why it is still open")
            if self.red_control != "not_applicable":
                raise ValueError(f"{self.id}: red control applies only to merged fixes")
        if self.harness == "mocked_internal" and not self.neighbours_gap:
            raise ValueError(
                f"{self.id}: a mocked-internal harness must name the boundary it cannot prove"
            )
        for path in self.paths:
            if not path.startswith("src/apps/"):
                raise ValueError(f"{self.id}: paths are repo-relative under src/apps/: {path}")
        for test in self.regression_tests:
            if not test.startswith("tests/"):
                raise ValueError(f"{self.id}: regression tests are API-relative: {test}")
        return self


class GateManifest(_Strict):
    schema_version: Literal[1]
    about: str
    boundaries: list[str]
    gates: list[Gate] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> GateManifest:
        ids = [gate.id for gate in self.gates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate gate ids")
        for gate in self.gates:
            if gate.boundary not in self.boundaries:
                raise ValueError(f"{gate.id}: boundary {gate.boundary} not in the listed set")
        return self


def load_manifest(path: Path = GATES_PATH) -> GateManifest:
    return GateManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def ci_union(manifest: GateManifest) -> tuple[list[str], list[str]]:
    """(paths, api_tests) the kri-577 CI journey must list: every merged gate not owned
    by another journey. Sorted and de-duplicated so the journey manifest is stable."""
    paths: set[str] = set()
    tests: set[str] = set()
    for gate in manifest.gates:
        if gate.status != "merged" or gate.journey:
            continue
        paths.update(gate.paths)
        tests.update(gate.regression_tests)
    return sorted(paths), sorted(tests)
