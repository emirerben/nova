"""KRI-1558: the speaker's place and size per section (server contract).

Rows 15-19 and 22 of `docs/reviews/kri-1558-section-layout-failure-modes.md`: the wire contract
shared with Swift (`tests/fixtures/speaker-background-parity/v6.json`), the `speakerSections`
capability, the lane (cut remap, strip), the compile (clip to the edit) and the pilot validation.
Everything runs through the real lane models, `compile_phone_subtitled_plan` and `EditRecipeV2`.
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
    SPEAKER_SECTIONS_FALLBACK_EVENT,
    PhoneSubtitledLaneRequest,
    PhoneSubtitledLanes,
    SubtitledBackground,
    SubtitledFullScreenWindow,
    SubtitledLayoutWindow,
    remap_lanes_for_cut,
    strip_unsupported_background,
)
from app.services import pipeline_trace
from app.services.client_protocol import (
    SPEAKER_SECTIONS_PROTOCOL,
    clear_request_context,
    set_client_features,
    set_client_protocol,
)
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    speaker_sections_client_supported,
    speaker_sections_planned_for,
    speaker_sections_planned_reason,
    speaker_sections_supported,
    speaker_sections_unavailable_reason,
    validate_phone_pilot_recipe,
)
from tests.kria.test_speaker_background import PRE_LANE_DIGEST, background_document
from tests.kria.test_speaker_background_color import PRE_COLOUR_IMAGE_DIGEST
from tests.kria.test_speaker_background_windows import (
    SECOND,
    THIRD,
    _background,
    _compile,
    _frame,
    _plan,
    _window,
)

PARITY_DIR = Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity"
PARITY_V6 = json.loads((PARITY_DIR / "v6.json").read_text())
OLDER = [
    json.loads((PARITY_DIR / name).read_text())
    for name in ("v1.json", "v2.json", "v3.json", "v4.json", "v5.json")
]
FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"
OURS = {"speakerBackground", "speakerBackgroundWindows", "speakerSections"}


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


def _open(monkeypatch, *, flag=True, token=True, windows=True, base=True, enabled=True) -> None:
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    monkeypatch.setattr(settings, "speaker_sections_enabled", flag)
    features = [f for f in settings.phone_render_verified_features if f not in OURS]
    if base:
        features.append("speakerBackground")
    if windows:
        features.append("speakerBackgroundWindows")
    if token:
        features.append("speakerSections")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _span(start_s, end_s, layout="own", preset=None) -> SubtitledLayoutWindow:
    return SubtitledLayoutWindow(
        start_s=start_s,
        end_s=end_s,
        layout=layout,
        speaker_frame=_frame(preset) if preset else None,
    )


def _sections_background(**changes) -> SubtitledBackground:
    """Own 0-3 s, small bottom-right over SECOND 3-6 s, THIRD alone 6-8 s, own 8-10 s."""
    return _background(
        windows=(_window(SECOND, 3.0, 6.0), _window(THIRD, 6.0, 8.0)),
        layout_windows=(
            _span(0.0, 3.0),
            _span(3.0, 6.0, "small", "small_bottom_right"),
            _span(6.0, 8.0, "picture"),
            _span(8.0, 10.0),
        ),
        **changes,
    )


def _layouts(layer) -> list[tuple[float, float, str]]:
    return [(w.start, w.end, w.layout) for w in layer.layout_windows or ()]


# --- row 16: Python and Swift agree on the new field (shared fixture) -------------------------


@pytest.mark.parametrize("case", PARITY_V6["valid"], ids=lambda case: case["name"])
def test_shared_fixture_v6(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    assert layer.model_dump(mode="json") == case["normalized"]
    assert SpeakerBackgroundLayer.model_validate(layer.model_dump(mode="json")) == layer
    assert SpeakerBackgroundLayer.model_validate_json(layer.model_dump_json()) == layer
    assert json.loads(layer.model_dump_json()) == case["normalized"]


@pytest.mark.parametrize("case", PARITY_V6["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_v6_invalid_layers_are_rejected(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate_json(json.dumps(case["layer"]))


def test_the_fixture_covers_the_shapes_the_phone_must_agree_on():
    valid = {case["name"] for case in PARITY_V6["valid"]}
    invalid = {case["name"] for case in PARITY_V6["invalid"]}
    assert {"forty-windows", "null-reads-as-absent", "frame-at-its-bounds"} <= valid
    assert {
        "empty-list",
        "overlap",
        "small-without-a-frame",
        "frame-on-own",
        "more-than-forty",
        "past-the-timeline-limit",
    } <= invalid


# --- row 15: nothing changes without the field ------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [case for fixture in OLDER for case in fixture["valid"]],
    ids=lambda case: case["name"],
)
def test_v1_to_v5_fixtures_still_normalize_unchanged(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    dumped = layer.model_dump(mode="json")
    assert dumped == case["normalized"]
    assert "layout_windows" not in dumped and not layer.uses_layouts


def test_a_recipe_without_the_field_keeps_todays_capabilities_and_digest():
    plain = EditRecipeV2.model_validate_json(FIXTURE.read_text())
    assert recipe_digest(plain) == PRE_LANE_DIGEST
    image = EditRecipeV2.model_validate(background_document())
    assert recipe_digest(image) == PRE_COLOUR_IMAGE_DIGEST
    assert "speakerSections" not in image.required_capabilities
    explicit_null = background_document(layout_windows=None)
    assert recipe_digest(EditRecipeV2.model_validate(explicit_null)) == PRE_COLOUR_IMAGE_DIGEST


def test_layout_windows_require_their_own_capability_and_round_trip():
    recipe = EditRecipeV2.model_validate(
        background_document(layout_windows=[{"start": 0, "end": 1, "layout": "picture"}])
    )
    assert recipe.speaker_background.uses_layouts
    assert {"speakerBackground", "speakerSections"} <= recipe.required_capabilities
    # A layout window is not a background window: that capability is not implied.
    assert "speakerBackgroundWindows" not in recipe.required_capabilities
    assert recipe_digest(recipe) != PRE_COLOUR_IMAGE_DIGEST
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_a_layout_window_past_the_recipe_end_is_refused():
    over = EditRecipeV2.model_validate(background_document()).duration + 1.0
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(
            background_document(layout_windows=[{"start": 0, "end": over, "layout": "own"}])
        )


def test_the_capability_is_a_known_recipe_capability():
    from typing import get_args

    from app.kria.recipes import MediaCapability

    assert "speakerSections" in get_args(MediaCapability)


# --- the lane --------------------------------------------------------------------------------


def test_the_lane_dumps_without_the_field_while_unused_and_round_trips_with_it():
    plain = PhoneSubtitledLaneRequest(background=_background()).model_dump(mode="json")
    assert "layout_windows" not in plain["background"]
    asked = PhoneSubtitledLaneRequest(background=_sections_background())
    dumped = asked.model_dump(mode="json")["background"]["layout_windows"]
    assert dumped[0] == {"start_s": 0.0, "end_s": 3.0, "layout": "own"}
    assert dumped[1]["speaker_frame"] == _frame().model_dump()
    assert PhoneSubtitledLaneRequest.model_validate(asked.model_dump(mode="json")) == asked


@pytest.mark.parametrize(
    "spans",
    [
        ({"start_s": 0, "end_s": 2, "layout": "small"},),  # a corner without a frame
        ({"start_s": 0, "end_s": 2, "layout": "own", "speaker_frame": _frame().model_dump()},),
        ({"start_s": 0, "end_s": 0.3, "layout": "own"},),
        ({"start_s": 0, "end_s": 4, "layout": "own"}, {"start_s": 3, "end_s": 6, "layout": "own"}),
    ],
    ids=["small_without_frame", "frame_on_own", "short", "overlap"],
)
def test_the_lane_refuses_what_the_phone_would_refuse(spans):
    raw = _background().model_dump(mode="json") | {"layout_windows": list(spans)}
    with pytest.raises(ValidationError):
        SubtitledBackground.model_validate(raw)


# --- row 17: compile emits and clips to the edit ----------------------------------------------


def test_compile_emits_the_layouts_when_the_gate_is_open(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_sections_background())
    layer = recipe.speaker_background
    assert _layouts(layer) == [
        (0.0, 3.0, "own"),
        (3.0, 6.0, "small"),
        (6.0, 8.0, "picture"),
        (8.0, 10.0, "own"),
    ]
    assert layer.layout_windows[1].frame.model_dump() == _frame().model_dump()
    assert layer.speaker_frame is None
    assert {"speakerSections", "speakerBackgroundWindows"} <= recipe.required_capabilities
    # What the phone receives decodes as a recipe again, unchanged.
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_compile_clips_to_the_edit_and_drops_what_falls_off_it(monkeypatch):
    _open(monkeypatch)
    # The edit is 10 s: [9.2, 14) clips to [9.2, 10); [9.8, 14) leaves 0.2 s -> dropped.
    kept = _compile(_background(layout_windows=(_span(0.0, 3.0), _span(9.2, 14.0, "picture"))))
    assert _layouts(kept.speaker_background) == [(0.0, 3.0, "own"), (9.2, 10.0, "picture")]
    short = _compile(_background(layout_windows=(_span(0.0, 3.0), _span(9.8, 14.0, "picture"))))
    assert _layouts(short.speaker_background) == [(0.0, 3.0, "own")]
    gone = _compile(_background(layout_windows=(_span(10.0, 12.0),)))
    assert gone.speaker_background.layout_windows is None
    assert "speakerSections" not in gone.required_capabilities
    assert "layout_windows" not in gone.model_dump(mode="json")["speaker_background"]


def test_compile_without_the_field_is_byte_identical(monkeypatch):
    _open(monkeypatch)
    with_field = _sections_background().model_copy(update={"layout_windows": ()})
    assert (
        _compile(with_field).model_dump_json()
        == _compile(
            _background(windows=(_window(SECOND, 3.0, 6.0), _window(THIRD, 6.0, 8.0)))
        ).model_dump_json()
    )


# --- rows 18 and 19: a speech cut moves the switches ------------------------------------------


def test_remap_moves_the_layouts_into_cut_time():
    plan = _plan((1.0, 2.0))  # one second of pause removed inside the first section
    remapped, dropped = remap_lanes_for_cut(
        PhoneSubtitledLanes(background=_sections_background()), plan
    )
    assert dropped == frozenset()
    assert [(w.start_s, w.end_s, w.layout) for w in remapped.background.layout_windows] == [
        (0.0, 2.0, "own"),
        (2.0, 5.0, "small"),
        (5.0, 7.0, "picture"),
        (7.0, 9.0, "own"),
    ]
    # The corner keeps its frame through the remap.
    assert remapped.background.layout_windows[1].speaker_frame == _frame()


def test_remap_across_a_switch_keeps_the_sections_touching_and_disjoint():
    # A removal [2.5, 3.5) straddles the 3 s switch: both sides end/start at the removal edge.
    remapped, _ = remap_lanes_for_cut(
        PhoneSubtitledLanes(background=_sections_background()), _plan((2.5, 3.5))
    )
    spans = [(w.start_s, w.end_s) for w in remapped.background.layout_windows]
    assert spans == [(0.0, 2.5), (2.5, 5.0), (5.0, 7.0), (7.0, 9.0)]


def test_remap_drops_a_section_inside_a_removal_or_left_under_half_a_second():
    background = _background(layout_windows=(_span(0.0, 2.0), _span(2.0, 2.8, "picture")))
    remapped, dropped = remap_lanes_for_cut(
        PhoneSubtitledLanes(background=background), _plan((2.0, 2.6))
    )
    assert [(w.start_s, w.end_s) for w in remapped.background.layout_windows] == [(0.0, 2.0)]
    assert dropped == frozenset()  # a layout carries no media


def test_a_cut_compile_keeps_the_switches_in_cut_time(monkeypatch):
    _open(monkeypatch)
    plan = _plan((1.0, 2.0))
    remapped, _ = remap_lanes_for_cut(PhoneSubtitledLanes(background=_sections_background()), plan)
    cut = _compile(remapped.background, cut_plan=plan)
    assert cut.duration == pytest.approx(9.0)
    assert _layouts(cut.speaker_background) == [
        (0.0, 2.0, "own"),
        (2.0, 5.0, "small"),
        (5.0, 7.0, "picture"),
        (7.0, 9.0, "own"),
    ]


# --- row 22: the gates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "protocol", "reason"),
    [
        ({"flag": False}, SPEAKER_SECTIONS_PROTOCOL, "sections_disabled"),
        ({"token": False}, SPEAKER_SECTIONS_PROTOCOL, "sections_token_not_verified"),
        ({}, SPEAKER_SECTIONS_PROTOCOL - 1, "client_protocol_below_min"),
        ({"windows": False}, SPEAKER_SECTIONS_PROTOCOL, "windows_token_not_verified"),
        ({"enabled": False}, SPEAKER_SECTIONS_PROTOCOL, "disabled"),
    ],
)
def test_a_closed_gate_drops_only_the_layouts_and_traces_it(
    monkeypatch, events, kwargs, protocol, reason
):
    _open(monkeypatch, **kwargs)
    set_client_protocol(protocol)
    set_client_features("speakerSections")
    assert speaker_sections_unavailable_reason() == reason
    background = _sections_background(frame=_frame())
    stripped = strip_unsupported_background(PhoneSubtitledLanes(background=background))
    if reason == "disabled":
        assert stripped.background is None  # the base switch takes the whole lane, as always
        return
    kept = stripped.background
    assert kept.layout_windows == ()
    if reason == "windows_token_not_verified":
        # Fill only: the windows gate also clears the layouts (and windows and frame).
        assert kept.windows == () and kept.speaker_frame is None
        assert [e[1] for e in events] == [SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT]
        assert events[0][2]["layout_window_count"] == 4
        return
    assert len(kept.windows) == 2 and kept.speaker_frame is not None  # everything else stays
    assert [(e[0], e[1], e[2]) for e in events] == [
        (
            "phone",
            SPEAKER_SECTIONS_FALLBACK_EVENT,
            {"media_id": background.media_id, "reason": reason, "window_count": 4},
        )
    ]


def test_an_open_gate_changes_nothing_and_traces_nothing(monkeypatch, events):
    _open(monkeypatch)
    set_client_protocol(SPEAKER_SECTIONS_PROTOCOL)
    set_client_features("speakerSections")
    lanes = PhoneSubtitledLanes(background=_sections_background())
    assert strip_unsupported_background(lanes) is lanes
    assert events == []


def test_a_closed_gate_equals_a_request_that_never_asked(monkeypatch, events):
    _open(monkeypatch, flag=False)
    asked = _compile(_sections_background())
    plain = _compile(_background(windows=(_window(SECOND, 3.0, 6.0), _window(THIRD, 6.0, 8.0))))
    assert asked.model_dump_json() == plain.model_dump_json()
    assert "speakerSections" not in asked.required_capabilities


def test_a_closed_opening_gate_and_a_closed_sections_gate_both_trace(monkeypatch, events):
    _open(monkeypatch, flag=False)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", False)
    background = _sections_background().model_copy(
        update={"full_screen_windows": (SubtitledFullScreenWindow(start_s=0.0, end_s=2.0),)}
    )
    stripped = strip_unsupported_background(PhoneSubtitledLanes(background=background))
    assert stripped.background.full_screen_windows == ()
    assert stripped.background.layout_windows == ()
    assert [e[1] for e in events] == [
        "speaker_full_opening_phone_fallback",
        SPEAKER_SECTIONS_FALLBACK_EVENT,
    ]


def test_the_worker_gate_ignores_the_build_and_the_planner_reads_the_stamp(monkeypatch):
    _open(monkeypatch)
    # No request: a worker has no app build, so only the server checks apply.
    assert speaker_sections_supported() and speaker_sections_unavailable_reason() is None
    assert speaker_sections_client_supported()
    sections = ["speakerSections"]
    assert speaker_sections_planned_for(SPEAKER_SECTIONS_PROTOCOL, sections)
    assert speaker_sections_planned_reason(SPEAKER_SECTIONS_PROTOCOL - 1, sections) == "app_too_old"
    assert speaker_sections_planned_reason(None, sections) == "app_too_old"
    # Tadic (#prs 2026-11-26): the number alone is not enough; the build must advertise them.
    assert speaker_sections_planned_reason(SPEAKER_SECTIONS_PROTOCOL) == "app_too_old"
    assert speaker_sections_planned_reason(SPEAKER_SECTIONS_PROTOCOL + 1, ["clipDim"]) == (
        "app_too_old"
    )
    _open(monkeypatch, token=False)
    assert speaker_sections_planned_reason(SPEAKER_SECTIONS_PROTOCOL, sections) == "unavailable"
    _open(monkeypatch, windows=False)
    assert speaker_sections_planned_reason(SPEAKER_SECTIONS_PROTOCOL, sections) == "unavailable"
    _open(monkeypatch, flag=False)
    assert speaker_sections_planned_reason(SPEAKER_SECTIONS_PROTOCOL, sections) == "disabled"


@pytest.mark.parametrize(
    ("protocol", "header", "expected"),
    [
        (SPEAKER_SECTIONS_PROTOCOL, "speakerSections", True),
        (SPEAKER_SECTIONS_PROTOCOL + 1, "clipDim, speakerSections", True),
        (SPEAKER_SECTIONS_PROTOCOL, None, False),
        (SPEAKER_SECTIONS_PROTOCOL + 1, "", False),
        (SPEAKER_SECTIONS_PROTOCOL + 1, "researchSelection,overlayEvents", False),
        (SPEAKER_SECTIONS_PROTOCOL - 1, "clipDim", False),  # TestFlight 2112
        (SPEAKER_SECTIONS_PROTOCOL - 1, "speakerSections", False),  # below the number
        (SPEAKER_SECTIONS_PROTOCOL, "speaker-sections", False),  # malformed token
    ],
)
def test_a_request_needs_the_number_and_the_advertised_feature(
    monkeypatch, protocol, header, expected
):
    _open(monkeypatch)
    set_client_protocol(protocol)
    set_client_features(header)
    assert speaker_sections_client_supported() is expected
    assert speaker_sections_supported() is expected
    assert (speaker_sections_unavailable_reason() is None) is expected


def test_the_config_minimum_is_the_protocol_constant():
    assert settings.speaker_sections_min_client_protocol == SPEAKER_SECTIONS_PROTOCOL
    assert settings.speaker_sections_enabled is False


# --- pilot validation -------------------------------------------------------------------------


def test_pilot_validation_refuses_unverified_layouts(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_sections_background())
    verified = [*recipe.required_capabilities]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in verified if c != "speakerSections"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "speakerSections"


# --- row 32: the authored (Range) timeline keeps them -----------------------------------------


def test_the_authored_timeline_keeps_the_layouts_from_the_recipe_and_from_the_document():
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, previous = fixture()  # the authored edit is one 3 s slot
    variant["user_timeline"]["slots"][0]["speaker_subject"] = True
    previous.speaker_background = SpeakerBackgroundLayer(
        subject_track_id=previous.tracks[0].id,
        media_kind="color",
        color="#000000",
        layout_windows=[
            {"start": 0.0, "end": 1.5, "layout": "own"},
            {"start": 1.5, "end": 9.0, "layout": "picture"},
        ],
    )
    kept = compile_phone_authored_timeline(job, variant, previous)
    assert _layouts(kept.speaker_background) == [(0.0, 1.5, "own"), (1.5, 3.0, "picture")]
    assert "speakerSections" in kept.required_capabilities

    variant["speaker_background"] = {
        "kind": "color",
        "color": "#000000",
        "layout_windows": [
            {"start_s": 0.0, "end_s": 1.5, "layout": "own"},
            {
                "start_s": 1.5,
                "end_s": 3.0,
                "layout": "small",
                "speaker_frame": _frame().model_dump(),
            },
        ],
    }
    saved = compile_phone_authored_timeline(job, variant, previous)
    assert _layouts(saved.speaker_background) == [(0.0, 1.5, "own"), (1.5, 3.0, "small")]
    assert saved.speaker_background.layout_windows[1].frame.model_dump() == _frame().model_dump()
