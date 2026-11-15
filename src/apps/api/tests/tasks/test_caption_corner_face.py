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

import copy
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


# ------------------------------------------- KRI-1483: an ASKED row on her corner face
#
# Qendresa Q2 run 3 (prod 2026-11-15, job 77c63496): "Captions word by word in a light grey box
# near the bottom" with her cut out small in the bottom-right corner. The asked spot is kept as
# given (KRI-1177: x 0.5, y 0.79), which is the row her corner face is drawn in: the grey box of
# 41 of the 47 corner captions touched her face (up to 26 % of a box), the look receipt said
# ``over_face: true`` and the done-list said "Done" with no note.
#
# Raising the row above her head leaves "near the bottom" (kit band 0.65-0.92); under her chin
# is past the frame budget (0.88). Her own videos keep the box beside her (V6: x-centre 0.38).
#
# F1  An asked row on her corner face is drawn as asked, on her face.
# F2  The slid block lands on the Kria mark or leaves the frame; a wrapped block leaves her row.
# F3  The captions of her full-screen opening slide too (she fills the frame there).
# F4  The other corner presets are not mirrored.
# F5  No room beside her: the spot is kept but the done-list still says a bare "Done".
# F6  No corner frame, or the default spot: anything changes.
# F7  An editor Save / reopen draws the captions back on her face.
# F8  An asked row that never touches her (middle) moves.
# OFF SPEAKER_FRAME_CAPTION_FACES_ENABLED=false is today's look.

Q2_CAPTIONS = BriefRequirement(
    id="r10",
    kind="style",
    scope="global",
    description="Captions word by word in a light grey box near the bottom.",
    facts={"captions": "word_by_word", "caption_box": "#E5E5E5", "caption_position": "bottom"},
)
Q2_ASK = {"box": "#E5E5E5", "position": "bottom"}  # the job's stamped `caption_ask`
# Her face on the take (`opening_title_placement.face_band`), where placement sees it in the
# corner (the frame math plus `_DRAWN_FACE_MARGIN`), and Apple Vision on the exported frames.
Q2_TAKE_FACE = (0.2067, 0.2113, 0.8452, 0.6175)
Q2_DRAWN_FACE = (0.627, 0.669, 0.915, 0.859)
Q2_KIT_FACE = (0.652, 0.692, 0.886, 0.828)
Q2_OPENING_S = 5.54  # `subtitled_lane_receipt.full_screen_opening_s`
# variants[0].caption_cues of the job: her opening pieces, then the widest corner pieces.
Q2_CUES = [
    {"id": cue_id, "text": text, "start_s": start, "end_s": end}
    for cue_id, text, start, end in (
        ("native-caption-1-1", "talking", 3.02, 3.36),
        ("native-caption-1-2", "about.", 3.36, 3.87),
        ("native-caption-2", "Number one,", 4.12, 5.08),
        ("native-caption-2-1", "Spec kit.", 5.08, 5.4),
        ("native-caption-3", "You write", 5.68, 6.04),
        ("native-caption-3-2", "first, and", 6.36, 7.06),
        ("native-caption-4", "agent builds", 7.5, 7.8),
        ("native-caption-5-1", "impeccable.", 10.266, 10.766),
        ("native-caption-8-2", "looking generic.", 15.006, 15.766),
        ("native-caption-9", "Number three,", 16.231, 16.971),
        ("native-caption-9-1", "trading agents.", 16.971, 17.751),
        ("native-caption-14-1", "ready-made sub-agents", 26.574, 27.474),
        ("native-caption-14-2", "you", 27.474, 27.754),
        ("native-caption-18", "Directors, leads,", 32.644, 33.124),
        ("native-caption-20", "Comment 'repo',", 36.014, 36.434),
    )
]
Q2_OPENING_IDS = {cue["id"] for cue in Q2_CUES if cue["end_s"] <= Q2_OPENING_S}
# Today's look receipt for the ask (the job's own, minus its two-word count).
Q2_AS_ASKED = {
    "position": "bottom",
    "box": "#E5E5E5",
    "words": 2,
    "size_px": 64,
    "position_x_frac": 0.5,
    "position_y_frac": 0.79,
}
ASKED_BOTTOM_ROW = (0.70, 0.90)  # `caption_look_ask.CAPTION_SPOT_Y_BANDS["bottom"]`
FRAME_BUDGET = 0.88  # `phone_captions.ASKED_CAPTION_FRAME_BUDGET_FRAC`


