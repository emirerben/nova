"""Render-bound checks for the Cunda hook plus contract-added facts."""

from __future__ import annotations

from typing import get_args

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.recipes import MediaCapability
from app.pipeline.canvas import Canvas
from app.pipeline.phone_plan_repair import compile_phone_guided_repaired
from app.pipeline.phone_text_style import measure_box
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from tests.pipeline.test_phone_guided_plan import fixture

COPY = {
    "en": (
        "Why Cunda Island is a must-visit for amazing food, historic streets, and the sea.",
        "Cunda is home to historical building, old churches, great restaurants and amazing beaches",
    ),
    "tr": (
        "Cunda, harika yemekleri, tarihi sokakları ve deniziyle neden mutlaka görülmeli?",
        "Cunda tarihi binalara, eski kiliselere, harika restoranlara ve güzel plajlara "
        "ev sahipliği yapar.",
    ),
}


def _overlap(a: tuple, b: tuple) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


@pytest.mark.parametrize(
    "orientation,size", [("landscape", (1920, 1080)), ("portrait", (1080, 1920))]
)
@pytest.mark.parametrize("language", ["en", "tr"])
def test_confirmed_wrapped_intro_copy_has_separate_ink_boxes(
    monkeypatch, orientation, size, language
):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))
    hook, facts = COPY[language]
    plan, bindings = fixture()
    title = TextElement(
        id="guided-title",
        text=hook,
        start_s=0,
        end_s=2.2,
        role="generative_intro",
        position="custom",
        x_frac=0.5,
        y_frac=0.16,
        font_family="Fraunces",
        size_px=76,
        effect="static",
        max_width_frac=0.84,
    )
    plan = plan.model_copy(update={"output_orientation": orientation, "text_elements": [title]})
    before = plan.model_dump(mode="json")
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(
            TextRequirement(role="opening", text=hook),
            TextRequirement(role="any", text=facts),
        )
    )
    result = compile_phone_guided_repaired(plan, bindings, contract=contract)
    verify_phone_recipe(contract, result.recipe, source_audio={"source": True})
    assert [r.text for r in result.plan.text_elements] == [hook, facts]
    assert plan.model_dump(mode="json") == before
    assert result.plan.text_elements[0] == title
    assert result.plan.story_timeline == plan.story_timeline
    canvas = Canvas(*size)
    boxes = [measure_box(r.model_dump(mode="json"), canvas) for r in result.plan.text_elements]
    assert not _overlap(*boxes), f"Confirmed words overlap in the rendered ink boxes: {boxes}"
    assert all(0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1 for b in boxes)
    # The old fixed offset must be rejected by this property even though it carries both literals.
    old = result.plan.text_elements[1].model_copy(update={"y_frac": 0.25})
    assert _overlap(boxes[0], measure_box(old.model_dump(mode="json"), canvas))


def test_existing_small_corner_text_is_an_obstacle_without_a_new_style_policy():
    from app.pipeline.phone_plan_repair import repair_plan_for_contract

    hook, facts = COPY["en"]
    plan, _ = fixture()
    title = TextElement(
        id="guided-title",
        text=hook,
        start_s=0,
        end_s=2.2,
        role="generative_intro",
        position="custom",
        x_frac=0.5,
        y_frac=0.16,
        font_family="Fraunces",
        size_px=76,
        effect="static",
        max_width_frac=0.84,
    )
    small = TextElement(
        id="small-corner",
        text="Cunda",
        start_s=0,
        end_s=2.2,
        position="custom",
        x_frac=0.1,
        y_frac=0.9,
        font_family="Fraunces",
        size_px=16,
    )
    plan = plan.model_copy(
        update={"output_orientation": "landscape", "text_elements": [title, small]}
    )
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(
            TextRequirement(role="opening", text=hook),
            TextRequirement(role="any", text=facts),
        )
    )
    repaired, _ = repair_plan_for_contract(plan, contract)
    assert repaired.text_elements[:2] == [title, small]
