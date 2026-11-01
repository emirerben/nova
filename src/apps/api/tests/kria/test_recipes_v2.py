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


def test_duck_music_during_speech_is_optional_and_derives_audio_ducking():
    document = json.loads(FIXTURE.read_text())
    recipe = EditRecipeV2.model_validate(document)
    assert recipe.audio.duck_music_during_speech is False
    # Default-off is omitted, so existing recipes serialize byte-identically.
    assert "duck_music_during_speech" not in json.loads(recipe.model_dump_json())["audio"]
    document["audio"]["duck_music_during_speech"] = True
    ducked = EditRecipeV2.model_validate(document)
    assert "audioDucking" in ducked.required_capabilities
    assert json.loads(ducked.model_dump_json())["audio"]["duck_music_during_speech"] is True
    assert EditRecipeV2.model_validate_json(ducked.model_dump_json()) == ducked


def visual_document():
    document = json.loads(FIXTURE.read_text())
    original = document["asset_manifest"]["assets"][0]
    document["asset_manifest"]["assets"][0] = {
        "kind": "visual",
        "id": original["id"],
        "visual_id": "5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10",
        "generation": "777",
        "fingerprint": original["fingerprint"],
    }
    return document


def test_visual_asset_derives_still_images_capability():
    # A client-declared capability list cannot omit it: an app that never
    # verified pool-photo downloads must refuse the recipe, not drop the photo.
    document = visual_document()
    assert document["required_capabilities"] == []
    recipe = EditRecipeV2.model_validate(document)
    assert "stillImages" in recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    assert recipe_digest(recipe) != recipe_digest(
        EditRecipeV2.model_validate(json.loads(FIXTURE.read_text()))
    )


def test_original_only_recipe_does_not_require_still_images():
    recipe = EditRecipeV2.model_validate(json.loads(FIXTURE.read_text()))
    assert "stillImages" not in recipe.required_capabilities


# --- KRI-121 round 2: pool videos and supporting-card stills ------------------


def add_visual_clip(document, media_kind):
    """Append a second pool asset of ``media_kind`` with a clip reading it."""
    asset = document["assets"][0] | {"id": "media-2", "relative_path": "media-2"}
    asset["fingerprint"] = asset["fingerprint"] | {"hex": "b" * 64}
    document["assets"].append(asset)
    document["asset_manifest"]["assets"].append(
        {
            "kind": "visual",
            "id": "media-2",
            "visual_id": "7d4f1b3a-0e5c-4a8d-b9f2-1c6e8a7b5d43",
            "generation": "888",
            "fingerprint": {"sha256": "b" * 64, "byte_count": 100},
        }
        | ({"media_kind": "video"} if media_kind == "video" else {})
    )
    clip = document["tracks"][0]["clips"][0]
    document["tracks"][0]["clips"].append(
        clip | {"id": "clip-2", "source_asset_id": "media-2", "timeline_start": 2.0}
    )
    return document


def test_video_visual_derives_visual_videos_not_still_images():
    document = visual_document()
    document["asset_manifest"]["assets"][0]["media_kind"] = "video"
    assert document["required_capabilities"] == []
    recipe = EditRecipeV2.model_validate(document)
    assert "visualVideos" in recipe.required_capabilities
    assert "stillImages" not in recipe.required_capabilities
    assert recipe.model_dump(mode="json")["asset_manifest"]["assets"][0]["media_kind"] == "video"
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    photo = EditRecipeV2.model_validate(visual_document())
    assert "visualVideos" not in photo.required_capabilities
    assert recipe_digest(recipe) != recipe_digest(photo)


