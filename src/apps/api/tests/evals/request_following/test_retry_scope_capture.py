"""Replay authored scope captures; no provider calls or rendered evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient
from app.agents.brief_extractor import BriefExtractionInput, BriefExtractorAgent
from app.kria.brief import CurrentPlanShape, route_requirements

CAPTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/prompt_coverage/retry_scope.json").read_text()
)


@pytest.mark.parametrize("case", CAPTURE["cases"], ids=lambda case: case["id"])
def test_live_scope_capture_keeps_semantic_route(case: dict) -> None:
    assert CAPTURE["provenance"]["kind"] == "authored_synthetic_live_capture"
    assert CAPTURE["provenance"]["rendered"] is False
    output = BriefExtractorAgent(ModelClient()).parse(
        case["calls"][0]["raw_text"], BriefExtractionInput.model_validate(case["input"])
    )
    assert output.request_scope == case["expected_scope"]
    if output.request_scope == "clarify":
        assert output.clarification and output.clarification.strip()
    else:
        # An existing editable plan remains an edit even with lexical retry cues.
        route = route_requirements(
            [],
            CurrentPlanShape(has_render=True),
            message=case["input"]["user_message"],
            full_replan=output.request_scope == "rebuild",
        )
        assert route == ("replan" if case["expected_scope"] == "rebuild" else "editor_ops")
