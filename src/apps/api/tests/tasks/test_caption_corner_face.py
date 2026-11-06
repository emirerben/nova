"""Qendresa case 1: captions are placed off the speaker's face where the phone DRAWS it.

On a Talk-to-camera edit with a speaker background and a corner frame, the phone draws the
canvas-sized speaker layer scaled by the frame's `width_fraction`, centred at its x/y fractions
(`RecipeVideoCompositor.framed`). Caption placement used to dodge the face where it sits in the
full-frame take (y ~0.2-0.4), so captions landed on her real face in the corner (y ~0.65-0.76),
and the caption review said `over_face: False`.

Ported from the Qendresa kit (`tests/bugs/test_q2_caption_over_corner_face.py`), plus the cases in
`docs/reviews/caption-corner-face-failure-modes.md`, all written before the fix. Runs the REAL phone
Talking worker and the REAL `_ensure_generation_environment` on the Green 3 / Cafe T2 harness.
Simulated I/O only: the proxy download is a no-op, the face sampler returns one face box for the
take, contrast sampling returns nothing, and the font model is skipped (as the harness does).

The six REAL faces are Apple Vision face boxes (the kit's `tools/frame_text_faces`) measured on
2026-11-06 from frames at 2/6/10 s of two real stand-in takes (`kria-green-screen-gt/woman_hair` and
`man_gestures`, `truth_over_black.mp4`, 1080x1920), full-frame and drawn in the bottom-right corner.
Vision's corner box matched the frame math below within 0.002, so the drawn faces are computed.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.pipeline import phone_text_background as background_mod
from app.pipeline import render_geometry as geometry_mod
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services import phone_text_styling as styling
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.tasks.test_green3_caption_look_worker import R1, R4, SEQUENCE
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture
    _recipe,
)
from tests.tasks.test_speaker_background_strategy_worker import _colour_job, _windows_job

# The kit's simulated face: a typical talking-head face in the upright take.
SIMULATED_FACE = (0.33, 0.22, 0.67, 0.40)

# Apple Vision face boxes (left, top, right, bottom) on the full-frame take, 2026-11-06.
REAL_FACES = {
    "man_gestures_2s": (0.316, 0.165, 0.736, 0.402),
    "man_gestures_6s": (0.228, 0.153, 0.637, 0.383),
    "man_gestures_10s": (0.265, 0.157, 0.670, 0.385),
    "woman_hair_2s": (0.348, 0.144, 0.658, 0.318),
    "woman_hair_6s": (0.295, 0.147, 0.621, 0.331),
    "woman_hair_10s": (0.323, 0.135, 0.638, 0.312),
}

CORNER = "small_bottom_right"


def _drawn(box, preset: str = CORNER):
    """Where the phone draws ``box`` (canvas fractions) with the speaker in ``preset``."""
    frame = SPEAKER_FRAME_PRESETS[preset]
    w, cx, cy = frame["width_fraction"], frame["x_fraction"], frame["y_fraction"]
    return (
        cx + (box[0] - 0.5) * w,
        cy + (box[1] - 0.5) * w,
        cx + (box[2] - 0.5) * w,
        cy + (box[3] - 0.5) * w,
    )


def _overlap(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _simulate_environment(monkeypatch, take_face):
    """Simulated I/O in the REAL `_ensure_generation_environment`; captures what placement saw."""
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
    import app.storage as storage_mod

    monkeypatch.setattr(storage_mod, "download_to_file", lambda *a, **k: None)
    monkeypatch.setattr(
        geometry_mod,
        "sample_face_regions",
        lambda path, points, **k: (
            [ProtectedRegion(0.0, 1800.0, NormalizedBox(*take_face), "face")],
            {"decoded": len(points)},
        ),
    )
    monkeypatch.setattr(background_mod, "sample_background", lambda *a, **k: None)
    captured: dict = {}
    original = styling.remember_caption_bounds

    def remember(layers, *, canvas):
        original(layers, canvas=canvas)
        policy = styling._POLICY.get() or {}
        captured["boxes"] = [dict(b) for b in policy.get("_caption_boxes") or []]
        captured["faces"] = [dict(f) for f in policy.get("_face_boxes") or []]

    monkeypatch.setattr(styling, "remember_caption_bounds", remember)
    return captured


def _finish(job, requirements, request):
    job.all_candidates["creator_request"] = request
    brief = CreativeBrief(version=1, requirements=list(requirements))
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
    return job


def _windows(
    monkeypatch, *, take_face=SIMULATED_FACE, requirements=(R1, R4), frame=CORNER, **extra
):
    captured = _simulate_environment(monkeypatch, take_face)
    job = _windows_job(monkeypatch, SEQUENCE, cadence_s=4.0, speaker_frame=frame, **extra)
    _finish(job, requirements, "Make it like a green screen. No music.")
    return job, captured


def _hits(captured, face) -> list[tuple]:
    return [
        tuple(round(v, 3) for v in b["box"]) for b in captured["boxes"] if _overlap(b["box"], face)
    ]


def _run(job):
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail


def test_default_captions_stay_off_her_face_in_the_corner(monkeypatch):
    job, captured = _windows(monkeypatch)
    _run(job)
    assert captured.get("boxes"), "no caption bounds were recorded"
    hits = _hits(captured, _drawn(SIMULATED_FACE))
    assert not hits, f"{len(hits)}/{len(captured['boxes'])} caption blocks cover her corner face"


@pytest.mark.parametrize("name", sorted(REAL_FACES))
def test_real_faces_in_the_corner_are_never_under_a_caption(monkeypatch, name):
    take_face = REAL_FACES[name]
    job, captured = _windows(monkeypatch, take_face=take_face)
    _run(job)
    hits = _hits(captured, _drawn(take_face))
    assert not hits, f"{name}: {len(hits)}/{len(captured['boxes'])} caption blocks on her face"


def test_placement_sees_her_face_where_the_phone_draws_it(monkeypatch):
    job, captured = _windows(monkeypatch)
    _run(job)
    faces = [tuple(f["box"]) for f in captured["faces"]]
    assert faces, "no face reached placement"
    # Where the phone draws it, with a little air around it (never tighter than the drawn face).
    drawn = _drawn(SIMULATED_FACE)
    assert all(
        f[0] <= drawn[0]
        and f[1] <= drawn[1]
        and f[2] >= drawn[2]
        and f[3] >= drawn[3]
        and max(abs(a - b) for a, b in zip(f, drawn, strict=True)) <= 0.02
        for f in faces
    ), faces


def test_asked_bottom_captions_over_her_face_are_said(monkeypatch):
    """KRI-1177: an asked spot stays hers; when it covers her drawn face the review says so."""
    captions = BriefRequirement(
        id="r3",
        kind="style",
        scope="global",
        description="Captions word by word in a light grey box near the bottom",
        facts={"captions": "word_by_word", "caption_position": "bottom"},
    )
    job, captured = _windows(monkeypatch, requirements=(R1, captions, R4))
    job.all_candidates["creator_request"] = (
        "Captions word by word in a light grey box near the bottom."
    )
    _run(job)
    variant = job.assembly_plan["variants"][0]
    covers = bool(_hits(captured, _drawn(SIMULATED_FACE)))
    over_face = (variant.get("caption_look_receipt") or {}).get("over_face")
    assert not covers or over_face is True, f"covers her face, but over_face={over_face!r}"


def test_inside_a_full_screen_opening_her_face_stays_full_frame(monkeypatch):
    """Item 16: she fills the canvas in the opening; only after it is she in the corner."""
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", True)
    job, captured = _windows(monkeypatch, full_screen_opening=True)
    features = [f for f in settings.phone_render_verified_features if f != "speakerFrameWindows"]
    monkeypatch.setattr(
        settings, "phone_render_verified_features", [*features, "speakerFrameWindows"]
    )
    job.all_candidates["creator_client_protocol"] = 41
    _run(job)
    spans = _recipe(job).speaker_background.full_screen_windows or []
    assert spans, "the opening was not laid out"
    opening_end = spans[0].end
    faces = captured["faces"]
    inside = [f for f in faces if f["end_s"] <= opening_end + 1e-6]
    after = [f for f in faces if f["start_s"] >= opening_end - 1e-6]
    assert inside and all(
        tuple(f["box"]) == pytest.approx(SIMULATED_FACE, abs=1e-6) for f in inside
    ), inside
    drawn = _drawn(SIMULATED_FACE)
    assert after and all(
        f["box"][0] <= drawn[0]
        and f["box"][1] <= drawn[1]
        and f["box"][3] <= 1.0
        and max(abs(a - b) for a, b in zip(f["box"], drawn, strict=True)) <= 0.02
        for f in after
    ), after


def test_a_colour_fill_with_the_speaker_in_the_corner_keeps_captions_off_her_face(monkeypatch):
    """KRI-1173 colour fills skip the contrast pass; the corner frame must still reach faces."""
    captured = _simulate_environment(monkeypatch, SIMULATED_FACE)
    job = _colour_job(
        monkeypatch, {"color": "#FFFFFF", "speaker_frame": CORNER}, windows_verified=True
    )
    _finish(job, (R1, R4), "Make it like a green screen on a white background. No music.")
    _run(job)
    layer = _recipe(job).speaker_background
    assert layer is not None and layer.media_kind == "color" and layer.speaker_frame is not None
    hits = _hits(captured, _drawn(SIMULATED_FACE))
    assert not hits, f"{len(hits)}/{len(captured['boxes'])} caption blocks cover her corner face"


def test_with_the_contrast_switch_off_captions_still_avoid_her_corner_face(monkeypatch):
    monkeypatch.setattr(settings, "caption_background_contrast_enabled", False)
    job, captured = _windows(monkeypatch)
    _run(job)
    hits = _hits(captured, _drawn(SIMULATED_FACE))
    assert not hits, f"{len(hits)}/{len(captured['boxes'])} caption blocks cover her corner face"


def test_kill_switch_off_places_faces_as_before(monkeypatch):
    """Control: SPEAKER_FRAME_CAPTION_FACES_ENABLED=false keeps today's full-frame boxes."""
    monkeypatch.setattr(settings, "speaker_frame_caption_faces_enabled", False, raising=False)
    job, captured = _windows(monkeypatch)
    _run(job)
    faces = [tuple(f["box"]) for f in captured["faces"]]
    assert faces and all(f == pytest.approx(SIMULATED_FACE, abs=1e-6) for f in faces), faces


