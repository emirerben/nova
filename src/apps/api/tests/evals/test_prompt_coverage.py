"""Structural checks for the KRI-524 prompt library.

These checks validate the corpus and its accounting. They do not call a model and
therefore never turn authored prompts into fabricated execution evidence.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.agents._schemas.edit_format import EDIT_FORMATS
from tests.evals.prompt_coverage import (
    FAMILIES,
    FIXTURE,
    load_cases,
    replay_exit_code,
    report,
    run_regression_suites,
    validate_cases,
)


def test_fixture_is_present_and_matches_schema() -> None:
    payload = json.loads(FIXTURE.read_text())
    assert payload["schema_version"] == 1
    result = validate_cases(load_cases())
    assert result["valid"], result["errors"]


def test_validation_rejects_unknown_enum_values_in_an_otherwise_valid_case() -> None:
    case = load_cases()[0]
    for field, value in (
        ("format", "future_format"),
        ("stage", "review"),
        ("family", "future_family"),
        ("evidence_status", "captured"),
    ):
        result = validate_cases([replace(case, **{field: value})])
        assert not result["valid"], field


def test_validation_rejects_execution_attached_to_unexecuted_library_case() -> None:
    case = load_cases()[0]
    result = validate_cases([replace(case, execution={"status": "passed"})])
    assert not result["valid"]


def test_library_has_distinct_creation_and_editing_prompts_for_every_format_and_family() -> None:
    cases = load_cases()
    assert len(cases) >= 200
    assert {case.format for case in cases} == set(EDIT_FORMATS)
    assert {case.family for case in cases} == set(FAMILIES)
    assert {case.stage for case in cases} == {"creation", "editing"}
    assert {case.kind for case in cases} >= {
        "user_example",
        "negative",
        "clarification",
        "unsupported",
    }
    assert all(
        token not in case.prompt
        for case in cases
        for token in ("montage", "talking_head", "day_vlog", "single_hero")
    )
    assert len({case.prompt.casefold() for case in cases}) == len(cases)
    for fmt in EDIT_FORMATS:
        for family in FAMILIES:
            assert {
                case.stage for case in cases if case.format == fmt and case.family == family
            } == {
                "creation",
                "editing",
            }


def test_report_separates_unexecuted_cases_from_real_results() -> None:
    result = report(load_cases())
    assert result["valid"]
    assert result["by_evidence_status"] == {"unexecuted": result["case_count"]}
    assert all(case.execution is None for case in load_cases())
    assert set(result["by_destination"]) == {"cloud_editor", "native_editor", "worker_render"}
    assert result["unexecuted_outcome_total"] == result["case_count"]
    assert set(result["by_failure_category"]) == {
        "wrong_text",
        "wrong_geometry",
        "wrong_timing",
        "source_loss",
        "selection_order",
        "audio_caption",
        "pacing_style",
        "compound_correction",
        "ambiguity",
        "unsupported_false_claim",
    }
    assert sum(result["by_failure_category"].values()) == result["case_count"]


def test_referenced_fixture_paths_and_evidence_nodes_exist() -> None:
    api_root = Path(__file__).resolve().parents[2]
    for case in load_cases():
        for fixture_path in case.fixture_paths:
            assert (api_root / fixture_path).is_file(), (case.case_id, fixture_path)
        for node in case.related_evidence:
            path = node.split("::", 1)[0]
            assert (api_root / path).is_file(), (case.case_id, node)


def test_each_case_points_to_a_real_reusable_replay_scenario() -> None:
    from tests.evals.prompt_coverage import SCENARIOS

    for case in load_cases():
        scenario = SCENARIOS[case.execution_scenario]
        api_root = Path(__file__).resolve().parents[2]
        if scenario["fixture"]:
            assert (api_root / scenario["fixture"]).is_file()
        if scenario["pytest"]:
            assert (api_root / scenario["pytest"].split("::", 1)[0]).is_file()


def test_regression_runner_returns_failure_for_injected_subprocess_failure() -> None:
    class Failed:
        returncode = 7
        stdout = "failed"
        stderr = "synthetic failure"

    result = run_regression_suites(run=lambda *args, **kwargs: Failed())
    assert result["passed"] is False
    assert result["returncode"] == 7
    assert replay_exit_code({"valid": True, "structural_regression": result}) == 1


def test_regression_runner_returns_success_for_injected_subprocess_success() -> None:
    class Passed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    result = run_regression_suites(run=lambda *args, **kwargs: Passed())
    assert result["passed"] is True
    assert replay_exit_code({"valid": True, "structural_regression": result}) == 0


def test_regression_runner_forces_replay_mode_and_uses_one_subprocess() -> None:
    calls = []

    class Passed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Passed()

    result = run_regression_suites(run=fake_run)
    assert result["passed"] is True
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0].endswith("python")
    assert "--eval-mode=replay" in command
    assert kwargs["env"]["NOVA_EVAL_MODE"] == "replay"
