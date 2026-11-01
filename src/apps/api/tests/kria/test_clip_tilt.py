"""KRI-1134: the clip-tilt recipe contract (`TimelineClip.backdrop`, `clipTilt`) and its gates.

Rendering lives on device (`ClipTiltCompositionTests`); `tests/fixtures/clip-tilt-parity/v1.json`
pins the wire for both sides. `rotation_degrees` is clockwise on screen.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.kria.device_render import recipe_digest
from app.kria.recipes import ClipBackdrop
from app.kria.recipes_v2 import EditRecipeV2
from app.services.client_protocol import (
    CLIP_TILT_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    clip_tilt_supported,
    recipe_has_clip_tilt,
    validate_phone_pilot_recipe,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"
PARITY = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/clip-tilt-parity/v1.json").read_text()
)


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def tilted_document(*, rotation=0.0, backdrop=None, card=False, **transform):
    document = json.loads(FIXTURE.read_text())
    clip = document["tracks"][0]["clips"][0]
    clip["transform"] |= {"rotation_degrees": rotation} | transform
    if backdrop is not None:
        clip["backdrop"] = backdrop
    if card:
        original = document["asset_manifest"]["assets"][0]
        document["asset_manifest"]["assets"][0] = {
            "kind": "visual",
            "id": original["id"],
            "visual_id": "5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10",
            "generation": "777",
            "fingerprint": original["fingerprint"],
        }
        clip["still_layout"] = "supporting_card"
        clip["source_duration"] = 2.0
    return document


@pytest.mark.parametrize("case", PARITY["valid"], ids=lambda case: case["name"])
def test_shared_fixture_valid_backdrops_normalize(case):
    backdrop = ClipBackdrop.model_validate(case["backdrop"])
    assert backdrop.model_dump(mode="json") == case["normalized"]


@pytest.mark.parametrize("case", PARITY["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_invalid_backdrops_are_rejected(case):
    with pytest.raises(ValidationError):
        ClipBackdrop.model_validate(case["backdrop"])


@pytest.mark.parametrize("case", PARITY["capability"], ids=lambda case: case["name"])
def test_shared_fixture_capability_inference(case):
    recipe = EditRecipeV2.model_validate(
        tilted_document(rotation=case["rotation_degrees"], backdrop=case["backdrop"])
    )
    assert ("clipTilt" in recipe.required_capabilities) is case["clip_tilt"]
    assert recipe_has_clip_tilt(recipe) is case["clip_tilt"]


def test_an_untilted_recipe_keeps_its_wire_and_digest():
    recipe = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    assert "backdrop" not in json.dumps(recipe.model_dump(mode="json"))
    assert "clipTilt" not in recipe.required_capabilities
    explicit_null = json.loads(FIXTURE.read_text())
    explicit_null["tracks"][0]["clips"][0]["backdrop"] = None
    assert recipe_digest(EditRecipeV2.model_validate(explicit_null)) == recipe_digest(recipe)


def test_a_tilted_recipe_round_trips_and_changes_the_digest():
    plain = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    tilted = EditRecipeV2.model_validate(
        tilted_document(rotation=30, backdrop={"kind": "color", "color": "#E91E8C"})
    )
    clip = tilted.model_dump(mode="json")["tracks"][0]["clips"][0]
    assert clip["backdrop"] == {"kind": "color", "color": "#E91E8C"}
    assert clip["transform"]["rotation_degrees"] == 30
    assert EditRecipeV2.model_validate_json(tilted.model_dump_json()) == tilted
    assert recipe_digest(tilted) != recipe_digest(plain)


def test_a_backdrop_is_refused_off_the_main_video_track():
    document = tilted_document(backdrop={"kind": "blur"})
    clip = document["tracks"][0]["clips"].pop()
    document["tracks"].append({"id": "overlay", "kind": "overlay", "clips": [clip]})
    document["tracks"][0]["clips"].append(
        clip | {"id": "base", "backdrop": None, "timeline_start": 0.0}
    )
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(document)


def test_a_supporting_card_may_turn_and_nothing_else():
    turned = EditRecipeV2.model_validate(tilted_document(rotation=30, card=True))
    assert "clipTilt" in turned.required_capabilities
    for refused in ({"scale": 1.2}, {"position_x": 5.0}, {"position_y": -3.0}):
        with pytest.raises(ValidationError):
            EditRecipeV2.model_validate(tilted_document(card=True, **refused))


def test_pilot_validation_refuses_an_unverified_tilt(monkeypatch):
    plain = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    tilted = EditRecipeV2.model_validate(tilted_document(rotation=30))
    verified = [*tilted.required_capabilities]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(tilted)

    monkeypatch.setattr(
        settings, "phone_render_verified_features", [c for c in verified if c != "clipTilt"]
    )
    validate_phone_pilot_recipe(plain)
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(tilted)
    assert refused.value.capability == "clipTilt"


def test_pilot_validation_refuses_a_tilt_added_after_compile(monkeypatch):
    tilted = EditRecipeV2.model_validate(tilted_document(backdrop={"kind": "blank"}))
    tilted.required_capabilities = set()
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(tilted)
    assert refused.value.capability == "clipTilt"


def test_support_gate_needs_flag_token_and_a_new_enough_build(monkeypatch):
    def gate(*, enabled, verified, client_protocol, require_client=True):
        monkeypatch.setattr(settings, "media_tilt_enabled", enabled)
        monkeypatch.setattr(
            settings, "phone_render_verified_features", ["clipTilt"] if verified else []
        )
        monkeypatch.setattr(
            "app.services.client_protocol.current_client_protocol", lambda: client_protocol
        )
        return clip_tilt_supported(require_client=require_client)

    assert gate(enabled=True, verified=True, client_protocol=28)
    assert not gate(enabled=False, verified=True, client_protocol=28)
    assert not gate(enabled=True, verified=False, client_protocol=28)
    assert not gate(enabled=True, verified=True, client_protocol=27)
    assert not gate(enabled=True, verified=True, client_protocol=None)
    assert gate(enabled=True, verified=True, client_protocol=None, require_client=False)


def test_the_feature_is_dark_by_default_and_pinned_to_protocol_28():
    fields = Settings.model_fields
    assert fields["media_tilt_enabled"].default is False
    assert fields["media_tilt_min_client_protocol"].default == CLIP_TILT_PROTOCOL == 28


def test_update_detail_tells_an_old_build_to_update_and_leaves_untilted_recipes_alone():
    from app.routes import device_render

    def detail(document, protocol):
        set_client_protocol(protocol)
        recipe = EditRecipeV2.model_validate(document)
        job = SimpleNamespace(assembly_plan={})
        status = SimpleNamespace(request=SimpleNamespace(recipe=recipe))
        return device_render._client_update_detail(job, {}, status)

    tilted, plain = tilted_document(rotation=30), tilted_document()
    assert detail(plain, 27) is None
    assert detail(tilted, 28) is None
    refused = detail(tilted, 27)
    assert refused == {
        "code": "client_update_required",
        "feature": "clip_tilt",
        "minimum_client_protocol": 28,
    }
