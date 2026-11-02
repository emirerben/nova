"""Replay gate for nova.kria.intent_review (structure and server judgment, no network).

Replay proves the output contract and what the server makes of it; it does not prove the model
catches anything. Detection is proven by the live qualification run, whose recorded replies then
replace the hand-written `raw_text` of the goldens (tests/evals/README.md, raw_text provenance).

  pytest tests/evals/test_intent_review_evals.py -v

Goldens carry `meta.expect` for the shadow tier (`review`) and the repair tier. Hostile
fixtures carry `meta.expect.void`, the reason the server must void the whole review for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._schemas.intent_review import InvalidReviewOutput
from app.agents.intent_review import IntentReviewInput, render_review_prompt
from app.kria.intent_review_catalog import ReviewCatalog
from app.kria.intent_review_disposition import RepairPreconditions, evaluate_review

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "intent_review"
AGENT_NAME = "nova.kria.intent_review"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)
GOLDEN = [p for p in FIXTURE_PATHS if p.parent.name == "golden"]
HOSTILE = [p for p in FIXTURE_PATHS if p.parent.name == "hostile"]
_IDS = lambda p: f"{p.parent.name}/{p.stem}"  # noqa: E731


def _judge(fixture, tier):
    catalog = ReviewCatalog.model_validate(fixture.input["catalog"])
    return evaluate_review(
        fixture.raw_text,
        catalog,
        tier=tier,
        repair_preconditions=RepairPreconditions(),
    )


def test_every_case_the_review_must_survive_has_a_fixture():
    names = {p.stem for p in FIXTURE_PATHS}
    assert {
        "correct_edit",
        "wrong_content_control",
        "wrong_wording_control",
        "wording_drift_needs_confirmation",
        "timing_miss_control",
        "audio_decoy",
        "unknown_requirement_id",
        "audio_id_finding",
        "hallucinated_frame_id",
        "unmet_without_evidence",
        "incompatible_defect_kind",
        "duplicate_requirement",
        "missing_requirement",
        "truncated_output",
        "injection_text_in_frame_manifest",
    } <= names


@pytest.mark.parametrize("fixture_path", GOLDEN, ids=_IDS)
def test_golden_fixture_passes_the_agent_contract(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    assert fixture.agent == AGENT_NAME
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )


@pytest.mark.parametrize("fixture_path", GOLDEN, ids=_IDS)
@pytest.mark.parametrize("tier", ["review", "repair"])
def test_golden_fixture_is_judged_as_expected_by_the_server(fixture_path: Path, tier: str) -> None:
    fixture = load_fixture(fixture_path)
    expected = fixture.meta["expect"][tier]
    evaluation = _judge(fixture, tier)
    assert evaluation.status == expected["status"], fixture.meta["case"]
    assert evaluation.reason == "reviewed"
    catalog = ReviewCatalog.model_validate(fixture.input["catalog"])
    if "candidate_kinds" in expected:
        disposition = evaluation.disposition
        used = {s.candidate_id for s in disposition.suggestions if s.candidate_id}
        used |= set(disposition.repair_candidate_ids)
        assert sorted(catalog.candidate(i).kind for i in used) == sorted(
            expected["candidate_kinds"]
        )
    if "why" in expected:
        assert [s.why for s in evaluation.disposition.suggestions] == expected["why"]
    if tier == "review":
        assert evaluation.disposition.repair_candidate_ids == []


def test_the_audio_decoy_is_withheld_and_never_issued_or_claimed() -> None:
    fixture = load_fixture(next(p for p in GOLDEN if p.stem == "audio_decoy"))
    catalog = ReviewCatalog.model_validate(fixture.input["catalog"])
    expected = fixture.meta["expect"]
    assert [w.model_dump() for w in catalog.withheld] == expected["withheld"]
    assert [r.brief_id for r in catalog.requirements] == expected["issued_brief_ids"]
    assert all(r.kind != "audio" for r in catalog.requirements)
    claimed = {f.requirement_id for f in _judge(fixture, "repair").findings}
    assert claimed <= set(catalog.requirement_ids)


@pytest.mark.parametrize("fixture_path", HOSTILE, ids=_IDS)
def test_hostile_reply_voids_the_whole_review(fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    with pytest.raises(InvalidReviewOutput) as voided:
        _judge(fixture, "repair")
    assert voided.value.reason == fixture.meta["expect"]["void"], fixture.meta["case"]


@pytest.mark.parametrize("fixture_path", HOSTILE, ids=_IDS)
def test_hostile_reply_never_passes_replay(fixture_path: Path) -> None:
    result = run_eval(load_fixture(fixture_path))
    assert not result.passed, "the live path would throw this reply away; replay must too"


@pytest.mark.parametrize("fixture_path", GOLDEN + HOSTILE, ids=_IDS)
def test_the_creators_words_stay_inside_one_json_string_of_the_prompt(fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    agent_input = IntentReviewInput.model_validate(fixture.input)
    prompt = render_review_prompt(agent_input)
    assert prompt.count(json.dumps(agent_input.original_request[:2000], ensure_ascii=False)) == 1
    for candidate in agent_input.catalog.candidates:
        assert candidate.candidate_id not in prompt
        assert json.dumps(candidate.target) not in prompt
    for name in (
        "request_json",
        "requirements_json",
        "frames_json",
        "defects_by_kind",
        "verdicts",
        "defect_kinds",
        "confidence_levels",
    ):
        assert f"${name}" not in prompt, f"unrendered template variable {name}"
