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


# --- KRI-1117: the Talk-to-camera lane (schema, compile, gates) ------------------------------

from app.config import settings  # noqa: E402
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan  # noqa: E402
from app.pipeline.phone_subtitled_lanes import (  # noqa: E402
    PhoneSubtitledLaneRequest,
    PhoneSubtitledLanes,
    SubtitledBackground,
    SubtitledLaneError,
    drop_lane,
    lane_names,
    remap_lanes_for_cut,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan  # noqa: E402
from app.pipeline.silence_cut import CutPlan, Removal  # noqa: E402
from app.services import pipeline_trace  # noqa: E402
from app.services.client_protocol import (  # noqa: E402
    clear_request_context,
    set_client_protocol,
)
from app.services.phone_rollout import validate_phone_pilot_recipe  # noqa: E402
from tests.pipeline.test_phone_subtitled_plan import (  # noqa: E402
    _CUES,
    _binding,
    _photo_visual,
    _pool_video_visual,
)

PHOTO = _photo_visual()
VIDEO = _pool_video_visual()


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _gate(monkeypatch, *, enabled=True, verified=True):
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    features = [f for f in settings.phone_render_verified_features if f != "speakerBackground"]
    if verified:
        features.append("speakerBackground")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _background(visual=PHOTO, **changes) -> SubtitledBackground:
    return SubtitledBackground(
        **{
            "media_id": visual.media_id,
            "gcs_path": visual.gcs_path,
            "generation": visual.generation,
            "kind": visual.kind,
        }
        | changes
    )


def _compile(background=None, *, visuals=(PHOTO, VIDEO), duration_s=10.0, **kwargs):
    lanes = PhoneSubtitledLanes(background=background) if background is not None else None
    return compile_phone_subtitled_plan(
        (_binding(duration_s=duration_s),),
        caption_cues=_CUES,
        visuals=visuals,
        lanes=lanes,
        **kwargs,
    )


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: seen.append((stage, event, data or {})),
    )
    return seen


def test_flag_off_is_byte_identical(monkeypatch, events):
    _gate(monkeypatch, enabled=False)
    asked = _compile(_background())
    never_asked = _compile()
    assert asked.model_dump_json() == never_asked.model_dump_json()
    assert recipe_digest(asked) == recipe_digest(never_asked)
    assert asked.speaker_background is None
    assert not any(asset.id.startswith("visual-") for asset in asked.assets)
    assert events == [
        (
            "phone",
            "speaker_background_phone_fallback",
            {"media_id": PHOTO.media_id, "reason": "disabled"},
        )
    ]


def test_verified_token_missing_strips(monkeypatch, events):
    _gate(monkeypatch, verified=False)
    asked = _compile(_background())
    assert asked.model_dump_json() == _compile().model_dump_json()
    assert [e[2]["reason"] for e in events] == ["token_not_verified"]


def test_protocol_below_min_strips(monkeypatch, events):
    _gate(monkeypatch)
    set_client_protocol(26)
    asked = _compile(_background())
    assert asked.model_dump_json() == _compile().model_dump_json()
    assert [e[2]["reason"] for e in events] == ["client_protocol_below_min"]
    set_client_protocol(27)
    assert _compile(_background()).speaker_background is not None


def test_a_worker_with_no_app_build_compiles_when_flag_and_token_hold(monkeypatch, events):
    _gate(monkeypatch)
    assert _compile(_background()).speaker_background is not None
    assert events == []


