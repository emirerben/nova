"""Per-fixture eval gate for nova.plan.clip_intent_planner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "clip_intent_planner"
AGENT_NAME = "nova.plan.clip_intent_planner"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


def _matches_requirement(row: dict[str, Any], requirement: dict[str, Any]) -> bool:
    """Match a planned operation to its requested source span.

    Attributes are model-authored summaries, so their exact wording is not a
    stable evaluation boundary.  The source quote is the grounding contract:
    it must point back to the distinct part of the creator request that asked
    for this operation.
    """
    if row.get("op") != requirement["op"]:
        return False

    # Source ownership is the routing boundary: clip labels go through vision;
    # transcript labels are deferred to the pinned narration materializer.
    # Fixtures that predate KRI-156 deliberately default to visual ownership.
    label_source = requirement.get("label_source", "clip")
    if row.get("label_source", "clip") != label_source:
        return False
    transcript_kind = requirement.get("transcript_kind")
    if row.get("transcript_kind") != transcript_kind:
        return False

    source_quote = str(row.get("source_quote") or "").casefold()
    source_keywords = requirement["source_quote_keywords"]
    if not all(keyword.casefold() in source_quote for keyword in source_keywords):
        return False

    creator_text = requirement.get("creator_text")
    if creator_text is not None and row.get("creator_text") != creator_text:
        return False

    position = requirement.get("position")
    return position is None or row.get("position") == position


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_clip_intent_planner_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    judge = judge_for(fixture.agent) if with_judge else None
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
    )
    assert result.passed, f"{result.fixture_id}: {result.summary()} {result.error}"

    intents = (result.output or {}).get("intents", [])
    expected = fixture.meta.get("expect_intents") or []
    for requirement in expected:
        assert any(_matches_requirement(row, requirement) for row in intents), requirement
    absent_ops = fixture.meta.get("expect_absent_ops") or []
    for op in absent_ops:
        assert not any(row["op"] == op for row in intents)
    assert bool((result.output or {}).get("intents")) is bool(
        fixture.meta.get("expect_any", expected)
    )
    if "expect_question" in fixture.meta:
        assert bool((result.output or {}).get("question")) is fixture.meta["expect_question"]
