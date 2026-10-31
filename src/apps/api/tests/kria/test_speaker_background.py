"""KRI-1115: the speaker-background recipe lane (contract only; rendering lives on device)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.kria.device_render import recipe_digest
from app.kria.portable_speaker_background import SpeakerBackgroundLayer
from app.kria.recipes_v2 import EditRecipeV2

FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"
PARITY = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity/v1.json").read_text()
)

# Digest of the fixture recipe on origin/main before this lane existed.
PRE_LANE_DIGEST = "088f1b0688b8ae81d89556e8e2ebe26defdc85ca1178a609d692c27f747791cd"


def background_document(media_kind="image", **layer):
    document = json.loads(FIXTURE.read_text())
    original = document["asset_manifest"]["assets"][0]
    document["asset_manifest"]["assets"][0] = {
        "kind": "visual",
        "id": original["id"],
        "visual_id": "5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10",
        "generation": "777",
        "fingerprint": original["fingerprint"],
    } | ({"media_kind": "video"} if media_kind == "video" else {})
    document["speaker_background"] = {
        "asset_id": original["id"],
        "media_kind": media_kind,
    } | layer
    return document


def test_recipe_without_a_background_keeps_its_wire_and_digest():
    recipe = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    assert recipe.speaker_background is None
    assert "speaker_background" not in recipe.model_dump(mode="json")
    assert "speakerBackground" not in recipe.required_capabilities
    assert recipe_digest(recipe) == PRE_LANE_DIGEST
    explicit_null = json.loads(FIXTURE.read_text()) | {"speaker_background": None}
    assert recipe_digest(EditRecipeV2.model_validate(explicit_null)) == PRE_LANE_DIGEST


def test_a_background_declares_the_capability_and_changes_the_digest():
    recipe = EditRecipeV2.model_validate(background_document())
    assert "speakerBackground" in recipe.required_capabilities
    assert "stillImages" in recipe.required_capabilities
    assert recipe_digest(recipe) != PRE_LANE_DIGEST
    wire = recipe.model_dump(mode="json")["speaker_background"]
    assert wire == {
        "asset_id": "media-1",
        "media_kind": "image",
        "source_start": 0.0,
        "loop": True,
        "subject_track_id": "subtitled",
    }
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_a_video_background_also_requires_visual_videos():
    recipe = EditRecipeV2.model_validate(background_document("video", source_start=3.0))
    assert {"speakerBackground", "visualVideos"} <= recipe.required_capabilities


@pytest.mark.parametrize(
    "mutation",
    ["unknown_asset", "kind_disagrees_with_manifest", "original_asset"],
)
def test_the_background_must_name_a_matching_visuals_pool_asset(mutation):
    document = background_document()
    if mutation == "unknown_asset":
        document["speaker_background"]["asset_id"] = "visual-missing"
    elif mutation == "kind_disagrees_with_manifest":
        document["speaker_background"]["media_kind"] = "video"
    else:
        document = json.loads(FIXTURE.read_text())
        document["speaker_background"] = {"asset_id": "media-1", "media_kind": "image"}
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(document)


@pytest.mark.parametrize("case", PARITY["valid"], ids=lambda case: case["name"])
def test_shared_fixture_valid_layers_normalize(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    assert layer.model_dump(mode="json") == case["normalized"]


@pytest.mark.parametrize("case", PARITY["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_invalid_layers_are_rejected(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])


def test_pilot_validation_refuses_unverified(monkeypatch):
    from app.config import settings
    from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe

    plain = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    backed = EditRecipeV2.model_validate(background_document())
    verified = [*backed.required_capabilities]

    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(backed)

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in verified if c != "speakerBackground"],
    )
    validate_phone_pilot_recipe(plain)
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(backed)
    assert refused.value.capability == "speakerBackground"


def test_pilot_validation_refuses_a_background_added_after_compile(monkeypatch):
    """The explicit gate matters when a caller mutates a recipe after capabilities were derived."""
    from app.config import settings
    from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe

    backed = EditRecipeV2.model_validate(background_document())
    backed.required_capabilities = set()
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(backed)
    assert refused.value.capability == "speakerBackground"


def test_support_gate_needs_flag_token_and_a_new_enough_build(monkeypatch):
    from app.config import settings
    from app.services.phone_rollout import speaker_background_supported

    def gate(*, enabled, verified, client_protocol, require_client=True):
        monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
        monkeypatch.setattr(
            settings, "phone_render_verified_features", ["speakerBackground"] if verified else []
        )
        monkeypatch.setattr(
            "app.services.client_protocol.current_client_protocol", lambda: client_protocol
        )
        return speaker_background_supported(require_client=require_client)

    assert gate(enabled=True, verified=True, client_protocol=27)
    assert not gate(enabled=False, verified=True, client_protocol=27)
    assert not gate(enabled=True, verified=False, client_protocol=27)
    assert not gate(enabled=True, verified=True, client_protocol=26)
    assert not gate(enabled=True, verified=True, client_protocol=None)
    assert gate(enabled=True, verified=True, client_protocol=None, require_client=False)


def test_the_feature_is_dark_by_default():
    from app.config import Settings
    from app.services.client_protocol import SPEAKER_BACKGROUND_PROTOCOL

    fields = Settings.model_fields
    assert fields["speaker_background_enabled"].default is False
    assert fields["speaker_background_min_client_protocol"].default == SPEAKER_BACKGROUND_PROTOCOL