def test_photo_visual_document_carries_no_media_kind():
    # Round-1 photo recipes are pinned by digest; the default kind stays off the wire.
    recipe = EditRecipeV2.model_validate(visual_document())
    assert recipe.model_dump(mode="json") == visual_document() | {
        "required_capabilities": ["stillImages"]
    }
    explicit = visual_document()
    explicit["asset_manifest"]["assets"][0]["media_kind"] = "image"
    assert recipe_digest(EditRecipeV2.model_validate(explicit)) == recipe_digest(recipe)


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("image", "video", {"stillImages", "visualVideos"}),
        ("video", "image", {"stillImages", "visualVideos"}),
        ("image", "image", {"stillImages"}),
        ("video", "video", {"visualVideos"}),
    ],
)
def test_mixed_visual_kinds_derive_each_capability(first, second, expected):
    document = visual_document()
    if first == "video":
        document["asset_manifest"]["assets"][0]["media_kind"] = "video"
    recipe = EditRecipeV2.model_validate(add_visual_clip(document, second))
    assert recipe.required_capabilities & {"stillImages", "visualVideos"} == expected
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def card_document(**clip):
    document = visual_document()
    document["tracks"][0]["clips"][0] |= {"still_layout": "supporting_card"} | clip
    return document


def test_still_card_round_trips_and_changes_the_digest():
    recipe = EditRecipeV2.model_validate(card_document())
    assert recipe.tracks[0].clips[0].still_layout == "supporting_card"
    # The card draws the same pinned photo: no capability beyond stillImages.
    assert recipe.required_capabilities == {"stillImages"}
    assert recipe.model_dump(mode="json")["tracks"][0]["clips"][0]["still_layout"] == (
        "supporting_card"
    )
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    assert recipe_digest(recipe) != recipe_digest(EditRecipeV2.model_validate(visual_document()))
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(card_document(still_layout="polaroid"))


def test_absent_still_layout_stays_off_the_wire():
    # Every already-issued recipe digest depends on the key being omitted.
    for document in (json.loads(FIXTURE.read_text()), visual_document()):
        recipe = EditRecipeV2.model_validate(document)
        assert recipe.tracks[0].clips[0].still_layout is None
        assert "still_layout" not in recipe.model_dump_json()
        explicit = json.loads(json.dumps(document))
        explicit["tracks"][0]["clips"][0]["still_layout"] = None
        assert recipe_digest(EditRecipeV2.model_validate(explicit)) == recipe_digest(recipe)


def test_still_card_requires_a_visuals_photo():
    original = json.loads(FIXTURE.read_text())
    original["tracks"][0]["clips"][0]["still_layout"] = "supporting_card"
    with pytest.raises(ValidationError, match="requires a Visuals photo"):
        EditRecipeV2.model_validate(original)
    video = card_document()
    video["asset_manifest"]["assets"][0]["media_kind"] = "video"
    with pytest.raises(ValidationError, match="requires a Visuals photo"):
        EditRecipeV2.model_validate(video)
    # Per clip, not per recipe: a photo elsewhere does not excuse a video card.
    mixed = add_visual_clip(visual_document(), "video")
    mixed["tracks"][0]["clips"][1]["still_layout"] = "supporting_card"
    with pytest.raises(ValidationError, match="requires a Visuals photo"):
        EditRecipeV2.model_validate(mixed)
    mixed["tracks"][0]["clips"][1]["still_layout"] = None
    mixed["tracks"][0]["clips"][0]["still_layout"] = "supporting_card"
    EditRecipeV2.model_validate(mixed)


@pytest.mark.parametrize(
    "clip",
    [
        {"rate": 2.0},
        {"look": "golden_hour"},
        {"hold_duration": 1.0},
        {"hold_duration": 0.0},
        {"transform": {"scale": 1.2}},
        {"transform": {"position_x": 10.0}},
        {"transform": {"position_y": -10.0}},
    ],
    ids=["rate", "look", "hold", "zero-hold", "scale", "x", "y"],
)
def test_still_card_must_be_a_plain_clip(clip):
    # The native card is composed once from the photo alone; any of these
    # would be silently ignored on device. A rotation is the exception: the
    # card turns inside itself (`clipTilt`).
    EditRecipeV2.model_validate(card_document())
    with pytest.raises(ValidationError, match="plain V2 main-track clip"):
        EditRecipeV2.model_validate(card_document(**clip))


