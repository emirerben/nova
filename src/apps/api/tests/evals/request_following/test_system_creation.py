"""Nine-format live captures replay through actual grounding/policy compilation."""

import json
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient
from app.agents._schemas.edit_format import EDIT_FORMATS
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
from app.routes.creator_agent import _apply_explicit_render_intent
from app.services.creator_capabilities import compile_strategy_to_plan

CASES = json.loads(
    (
        Path(__file__).resolve().parents[2] / "fixtures/prompt_coverage/system_creation.json"
    ).read_text()
)["cases"]


def test_creation_matrix_has_real_manifests_for_all_formats_and_destinations():
    assert {(r["format"], r["destination"]) for r in CASES} == {
        (f, d) for f in EDIT_FORMATS for d in ["phone", "cloud"]
    }
    assert all(r["calls"] for r in CASES)


@pytest.mark.parametrize("row", CASES, ids=lambda r: r["case"])
def test_creation_request_reaches_policy_and_compiler(row, prod_profile):
    inp = MainCreatorInput.model_validate(row["input"])
    output = MainCreatorAgent(ModelClient()).parse(row["calls"][-1]["raw_text"], inp)
    expected = row["expected"]
    manifest = inp.capability_manifest
    if expected["outcome"] == "unavailable":
        assert output.action.kind == "ask_user"
        caps = manifest.capabilities
        assert (
            not caps["edit_format:" + row["format"]].available
            or not caps["phone_format:" + row["format"]].available
        )
        assert output.action.question
        return
    if expected["outcome"] == "clarification":
        assert output.action.kind == "ask_user"
        assert "clip" in output.action.question.lower()
        return
    assert output.action.kind == "propose_strategy"
    strategy = _apply_explicit_render_intent(
        output.action.strategy,
        inp.user_message,
        manifest=manifest,
        latest_user_message=inp.user_message,
        render_intent_evidence=output.action.render_intent_evidence,
    )
    checked = check_strategy_for_runtime_v2(manifest, strategy)
    assert isinstance(checked, CheckedStrategy), checked
    plan = compile_strategy_to_plan(manifest, checked.strategy)
    effective_format = plan.strategy.archetype or plan.strategy.edit_format
    assert effective_format == expected["format"]
    assert plan.strategy.audio_strategy == expected["audio"]
    assert plan.commands, "a valid strategy without executable commands is not success"
    assert all(c.command != "publish" for c in plan.commands)