def _q2_compile(*, frame=CORNER, take_face=Q2_TAKE_FACE, ask=Q2_ASK, opening_s=Q2_OPENING_S):
    """The real caption look and compile for the job's inputs: ``(boxes, look, receipt)`` with
    one ``(cue id, start, grey box)`` per caption. The policy is what the worker holds once the
    corner background is registered; no worker, no I/O."""
    from app.pipeline.phone_captions import compile_caption_layers

    policy: dict = {
        "version": 1,
        "requested": {},
        "preferences": {"all_text": {}},
        "selected_fonts": {},
        "selection_calls": 2,
        "_environment_sampled": True,
        "_face_boxes_raw": [{"start_s": 0.0, "end_s": 60.0, "box": take_face, "kind": "face"}],
    }
    if ask is not None:
        policy["caption_ask"] = dict(ask)
    if frame is not None:
        preset = SPEAKER_FRAME_PRESETS[frame]
        policy["_speaker_layout"] = {
            "frame": {
                "width": preset["width_fraction"],
                "x": preset["x_fraction"],
                "y": preset["y_fraction"],
            },
            "full_screen": [{"start_s": 0.0, "end_s": opening_s}] if opening_s else [],
        }
    policy["_face_boxes"] = styling._faces_where_drawn(policy["_face_boxes_raw"], policy)
    with styling.phone_text_policy_scope(policy):
        layers = compile_caption_layers(
            [dict(cue) for cue in Q2_CUES], canvas_width=1080, canvas_height=1920, style="word"
        )
        live = styling._POLICY.get()
        look, receipt = live["caption_look"], dict(live.get("_caption_look_receipt") or {})
        _Q2_SAVED.clear()
        _Q2_SAVED.update(styling.generation_style_fields())
    return _q2_boxes(layers), look, receipt


_Q2_SAVED: dict = {}  # the variant fields the last `_q2_compile` would persist


def _q2_boxes(layers) -> list[tuple]:
    boxes = []
    for cue, layer in zip(Q2_CUES, layers, strict=True):
        back = layer.background
        box = (
            None
            if back is None
            else (
                back.left / 1080,
                back.top / 1920,
                (back.left + back.width) / 1080,
                (back.top + back.height) / 1920,
            )
        )
        boxes.append((cue["id"], float(layer.start), box))
    return boxes


def _centre_x(box) -> float:
    return (box[0] + box[2]) / 2


def _mirrored(box):
    return (1 - box[2], box[1], 1 - box[0], box[3])


def test_her_asked_bottom_box_sits_beside_her_corner_face_not_on_it():
    boxes, look, receipt = _q2_compile()

    corner = [box for _id, start, box in boxes if start >= Q2_OPENING_S]
    assert corner and all(box is not None for box in corner), "the grey box is kept"
    for face in (Q2_DRAWN_FACE, Q2_KIT_FACE):
        hits = [tuple(round(v, 3) for v in box) for box in corner if _overlap(box, face)]
        assert not hits, f"{len(hits)}/{len(corner)} corner captions on her face: {hits}"
    # One x for every slid caption (no sideways jitter), left of her, clear of the frame edge.
    centres = [_centre_x(box) for box in corner]
    assert max(centres) - min(centres) <= 0.003, sorted({round(c, 3) for c in centres})
    assert 0.25 <= centres[0] < 0.45
    assert min(box[0] for box in corner) >= 0.05
    # Her row: "near the bottom" (kit: median 0.65-0.92), a wrapped block included.
    wrapped = [box for box in corner if box[3] - box[1] > 0.07]
    assert wrapped, "setup: her widest pieces wrap beside the Kria mark"
    for box in corner:
        assert ASKED_BOTTOM_ROW[0] <= (box[1] + box[3]) / 2 <= ASKED_BOTTOM_ROW[1], box
        assert box[3] <= FRAME_BUDGET + 1e-3, box
    one_row = [box for box in corner if box not in wrapped]
    assert all((box[1] + box[3]) / 2 == pytest.approx(0.79, abs=0.005) for box in one_row)
    assert receipt["position_y_frac"] == 0.79 and receipt["position"] == "bottom"
    assert receipt["over_face"] is False
    assert look.position_y_frac == 0.79 and look.box_color == "#E5E5E5"


def test_the_captions_of_her_full_screen_opening_stay_centred():
    boxes, _look, _receipt = _q2_compile()

    opening = [box for cue_id, _start, box in boxes if cue_id in Q2_OPENING_IDS]
    assert len(opening) == len(Q2_OPENING_IDS) == 4
    for box in opening:
        assert _centre_x(box) == pytest.approx(0.5, abs=0.003), box
        assert (box[1] + box[3]) / 2 == pytest.approx(0.79, abs=0.005)
        assert not _overlap(box, Q2_TAKE_FACE), "under her full-frame face, as today"


