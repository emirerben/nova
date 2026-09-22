"""Per-fixture eval gate for nova.plan.clip_request_resolver (KRI-127 Lane C).

Run modes (see tests/evals/README.md for full guide):
  pytest tests/evals/test_clip_request_resolver_evals.py -v
  pytest tests/evals/test_clip_request_resolver_evals.py -v --with-judge
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "clip_request_resolver"
AGENT_NAME = "nova.plan.clip_request_resolver"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_clip_request_resolver_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")

    judge = judge_for(fixture.agent) if with_judge else None
    client = live_model_client if eval_mode == "live" else None

    result = run_eval(
        fixture,
        model_client=client,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )

    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )

    # Extra acceptance criteria beyond the generic structural floor.
    output = result.output or {}
    input_clip_aliases = {c["alias"] for c in fixture.input.get("clips", [])}
    input_intent_ids = {i["intent_id"] for i in fixture.input.get("intents", [])}
    for intent_out in output.get("intents", []):
        assert intent_out["intent_id"] in input_intent_ids
        for a in intent_out.get("assignments", []):
            assert a["media"] in input_clip_aliases, "assignment references an unknown clip"
            if a.get("value") is not None:
                assert len(a["value"].split()) <= 3, "label value exceeds 3 words"
        for nv in intent_out.get("needs_vision", []):
            assert nv["media"] in input_clip_aliases, "needs_vision references an unknown clip"

    # Per-fixture acceptance: {intent_id: [aliases that MUST be assigned or sent to vision]}.
    # In live mode this is what proves the model found the clips, not just valid JSON.
    by_intent = {i["intent_id"]: i for i in output.get("intents", [])}
    for intent_id, aliases in (fixture.meta.get("expect_covered") or {}).items():
        got = by_intent.get(intent_id) or {}
        covered = {a["media"] for a in got.get("assignments", [])} | {
            nv["media"] for nv in got.get("needs_vision", [])
        }
        missing = sorted(set(aliases) - covered)
        assert not missing, f"{result.fixture_id}: intent {intent_id} never considered {missing}"
    for intent_id, aliases in (fixture.meta.get("expect_not_assigned") or {}).items():
        got = by_intent.get(intent_id) or {}
        wrong = sorted({a["media"] for a in got.get("assignments", [])} & set(aliases))
        assert not wrong, f"{result.fixture_id}: intent {intent_id} wrongly assigned {wrong}"

    # Explicit creator selections are stronger than generic coverage: they must
    # become assignments (not a vision deferral), with no extra members.
    for intent_id, aliases in (fixture.meta.get("expect_assignment_set") or {}).items():
        got = by_intent.get(intent_id) or {}
        assigned = {a["media"] for a in got.get("assignments", [])}
        assert assigned == set(aliases), (
            f"{result.fixture_id}: intent {intent_id} assigned {sorted(assigned)}, "
            f"expected exactly {sorted(aliases)}"
        )
        assert not got.get("needs_vision", []), (
            f"{result.fixture_id}: explicit selection {intent_id} must not defer to vision"
        )

    if fixture.meta.get("expect_no_question"):
        questioned = [
            intent_out["intent_id"]
            for intent_out in output.get("intents", [])
            if intent_out.get("question")
        ]
        assert not questioned, f"{result.fixture_id}: unexpected clarification for {questioned}"

    if fixture.meta.get("expect_needs_vision_nonempty"):
        total_needs_vision = sum(
            len(intent_out.get("needs_vision", [])) for intent_out in output.get("intents", [])
        )
        assert total_needs_vision > 0, (
            f"{result.fixture_id}: expected at least one needs_vision entry "
            "(the record is too vague to answer directly) but got none"
        )
        total_assignments = sum(
            len(intent_out.get("assignments", [])) for intent_out in output.get("intents", [])
        )
        assert total_assignments == 0, (
            f"{result.fixture_id}: expected zero guessed assignments when every "
            "clip's record is too vague, got {total_assignments}"
        )
