"""KRI-1173: a solid-colour FILL behind the speaker (server half).

Rows S46-S53 and S60 of `docs/reviews/speaker-background-color-failure-modes.md`: the wire
contract shared with Swift (`tests/fixtures/speaker-background-parity/v3.json`), the lane, the
gates (protocol 34 + the `speakerBackgroundColor` token), the strip and the compile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.kria.device_render import recipe_digest
from app.kria.portable_speaker_background import SpeakerBackgroundLayer
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_subtitled_lanes import (
    SPEAKER_BACKGROUND_COLOR_FALLBACK_EVENT,
    SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT,
    PhoneSubtitledLaneRequest,
    PhoneSubtitledLanes,
    SubtitledBackground,
    SubtitledBackgroundWindow,
    SubtitledSpeakerFrame,
    background_media_refs,
    background_pins,
    remap_lanes_for_cut,
    strip_unsupported_background,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.silence_cut import CutPlan, Removal
from app.services import pipeline_trace
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    speaker_background_color_supported,
    speaker_background_color_unavailable_reason,
    validate_phone_pilot_recipe,
)
from app.services.phone_subtitled_editor import (
    lanes_from_recipe,
    speaker_background_document,
)
from tests.kria.test_speaker_background import PRE_LANE_DIGEST, background_document
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    _binding,
    _photo_visual,
    _pool_video_visual,
)

PARITY_DIR = Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity"
PARITY_V3 = json.loads((PARITY_DIR / "v3.json").read_text())
PARITY_V2 = json.loads((PARITY_DIR / "v2.json").read_text())
PARITY_V1 = json.loads((PARITY_DIR / "v1.json").read_text())
FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"

# Digests of `background_document()` recipes computed on origin/main (69fded894) BEFORE the colour
# fill existed: a photo / video fill must keep serialising byte-for-byte (S48).
PRE_COLOUR_IMAGE_DIGEST = "e62ab048c761e17620e4dcd3b6e8ed38c01734b90cfdd27acbe94b094f9070c5"
PRE_COLOUR_VIDEO_DIGEST = "6a5d5bf2c2aa0d8a21bc7ebf7cc8f922caa71ece405b603f444b692ccbc0b5e4"

PHOTO = _photo_visual()
WINDOW_PHOTO = _photo_visual(
    media_id="photo-2", gcs_path="users/owner/plan/item/pool/photo-2.jpg", generation="78"
)
WINDOW_VIDEO = _pool_video_visual()
COLOUR_TOKENS = ("speakerBackground", "speakerBackgroundColor")


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: seen.append((stage, event, data or {})),
    )
    return seen


def _open(monkeypatch, *, colour=True, windows=True, base=True, enabled=True) -> None:
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    ours = {"speakerBackground", "speakerBackgroundColor", "speakerBackgroundWindows"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if base:
        features.append("speakerBackground")
    if colour:
        features.append("speakerBackgroundColor")
    if windows:
        features.append("speakerBackgroundWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _colour(color="#FFFFFF", **changes) -> SubtitledBackground:
    return SubtitledBackground(**{"kind": "color", "color": color} | changes)


def _media() -> SubtitledBackground:
    return SubtitledBackground(
        media_id=PHOTO.media_id,
        gcs_path=PHOTO.gcs_path,
        generation=PHOTO.generation,
        kind="image",
    )


def _window(visual=WINDOW_PHOTO, start_s=0.0, end_s=3.0, **changes) -> SubtitledBackgroundWindow:
    return SubtitledBackgroundWindow(
        **{
            "media_id": visual.media_id,
            "gcs_path": visual.gcs_path,
            "generation": visual.generation,
            "kind": visual.kind,
            "start_s": start_s,
            "end_s": end_s,
        }
        | changes
    )


def _frame() -> SubtitledSpeakerFrame:
    return SubtitledSpeakerFrame(width_fraction=0.42, x_fraction=0.76, y_fraction=0.80)


def _compile(background=None, *, visuals=(), duration_s=10.0, **kwargs):
    lanes = PhoneSubtitledLanes(background=background) if background is not None else None
    return compile_phone_subtitled_plan(
        (_binding(duration_s=duration_s),),
        caption_cues=_CUES,
        visuals=visuals,
        lanes=lanes,
        **kwargs,
    )


# --- S46/S47: Python and Swift agree on the colour fields (shared fixture) ---------------------


@pytest.mark.parametrize("case", PARITY_V3["valid"], ids=lambda case: case["name"])
def test_shared_fixture_v3(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    assert layer.model_dump(mode="json") == case["normalized"]
    assert SpeakerBackgroundLayer.model_validate(layer.model_dump(mode="json")) == layer
    assert SpeakerBackgroundLayer.model_validate_json(layer.model_dump_json()) == layer


@pytest.mark.parametrize("case", PARITY_V3["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_v3_invalid_layers_are_rejected(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate_json(json.dumps(case["layer"]))


@pytest.mark.parametrize(
    "case", [*PARITY_V1["valid"], *PARITY_V2["valid"]], ids=lambda case: case["name"]
)
def test_v1_and_v2_fixtures_still_normalize_unchanged(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    dumped = layer.model_dump(mode="json")
    assert dumped == case["normalized"]
    assert "color" not in dumped and not layer.uses_color


@pytest.mark.parametrize(
    "case", [*PARITY_V1["invalid"], *PARITY_V2["invalid"]], ids=lambda case: case["name"]
)
def test_v1_and_v2_fixtures_still_reject(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])


# --- S48: a photo / video fill is byte-identical to today --------------------------------------


def test_media_fill_is_byte_identical_to_today():
    recipe = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    assert recipe_digest(recipe) == PRE_LANE_DIGEST
    image = EditRecipeV2.model_validate(background_document())
    video = EditRecipeV2.model_validate(background_document("video", source_start=3.0))
    assert recipe_digest(image) == PRE_COLOUR_IMAGE_DIGEST
    assert recipe_digest(video) == PRE_COLOUR_VIDEO_DIGEST
    wire = image.model_dump(mode="json")["speaker_background"]
    assert set(wire) == {"asset_id", "media_kind", "source_start", "loop", "subject_track_id"}
    assert "speakerBackgroundColor" not in image.required_capabilities


def test_a_lane_media_fill_dumps_without_the_colour_key():
    dumped = PhoneSubtitledLaneRequest(background=_media()).model_dump(mode="json")["background"]
    assert dumped == {
        "media_id": PHOTO.media_id,
        "gcs_path": PHOTO.gcs_path,
        "generation": PHOTO.generation,
        "kind": "image",
        "source_start_s": 0.0,
    }


# --- the recipe: a colour needs no manifest asset ----------------------------------------------


def test_a_colour_fill_needs_no_asset_and_declares_its_capabilities():
    document = json.loads(FIXTURE.read_text()) | {
        "speaker_background": {"media_kind": "color", "color": "#00FF00"}
    }
    recipe = EditRecipeV2.model_validate(document)
    assert set(COLOUR_TOKENS) <= recipe.required_capabilities
    assert "speakerBackgroundWindows" not in recipe.required_capabilities
    assert not {"stillImages", "visualVideos"} & (
        recipe.required_capabilities
        - EditRecipeV2.model_validate_json(FIXTURE.read_text()).required_capabilities
    )
    assert recipe.model_dump(mode="json")["speaker_background"] == {
        "media_kind": "color",
        "color": "#00FF00",
        "source_start": 0.0,
        "loop": True,
        "subject_track_id": "subtitled",
    }
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_the_colour_capability_enum_is_in_the_media_capability_vocabulary():
    from typing import get_args

    from app.kria.recipes import MediaCapability

    assert "speakerBackgroundColor" in get_args(MediaCapability)


# --- S49: the gates -----------------------------------------------------------------------------


def test_the_colour_protocol_is_34_and_the_token_is_dark_in_production():
    assert Settings.model_fields["speaker_background_color_min_client_protocol"].default == 34
    assert "speakerBackgroundColor" not in Settings.model_fields[
        "phone_render_verified_features"
    ].get_default(call_default_factory=True)


def test_protocol_below_34_strips_the_colour_lane(monkeypatch, events):
    _open(monkeypatch)
    set_client_protocol(33)
    asked = _compile(_colour("#FFFFFF"))
    assert asked.speaker_background is None
    assert asked.model_dump_json() == _compile().model_dump_json()
    assert [(e[1], e[2]) for e in events] == [
        (
            SPEAKER_BACKGROUND_COLOR_FALLBACK_EVENT,
            {
                "reason": "client_protocol_below_min",
                "color": "#FFFFFF",
                "window_count": 0,
                "had_frame": False,
            },
        )
    ]
    events.clear()
    set_client_protocol(34)
    assert _compile(_colour("#FFFFFF")).speaker_background.color == "#FFFFFF"
    assert events == []


def test_a_media_fill_is_untouched_by_the_colour_gate(monkeypatch, events):
    _open(monkeypatch, colour=False)
    set_client_protocol(33)
    background = _media()
    kept = strip_unsupported_background(PhoneSubtitledLanes(background=background))
    assert kept.background == background
    assert _compile(background, visuals=(PHOTO,)).speaker_background.asset_id == (
        f"visual-{PHOTO.media_id}"
    )
    assert events == []


# --- S50: token missing / kill switch off -------------------------------------------------------


def test_colour_token_missing_strips_the_lane(monkeypatch, events):
    _open(monkeypatch, colour=False)
    background = _colour("#1A2B3C", windows=(_window(),), speaker_frame=_frame())
    asked = _compile(background, visuals=(WINDOW_PHOTO,))
    # The whole lane goes: no media fill to fall back to, and windows need a fill.
    assert asked.speaker_background is None
    assert asked.model_dump_json() == _compile().model_dump_json()
    assert [(e[1], e[2]) for e in events] == [
        (
            SPEAKER_BACKGROUND_COLOR_FALLBACK_EVENT,
            {
                "reason": "color_token_not_verified",
                "color": "#1A2B3C",
                "window_count": 1,
                "had_frame": True,
            },
        )
    ]


def test_base_gate_closed_strips_the_colour_lane_with_the_base_event(monkeypatch, events):
    _open(monkeypatch, enabled=False)
    asked = _compile(_colour("#FFFFFF"))
    assert asked.speaker_background is None
    assert [(e[1], e[2]) for e in events] == [
        ("speaker_background_phone_fallback", {"color": "#FFFFFF", "reason": "disabled"})
    ]


def test_a_media_fill_closed_by_the_base_gate_keeps_its_media_id_in_the_trace(monkeypatch, events):
    _open(monkeypatch, enabled=False)
    assert _compile(_media(), visuals=(PHOTO,)).speaker_background is None
    assert events[0][2] == {"media_id": PHOTO.media_id, "reason": "disabled"}


def test_windows_gate_closed_keeps_the_colour_and_drops_the_windows(monkeypatch, events):
    _open(monkeypatch, windows=False)
    background = _colour("#FFFFFF", windows=(_window(),), speaker_frame=_frame())
    stripped = _compile(background, visuals=(WINDOW_PHOTO,))
    assert stripped.model_dump_json() == _compile(_colour("#FFFFFF")).model_dump_json()
    assert stripped.speaker_background.color == "#FFFFFF"
    assert [(e[1], e[2]) for e in events] == [
        (
            SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT,
            {"reason": "windows_token_not_verified", "window_count": 1, "had_frame": True},
        )
    ]


def test_the_colour_reason_names_the_first_failing_check(monkeypatch):
    _open(monkeypatch)
    assert speaker_background_color_unavailable_reason(require_client=False) is None
    assert speaker_background_color_supported(require_client=False)
    set_client_protocol(33)
    assert speaker_background_color_unavailable_reason(require_client=True) == (
        "client_protocol_below_min"
    )
    assert not speaker_background_color_supported(require_client=True)
    _open(monkeypatch, colour=False)
    assert speaker_background_color_unavailable_reason(require_client=False) == (
        "color_token_not_verified"
    )
    _open(monkeypatch, base=False)
    assert speaker_background_color_unavailable_reason(require_client=False) == (
        "token_not_verified"
    )
    _open(monkeypatch, enabled=False)
    assert speaker_background_color_unavailable_reason(require_client=False) == "disabled"


def test_pilot_validation_refuses_unverified_colour(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_colour("#FFFFFF"))
    verified = [*recipe.required_capabilities]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(recipe)

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in verified if c != "speakerBackgroundColor"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "speakerBackgroundColor"

    # The explicit gate also covers a recipe mutated after its capabilities were derived.
    recipe.required_capabilities = set()
    monkeypatch.setattr(settings, "phone_render_verified_features", ["speakerBackground"])
    with pytest.raises(PhoneCapabilityUnavailable) as late:
        validate_phone_pilot_recipe(recipe)
    assert late.value.capability == "speakerBackgroundColor"

    # A photo fill stays valid with only the base token.
    _open(monkeypatch, colour=False)
    media = _compile(_media(), visuals=(PHOTO,))
    monkeypatch.setattr(
        settings, "phone_render_verified_features", sorted(media.required_capabilities)
    )
    validate_phone_pilot_recipe(media)


# --- S52: a colour under windows and a corner speaker -------------------------------------------


def test_colour_fill_with_windows_compiles(monkeypatch):
    _open(monkeypatch)
    background = _colour(
        "#1A2B3C",
        windows=(
            _window(WINDOW_PHOTO, 0.0, 3.0),
            _window(WINDOW_VIDEO, 5.0, 8.0, source_start_s=1.5),
        ),
        speaker_frame=_frame(),
    )
    recipe = _compile(background, visuals=(WINDOW_PHOTO, WINDOW_VIDEO))
    layer = recipe.speaker_background
    assert (layer.media_kind, layer.color, layer.asset_id) == ("color", "#1A2B3C", None)
    assert [(w.asset_id, w.media_kind, w.start, w.end) for w in layer.windows] == [
        (f"visual-{WINDOW_PHOTO.media_id}", "image", 0.0, 3.0),
        (f"visual-{WINDOW_VIDEO.media_id}", "video", 5.0, 8.0),
    ]
    assert layer.speaker_frame.x_fraction == 0.76
    assert {
        "speakerBackground",
        "speakerBackgroundColor",
        "speakerBackgroundWindows",
        "stillImages",
        "visualVideos",
    } <= recipe.required_capabilities
    # Only the windows' visuals are in the manifest: the colour pins nothing.
    assert {a.id for a in recipe.asset_manifest.assets} >= {
        f"visual-{WINDOW_PHOTO.media_id}",
        f"visual-{WINDOW_VIDEO.media_id}",
    }
    assert f"visual-{PHOTO.media_id}" not in {a.id for a in recipe.asset_manifest.assets}
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    monkeypatch.setattr(
        settings, "phone_render_verified_features", sorted(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_a_colour_alone_compiles_to_the_plain_recipe_plus_the_layer(monkeypatch):
    _open(monkeypatch)
    plain = _compile()
    coloured = _compile(_colour("#FFFFFF"))
    assert coloured.model_dump(
        mode="json", exclude={"speaker_background", "required_capabilities"}
    ) == (plain.model_dump(mode="json", exclude={"required_capabilities"}))
    assert coloured.required_capabilities - plain.required_capabilities == set(COLOUR_TOKENS)
    assert coloured.assets == plain.assets
    assert coloured.asset_manifest == plain.asset_manifest


def test_a_colour_window_without_a_pinned_visual_fails_the_lane(monkeypatch):
    from app.pipeline.phone_subtitled_lanes import SubtitledLaneError

    _open(monkeypatch)
    with pytest.raises(SubtitledLaneError) as failed:
        _compile(_colour("#FFFFFF", windows=(_window(),)), visuals=())
    assert failed.value.lane == "background"


# --- the lane model -----------------------------------------------------------------------------


def test_the_lane_stores_the_colour_uppercase_but_the_recipe_wire_is_strict():
    assert _colour("#abcdef").color == "#ABCDEF"
    assert _colour(" #00ff00 ").color == "#00FF00"
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer(media_kind="color", color="#abcdef")


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "color"},
        {"kind": "color", "color": "white"},
        {"kind": "color", "color": "#FFF"},
        {"kind": "color", "color": "#FFFFFF80"},
        {"kind": "color", "color": "#FFFFFF", "media_id": "photo-1"},
        {"kind": "color", "color": "#FFFFFF", "source_start_s": 2.0},
        {"kind": "image", "color": "#FFFFFF", "media_id": "p", "gcs_path": "g", "generation": "1"},
        {"kind": "image"},
        {"kind": "video", "media_id": "p"},
    ],
)
def test_the_lane_rejects_a_malformed_fill(fields):
    with pytest.raises(ValidationError):
        SubtitledBackground(**fields)


def test_the_persisted_request_round_trips_a_colour_with_windows():
    request = PhoneSubtitledLaneRequest(
        background=_colour("#1A2B3C", windows=(_window(),), speaker_frame=_frame())
    )
    dumped = request.model_dump(mode="json")["background"]
    assert dumped["kind"] == "color" and dumped["color"] == "#1A2B3C"
    assert not {"media_id", "gcs_path", "generation"} & set(dumped)
    assert PhoneSubtitledLaneRequest.model_validate(request.model_dump(mode="json")) == request


# --- S60: a colour reserves and pins nothing ----------------------------------------------------


def test_background_media_refs_and_pins_of_a_colour_cover_only_its_windows():
    assert background_media_refs(_colour()) == ()
    assert background_pins(_colour()) == {}
    with_windows = _colour(
        windows=(_window(WINDOW_PHOTO, 0.0, 2.0), _window(WINDOW_VIDEO, 3.0, 5.0))
    )
    assert [ref[0] for ref in background_media_refs(with_windows)] == [
        WINDOW_PHOTO.media_id,
        WINDOW_VIDEO.media_id,
    ]
    assert set(background_pins(with_windows)) == {WINDOW_PHOTO.media_id, WINDOW_VIDEO.media_id}


def test_a_speech_cut_moves_the_windows_over_a_colour_and_never_the_colour():
    background = _colour(
        "#FFFFFF", windows=(_window(WINDOW_PHOTO, 2.0, 5.0),), speaker_frame=_frame()
    )
    plan = CutPlan(
        keep_segments=[(0.0, 1.0), (2.0, 10.0)],
        removed=[Removal(start_s=1.0, end_s=2.0, reason="pause")],
        time_saved_s=1.0,
    )
    remapped, dropped = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), plan)
    assert dropped == frozenset()
    kept = remapped.background
    assert (kept.kind, kept.color, kept.media_id) == ("color", "#FFFFFF", None)
    assert [(w.start_s, w.end_s) for w in kept.windows] == [(1.0, 4.0)]
    assert kept.speaker_frame == background.speaker_frame


# --- the editor document and the recipe round trip ----------------------------------------------


def test_the_editor_document_of_a_colour_has_no_media_id_or_source_start():
    assert speaker_background_document(_colour("#FFFFFF")) == {
        "kind": "color",
        "color": "#FFFFFF",
    }
    document = speaker_background_document(
        _colour("#FFFFFF", windows=(_window(),), speaker_frame=_frame())
    )
    assert set(document) == {"kind", "color", "windows", "speaker_frame"}
    # A media document keeps today's shape.
    assert speaker_background_document(_media()) == {
        "media_id": PHOTO.media_id,
        "kind": "image",
        "source_start_s": 0.0,
    }


def test_lanes_from_recipe_round_trips_a_colour_layer(monkeypatch):
    _open(monkeypatch)
    background = _colour(
        "#1A2B3C", windows=(_window(WINDOW_PHOTO, 0.0, 3.0),), speaker_frame=_frame()
    )
    recipe = _compile(background, visuals=(WINDOW_PHOTO,))
    rebuilt = lanes_from_recipe(recipe, visuals=(WINDOW_PHOTO,))
    assert rebuilt.background == background
    plain_colour = lanes_from_recipe(_compile(_colour("#FFFFFF")), visuals=())
    assert plain_colour.background == _colour("#FFFFFF")


def test_the_strip_keeps_a_media_fill_and_its_windows_exactly_as_today(monkeypatch, events):
    _open(monkeypatch)
    set_client_protocol(32)
    background = SubtitledBackground(
        media_id=PHOTO.media_id,
        gcs_path=PHOTO.gcs_path,
        generation=PHOTO.generation,
        kind="image",
        windows=(_window(),),
        speaker_frame=_frame(),
    )
    lanes = PhoneSubtitledLanes(background=background)
    assert strip_unsupported_background(lanes) is lanes
    assert events == []