@pytest.mark.parametrize("frame", ["small_bottom_left"])
def test_the_other_bottom_corner_is_mirrored(frame):
    boxes, _look, receipt = _q2_compile(frame=frame)

    corner = [box for _id, start, box in boxes if start >= Q2_OPENING_S]
    hits = [box for box in corner if _overlap(box, _mirrored(Q2_DRAWN_FACE))]
    assert not hits, f"{len(hits)}/{len(corner)} corner captions on her face"
    centres = [_centre_x(box) for box in corner]
    assert max(centres) - min(centres) <= 0.003 and centres[0] > 0.55
    assert max(box[2] for box in corner) <= 0.95
    assert receipt["over_face"] is False and receipt["position_y_frac"] == 0.79


def test_no_room_beside_her_keeps_her_spot_and_says_it_covers_her():
    # A face as wide as her corner frame: no side of the row holds her widest caption.
    boxes, look, receipt = _q2_compile(take_face=(0.0, 0.2113, 1.0, 0.6175))

    assert {round(_centre_x(box), 2) for _id, _start, box in boxes} == {0.5}
    assert look.line_overrides is None and look.max_width_frac == 0.68
    assert receipt == {**Q2_AS_ASKED, "over_face": True, "beside_speaker": False}


def test_switch_off_keeps_her_asked_spot_as_today(monkeypatch):
    monkeypatch.setattr(settings, "speaker_frame_caption_faces_enabled", False)
    boxes, look, receipt = _q2_compile()

    assert {round(_centre_x(box), 2) for _id, _start, box in boxes} == {0.5}
    assert (look.position_x_frac, look.position_y_frac, look.line_overrides) == (0.5, 0.79, None)
    # The old full-frame faces: the review does not see her in the corner.
    assert receipt == {**Q2_AS_ASKED, "over_face": False}
    hits = [
        box for _id, start, box in boxes if start >= Q2_OPENING_S and _overlap(box, Q2_KIT_FACE)
    ]
    assert hits, "setup: with the switch off the asked row is on her corner face"


@pytest.mark.parametrize(
    ("frame", "ask", "x"),
    [
        (None, Q2_ASK, 0.5),  # no corner frame: she is full-frame, the row is under her face
        (CORNER, {"position": "middle"}, 0.5),  # a row that never touches her in the corner
    ],
    ids=["no_corner_frame", "asked_middle"],
)
def test_an_asked_row_that_never_touches_her_is_unchanged(frame, ask, x):
    boxes, look, receipt = _q2_compile(frame=frame, ask=ask, opening_s=None)

    assert look.position_x_frac == x and look.line_overrides is None
    assert receipt["position_x_frac"] == x and receipt["over_face"] is False
    assert set(receipt) <= {*Q2_AS_ASKED, "over_face"}, "no slide was tried"


def test_the_default_spot_dodges_her_corner_face_as_before():
    _boxes, look, receipt = _q2_compile(ask=None)

    assert look.line_overrides is None and receipt == {}


def test_a_save_and_reopen_keeps_the_opening_centred_and_the_rest_beside_her():
    """F7: the editor recompiles from the saved variant fields (`caption_look_from_variant`):
    the slid x, its wrap width and the opening's per-line x all come back."""
    from app.pipeline.phone_captions import caption_look_from_variant, compile_caption_layers

    drawn, look, _receipt = _q2_compile()
    style = _Q2_SAVED["caption_editor_style"]
    assert style["position_x_frac"] == look.position_x_frac
    assert style["line_overrides"] == {
        cue_id: {"position_x_frac": 0.5} for cue_id in Q2_OPENING_IDS
    }
    reopened = compile_caption_layers(
        [dict(cue) for cue in Q2_CUES],
        canvas_width=1080,
        canvas_height=1920,
        style="word",
        look=caption_look_from_variant({**_Q2_SAVED, "caption_cues": Q2_CUES}),
    )
    for (_id, _start, was), (_id2, _start2, now) in zip(drawn, _q2_boxes(reopened), strict=True):
        assert now == pytest.approx(was, abs=1e-6)


# The real phone Talking worker (the take's face goes through the REAL environment sampler,
# the background registers after the first styling pass, as in prod).


