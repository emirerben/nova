"""Qendresa item 18: the lane and the compile that aim a background at its readable text.

Real lane models, real `PhoneVisualBinding`s and the real `compile_phone_subtitled_plan`. The
gate (`phone_rollout.background_focus_unavailable_reason`) is patched open or closed per test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.pipeline.phone_subtitled_lanes import (
    BACKGROUND_FOCUS_FALLBACK_EVENT,
    PhoneSubtitledLaneRequest,
    PhoneSubtitledLanes,
    SubtitledBackground,
    SubtitledBackgroundFocus,
    remap_lanes_for_cut,
    strip_unsupported_background,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services import phone_rollout, pipeline_trace
from app.services.background_text_focus import TextColumn, focus_for_text_column
from app.services.client_protocol import clear_request_context
from app.services.phone_sources import PhoneVisualBinding
from tests.kria.test_speaker_background_color import _open as _open_colour
from tests.kria.test_speaker_background_windows import (
    FILL,
    SECOND,
    THIRD,
    _background,
    _open,
    _plan,
    _window,
)
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding

COLUMN = TextColumn(left=0.08, right=0.46, line_pitch=0.032, source_width=1920, source_height=1080)
PORTRAIT = (1080, 1920)


class _Bound(PhoneVisualBinding):
    """A binding that carries the in-memory measure (the field the bind step sets)."""

    text_column: TextColumn | None = None


def _measured(visual: PhoneVisualBinding, column: TextColumn | None = COLUMN) -> _Bound:
    return _Bound(**visual.model_dump(), text_column=column)


def _expected(column: TextColumn = COLUMN, canvas=PORTRAIT):
    return focus_for_text_column(
        column,
        source_w=column.source_width,
        source_h=column.source_height,
        canvas_w=canvas[0],
        canvas_h=canvas[1],
    )


def _gate(monkeypatch, *, open_: bool, calls: list | None = None):
    def reason(**_kwargs):
        if calls is not None:
            calls.append(1)
        return None if open_ else "focus_disabled"

    monkeypatch.setattr(phone_rollout, "background_focus_unavailable_reason", reason, raising=False)


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    clear_request_context()
    _open(monkeypatch)
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


def _compile(background, *, visuals, **kwargs):
    return compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=_CUES,
        visuals=visuals,
        lanes=PhoneSubtitledLanes(background=background),
        **kwargs,
    )


def _wire(recipe) -> dict:
    return recipe.model_dump(mode="json")["speaker_background"]


# --- lane model -----------------------------------------------------------------------------


def test_a_lane_focus_needs_x_or_zoom():
    with pytest.raises(ValidationError):
        SubtitledBackgroundFocus(y=0.3)
    assert SubtitledBackgroundFocus(x=0.0).y == 0.5
    assert SubtitledBackgroundFocus(zoom=2.0).x is None
    for bad in ({"x": 1.01}, {"x": -0.1}, {"zoom": 0.19}, {"zoom": 4.01}, {"x": 0.5, "y": 2}):
        with pytest.raises(ValidationError):
            SubtitledBackgroundFocus(**bad)


def test_lane_dumps_are_unchanged_while_no_focus_is_set():
    background = _background(windows=(_window(),))
    dumped = PhoneSubtitledLaneRequest(background=background).model_dump(mode="json")["background"]
    assert "focus" not in dumped and "focus" not in dumped["windows"][0]
    focused = _background(
        windows=(_window(focus=SubtitledBackgroundFocus(x=0.2)),),
        focus=SubtitledBackgroundFocus(zoom=1.5),
    )
    dumped = PhoneSubtitledLaneRequest(background=focused).model_dump(mode="json")["background"]
    assert dumped["focus"] == {"y": 0.5, "zoom": 1.5} and dumped["windows"][0]["focus"] == {
        "x": 0.2,
        "y": 0.5,
    }
    assert PhoneSubtitledLaneRequest.model_validate(
        PhoneSubtitledLaneRequest(background=focused).model_dump(mode="json")
    ) == PhoneSubtitledLaneRequest(background=focused)
    assert focused.uses_focus and not background.uses_focus


def test_a_colour_fill_with_a_focus_is_invalid_but_its_windows_may_carry_one():
    with pytest.raises(ValidationError):
        SubtitledBackground(kind="color", color="#112233", focus=SubtitledBackgroundFocus(x=0.1))
    ok = SubtitledBackground(
        kind="color",
        color="#112233",
        windows=(_window(focus=SubtitledBackgroundFocus(x=0.1)),),
    )
    assert ok.uses_focus


def test_a_speech_cut_keeps_a_windows_focus():
    focus = SubtitledBackgroundFocus(x=0.9, zoom=2.0)
    lanes = PhoneSubtitledLanes(
        background=_background(windows=(_window(SECOND, 5.0, 9.0, focus=focus),))
    )
    remapped, _ = remap_lanes_for_cut(lanes, _plan((1.0, 2.0)))
    (window,) = remapped.background.windows
    assert window.start_s == pytest.approx(4.0) and window.focus == focus


# --- strip ----------------------------------------------------------------------------------


def _focused_lanes() -> PhoneSubtitledLanes:
    focus = SubtitledBackgroundFocus(x=0.0, zoom=2.0)
    return PhoneSubtitledLanes(
        background=_background(
            windows=(_window(SECOND, 0.0, 3.0, focus=focus), _window(THIRD, 3.0, 6.0)),
            focus=focus,
        )
    )


def test_strip_drops_every_focus_with_a_trace_when_the_gate_is_closed(monkeypatch, events):
    _gate(monkeypatch, open_=False)
    stripped = strip_unsupported_background(_focused_lanes())
    background = stripped.background
    assert background.focus is None and all(w.focus is None for w in background.windows)
    assert len(background.windows) == 2 and background.media_id == FILL.media_id
    ((stage, event, detail),) = events
    assert (stage, event) == ("phone", BACKGROUND_FOCUS_FALLBACK_EVENT)
    assert BACKGROUND_FOCUS_FALLBACK_EVENT == "background_focus_phone_fallback"
    assert detail["reason"] == "focus_disabled"
    assert detail["window_count"] == 1 and detail["had_fill_focus"] is True


def test_strip_keeps_the_focus_when_the_gate_is_open(monkeypatch, events):
    _gate(monkeypatch, open_=True)
    lanes = _focused_lanes()
    assert strip_unsupported_background(lanes) == lanes
    assert events == []


def test_strip_without_a_focus_never_asks_the_focus_gate(monkeypatch, events):
    calls: list = []
    _gate(monkeypatch, open_=False, calls=calls)
    lanes = PhoneSubtitledLanes(background=_background(windows=(_window(),)))
    assert strip_unsupported_background(lanes) == lanes
    assert calls == [] and events == []


def test_strip_still_checks_a_fill_focus_when_the_windows_gate_drops_the_windows(
    monkeypatch, events
):
    _gate(monkeypatch, open_=False)
    _open(monkeypatch, windows=False)
    stripped = strip_unsupported_background(_focused_lanes())
    assert stripped.background.windows == () and stripped.background.focus is None
    names = [event for _stage, event, _ in events]
    assert "speaker_background_windows_phone_fallback" in names
    assert BACKGROUND_FOCUS_FALLBACK_EVENT in names


# --- compile: auto focus --------------------------------------------------------------------


def test_the_measured_column_becomes_the_fill_focus(monkeypatch):
    _gate(monkeypatch, open_=True)
    recipe = _compile(_background(), visuals=(_measured(FILL),))
    expected = _expected()
    # KRI-1307: the measure aims the view (x) even where it keeps the cover zoom (1x).
    assert expected is not None and (expected.x, expected.zoom) != (0.5, 1.0)
    assert _wire(recipe)["focus"] == {"x": expected.x, "y": expected.y, "zoom": expected.zoom}
    assert "backgroundFocus" in recipe.required_capabilities


def test_every_window_gets_its_own_measure(monkeypatch):
    _gate(monkeypatch, open_=True)
    other = TextColumn(left=0.5, right=0.95, line_pitch=0.04, source_width=1920, source_height=1080)
    windows = (_window(SECOND, 0.0, 3.0), _window(THIRD, 3.0, 6.0), _window(FILL, 6.0, 9.0))
    recipe = _compile(
        _background(windows=windows),
        visuals=(
            _measured(FILL),
            _measured(SECOND),
            _measured(THIRD, other),
        ),
    )
    wire = _wire(recipe)
    assert wire["windows"][0]["focus"]["x"] == _expected().x
    assert wire["windows"][1]["focus"]["x"] == _expected(other).x
    assert wire["windows"][2]["focus"]["x"] == _expected().x  # the fill photo again


def test_lane_x_wins_over_the_measure_while_the_measured_zoom_fills_the_gap(monkeypatch):
    _gate(monkeypatch, open_=True)
    recipe = _compile(
        _background(focus=SubtitledBackgroundFocus(x=1.0)), visuals=(_measured(FILL),)
    )
    expected = _expected()
    assert _wire(recipe)["focus"] == {"x": 1.0, "y": 0.5, "zoom": expected.zoom}


def test_lane_zoom_wins_and_x_comes_from_the_measure(monkeypatch):
    _gate(monkeypatch, open_=True)
    recipe = _compile(
        _background(focus=SubtitledBackgroundFocus(zoom=1.25)), visuals=(_measured(FILL),)
    )
    assert _wire(recipe)["focus"] == {"x": _expected().x, "y": 0.5, "zoom": 1.25}


def test_a_lane_focus_alone_fills_the_missing_part_from_todays_drawing(monkeypatch):
    _gate(monkeypatch, open_=True)
    recipe = _compile(_background(focus=SubtitledBackgroundFocus(x=0.0)), visuals=(FILL,))
    assert _wire(recipe)["focus"] == {"x": 0.0, "y": 0.5, "zoom": 1.0}
    recipe = _compile(_background(focus=SubtitledBackgroundFocus(zoom=2.0)), visuals=(FILL,))
    assert _wire(recipe)["focus"] == {"x": 0.5, "y": 0.5, "zoom": 2.0}


def test_todays_drawing_is_never_written_as_a_focus(monkeypatch):
    _gate(monkeypatch, open_=True)
    plain = _compile(_background(), visuals=(FILL,))
    same = _compile(_background(focus=SubtitledBackgroundFocus(x=0.5, zoom=1.0)), visuals=(FILL,))
    assert "focus" not in _wire(same)
    assert same.model_dump_json() == plain.model_dump_json()
    assert "backgroundFocus" not in same.required_capabilities


def test_a_closed_gate_leaves_no_focus_and_the_recipe_bytes_of_a_plain_lane(monkeypatch):
    plain = _compile(_background(windows=(_window(),)), visuals=(FILL, SECOND))
    _gate(monkeypatch, open_=False)
    closed = _compile(
        _background(
            windows=(_window(focus=SubtitledBackgroundFocus(x=0.0)),),
            focus=SubtitledBackgroundFocus(zoom=2.0),
        ),
        visuals=(_measured(FILL), _measured(SECOND)),
    )
    assert closed.model_dump_json() == plain.model_dump_json()
    assert "backgroundFocus" not in closed.required_capabilities


def test_no_measure_and_no_lane_focus_is_byte_identical_and_never_asks_the_gate(monkeypatch):
    calls: list = []
    _gate(monkeypatch, open_=True, calls=calls)
    background = _background(windows=(_window(),))
    today = _compile(background, visuals=(FILL, SECOND))
    unmeasured = _compile(background, visuals=(_measured(FILL, None), _measured(SECOND, None)))
    assert unmeasured.model_dump_json() == today.model_dump_json()
    assert calls == []
    assert "focus" not in today.model_dump_json()


def test_the_gate_is_asked_at_most_once_per_compile(monkeypatch):
    calls: list = []
    _gate(monkeypatch, open_=True, calls=calls)
    windows = (_window(SECOND, 0.0, 3.0), _window(THIRD, 3.0, 6.0))
    _compile(
        _background(windows=windows), visuals=(_measured(FILL), _measured(SECOND), _measured(THIRD))
    )
    assert len(calls) == 1


def test_a_colour_fill_takes_no_focus_but_its_windows_do(monkeypatch):
    _open_colour(monkeypatch)
    _gate(monkeypatch, open_=True)
    background = SubtitledBackground(
        kind="color",
        color="#112233",
        windows=(
            _window(SECOND, 0.0, 4.0),
            _window(THIRD, 4.0, 8.0, focus=SubtitledBackgroundFocus(x=0.0)),
        ),
    )
    recipe = _compile(background, visuals=(_measured(SECOND), _measured(THIRD)))
    wire = _wire(recipe)
    assert "focus" not in wire and wire["media_kind"] == "color"
    assert wire["windows"][0]["focus"]["x"] == _expected().x
    assert wire["windows"][1]["focus"] == {"x": 0.0, "y": 0.5, "zoom": _expected().zoom}
    assert "backgroundFocus" in recipe.required_capabilities


def test_a_landscape_canvas_with_a_landscape_recording_gets_no_auto_focus(monkeypatch):
    _gate(monkeypatch, open_=True)
    recipe = _compile(_background(), visuals=(_measured(FILL),), output_orientation="landscape")
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    assert "focus" not in _wire(recipe)


def test_a_measure_without_its_size_is_skipped(monkeypatch):
    _gate(monkeypatch, open_=True)
    sizeless = TextColumn(left=0.1, right=0.5, line_pitch=0.03)
    recipe = _compile(_background(), visuals=(_measured(FILL, sizeless),))
    assert "focus" not in _wire(recipe)
