import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.kria.device_render import DeviceRenderRequest, make_device_request, recipe_digest
from app.kria.recipes import EditRecipeV1
from app.kria.recipes_v2 import EditRecipeV2

FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"


def test_shared_v2_fixture_and_request_round_trip():
    recipe = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    assert recipe.model_dump(mode="json") == json.loads(FIXTURE.read_text())
    request = make_device_request(
        job_id=uuid.uuid4(), variant_id="guided_story", revision=1, recipe=recipe
    )
    restored = DeviceRenderRequest.model_validate_json(request.model_dump_json())
    assert isinstance(restored.recipe, EditRecipeV2)
    assert restored == request


def test_generation_and_original_binding_participate_in_recipe_digest():
    document = json.loads(FIXTURE.read_text())
    before = recipe_digest(EditRecipeV2.model_validate(document))
    document["asset_manifest"]["assets"][0]["media_id"] = "other-original"
    assert recipe_digest(EditRecipeV2.model_validate(document)) != before


def test_look_requires_capability_and_changes_recipe_digest():
    document = json.loads(FIXTURE.read_text())
    before = recipe_digest(EditRecipeV2.model_validate(document))
    document["tracks"][0]["clips"][0]["look"] = "golden_hour"
    recipe = EditRecipeV2.model_validate(document)
    assert "goldenHourLook" in recipe.required_capabilities
    assert recipe_digest(recipe) != before
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    document["tracks"][0]["clips"][0]["look"] = "stadium_diffusion"
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(document)


@pytest.mark.parametrize("kind", ["fade_black", "fade_white", "wipe_left", "wipe_right"])
def test_extended_transition_requires_its_own_capability(kind):
    document = json.loads(FIXTURE.read_text())
    document["tracks"][0]["clips"][0]["transition"] = {"kind": kind, "duration": 0.2}
    document["required_capabilities"] = ["crossfade"]
    recipe = EditRecipeV2.model_validate(document)
    assert "clipTransitions" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


@pytest.mark.parametrize("mutation", ["missing", "extra", "path", "fingerprint", "v1"])
def test_v2_asset_projection_cannot_disagree_with_manifest(mutation):
    document = json.loads(FIXTURE.read_text())
    if mutation == "missing":
        document["asset_manifest"]["assets"] = []
    elif mutation == "extra":
        document["asset_manifest"]["assets"].append(
            document["asset_manifest"]["assets"][0] | {"id": "other"}
        )
    elif mutation == "path":
        document["assets"][0]["relative_path"] = "originals/clip.mov"
    elif mutation == "fingerprint":
        document["assets"][0]["fingerprint"]["hex"] = "b" * 64
    else:
        document["schema_version"] = 1
        document["renderer_version"] = "kria-ios-1"
    with pytest.raises(ValidationError):
        (EditRecipeV1 if mutation == "v1" else EditRecipeV2).model_validate(document)


def test_camera_program_changes_digest_and_cannot_exceed_timeline():
    document = json.loads(FIXTURE.read_text())
    before = recipe_digest(EditRecipeV2.model_validate(document))
    document["camera_pulses"] = [{"id": "pulse", "start": 0, "end": 1, "intensity": 0.04}]
    recipe = EditRecipeV2.model_validate(document)
    assert "cameraEffects" in recipe.required_capabilities
    assert recipe_digest(recipe) != before
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    for mutation in [
        {"end": 1801},
        {"end": 0},
        {"intensity": 0.09},
        {"intensity": float("nan")},
    ]:
        invalid = document | {"camera_pulses": [document["camera_pulses"][0] | mutation]}
        with pytest.raises(ValidationError):
            EditRecipeV2.model_validate(invalid)


def test_visual_blocks_require_bounded_silent_overlay_and_rollout_gate():
    from app.services.phone_rollout import validate_phone_pilot_recipe

    document = json.loads(FIXTURE.read_text())
    source = document["tracks"][0]["clips"][0]
    overlay = source | {
        "id": "visual-shot",
        "volume": 0,
        "transition": None,
        "visual_placement": {"order": 1, "window_start": 0, "window_end": 10},
    }
    document["tracks"].append({"id": "visual", "kind": "overlay", "clips": [overlay]})
    recipe = EditRecipeV2.model_validate(document)
    assert "visualBlocks" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    with pytest.raises(ValueError, match="Visual blocks"):
        validate_phone_pilot_recipe(recipe)
    overlay["volume"] = 1
    with pytest.raises(ValidationError, match="silent bounded"):
        EditRecipeV2.model_validate(document)


def test_mute_windows_cannot_reference_absent_clip():
    document = json.loads(FIXTURE.read_text())
    document["audio"]["mute_windows"] = [{"start": 0, "end": 1, "clip_ids": ["absent"]}]
    with pytest.raises(ValidationError, match="unknown clip"):
        EditRecipeV2.model_validate(document)


def test_duplicate_camera_identity_is_rejected():
    document = json.loads(FIXTURE.read_text())
    pulse = {"id": "pulse", "start": 0, "end": 1, "intensity": 0.04}
    document["camera_pulses"] = [pulse, pulse | {"start": 1, "end": 2}]
    with pytest.raises(ValidationError, match="camera pulses must be unique"):
        EditRecipeV2.model_validate(document)


def test_visual_fill_roundtrip_capability_and_invalid_windows():
    document = json.loads(FIXTURE.read_text())
    fill = {
        "id": "fill",
        "start": 0,
        "end": 1,
        "order": 0,
        "kind": "solid",
        "color": {"red": 0.1, "green": 0.2, "blue": 0.3, "alpha": 1},
    }
    document["visual_fills"] = [fill]
    recipe = EditRecipeV2.model_validate(document)
    assert "visualBlocks" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    for invalid in [[fill, fill], [fill | {"end": recipe.duration + 1}]]:
        with pytest.raises(ValidationError, match="visual fills must be unique and within"):
            EditRecipeV2.model_validate(document | {"visual_fills": invalid})
    for invalid in [fill | {"start": 1}, fill | {"kind": "gradient"}]:
        with pytest.raises(ValidationError, match="positive window and gradient endpoints"):
            EditRecipeV2.model_validate(document | {"visual_fills": [invalid]})


def test_mute_window_roundtrip_capability_and_timeline_boundary():
    document = json.loads(FIXTURE.read_text())
    recipe = EditRecipeV2.model_validate(document)
    window = {"start": 0, "end": recipe.duration, "clip_ids": [recipe.tracks[0].clips[0].id]}
    document["audio"]["mute_windows"] = [window]
    recipe = EditRecipeV2.model_validate(document)
    assert "visualBlocks" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    document["audio"]["mute_windows"] = [window | {"end": recipe.duration + 1}]
    with pytest.raises(ValidationError, match="mute window exceeds the timeline"):
        EditRecipeV2.model_validate(document)
