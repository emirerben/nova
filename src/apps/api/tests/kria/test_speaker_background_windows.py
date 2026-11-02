"""KRI-1162: timed background windows + the speaker frame (commentary-style green screen).

Server half of rows S27-S35 of `docs/reviews/speaker-background-windows-failure-modes.md`:
the recipe contract, the lane, the speech-cut remap, the gates and the compile.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.kria.device_render import recipe_digest
from app.kria.portable_speaker_background import (
    SpeakerBackgroundLayer,
    speaker_frame_for_preset,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_subtitled_lanes import (
    SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT,
    PhoneSubtitledLaneRequest,
    PhoneSubtitledLanes,
    SubtitledBackground,
    SubtitledBackgroundWindow,
    SubtitledLaneError,
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
    speaker_background_windows_unavailable_reason,
    validate_phone_pilot_recipe,
)
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    _binding,
    _photo_visual,
    _pool_video_visual,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json"
PARITY = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity/v2.json").read_text()
)
PARITY_V1 = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/speaker-background-parity/v1.json").read_text()
)

FILL = _photo_visual()
SECOND = _photo_visual(
    media_id="photo-2", gcs_path="users/owner/plan/item/pool/photo-2.jpg", generation="78"
)
THIRD = _photo_visual(
    media_id="photo-3",
    gcs_path="users/owner/plan/item/pool/photo-3.jpg",
    generation="79",
    sha256="d" * 64,
)
VIDEO = _pool_video_visual()
ALL_VISUALS = (FILL, SECOND, THIRD, VIDEO)


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


def _open(monkeypatch, *, windows=True, enabled=True, base=True) -> None:
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    features = [
        f
        for f in settings.phone_render_verified_features
        if f not in {"speakerBackground", "speakerBackgroundWindows"}
    ]
    if base:
        features.append("speakerBackground")
    if windows:
        features.append("speakerBackgroundWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _window(visual=SECOND, start_s=0.0, end_s=2.0, **changes) -> SubtitledBackgroundWindow:
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


def _background(visual=FILL, windows=(), frame=None, **changes) -> SubtitledBackground:
    return SubtitledBackground(
        **{
            "media_id": visual.media_id,
            "gcs_path": visual.gcs_path,
            "generation": visual.generation,
            "kind": visual.kind,
            "windows": tuple(windows),
            "speaker_frame": frame,
        }
        | changes
    )


def _frame(preset="small_bottom_right") -> SubtitledSpeakerFrame:
    return SubtitledSpeakerFrame(**speaker_frame_for_preset(preset).model_dump())


def _compile(background=None, *, visuals=ALL_VISUALS, duration_s=10.0, **kwargs):
    lanes = PhoneSubtitledLanes(background=background) if background is not None else None
    return compile_phone_subtitled_plan(
        (_binding(duration_s=duration_s),),
        caption_cues=_CUES,
        visuals=visuals,
        lanes=lanes,
        **kwargs,
    )


def _plan(*removed: tuple[float, float], total_s: float = 10.0) -> CutPlan:
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in removed:
        keep.append((cursor, start))
        cursor = end
    keep.append((cursor, total_s))
    return CutPlan(
        keep_segments=keep,
        removed=[Removal(start_s=s, end_s=e, reason="pause") for s, e in removed],
        time_saved_s=sum(e - s for s, e in removed),
    )


def _three_windows(**frame):
    return _background(
        windows=(
            _window(SECOND, 0.0, 3.0),
            _window(THIRD, 3.0, 6.0),
            _window(FILL, 6.0, 9.0),
        ),
        **frame,
    )


# --- S34: Python and Swift agree on the new fields (shared fixture) ---------------------------


@pytest.mark.parametrize("case", PARITY["valid"], ids=lambda case: case["name"])
def test_shared_fixture_v2(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    assert layer.model_dump(mode="json") == case["normalized"]
    assert SpeakerBackgroundLayer.model_validate(layer.model_dump(mode="json")) == layer


@pytest.mark.parametrize("case", PARITY["invalid"], ids=lambda case: case["name"])
def test_shared_fixture_v2_invalid_layers_are_rejected(case):
    with pytest.raises(ValidationError):
        SpeakerBackgroundLayer.model_validate(case["layer"])


@pytest.mark.parametrize("case", PARITY_V1["valid"], ids=lambda case: case["name"])
def test_shared_fixture_v1_still_normalizes_with_neither_new_key(case):
    layer = SpeakerBackgroundLayer.model_validate(case["layer"])
    dumped = layer.model_dump(mode="json")
    assert dumped == case["normalized"]
    assert "windows" not in dumped and "speaker_frame" not in dumped
    assert not layer.uses_windows


# --- S28: the right background at a time ------------------------------------------------------


def test_windows_sorted_and_disjoint():
    # Touching ends are fine; an overlap or an out-of-order list is not (layer and lane).
    _background(windows=(_window(SECOND, 0.0, 3.0), _window(THIRD, 3.0, 6.0)))
    with pytest.raises(ValidationError):
        _background(windows=(_window(SECOND, 0.0, 3.5), _window(THIRD, 3.0, 6.0)))
    with pytest.raises(ValidationError):
        _background(windows=(_window(THIRD, 3.0, 6.0), _window(SECOND, 0.0, 3.0)))
    with pytest.raises(ValidationError):
        _window(SECOND, 1.0, 1.4)  # shorter than 0.5 s
    with pytest.raises(ValidationError):
        _window(SECOND, 2.0, 1.0)
    with pytest.raises(ValidationError):
        _window(SECOND, 0.0, 2.0, kind="image", source_start_s=1.0)  # a photo has no start


def test_recipe_windows_must_name_matching_visuals_assets_inside_the_timeline(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_three_windows())
    document = json.loads(recipe.model_dump_json())
    layer = document["speaker_background"]

    broken = json.loads(json.dumps(document))
    broken["speaker_background"]["windows"][0]["asset_id"] = "visual-not-in-the-manifest"
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(broken)

    broken = json.loads(json.dumps(document))
    broken["speaker_background"]["windows"][0]["media_kind"] = "video"
    with pytest.raises(ValidationError):
        EditRecipeV2.model_validate(broken)

    broken = json.loads(json.dumps(document))
    broken["speaker_background"]["windows"][-1]["end"] = recipe.duration + 1
    with pytest.raises(ValidationError, match="exceeds the timeline"):
        EditRecipeV2.model_validate(broken)

    assert len(layer["windows"]) == 3
    assert EditRecipeV2.model_validate(document) == recipe


# --- compile: windows + frame + manifest assets ------------------------------------------------


def test_compile_emits_windows_frame_and_manifest_assets(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_three_windows(frame=_frame("small_bottom_right")))
    layer = recipe.speaker_background
    assert layer is not None
    assert [(w.asset_id, w.start, w.end) for w in layer.windows] == [
        (f"visual-{SECOND.media_id}", 0.0, 3.0),
        (f"visual-{THIRD.media_id}", 3.0, 6.0),
        (f"visual-{FILL.media_id}", 6.0, 9.0),
    ]
    assert layer.asset_id == f"visual-{FILL.media_id}"  # the fill is untouched
    assert (layer.speaker_frame.width_fraction, layer.speaker_frame.x_fraction) == (0.42, 0.76)
    assert layer.speaker_frame.y_fraction == 0.80
    assert {"speakerBackground", "speakerBackgroundWindows"} <= recipe.required_capabilities
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    for window in layer.windows:
        assert manifest[window.asset_id].media_kind == window.media_kind
    # The same photo may fill and open: one manifest entry, not two.
    assert [a.id for a in recipe.asset_manifest.assets].count(f"visual-{FILL.media_id}") == 1
    assert {asset.id for asset in recipe.assets} == set(manifest)
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    monkeypatch.setattr(
        settings, "phone_render_verified_features", sorted(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_a_speaker_frame_alone_needs_the_windows_capability(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_background(frame=_frame("small_top_left")))
    assert recipe.speaker_background.windows is None
    assert "windows" not in recipe.model_dump(mode="json")["speaker_background"]
    assert "speakerBackgroundWindows" in recipe.required_capabilities


def test_a_video_window_keeps_its_start_and_pins_its_footage(monkeypatch):
    _open(monkeypatch)
    background = _background(windows=(_window(VIDEO, 1.0, 4.0, source_start_s=2.5),))
    recipe = _compile(background)
    window = recipe.speaker_background.windows[0]
    assert (window.media_kind, window.source_start, window.start, window.end) == (
        "video",
        2.5,
        1.0,
        4.0,
    )
    assert {"visualVideos", "speakerBackgroundWindows"} <= recipe.required_capabilities
    media = next(a for a in recipe.assets if a.id == window.asset_id)
    assert media.duration == VIDEO.duration_s


# --- S39: a window past the edit end is clipped ----------------------------------------------


def test_a_window_past_the_edit_end_is_clipped(monkeypatch):
    _open(monkeypatch)
    background = _background(
        windows=(
            _window(SECOND, 0.0, 4.0),
            _window(THIRD, 4.0, 12.0),  # overruns the 10 s take: shortened
            _window(FILL, 12.0, 14.0),  # entirely past it: dropped
        )
    )
    recipe = _compile(background, duration_s=10.0)
    assert recipe.duration == pytest.approx(10.0)
    assert [(w.start, w.end) for w in recipe.speaker_background.windows] == [
        (0.0, 4.0),
        (4.0, pytest.approx(recipe.duration)),
    ]
    starts_at_the_end = _background(windows=(_window(SECOND, 10.0, 14.0),))
    assert _compile(starts_at_the_end, duration_s=10.0).speaker_background.windows is None


def test_a_sliver_left_at_the_edit_end_is_dropped_not_a_failed_recipe(monkeypatch):
    _open(monkeypatch)
    background = _background(windows=(_window(SECOND, 0.0, 9.8), _window(THIRD, 9.8, 12.0)))
    recipe = _compile(background, duration_s=10.0)
    assert [w.asset_id for w in recipe.speaker_background.windows] == [f"visual-{SECOND.media_id}"]


def test_every_window_clipped_away_leaves_the_fill_alone(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_background(windows=(_window(SECOND, 10.0, 12.0),)), duration_s=10.0)
    assert recipe.speaker_background.windows is None
    assert "windows" not in recipe.model_dump(mode="json")["speaker_background"]


# --- S30: a video window without footage fails the lane ---------------------------------------


@pytest.mark.parametrize("case", ["start_past_end", "no_probed_duration", "unpinned", "kind"])
def test_a_video_window_without_footage_fails_the_lane(monkeypatch, case):
    _open(monkeypatch)
    visuals = ALL_VISUALS
    if case == "start_past_end":
        window = _window(VIDEO, 0.0, 2.0, source_start_s=VIDEO.duration_s)
    elif case == "no_probed_duration":
        window = _window(VIDEO, 0.0, 2.0)
        visuals = (FILL, VIDEO.model_copy(update={"duration_s": None}))
    elif case == "unpinned":
        window = _window(SECOND, 0.0, 2.0, generation="not-the-pinned-generation")
    else:
        window = _window(SECOND, 0.0, 2.0, kind="video")
    with pytest.raises(SubtitledLaneError) as failed:
        _compile(_background(windows=(window,)), visuals=visuals)
    assert failed.value.lane == "background"


# --- S29: a speech cut moves the windows, not the fill ----------------------------------------


def test_windows_follow_a_speech_cut(monkeypatch):
    background = _three_windows(frame=_frame())
    plan = _plan((1.0, 2.0))  # one second of pause removed inside the first window
    remapped, dropped = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), plan)
    assert dropped == frozenset()
    kept = remapped.background
    assert [(w.start_s, w.end_s) for w in kept.windows] == [(0.0, 2.0), (2.0, 5.0), (5.0, 8.0)]
    assert kept.media_id == background.media_id and kept.source_start_s == 0.0  # the fill
    assert kept.speaker_frame == background.speaker_frame
    assert [w.media_id for w in kept.windows] == [SECOND.media_id, THIRD.media_id, FILL.media_id]

    _open(monkeypatch)
    cut = _compile(kept, cut_plan=plan)
    assert cut.duration == pytest.approx(9.0)
    assert [(w.start, w.end) for w in cut.speaker_background.windows] == [
        (0.0, 2.0),
        (2.0, 5.0),
        (5.0, 8.0),
    ]


def test_a_window_inside_a_cut_is_dropped_with_a_receipt():
    background = _three_windows()
    plan = _plan((3.0, 6.0))  # exactly the second window
    remapped, dropped = remap_lanes_for_cut(PhoneSubtitledLanes(background=background), plan)
    assert dropped == frozenset({THIRD.media_id})
    assert [(w.media_id, w.start_s, w.end_s) for w in remapped.background.windows] == [
        (SECOND.media_id, 0.0, 3.0),
        (FILL.media_id, 3.0, 6.0),
    ]


def test_a_dropped_window_whose_photo_still_shows_elsewhere_gets_no_receipt():
    background = _background(
        windows=(_window(SECOND, 0.0, 2.0), _window(THIRD, 2.0, 4.0), _window(SECOND, 4.0, 6.0))
    )
    remapped, dropped = remap_lanes_for_cut(
        PhoneSubtitledLanes(background=background), _plan((0.0, 2.0))
    )
    assert dropped == frozenset()
    assert [w.media_id for w in remapped.background.windows] == [THIRD.media_id, SECOND.media_id]


def test_a_cut_that_leaves_a_window_under_half_a_second_drops_it():
    background = _background(windows=(_window(SECOND, 0.0, 2.0), _window(THIRD, 2.0, 4.0)))
    remapped, dropped = remap_lanes_for_cut(
        PhoneSubtitledLanes(background=background), _plan((0.0, 1.7))
    )
    assert dropped == frozenset({SECOND.media_id})
    assert [w.media_id for w in remapped.background.windows] == [THIRD.media_id]


def test_a_cut_that_removes_nothing_returns_the_lanes_untouched():
    lanes = PhoneSubtitledLanes(background=_three_windows())
    remapped, dropped = remap_lanes_for_cut(
        lanes, CutPlan(keep_segments=[(0.0, 10.0)], removed=[], time_saved_s=0.0)
    )
    assert remapped is lanes and dropped == frozenset()


# --- S32/S33: gates -----------------------------------------------------------------------------


def test_protocol_below_32_strips_windows_keeps_fill(monkeypatch, events):
    _open(monkeypatch)
    background = _three_windows(frame=_frame())
    set_client_protocol(30)
    stripped = _compile(background)
    fill_only = _compile(_background())
    assert stripped.model_dump_json() == fill_only.model_dump_json()
    assert stripped.speaker_background is not None and stripped.speaker_background.windows is None
    assert "speakerBackgroundWindows" not in stripped.required_capabilities
    assert events == [
        (
            "phone",
            SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT,
            {
                "media_id": FILL.media_id,
                "reason": "client_protocol_below_min",
                "window_count": 3,
                "had_frame": True,
            },
        )
    ]
    events.clear()
    set_client_protocol(32)
    assert _compile(background).speaker_background.windows is not None
    assert events == []


def test_windows_token_missing_strips_windows_keeps_fill(monkeypatch, events):
    _open(monkeypatch, windows=False)
    asked = _compile(_three_windows(frame=_frame()))
    assert asked.model_dump_json() == _compile(_background()).model_dump_json()
    assert asked.speaker_background is not None
    assert [(e[1], e[2]["reason"], e[2]["window_count"], e[2]["had_frame"]) for e in events] == [
        (SPEAKER_BACKGROUND_WINDOWS_FALLBACK_EVENT, "windows_token_not_verified", 3, True)
    ]


def test_a_frame_alone_is_stripped_the_same_way(monkeypatch, events):
    _open(monkeypatch, windows=False)
    asked = _compile(_background(frame=_frame()))
    assert asked.model_dump_json() == _compile(_background()).model_dump_json()
    assert events[0][2]["window_count"] == 0 and events[0][2]["had_frame"] is True


def test_base_gate_closed_still_strips_the_whole_lane(monkeypatch, events):
    _open(monkeypatch, enabled=False)
    asked = _compile(_three_windows(frame=_frame()))
    assert asked.speaker_background is None
    assert asked.model_dump_json() == _compile().model_dump_json()
    assert [e[1] for e in events] == ["speaker_background_phone_fallback"]
    assert events[0][2]["reason"] == "disabled"


def test_strip_works_on_the_persisted_request_too(monkeypatch, events):
    _open(monkeypatch, windows=False)
    request = PhoneSubtitledLaneRequest(background=_three_windows(frame=_frame()))
    stripped = strip_unsupported_background(request)
    assert stripped.background.windows == () and stripped.background.speaker_frame is None
    assert stripped.background.media_id == FILL.media_id
    assert "windows" not in stripped.model_dump(mode="json")["background"]


def test_the_windows_reason_names_the_first_failing_check(monkeypatch):
    _open(monkeypatch)
    assert speaker_background_windows_unavailable_reason(require_client=False) is None
    set_client_protocol(30)
    assert speaker_background_windows_unavailable_reason(require_client=True) == (
        "client_protocol_below_min"
    )
    _open(monkeypatch, windows=False)
    assert speaker_background_windows_unavailable_reason(require_client=False) == (
        "windows_token_not_verified"
    )
    _open(monkeypatch, enabled=False)
    assert speaker_background_windows_unavailable_reason(require_client=False) == "disabled"


def test_pilot_validation_refuses_unverified_windows(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_three_windows())
    verified = [*recipe.required_capabilities]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(recipe)

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in verified if c != "speakerBackgroundWindows"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "speakerBackgroundWindows"

    # The first-cut recipe (fill only) stays valid with only the base token.
    validate_phone_pilot_recipe(_compile(_background()))


def test_pilot_validation_refuses_windows_added_after_compile(monkeypatch):
    """The explicit gate matters when a caller mutates a recipe after capabilities were derived."""
    _open(monkeypatch)
    recipe = _compile(_three_windows())
    recipe.required_capabilities = set()
    monkeypatch.setattr(settings, "phone_render_verified_features", ["speakerBackground"])
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "speakerBackgroundWindows"


def test_the_windows_protocol_is_32_and_the_token_is_dark_in_production():
    from app.config import Settings

    assert Settings.model_fields["speaker_background_windows_min_client_protocol"].default == 32
    assert "speakerBackgroundWindows" not in Settings.model_fields[
        "phone_render_verified_features"
    ].get_default(call_default_factory=True)


# --- S33: no windows / no frame is byte-identical to today -------------------------------------


def test_no_windows_no_frame_is_byte_identical_to_today(monkeypatch):
    _open(monkeypatch)
    recipe = _compile(_background())
    wire = recipe.model_dump(mode="json")["speaker_background"]
    assert wire == {
        "asset_id": f"visual-{FILL.media_id}",
        "media_kind": "image",
        "source_start": 0.0,
        "loop": True,
        "subject_track_id": "subtitled",
    }
    assert "speakerBackgroundWindows" not in recipe.required_capabilities
    # Windows given as an empty tuple / a null frame change nothing about the dump or digest.
    again = _compile(_background(windows=(), frame=None))
    assert again.model_dump_json() == recipe.model_dump_json()
    assert recipe_digest(again) == recipe_digest(recipe)
    # And a lane without a background still dumps without the key at all.
    assert "background" not in PhoneSubtitledLanes().model_dump(mode="json")
    dumped = PhoneSubtitledLanes(background=_background()).model_dump(mode="json")["background"]
    assert "windows" not in dumped and "speaker_frame" not in dumped


def test_a_lane_with_windows_round_trips_through_the_persisted_request():
    request = PhoneSubtitledLaneRequest(background=_three_windows(frame=_frame("small_top_right")))
    dumped = request.model_dump(mode="json")
    assert len(dumped["background"]["windows"]) == 3
    assert dumped["background"]["speaker_frame"] == {
        "width_fraction": 0.42,
        "x_fraction": 0.76,
        "y_fraction": 0.22,
    }
    assert PhoneSubtitledLaneRequest.model_validate(dumped) == request


# --- the worker-facing helper (S35) ------------------------------------------------------------


def test_background_media_refs_cover_fill_and_windows_once_each():
    background = _background(
        windows=(_window(SECOND, 0.0, 2.0), _window(FILL, 2.0, 4.0), _window(SECOND, 4.0, 6.0))
    )
    refs = background_media_refs(background)
    assert [ref[0] for ref in refs] == [FILL.media_id, SECOND.media_id]
    assert refs[1] == (SECOND.media_id, SECOND.gcs_path, SECOND.generation, "image")
    assert background_pins(background) == {
        FILL.media_id: ("image", FILL.gcs_path, FILL.generation),
        SECOND.media_id: ("image", SECOND.gcs_path, SECOND.generation),
    }
    assert background_media_refs(None) == ()
