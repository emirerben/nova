import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.kria.portable_motion import MotionSceneProgram
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.motion_scene import MOTION_RUNTIME_HASH
from app.services.phone_rollout import validate_phone_pilot_recipe


def route():
    return {
        "id": "route",
        "preset_id": "route_trace",
        "preset_version": 1,
        "start_frame": 0,
        "end_frame_exclusive": 60,
        "palette": {"primary": "#FFFFFF", "accent": "#123456"},
        "intensity": 0.5,
    }


def test_motion_recipe_is_source_bound_and_not_advertised_before_qualification():
    raw = json.loads(
        (Path(__file__).parents[1] / "fixtures/kria_positioned_text_v2.json").read_text()
    )
    raw["motion_scenes"] = {
        "instances": [route()],
        "runtime_hash": MOTION_RUNTIME_HASH,
        "font_asset_id": "font",
    }
    recipe = EditRecipeV2.model_validate(raw)
    assert "motionScenes" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    with pytest.raises(ValueError, match="Motion scenes await"):
        validate_phone_pilot_recipe(recipe)
    raw["motion_scenes"]["font_asset_id"] = "media-1"
    with pytest.raises(ValidationError, match="exact library font"):
        EditRecipeV2.model_validate(raw)
    raw["motion_scenes"]["font_asset_id"] = "font"
    raw["motion_scenes"]["instances"][0]["end_frame_exclusive"] = 61
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(raw)


def test_motion_image_program_accepts_aliases_and_rejects_storage_paths():
    scene = {
        **route(),
        "preset_id": "card_stack",
        "params": {"assets": [{"asset_id": "a"}, {"asset_id": "b"}, {"asset_id": "c"}]},
    }
    payload = {
        "instances": [scene],
        "runtime_hash": MOTION_RUNTIME_HASH,
        "font_asset_id": "font",
        "image_asset_ids": {"a": "image-a", "b": "image-b", "c": "image-c"},
    }
    program = MotionSceneProgram.model_validate(payload)
    assert "gcs_path" not in program.model_dump_json()
    scene["params"]["assets"][0]["gcs_path"] = "users/owner/image.png"
    with pytest.raises(ValidationError, match="manifest aliases"):
        MotionSceneProgram.model_validate(payload)
