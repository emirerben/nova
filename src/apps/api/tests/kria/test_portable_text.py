import json

import pytest
from pydantic import ValidationError

from app.kria.recipes_v2 import EditRecipeV2
from tests.kria.test_recipes_v2 import FIXTURE


def text_document():
    document = json.loads(FIXTURE.read_text())
    fingerprint = {"sha256": "f" * 64, "byte_count": 100}
    document["asset_manifest"]["assets"].append(
        {
            "kind": "library",
            "id": "font",
            "catalog": "font",
            "catalog_id": "Inter-Regular.ttf",
            "generation": "f" * 64,
            "fingerprint": fingerprint,
        }
    )
    document["assets"].append(
        {
            "id": "font",
            "relative_path": "font",
            "fingerprint": {"algorithm": "sha256", "hex": "f" * 64, "byte_count": 100},
        }
    )
    ink = {"red": 1, "green": 1, "blue": 1, "alpha": 1}
    document["text_layers"] = [
        {
            "id": "caption",
            "start": 0.2,
            "end": 1.0,
            "anchor_x": 540,
            "anchor_y": 960,
            "rotation_degrees": 0,
            "runs": [
                {
                    "text": "Hello world",
                    "font_asset_id": "font",
                    "font_size": 72,
                    "x": 200,
                    "baseline_y": 960,
                    "letter_spacing": 0,
                    "shaped": True,
                    "fill": ink,
                    "stroke": ink,
                    "stroke_width": 0,
                }
            ],
        }
    ]
    return document


def test_independent_text_uses_library_font_and_own_time_window():
    recipe = EditRecipeV2.model_validate(text_document())
    assert recipe.text_layers[0].start == 0.2
    assert recipe.text_layers[0].runs[0].font_asset_id == "font"


@pytest.mark.parametrize(
    "mutation",
    [
        "original",
        "missing",
        "unknown_effect",
        "duplicate",
        "outside",
        "empty",
        "unknown_appearance",
    ],
)
def test_text_contract_fails_closed(mutation):
    document = text_document()
    layer = document["text_layers"][0]
    if mutation == "original":
        layer["runs"][0]["font_asset_id"] = document["assets"][0]["id"]
    elif mutation == "missing":
        layer["runs"][0]["font_asset_id"] = "missing"
    elif mutation == "unknown_effect":
        layer["effect"] = "unsupported"
    elif mutation == "duplicate":
        document["text_layers"].append(layer.copy())
    elif mutation == "outside":
        layer["end"] = 1800
    elif mutation == "empty":
        layer["end"] = layer["start"]
    else:
        layer["runs"][0]["glow"] = True
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(document)


def test_shared_positioned_text_fixture_is_current():
    expected = EditRecipeV2.model_validate(text_document()).model_dump(mode="json")
    assert json.loads((FIXTURE.parent / "kria_positioned_text_v2.json").read_text()) == expected


def test_animated_text_accepts_legacy_or_complete_motion_and_rejects_reveal_effects():
    from dataclasses import asdict

    from app.pipeline.text_motion_v2 import normalize_text_motion

    document = text_document()
    layer = document["text_layers"][0]
    layer["effect"] = "fade-in"
    assert EditRecipeV2.model_validate(document).text_layers[0].motion is None
    layer["motion"] = asdict(normalize_text_motion("fade-in", {"version": 2}))
    assert EditRecipeV2.model_validate(document).text_layers[0].motion is not None
    layer["motion"]["unknown"] = True
    with pytest.raises(ValueError):
        EditRecipeV2.model_validate(document)
    del layer["motion"]["unknown"]
    layer["effect"] = "typewriter"
    with pytest.raises(ValueError):
        EditRecipeV2.model_validate(document)


def test_ink_reveal_requires_valid_bounds_and_other_effects_reject_them():
    document = text_document()
    layer = document["text_layers"][0]
    layer["effect"] = "ink-reveal"
    with pytest.raises(ValueError, match="reveal bounds"):
        EditRecipeV2.model_validate(document)
    layer["reveal_bounds"] = {"left": 0, "top": 0, "right": 100, "bottom": 100}
    assert EditRecipeV2.model_validate(document).text_layers[0].reveal_bounds.right == 100
    layer["reveal_bounds"]["right"] = 0
    with pytest.raises(ValueError, match="positive area"):
        EditRecipeV2.model_validate(document)
    layer["reveal_bounds"]["right"] = 100
    layer["effect"] = "none"
    with pytest.raises(ValueError, match="reveal bounds"):
        EditRecipeV2.model_validate(document)


@pytest.mark.parametrize("seed", [0, 101, 4294967295])
def test_dissolve_requires_and_preserves_explicit_renderer_seed(seed):
    document = text_document()
    document["text_layers"][0].update(effect="dissolve-out", dissolve_seed=seed)
    assert EditRecipeV2.model_validate(document).text_layers[0].dissolve_seed == seed


@pytest.mark.parametrize(
    "effect,seed",
    [
        ("static", 101),
        ("dissolve-out", None),
        ("dissolve-out", -1),
        ("dissolve-out", 4294967296),
        ("dissolve-out", True),
        ("dissolve-out", 1.5),
    ],
)
def test_dissolve_rejects_missing_or_invalid_seed(effect, seed):
    document = text_document()
    document["text_layers"][0].update(effect=effect, dissolve_seed=seed)
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(document)
