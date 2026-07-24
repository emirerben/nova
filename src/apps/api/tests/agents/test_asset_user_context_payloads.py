"""User-authored visual context reaches matcher prompt payloads."""

from __future__ import annotations

import json
import re

from app.agents.overlay_placement import (
    OverlayPlacementAgent,
    OverlayPlacementInput,
    PlacementAsset,
)
from app.agents.visual_treatment_planner import (
    VisualTreatmentAsset,
    VisualTreatmentPlannerAgent,
    VisualTreatmentPlannerInput,
)


def _extract_assets_json(prompt: str) -> list[dict]:
    marker = "AVAILABLE ANALYSED ASSETS" if "AVAILABLE ANALYSED ASSETS" in prompt else "asset pool"
    tail = prompt.split(marker, 1)[1]
    match = re.search(r"\[\{.*?\}\]", tail, flags=re.DOTALL)
    assert match is not None
    return json.loads(match.group(0))


def test_overlay_placement_prompt_labels_user_context() -> None:
    agent = OverlayPlacementAgent.__new__(OverlayPlacementAgent)
    prompt = agent.render_prompt(
        OverlayPlacementInput(
            words=[{"word": "churn", "start_s": 1.0, "end_s": 1.3}],
            assets=[
                PlacementAsset(
                    asset_id="a1",
                    kind="image",
                    user_context="This is the churn dashboard",
                    subject="chart",
                    description="Nova sees a generic graph",
                )
            ],
            duration_s=10.0,
        )
    )
    assert "user_context" in prompt
    assert "highest-priority semantic clue" in prompt
    assert _extract_assets_json(prompt)[0]["user_context"] == "This is the churn dashboard"


def test_visual_treatment_prompt_labels_user_context() -> None:
    agent = VisualTreatmentPlannerAgent.__new__(VisualTreatmentPlannerAgent)
    prompt = agent.render_prompt(
        VisualTreatmentPlannerInput(
            words=[{"word": "launch", "start_s": 1.0, "end_s": 1.4}],
            assets=[
                VisualTreatmentAsset(
                    asset_id="a1",
                    kind="image",
                    user_context="This is the launch-week photo",
                    subject="people",
                    description="Nova sees a team photo",
                )
            ],
            duration_s=12.0,
        )
    )
    assert "user_context" in prompt
    assert "creator-authored" in prompt
    assert _extract_assets_json(prompt)[0]["user_context"] == "This is the launch-week photo"