def test_still_card_must_sit_on_the_main_video_track():
    document = visual_document()
    overlay = document["tracks"][0]["clips"][0] | {"id": "overlay-clip", "transition": None}
    document["tracks"].append({"id": "overlay", "kind": "overlay", "clips": [overlay]})
    EditRecipeV2.model_validate(document)
    overlay["still_layout"] = "supporting_card"
    with pytest.raises(ValidationError, match="plain V2 main-track clip"):
        EditRecipeV2.model_validate(document)


def test_still_card_cannot_ride_a_v1_recipe():
    document = json.loads(FIXTURE.read_text())
    document["schema_version"] = 1
    document["renderer_version"] = "kria-ios-1"
    del document["asset_manifest"], document["text_layers"]
    EditRecipeV1.model_validate(document)
    document["tracks"][0]["clips"][0]["still_layout"] = "supporting_card"
    with pytest.raises(ValidationError, match="plain V2 main-track clip"):
        EditRecipeV1.model_validate(document)


# ---- KRI-589: freehand drawing layers ------------------------------------------------

_DRAWING_LAYER = {
    "id": "d1",
    "start": 0.5,
    "end": 1.5,
    "canvas_aspect": 0.5625,
    "center_x": 0.5,
    "center_y": 0.5,
    "scale": 1.0,
    "rotation_deg": 0.0,
    "entrance": "draw",
    "draw_duration": 0.6,
    "exit": "none",
    "strokes": [
        {
            "color": "#FF3B30",
            "opacity": 1.0,
            "width_frac": 0.012,
            "texture": "pen",
            "seed": 7,
            "points": [[0.1, 0.1], [0.4, 0.4]],
        }
    ],
}


def test_empty_drawing_layers_leave_the_recipe_byte_identical():
    """Old recipes (and their digests) must not change: the key is absent while empty."""
    document = json.loads(FIXTURE.read_text())
    recipe = EditRecipeV2.model_validate(document)
    assert "drawing_layers" not in recipe.model_dump(mode="json")
    assert "freehandDrawing" not in recipe.required_capabilities
    document["drawing_layers"] = []
    assert recipe_digest(EditRecipeV2.model_validate(document)) == recipe_digest(recipe)


def test_drawing_layers_declare_the_capability_and_change_the_digest():
    document = json.loads(FIXTURE.read_text())
    before = recipe_digest(EditRecipeV2.model_validate(document))
    document["drawing_layers"] = [_DRAWING_LAYER]
    recipe = EditRecipeV2.model_validate(document)
    assert "freehandDrawing" in recipe.required_capabilities
    assert recipe_digest(recipe) != before
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


@pytest.mark.parametrize("mutation", ["duplicate", "past_the_end", "draw_longer_than_window"])
def test_drawing_layers_must_be_unique_inside_the_timeline_and_the_window(mutation):
    document = json.loads(FIXTURE.read_text())
    layer = dict(_DRAWING_LAYER)
    if mutation == "duplicate":
        document["drawing_layers"] = [layer, layer]
    elif mutation == "past_the_end":
        document["drawing_layers"] = [{**layer, "end": 1700.0}]
    else:
        document["drawing_layers"] = [{**layer, "draw_duration": 5.0}]
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(document)


def test_pilot_validator_accepts_drawings_only_when_the_device_verified_them(monkeypatch):
    from app.config import settings
    from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe

    document = json.loads(FIXTURE.read_text())
    plain = EditRecipeV2.model_validate(document)
    document["drawing_layers"] = [_DRAWING_LAYER]
    drawn = EditRecipeV2.model_validate(document)
    verified = [*plain.required_capabilities, "freehandDrawing"]

    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(drawn)

    monkeypatch.setattr(settings, "phone_render_verified_features", verified[:-1])
    validate_phone_pilot_recipe(plain)  # no drawings: unaffected
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(drawn)
    assert "freehandDrawing" in str(refused.value.capability)