def test_without_a_corner_frame_faces_are_unchanged(monkeypatch):
    """Control: a full-size speaker (no frame) keeps the full-frame face."""
    job, captured = _windows(monkeypatch, frame=None)
    _run(job)
    assert _recipe(job).speaker_background.speaker_frame is None
    faces = [tuple(f["box"]) for f in captured["faces"]]
    assert faces and all(f == pytest.approx(SIMULATED_FACE, abs=1e-6) for f in faces), faces


# --- Review on #2127 (Emir, 2026-11-06): a background that fails to bind after registering ---
# The compiler registers the corner background (and styles the captions against it) before the
# background media is bound; a video whose start is at/past its end fails that bind, the worker
# drops the background lane and compiles again with no background. The final recipe draws the
# speaker full-frame, so caption placement and the review must use her full-frame face again.


def _failed_background_job(monkeypatch, *, requirements=(R1, R4), take_face=SIMULATED_FACE):
    from tests.tasks.test_kri841_talking_ending_clip import BITE, BITE_S

    captured = _simulate_environment(monkeypatch, take_face)
    job = _windows_job(
        monkeypatch,
        [],
        speaker_frame=CORNER,
        media_id=f"asset-{BITE}",
        source_start_s=BITE_S,  # at the video's end: nothing playable, the bind fails
    )
    _finish(job, requirements, "Make it like a green screen. No music.")
    return job, captured


