"""Qendresa item 16: the speaker FULL SCREEN in their own take, then the corner (server contract).

Rows S76, S77, S79, S80 and S81 (server half) of
`docs/reviews/speaker-background-windows-failure-modes.md`: the wire contract shared with Swift
(`tests/fixtures/speaker-background-parity/v4.json`), the `speakerFrameWindows` capability, the
lane (cut remap, strip), the compile (clip to the edit) and the pilot validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.kria.device_render import recipe_digest
from app.kria.portable_speaker_background import SpeakerBackgroundLayer
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_subtitled_lanes import (
    SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT,
    SPEAKER_FULL_OPENING_FALLBACK_EVENT,
    PhoneSubtitledLaneRequest,
    PhoneSubtitledLanes,
    SubtitledBackground,
    SubtitledFullScreenWindow,
    remap_lanes_for_cut,
    strip_unsupported_background,
)
from app.services import pipeline_trace
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_rollout import PhoneCapabilityUnavailable, validate_phone_pilot_recipe
from tests.kria.test_speaker_background import PRE_LANE_DIGEST, background_document
from tests.kria.test_speaker_background_color import (
    PRE_COLOUR_IMAGE_DIGEST,
    PRE_COLOUR_VIDEO_DIGEST,
)
from tests.kria.test_speaker_background_windows import (
    ALL_VISUALS,
    FILL,
    SECOND,
    _background,
    _compile,
    _frame,
    _plan,
    _three_windows,
    _window,
)

PARITY_DIR = Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity"
PARITY_V4 = json.loads((PARITY_DIR / "v4.json").read_text())
OLDER = [json.loads((PARITY_DIR / name).read_text()) for name in ("v1.json", "v2.json", "v3.json")]
FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"


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


def _open(
    monkeypatch,
    *,
    opening=True,
    token=True,
    windows=True,
    base=True,
    enabled=True,
    flag=True,
) -> None:
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", flag)
    ours = {"speakerBackground", "speakerBackgroundWindows", "speakerFrameWindows"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if base:
        features.append("speakerBackground")
    if windows:
        features.append("speakerBackgroundWindows")
    if token:
        features.append("speakerFrameWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    if opening:
        set_client_protocol(38)


def _opening(start_s=0.0, end_s=4.0) -> SubtitledFullScreenWindow:
    return SubtitledFullScreenWindow(start_s=start_s, end_s=end_s)


def _with_opening(background: SubtitledBackground, *spans) -> SubtitledBackground:
    return background.model_copy(update={"full_screen_windows": tuple(spans or (_opening(),))})


# --- S76: Python and Swift agree on the new field (shared fixture) -----------------------------


@pytest.mark.parametrize("case", PARITY_V4["valid"], ids=lambda case: case["name"])
def test_shared_fixture_v4(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    assert layer.model_dump(mode="json") == case["normalized"]
    assert SpeakerBackgroundLayer.model_validate(layer.model_dump(mode="json")) == layer
    assert SpeakerBackgroundLayer.model_validate_json(layer.model_dump_json()) == layer


@pytest.mark.parametrize("case", PARITY_V4["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_v4_invalid_layers_are_rejected(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate_json(json.dumps(case["layer"]))


@pytest.mark.parametrize(
    "case",
    [case for fixture in OLDER for case in fixture["valid"]],
    ids=lambda case: case["name"],
)
def test_v1_to_v3_fixtures_still_normalize_unchanged(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    dumped = layer.model_dump(mode="json")
    assert dumped == case["normalized"]
    assert "full_screen_windows" not in dumped and not layer.uses_full_screen


@pytest.mark.parametrize(
    "case",
    [case for fixture in OLDER for case in fixture["invalid"]],
    ids=lambda case: case["name"],
)
def test_v1_to_v3_fixtures_still_reject(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])


# --- S79: the capability is derived only when used ---------------------------------------------


def test_a_recipe_without_the_field_keeps_exactly_todays_capabilities_and_digest():
    plain = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    assert recipe_digest(plain) == PRE_LANE_DIGEST
    image = EditRecipeV2.model_validate(background_document())
    video = EditRecipeV2.model_validate(background_document("video", source_start=3.0))
    assert recipe_digest(image) == PRE_COLOUR_IMAGE_DIGEST
    assert recipe_digest(video) == PRE_COLOUR_VIDEO_DIGEST
    for recipe in (plain, image, video):
        assert "speakerFrameWindows" not in recipe.required_capabilities
        assert "full_screen_windows" not in json.dumps(recipe.model_dump(mode="json"))
    explicit_null = background_document(full_screen_windows=None)
    assert recipe_digest(EditRecipeV2.model_validate(explicit_null)) == PRE_COLOUR_IMAGE_DIGEST


def test_full_screen_windows_require_the_speaker_frame_windows_capability():
    recipe = EditRecipeV2.model_validate(
        background_document(full_screen_windows=[{"start": 0, "end": 1}])
    )
    assert recipe.speaker_background.uses_full_screen
    assert {"speakerBackground", "speakerFrameWindows"} <= recipe.required_capabilities
    # Its own capability: the windows one is NOT implied by it.
    assert "speakerBackgroundWindows" not in recipe.required_capabilities
    assert recipe_digest(recipe) != PRE_COLOUR_IMAGE_DIGEST
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_a_full_screen_window_past_the_recipe_end_is_refused():
    plain = EditRecipeV2.model_validate(background_document())
    over = plain.duration + 1.0
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(
            background_document(full_screen_windows=[{"start": 0, "end": over}])
        )


def test_the_capability_is_a_known_recipe_capability():
    from typing import get_args

    from app.kria.recipes import MediaCapability

    assert "speakerFrameWindows" in get_args(MediaCapability)


# --- S76/S81: the lane --------------------------------------------------------------------------


def test_the_lane_dumps_without_the_field_while_unused_and_roundtrips_with_it():
    plain = PhoneSubtitledLaneRequest(background=_background()).model_dump(mode="json")
    assert "full_screen_windows" not in plain["background"]
    asked = PhoneSubtitledLaneRequest(background=_with_opening(_background()))
    dumped = asked.model_dump(mode="json")["background"]
    assert dumped["full_screen_windows"] == [{"start_s": 0.0, "end_s": 4.0}]
    assert PhoneSubtitledLaneRequest.model_validate(asked.model_dump(mode="json")) == asked


# --- compile: emits and clips to the edit (S80) -------------------------------------------------


def test_compile_emits_the_windows_when_the_gate_is_open(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_with_opening(_background(), _opening(0.0, 4.0), _opening(8.0, 9.0)))
    layer = recipe.speaker_background
    assert [(w.start, w.end) for w in layer.full_screen_windows] == [(0.0, 4.0), (8.0, 9.0)]
    assert "speakerFrameWindows" in recipe.required_capabilities
    assert recipe.model_dump(mode="json")["speaker_background"]["full_screen_windows"] == [
        {"start": 0.0, "end": 4.0},
        {"start": 8.0, "end": 9.0},
    ]


def test_compile_clips_to_the_edit_and_drops_what_falls_off_it(monkeypatch):
    _open(monkeypatch)
    # The edit is 10 s: [9.2, 14) clips to [9.2, 10) -> 0.8 s kept; [9.8, 14) leaves 0.2 s ->
    # dropped; [10, 12) starts at the end -> dropped.
    kept = _compile(_with_opening(_background(), _opening(0.0, 3.0), _opening(9.2, 14.0)))
    assert [(w.start, w.end) for w in kept.speaker_background.full_screen_windows] == [
        (0.0, 3.0),
        (9.2, 10.0),
    ]
    short = _compile(_with_opening(_background(), _opening(0.0, 3.0), _opening(9.8, 14.0)))
    assert [(w.start, w.end) for w in short.speaker_background.full_screen_windows] == [(0.0, 3.0)]
    off = _compile(_with_opening(_background(), _opening(0.0, 3.0), _opening(10.0, 12.0)))
    assert [(w.start, w.end) for w in off.speaker_background.full_screen_windows] == [(0.0, 3.0)]
    # Every window clipped away -> the field is simply absent (never an empty list).
    gone = _compile(_with_opening(_background(), _opening(10.0, 12.0)))
    assert gone.speaker_background.full_screen_windows is None
    assert "speakerFrameWindows" not in gone.required_capabilities
    assert "full_screen_windows" not in gone.model_dump(mode="json")["speaker_background"]


def test_compile_without_the_field_is_byte_identical(monkeypatch):
    _open(monkeypatch)
    assert _compile(_three_windows(frame=_frame())).model_dump_json() == (
        _compile(_three_windows(frame=_frame())).model_dump_json()
    )
    wire = _compile(_three_windows(frame=_frame())).model_dump(mode="json")["speaker_background"]
    assert "full_screen_windows" not in wire


# --- S80: a speech cut moves the switch ---------------------------------------------------------


def test_remap_moves_the_windows_into_cut_time():
    background = _with_opening(_three_windows(), _opening(0.0, 4.0), _opening(6.0, 9.0))
    plan = _plan((1.0, 2.0))  # one second of pause removed inside the opening
    remapped, dropped = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), plan)
    assert dropped == frozenset()
    assert [(w.start_s, w.end_s) for w in remapped.background.full_screen_windows] == [
        (0.0, 3.0),
        (5.0, 8.0),
    ]


def test_remap_works_when_only_the_full_screen_windows_exist():
    background = _with_opening(_background(), _opening(2.0, 5.0))
    remapped, _ = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), _plan((0.0, 1.0)))
    assert [(w.start_s, w.end_s) for w in remapped.background.full_screen_windows] == [(1.0, 4.0)]


def test_remap_clips_at_the_removal_edges():
    background = _with_opening(_background(), _opening(0.0, 4.0))
    # A removal [3, 6) eats the tail of the opening: it ends where the removal starts.
    tail, _ = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), _plan((3.0, 6.0)))
    assert [(w.start_s, w.end_s) for w in tail.background.full_screen_windows] == [(0.0, 3.0)]


def test_remap_drops_one_inside_a_removal_or_left_under_half_a_second():
    background = _with_opening(_background(), _opening(0.0, 2.0), _opening(3.0, 3.8))
    inside, _ = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), _plan((3.0, 4.0)))
    assert [(w.start_s, w.end_s) for w in inside.background.full_screen_windows] == [(0.0, 2.0)]
    short = _with_opening(_background(), _opening(0.0, 2.0))
    left, _ = remap_lanes_for_cut(PhoneSubtitledLanes(background=short), _plan((0.0, 1.7)))
    assert left.background.full_screen_windows == ()
    # Dropping an opening is not a dropped media receipt.
    _, dropped = remap_lanes_for_cut(PhoneSubtitledLanes(background=short), _plan((0.0, 1.7)))
    assert dropped == frozenset()


def test_a_cut_compile_keeps_the_switch_in_cut_time(monkeypatch):
    _open(monkeypatch)
    plan = _plan((1.0, 2.0))
    remapped, _ = remap_lanes_for_cut(
        PhoneSubtitledLanes(background=_with_opening(_background(), _opening(0.0, 4.0))), plan
    )
    cut = _compile(remapped.background, cut_plan=plan)
    assert cut.duration == pytest.approx(9.0)
    assert [(w.start, w.end) for w in cut.speaker_background.full_screen_windows] == [(0.0, 3.0)]


# --- S77: the gates -----------------------------------------------------------------------------


def _bg_all() -> SubtitledBackground:
    return _with_opening(
        _background(windows=(_window(SECOND, 0.0, 3.0),), frame=_frame()), _opening(0.0, 3.0)
    )


@pytest.mark.parametrize(
    ("kwargs", "protocol", "reason"),
    [
        ({"flag": False}, 38, "full_opening_disabled"),
        ({"token": False}, 38, "full_opening_token_not_verified"),
        ({}, 37, "client_protocol_below_min"),
        ({"windows": False}, 38, "windows_token_not_verified"),
        ({"enabled": False}, 38, "disabled"),
    ],
)
def test_a_closed_gate_drops_only_the_windows_and_traces_it(
    monkeypatch, events, kwargs, protocol, reason
):
    _open(monkeypatch, **kwargs)
    set_client_protocol(protocol)
    background = _with_opening(
        _background(windows=(_window(SECOND, 0.0, 3.0),), frame=_frame()), _opening(0.0, 3.0)
    )
    stripped = strip_unsupported_background(PhoneSubtitledLanes(background=background))
    if reason == "disabled":
        # The base switch wins: the whole lane goes, as it always did.
        assert stripped.background is None
        return
    if reason == "windows_token_not_verified":
        # Fill only: the windows gate also clears the opening (and the windows and the frame).
        kept = stripped.background
        assert kept.windows == () and kept.speaker_frame is None
        assert kept.full_screen_windows == () and kept.media_id == FILL.media_id
        assert [e[1] for e in events] == [SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT]
        assert events[0][2]["full_screen_window_count"] == 1
        return
    kept = stripped.background
    assert kept.full_screen_windows == ()
    assert len(kept.windows) == 1 and kept.speaker_frame is not None  # everything else stays
    assert kept.media_id == FILL.media_id
    assert [(e[0], e[1], e[2]) for e in events] == [
        (
            "phone",
            SPEAKER_FULL_OPENING_FALLBACK_EVENT,
            {"media_id": FILL.media_id, "reason": reason, "window_count": 1},
        )
    ]


def test_an_open_gate_changes_nothing_and_traces_nothing(monkeypatch, events):
    _open(monkeypatch)
    lanes = PhoneSubtitledLanes(background=_bg_all())
    assert strip_unsupported_background(lanes) is lanes
    assert events == []


def test_a_closed_gate_equals_a_request_that_never_asked(monkeypatch, events):
    _open(monkeypatch, flag=False)
    asked = _compile(_with_opening(_three_windows(frame=_frame()), _opening(0.0, 3.0)))
    plain = _compile(_three_windows(frame=_frame()))
    assert asked.model_dump_json() == plain.model_dump_json()
    assert "speakerFrameWindows" not in asked.required_capabilities


def test_a_windows_gate_closed_lane_without_opening_has_no_extra_trace_key(monkeypatch, events):
    _open(monkeypatch, windows=False)
    strip_unsupported_background(PhoneSubtitledLanes(background=_three_windows(frame=_frame())))
    assert "full_screen_window_count" not in events[0][2]


def test_strip_works_on_the_persisted_request_too(monkeypatch, events):
    _open(monkeypatch, flag=False)
    request = PhoneSubtitledLaneRequest(background=_with_opening(_background()))
    stripped = strip_unsupported_background(request)
    assert stripped.background.full_screen_windows == ()
    assert [e[1] for e in events] == [SPEAKER_FULL_OPENING_FALLBACK_EVENT]


# --- S76/S77: pilot validation ------------------------------------------------------------------


def test_pilot_validation_refuses_an_unverified_opening(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_with_opening(_background()))
    verified = [*recipe.required_capabilities]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(recipe)

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in verified if c != "speakerFrameWindows"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "speakerFrameWindows"

    # Also a recipe mutated after its capabilities were derived.
    recipe.required_capabilities = set()
    monkeypatch.setattr(settings, "phone_render_verified_features", ["speakerBackground"])
    with pytest.raises(PhoneCapabilityUnavailable) as late:
        validate_phone_pilot_recipe(recipe)
    assert late.value.capability == "speakerFrameWindows"
    assert ALL_VISUALS  # imported fixtures stay referenced