def test_supported_photo_compiles_validates_and_derives_the_capability(monkeypatch):
    _gate(monkeypatch)
    recipe = _compile(_background())
    layer = recipe.speaker_background
    assert layer is not None
    assert (layer.asset_id, layer.media_kind, layer.source_start, layer.loop) == (
        f"visual-{PHOTO.media_id}",
        "image",
        0.0,
        True,
    )
    assert layer.subject_track_id == "subtitled"
    assert "speakerBackground" in recipe.required_capabilities
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    assert manifest[layer.asset_id].visual_id == PHOTO.media_id
    assert manifest[layer.asset_id].generation == PHOTO.generation
    assert {asset.id for asset in recipe.assets} == set(manifest)
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    monkeypatch.setattr(
        settings, "phone_render_verified_features", sorted(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_a_video_background_keeps_its_start_and_loops(monkeypatch):
    _gate(monkeypatch)
    recipe = _compile(_background(VIDEO, source_start_s=2.5))
    layer = recipe.speaker_background
    assert (layer.media_kind, layer.source_start, layer.loop) == ("video", 2.5, True)
    assert {"speakerBackground", "visualVideos"} <= recipe.required_capabilities
    media = next(a for a in recipe.assets if a.id == layer.asset_id)
    assert media.duration == VIDEO.duration_s


def test_a_background_adds_no_clip_and_never_touches_the_main_track(monkeypatch):
    _gate(monkeypatch)
    with_background, without = _compile(_background(VIDEO)), _compile()
    assert [t.model_dump(mode="json") for t in with_background.tracks] == [
        t.model_dump(mode="json") for t in without.tracks
    ]
    assert with_background.duration == without.duration


@pytest.mark.parametrize(
    "case",
    ["unpinned", "kind_mismatch", "start_past_end", "start_at_end", "no_probed_duration"],
)
def test_an_unplayable_background_fails_its_lane_and_dropping_it_restores_the_edit(
    monkeypatch, case
):
    _gate(monkeypatch)
    visuals = (PHOTO, VIDEO)
    if case == "unpinned":
        background = _background(PHOTO, generation="not-the-pinned-generation")
    elif case == "kind_mismatch":
        background = _background(VIDEO, kind="image")
    elif case == "start_past_end":
        background = _background(VIDEO, source_start_s=VIDEO.duration_s + 1)
    elif case == "start_at_end":
        background = _background(VIDEO, source_start_s=VIDEO.duration_s)
    else:
        background = _background(VIDEO)
        visuals = (PHOTO, VIDEO.model_copy(update={"duration_s": None}))
    with pytest.raises(SubtitledLaneError) as failed:
        _compile(background, visuals=visuals)
    assert failed.value.lane == "background"
    assert isinstance(failed.value, UnsupportedPhonePlan)
    lanes = drop_lane(PhoneSubtitledLanes(background=background), "background")
    assert lane_names(lanes) == ()
    retried = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),), caption_cues=_CUES, visuals=visuals, lanes=lanes
    )
    assert retried.model_dump_json() == _compile().model_dump_json()


def test_the_lane_model_refuses_a_photo_with_a_start_and_a_negative_start():
    with pytest.raises(ValidationError):
        _background(PHOTO, source_start_s=1.0)
    with pytest.raises(ValidationError):
        _background(VIDEO, source_start_s=-0.1)


def test_a_request_without_a_background_dumps_exactly_as_before():
    assert "background" not in PhoneSubtitledLaneRequest().model_dump(mode="json")
    assert "background" not in PhoneSubtitledLanes().model_dump(mode="json")
    request = PhoneSubtitledLaneRequest(background=_background())
    assert request.model_dump(mode="json")["background"]["media_id"] == PHOTO.media_id
    assert PhoneSubtitledLaneRequest.model_validate(request.model_dump(mode="json")) == request


def test_speaker_background_survives_speech_cut(monkeypatch):
    """S13: the background is whole-edit, so a cut moves nothing in it."""
    _gate(monkeypatch)
    background = _background(VIDEO, source_start_s=1.0)
    lanes = PhoneSubtitledLanes(background=background)
    plan = CutPlan(
        keep_segments=[(0.0, 2.0), (4.0, 10.0)],
        removed=[Removal(start_s=2.0, end_s=4.0, reason="pause")],
        time_saved_s=2.0,
    )
    remapped, dropped = remap_lanes_for_cut(lanes, plan)
    assert remapped is not None and remapped.background == background
    assert dropped == frozenset()
    cut = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=_CUES,
        visuals=(VIDEO,),
        lanes=remapped,
        cut_plan=plan,
    )
    uncut = _compile(background, visuals=(VIDEO,))
    assert cut.duration < uncut.duration
    assert cut.speaker_background == uncut.speaker_background


def test_the_production_profile_strips_the_lane(monkeypatch, events):
    """Prod has neither the flag nor the `speakerBackground` token."""
    from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile

    apply_prod_profile(monkeypatch)
    assert "speakerBackground" not in PROD_VERIFIED_FEATURES
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    asked = _compile(_background())
    assert asked.speaker_background is None
    assert asked.model_dump_json() == _compile().model_dump_json()
    assert [e[2]["reason"] for e in events] == ["token_not_verified"]