def test_a_background_dropped_after_binding_puts_her_face_back_full_frame(monkeypatch):
    registered: list[bool] = []
    original = styling.register_generation_background

    def spy(background, canvas=None, focus_for=None):
        registered.append(getattr(background, "speaker_frame", None) is not None)
        return original(background, canvas=canvas, focus_for=focus_for)

    monkeypatch.setattr(styling, "register_generation_background", spy)
    job, captured = _failed_background_job(monkeypatch)
    _run(job)
    # Setup: the corner background was registered, then dropped, and the final recipe has none.
    assert True in registered, registered
    assert _recipe(job).speaker_background is None
    drops = job.assembly_plan["variants"][0]["phone_lane_receipt"]["dropped"]
    assert any(d.get("lane") == "background" for d in drops), drops
    faces = [tuple(f["box"]) for f in captured["faces"]]
    assert faces and all(f == pytest.approx(SIMULATED_FACE, abs=1e-6) for f in faces), faces
    hits = _hits(captured, SIMULATED_FACE)
    assert not hits, f"{len(hits)}/{len(captured['boxes'])} caption blocks on her full-frame face"


# A take where she sits low in the frame: full-frame, middle captions cross her face; drawn in the
# corner she would be far from them.
LOW_FACE = (0.30, 0.42, 0.70, 0.62)


