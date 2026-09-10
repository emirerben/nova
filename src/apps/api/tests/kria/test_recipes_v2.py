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
