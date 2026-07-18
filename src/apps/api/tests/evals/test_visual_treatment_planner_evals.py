"""Replay/live eval gate for Nova's first-class visual-treatment planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "visual_treatment_planner"
AGENT_NAME = "nova.compose.visual_treatment_planner"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no visual-treatment planner fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_visual_treatment_planner_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    judge = judge_for(fixture.agent) if with_judge else None
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n  error: {result.error}"
    )
    expected = fixture.meta.get("expected_section_items") or []
    if expected:
        assert result.output is not None
        cards = [
            treatment
            for treatment in result.output.get("treatments", [])
            if treatment.get("kind") == "text_card"
        ]
        assert len(cards) == len(expected), (
            f"expected exactly {len(expected)} structured cards, got {len(cards)}: {cards}"
        )
        for card, target in zip(cards, expected, strict=True):
            assert card["purpose"] == "section_item"
            assert card["text"] == target["text"]
            assert float(card["start_s"]) == pytest.approx(float(target["start_s"]), abs=0.01)
            assert float(card["end_s"]) == pytest.approx(float(target["end_s"]), abs=0.01)
