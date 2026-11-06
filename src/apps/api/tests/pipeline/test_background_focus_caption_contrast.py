"""Qendresa item 18 x Green 5 (#2099): a caption's colour is judged against the part of an aimed
recording the phone actually shows.

Since #2099 the automatic caption colour on a speaker-background edit is chosen against the
background picture (`phone_text_styling._background_frames`), sampled as the plain cover fit.
Item 18 aims a recording at its readable text (a focal point and a zoom), so the viewer sees a
different part of it: a zoomed-in white page where the cover fit shows a dark video player.

Real `compile_phone_subtitled_plan`, real lane models and bindings, real ffmpeg sampling of real
pictures. Only the download (a local copy), the paid font pick and the rollout gate are faked.

Ways the fix could fail (written before the fix):
1. Every segment is still sampled as the plain cover fit, so an aimed recording's caption colour
   is chosen against pixels the viewer never sees (white text over a zoomed-in white page).
2. Only a creator-named aim counts: the automatic aim from the measured text column (the common
   case, no words from her) is ignored.
3. The aims mix: a window's aim used for the fill, or the fill's for a window.
4. The sampled part is not the phone's rectangle (`VisualMediaPlacement`: scale = cover x zoom,
   never below contain; offset = (canvas - picture) x focal), e.g. the focal point read from the
   wrong edge.
5. A zoom below cover (zoomed out) asks ffmpeg for a crop larger than the frame: the sample fails
   and the colour falls back to the speaker take (Green 5's bug back), or the black bands the
   phone draws around the picture are left out.
6. On a landscape canvas the aimed part is cut to the portrait shape.
7. With the gate closed (flag off, an older build) anything changes: the recorded background and
   its frames must be exactly what #2099 records.
8. The compile asks the rollout gate twice (once for the recipe, once for the colour).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledBackgroundFocus
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services import phone_text_styling as styling
from app.services import pipeline_trace
from app.services.background_text_focus import TextColumn, focus_for_text_column
from app.services.client_protocol import clear_request_context
from tests.kria.test_speaker_background_windows import FILL, SECOND, _background, _open, _window
from tests.pipeline.test_background_focus_compile import _gate, _measured
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding

# A 16:9 screen recording whose readable column is its left 30 %: white page, dark elsewhere.
LEFT_PAGE = TextColumn(
    left=0.0, right=0.28, line_pitch=0.032, source_width=1920, source_height=1080
)
WHITE = (0.0, 0.3)


def _picture(path: Path, size: tuple[int, int], band: str) -> Path:
    """Black ``size`` picture with a white band: ``left``/``right`` 30 % of the width, or ``top``
    30 % of the height."""
    w, h = size
    box = {
        "left": f"x=0:y=0:w={int(w * 0.3)}:h={h}",
        "right": f"x={w - int(w * 0.3)}:y=0:w={int(w * 0.3)}:h={h}",
        "top": f"x=0:y=0:w={w}:h={int(h * 0.3)}",
    }[band]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=black:s={w}x{h}"]
        + ["-vf", f"drawbox={box}:color=white:t=fill", "-frames:v", "1", "-q:v", "2", str(path)],
        check=True,
    )
    return path


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    clear_request_context()
    _open(monkeypatch)
    monkeypatch.setattr(styling, "select_fonts", lambda *a, **k: {})  # the paid font pick
    yield
    clear_request_context()


@pytest.fixture
def events(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        pipeline_trace, "record_pipeline_event", lambda stage, event, data=None: seen.append(event)
    )
    return seen


def _media(monkeypatch, tmp_path, *, fill: str = "left", window: str = "right", size=(1920, 1080)):
    files = {
        FILL.gcs_path: _picture(tmp_path / "fill.jpg", size, fill),
        SECOND.gcs_path: _picture(tmp_path / "window.jpg", size, window),
    }

    def download(remote, local, *args, **kwargs):  # noqa: ARG001
        Path(local).write_bytes(files[str(remote)].read_bytes())
        return local

    import app.storage as storage

    monkeypatch.setattr(storage, "download_to_file", download)


def _compile(background, *, visuals, **kwargs) -> tuple[object, dict]:
    """The compile inside a generation styling scope: the recipe and the styling policy after it
    (what Green 5 recorded behind the captions)."""
    with styling.phone_text_policy_scope({"preferences": {}, "requested": {}}):
        recipe = compile_phone_subtitled_plan(
            (_binding(duration_s=10.0),),
            caption_cues=_CUES,
            visuals=visuals,
            lanes=PhoneSubtitledLanes(background=background),
            **kwargs,
        )
        return recipe, styling.current_phone_text_policy()


def _brightness(policy: dict, start_s: float) -> float:
    frame = next(f for f in policy["_background_frames"] if abs(f["start_s"] - start_s) < 1e-6)
    return sum(frame["pixels"]) / len(frame["pixels"]) / 255


def _phone_share(
    band: tuple[float, float], axis: str, *, focus, source=(1920, 1080), canvas=(1080, 1920)
) -> float:
    """The share of the canvas that is the white band, placed the way the phone places an aimed
    picture (`VisualMediaPlacement`): scale = max(cover x zoom, contain), offset = (canvas -
    picture) x focal, black where the picture does not reach."""
    (sw, sh), (cw, ch) = source, canvas
    cover, contain = max(cw / sw, ch / sh), min(cw / sw, ch / sh)
    fx, fy, zoom = (0.5, 0.5, 1.0) if focus is None else (focus["x"], focus["y"], focus["zoom"])
    scale = max(cover * zoom, contain)
    pw, ph = sw * scale, sh * scale
    ox, oy = (cw - pw) * fx, (ch - ph) * fy

    def seen(lo: float, hi: float, size: float) -> float:
        return max(0.0, min(hi, size) - max(lo, 0.0)) / size

    if axis == "x":
        return seen(ox + band[0] * pw, ox + band[1] * pw, cw) * seen(oy, oy + ph, ch)
    return seen(ox, ox + pw, cw) * seen(oy + band[0] * ph, oy + band[1] * ph, ch)


def test_the_measured_aim_is_what_the_caption_colour_is_judged_against(monkeypatch, tmp_path):
    """Failure modes 1, 2, 4: the automatic aim from the measure (no words from her)."""
    _gate(monkeypatch, open_=True)
    _media(monkeypatch, tmp_path)
    recipe, policy = _compile(
        _background(windows=(_window(SECOND, 2.0, 4.0),)),
        visuals=(_measured(FILL, LEFT_PAGE), SECOND),
    )
    focus = recipe.model_dump(mode="json")["speaker_background"]["focus"]
    expected = focus_for_text_column(LEFT_PAGE, source_w=1920, source_h=1080)
    assert focus == {"x": expected.x, "y": expected.y, "zoom": expected.zoom}
    # The phone shows the white page across the whole frame; the plain cover fit shows none of it.
    assert _phone_share(WHITE, "x", focus=focus) > 0.9
    assert _phone_share(WHITE, "x", focus=None) == 0.0
    assert _brightness(policy, 0.0) == pytest.approx(
        _phone_share(WHITE, "x", focus=focus), abs=0.06
    )


def test_each_window_is_judged_by_its_own_aim(monkeypatch, tmp_path):
    """Failure mode 3: a window aimed at its right column; the fill (no aim, no measure) is not."""
    _gate(monkeypatch, open_=True)
    _media(monkeypatch, tmp_path)
    aim = SubtitledBackgroundFocus(x=1.0)
    recipe, policy = _compile(
        _background(windows=(_window(SECOND, 2.0, 4.0, focus=aim),)), visuals=(FILL, SECOND)
    )
    wire = recipe.model_dump(mode="json")["speaker_background"]
    assert "focus" not in wire and wire["windows"][0]["focus"] == {"x": 1.0, "y": 0.5, "zoom": 1.0}
    assert _brightness(policy, 0.0) < 0.03  # the fill's cover fit: its white band is off-frame
    expected = _phone_share((0.7, 1.0), "x", focus={"x": 1.0, "y": 0.5, "zoom": 1.0})
    assert expected > 0.9
    assert _brightness(policy, 2.0) == pytest.approx(expected, abs=0.06)


def test_a_zoom_below_cover_is_sampled_with_the_phones_black_bands(monkeypatch, tmp_path, events):
    """Failure mode 5: zoomed out, the picture no longer fills the frame."""
    _gate(monkeypatch, open_=True)
    _media(monkeypatch, tmp_path)
    aim = SubtitledBackgroundFocus(x=0.0, zoom=0.5)
    _recipe, policy = _compile(
        _background(windows=(_window(SECOND, 2.0, 4.0),), focus=aim), visuals=(FILL, SECOND)
    )
    assert "styling_background_sample_failed" not in events
    expected = _phone_share(WHITE, "x", focus={"x": 0.0, "y": 0.5, "zoom": 0.5})
    assert 0.15 < expected < 0.35  # the white page, cut by the black bands above and below
    assert _brightness(policy, 0.0) == pytest.approx(expected, abs=0.06)


def test_a_landscape_canvas_samples_the_aimed_part_in_its_own_shape(monkeypatch, tmp_path):
    """Failure mode 6: a portrait recording aimed at its top on a 16:9 edit."""
    _gate(monkeypatch, open_=True)
    _media(monkeypatch, tmp_path, fill="top", size=(1080, 1920))
    aim = SubtitledBackgroundFocus(x=0.5, y=0.0)
    recipe, policy = _compile(
        _background(windows=(_window(SECOND, 2.0, 4.0),), focus=aim),
        visuals=(FILL, SECOND),
        output_orientation="landscape",
    )
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    focus = {"x": 0.5, "y": 0.0, "zoom": 1.0}
    expected = _phone_share(WHITE, "y", focus=focus, source=(1080, 1920), canvas=(1920, 1080))
    assert expected > 0.9
    assert _brightness(policy, 0.0) == pytest.approx(expected, abs=0.06)


def test_a_closed_gate_records_exactly_what_green5_records(monkeypatch, tmp_path):
    """Failure mode 7: no aim anywhere, the plain cover fit, byte-identical spec."""
    _gate(monkeypatch, open_=False)
    _media(monkeypatch, tmp_path)
    aim = SubtitledBackgroundFocus(x=0.0)
    recipe, policy = _compile(
        _background(windows=(_window(SECOND, 2.0, 4.0, focus=aim),)),
        visuals=(_measured(FILL, LEFT_PAGE), SECOND),
    )
    assert "focus" not in recipe.model_dump_json()
    assert policy["_speaker_background"] == {
        "fill": {"path": FILL.gcs_path, "kind": "image", "source_start": 0.0},
        "windows": [
            {
                "path": SECOND.gcs_path,
                "kind": "image",
                "source_start": 0.0,
                "start_s": 2.0,
                "end_s": 4.0,
            }
        ],
        "frame": None,
    }
    assert _brightness(policy, 0.0) < 0.03 and _brightness(policy, 2.0) < 0.03


def test_the_gate_is_asked_once_for_the_recipe_and_the_caption_colour(monkeypatch, tmp_path):
    """Failure mode 8: the measured aim (no lane focus, so the lane strip never asks) costs one
    gate question for the recipe and the caption colour together."""
    calls: list = []
    _gate(monkeypatch, open_=True, calls=calls)
    _media(monkeypatch, tmp_path)
    recipe, policy = _compile(
        _background(windows=(_window(SECOND, 2.0, 4.0),)),
        visuals=(_measured(FILL, LEFT_PAGE), SECOND),
    )
    assert "focus" in recipe.model_dump(mode="json")["speaker_background"]
    assert len(calls) == 1