def _q2_job(monkeypatch, *, take_face=Q2_TAKE_FACE, captions=Q2_CAPTIONS):
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", True)
    captured = _simulate_environment(monkeypatch, take_face)
    job = _windows_job(monkeypatch, SEQUENCE, cadence_s=4.0, speaker_frame=CORNER)
    features = [f for f in settings.phone_render_verified_features if f != "captionShape"]
    monkeypatch.setattr(settings, "phone_render_verified_features", [*features, "captionShape"])
    job.all_candidates["voiceover_caption_style"] = "word"
    job.all_candidates["creator_client_protocol"] = 36
    _finish(job, (R1, captions, R4), "Captions word by word in a light grey box near the bottom.")
    return job, captured


def _q2_rects(layers) -> list[tuple]:
    return [
        (
            layer.background.left / 1080,
            layer.background.top / 1920,
            (layer.background.left + layer.background.width) / 1080,
            (layer.background.top + layer.background.height) / 1920,
        )
        for layer in layers
        if layer.id.startswith("caption-") and layer.background is not None
    ]


def _q2_receipt(req, variant):
    from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant

    (receipt,) = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


def test_the_worker_draws_her_grey_box_beside_her_and_the_review_says_done(monkeypatch):
    job, _captured = _q2_job(monkeypatch)
    _run(job)

    variant = job.assembly_plan["variants"][0]
    rects = _q2_rects(_recipe(job).text_layers)
    assert len(rects) > 10
    for face in (Q2_DRAWN_FACE, Q2_KIT_FACE):
        hits = [tuple(round(v, 3) for v in r) for r in rects if _overlap(r, face)]
        assert not hits, f"{len(hits)}/{len(rects)} captions on her corner face: {hits[:3]}"
    assert all(ASKED_BOTTOM_ROW[0] <= (r[1] + r[3]) / 2 <= ASKED_BOTTOM_ROW[1] for r in rects)
    look = variant["caption_look_receipt"]
    assert (look["position"], look["position_y_frac"], look["over_face"]) == ("bottom", 0.79, False)
    assert variant["caption_shape"]["box"] == "#E5E5E5"
    assert _q2_receipt(Q2_CAPTIONS, variant).status == "met"
    # The look on its own never says she is covered when she is not.
    alone = Q2_CAPTIONS.model_copy(
        update={"facts": {"caption_box": "#E5E5E5", "caption_position": "bottom"}}
    )
    receipt = _q2_receipt(alone, variant)
    assert receipt.status == "met" and "over you" not in (receipt.reason or ""), receipt


def test_an_editor_save_and_reopen_keeps_her_captions_beside_her(monkeypatch):
    from app.pipeline.phone_captions import caption_look_from_variant, compile_caption_layers

    job, _captured = _q2_job(monkeypatch)
    _run(job)
    variant = job.assembly_plan["variants"][0]
    drawn = _q2_rects(_recipe(job).text_layers)
    saved = compile_caption_layers(
        variant["caption_cues"],
        canvas_width=1080,
        canvas_height=1920,
        style="word",
        look=caption_look_from_variant(variant),
    )

    assert drawn and _q2_rects(saved) == pytest.approx(drawn, abs=1e-6)


def test_the_worker_with_no_room_beside_her_keeps_her_spot_and_the_review_says_so(monkeypatch):
    job, _captured = _q2_job(monkeypatch, take_face=(0.0, 0.2113, 1.0, 0.6175))
    _run(job)

    variant = job.assembly_plan["variants"][0]
    look = variant["caption_look_receipt"]
    assert (look["position_x_frac"], look["position_y_frac"], look["over_face"]) == (
        0.5,
        0.79,
        True,
    )
    receipt = _q2_receipt(Q2_CAPTIONS, variant)
    assert receipt.status == "partial", receipt
    assert "cover a face in places" in receipt.reason


def test_the_review_reads_the_opening_lines_as_the_writers_own(monkeypatch):
    """The opening's centred lines are per-line overrides the writer drew: the finished check
    still says the look is hers, and only because the receipt says who drew them."""
    job, _captured = _q2_job(monkeypatch)
    _run(job)
    variant = copy.deepcopy(job.assembly_plan["variants"][0])
    # The same render with a full-screen opening: its first caption kept centred.
    variant["caption_cues"][0]["id"] = "native-caption-0"
    variant["caption_editor_style"]["line_overrides"] = {
        "native-caption-0": {"position_x_frac": 0.5}
    }
    variant["caption_look_receipt"]["full_screen_x_frac"] = 0.5
    assert _q2_receipt(Q2_CAPTIONS, variant).status == "met"

    # Control: the same line with no word from the writer is an edit of her look.
    del variant["caption_look_receipt"]["beside_speaker"]
    edited = _q2_receipt(Q2_CAPTIONS, variant)
    assert edited.status == "partial" and "no longer look" in edited.reason
