"""Qendresa item 18: aim a background recording at its readable text (recipe wire contract).

The shared fixture `tests/fixtures/speaker-background-parity/v5.json` (Swift reads the same file),
the `backgroundFocus` capability and the guarantee that a layer without a focus is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.kria.device_render import recipe_digest
from app.kria.portable_speaker_background import (
    MAX_FOCUS_ZOOM,
    MIN_FOCUS_ZOOM,
    SpeakerBackgroundFocus,
    SpeakerBackgroundLayer,
)
from app.kria.recipes import MediaCapability
from app.kria.recipes_v2 import EditRecipeV2
from tests.kria.test_speaker_background import PRE_LANE_DIGEST, background_document
from tests.kria.test_speaker_background_color import (
    PRE_COLOUR_IMAGE_DIGEST,
    PRE_COLOUR_VIDEO_DIGEST,
)

PARITY_DIR = Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity"
PARITY_V5 = json.loads((PARITY_DIR / "v5.json").read_text())
OLDER = [
    json.loads((PARITY_DIR / name).read_text())
    for name in ("v1.json", "v2.json", "v3.json", "v4.json")
]


@pytest.mark.parametrize("case", PARITY_V5["valid"], ids=lambda case: case["name"])
def test_shared_fixture_v5(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    assert layer.model_dump(mode="json") == case["normalized"]
    assert SpeakerBackgroundLayer.model_validate(layer.model_dump(mode="json")) == layer
    assert SpeakerBackgroundLayer.model_validate_json(layer.model_dump_json()) == layer
    assert json.loads(layer.model_dump_json()) == case["normalized"]


@pytest.mark.parametrize("case", PARITY_V5["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_v5_invalid_layers_are_rejected(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate_json(json.dumps(case["layer"]))


@pytest.mark.parametrize(
    "case",
    [case for fixture in OLDER for case in fixture["valid"]],
    ids=lambda case: case["name"],
)
def test_v1_to_v4_fixtures_still_normalize_unchanged_without_a_focus(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    dumped = layer.model_dump(mode="json")
    assert dumped == case["normalized"]
    assert "focus" not in json.dumps(dumped)
    assert not layer.uses_focus


def test_the_fixture_covers_every_shape_the_phone_must_agree_on():
    names = {case["name"] for case in PARITY_V5["valid"]}
    assert len(PARITY_V5["valid"]) >= 8 and len(PARITY_V5["invalid"]) >= 9
    assert {
        "zoom-at-the-lower-bound",
        "zoom-at-the-upper-bound",
        "null-focus-reads-as-absent",
    } <= names


def test_zoom_bounds_are_the_documented_ones():
    assert (MIN_FOCUS_ZOOM, MAX_FOCUS_ZOOM) == (0.2, 4.0)
    for bad in ({"x": 0.5, "y": 0.5, "zoom": float("nan")}, {"x": float("inf"), "y": 0, "zoom": 1}):
        with pytest.raises(ValidationError):
            SpeakerBackgroundFocus.model_validate(bad)


def test_uses_focus_sees_the_fill_and_any_window():
    plain = SpeakerBackgroundLayer(asset_id="a", media_kind="image")
    assert not plain.uses_focus
    focus = {"x": 0.2, "y": 0.5, "zoom": 1.5}
    fill = SpeakerBackgroundLayer(asset_id="a", media_kind="image", focus=focus)
    assert fill.uses_focus
    window = {"asset_id": "b", "media_kind": "image", "start": 0, "end": 2}
    one = SpeakerBackgroundLayer(
        asset_id="a",
        media_kind="image",
        windows=[window, window | {"start": 2, "end": 4, "focus": focus}],
    )
    assert one.uses_focus
    assert not SpeakerBackgroundLayer(asset_id="a", media_kind="image", windows=[window]).uses_focus


def test_a_recipe_without_a_focus_keeps_exactly_todays_capabilities_and_digest():
    plain = EditRecipeV2.model_validate_json(
        (Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json").read_text()
    )
    assert recipe_digest(plain) == PRE_LANE_DIGEST
    image = EditRecipeV2.model_validate(background_document())
    video = EditRecipeV2.model_validate(background_document("video", source_start=3.0))
    assert recipe_digest(image) == PRE_COLOUR_IMAGE_DIGEST
    assert recipe_digest(video) == PRE_COLOUR_VIDEO_DIGEST
    for recipe in (plain, image, video):
        assert "backgroundFocus" not in recipe.required_capabilities
        assert "focus" not in json.dumps(recipe.model_dump(mode="json"))
    null_focus = background_document(focus=None)
    assert recipe_digest(EditRecipeV2.model_validate(null_focus)) == PRE_COLOUR_IMAGE_DIGEST


def test_a_fill_focus_requires_the_background_focus_capability():
    recipe = EditRecipeV2.model_validate(
        background_document(focus={"x": 0.1, "y": 0.5, "zoom": 1.7})
    )
    assert recipe.speaker_background.uses_focus
    assert {"speakerBackground", "backgroundFocus"} <= recipe.required_capabilities
    # Its own capability: the windows one is NOT implied by it.
    assert "speakerBackgroundWindows" not in recipe.required_capabilities
    assert recipe_digest(recipe) != PRE_COLOUR_IMAGE_DIGEST
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_the_capability_is_a_known_recipe_capability():
    from typing import get_args

    assert "backgroundFocus" in get_args(MediaCapability)