def test_after_a_dropped_background_the_review_judges_the_full_frame_face(monkeypatch):
    """An asked middle spot stays hers (KRI-1177); the review must say it covers her face, which
    the stale corner box would deny."""
    captions = BriefRequirement(
        id="r3",
        kind="style",
        scope="global",
        description="Captions in the middle",
        facts={"captions": "word_by_word", "caption_position": "middle"},
    )
    job, captured = _failed_background_job(
        monkeypatch, requirements=(R1, captions, R4), take_face=LOW_FACE
    )
    _run(job)
    assert _recipe(job).speaker_background is None
    assert _hits(captured, LOW_FACE), "setup: middle captions should cross her full-frame face"
    variant = job.assembly_plan["variants"][0]
    over_face = (variant.get("caption_look_receipt") or {}).get("over_face")
    assert over_face is True, f"captions cover her full-frame face, review over_face={over_face!r}"


def test_a_dropped_background_judges_the_caption_colour_on_her_take_again(monkeypatch, tmp_path):
    """Second review on #2127: the contrast frames are restored, exercised with REAL sampling.

    Green 5's harness samples real luminance grids (take and backgrounds). The corner background is
    registered (the caption colour is chosen against the room behind her), then its fill fails to
    bind (a video starting at its end) and the worker compiles without it. The finished look must
    be exactly what a Talking edit with no background picks from her take (Green 5's own control:
    #767676, no outline), never the look chosen against the dropped room."""
    from tests.tasks import test_green45_caption_contrast as green5
    from tests.tasks.test_kri841_talking_ending_clip import BITE, BITE_S

    job = green5._job(monkeypatch, tmp_path)
    background = job.all_candidates["creator_strategy"]["background_media"]
    background.update(media_id=f"asset-{BITE}", source_start_s=BITE_S, speaker_frame=CORNER)
    seen: list[bool] = []
    original = styling.register_generation_background

    def spy(background, canvas=None, focus_for=None):
        seen.append(background is not None)
        return original(background, canvas=canvas, focus_for=focus_for)

    monkeypatch.setattr(styling, "register_generation_background", spy)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    assert True in seen, "setup: the background was never registered"
    assert _recipe(job).speaker_background is None, "setup: the background was not dropped"
    assert green5._look(job) == {
        "caption_text_color": "#767676",
        "caption_stroke_width": 0,
        "caption_shadow_enabled": False,
        "stroke_color": None,
    }
