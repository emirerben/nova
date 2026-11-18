"""KRI-1525: Talking captions on a sideways take shown with black bars.

Cusco DJI kit T1 "Good Take" (job d988bb99) and T2 "list 1" (job 887f89d6): a 1920x1080 take on
phone Talking, `landscape_fit: "fit"`. The caption look is chosen before the plan is compiled
(KRI-632 face band), so caption styling sampled the take as if it filled the canvas: the face
covered the whole frame, the captions were squeezed into the top strip at 34-38 px, and T1's bright
footage gave them black ink on the black bar.

Every case below is from `docs/reviews/kri-1525-letterbox-caption-geometry-failure-modes.md`,
written before the fix. The worker-level cases run the REAL phone Talking worker and the REAL
`_ensure_generation_environment` / caption placement; only I/O is simulated: the proxy download is a
no-op, the face sampler returns a fixed face box for the take and the background sampler returns a
synthetic frame (bright footage; the fitted picture over black when asked for one).

`T2_FACE` is the raw detection Kria's own face sampler (`app.pipeline.face_sampler_worker`, OpenCV
Haar) returned on a 568x320 proxy of the kit's T2 take (DJI 0130) at 10 s, 2026-11-17; the fake
sampler pads it the way `sample_face_regions` does (`_face_protection_box`).
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline import phone_text_background as background_mod
from app.pipeline import render_geometry as geometry_mod
from app.pipeline.canvas import PORTRAIT
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services import phone_text_styling as styling
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.tasks.test_green3_caption_look_worker import R4
from tests.tasks.test_kri841_talking_ending_clip import _offline  # noqa: F401 - autouse fixture
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _setup_landscape_subtitled,
    _setup_subtitled,
)

T2_FACE = (0.383, 0.163, 0.652, 0.641)
# A face at the left side of a sideways take: outside the middle third a cover crop keeps.
SIDE_FACE = (0.05, 0.20, 0.25, 0.60)
# 1920x1080 into 1080x1920 with bars: contain / cover (`fit_transform`).
FIT_SCALE = 0.31640625
# The fitted picture's band on the canvas: 1080 x 607.5 px, i.e. FIT_SCALE of the height, centred.
BAND = (0.5 - FIT_SCALE / 2, 0.5 + FIT_SCALE / 2)

BRIGHT, BLACK = 235, 0


def _frame(*, letterbox: bool) -> bytes:
    """A gray analysis frame: bright footage everywhere, or bright only inside the fitted band."""
    width, height = background_mod.WIDTH, background_mod.HEIGHT
    rows = []
    for row in range(height):
        inside = BAND[0] <= (row + 0.5) / height <= BAND[1]
        rows.append(bytes([BRIGHT if inside or not letterbox else BLACK]) * width)
    return b"".join(rows)


def _padded(face) -> tuple:
    box = geometry_mod._face_protection_box(NormalizedBox(*face))
    return (box.left, box.top, box.right, box.bottom)


def _simulate_io(monkeypatch, take_face, *, calls: list | None = None):
    import app.storage as storage_mod

    monkeypatch.setattr(storage_mod, "download_to_file", lambda *a, **k: None)
    raw = NormalizedBox(*take_face) if take_face is not None else None

    def faces(path, points, *, raw_boxes=False, **kwargs):
        if raw is None:  # a take with nobody in it
            return [], {"decoded": len(points), "attempted": len(points), "detected": 0}
        # As `sample_face_regions` returns them: padded for protection unless asked raw.
        box = raw if raw_boxes else geometry_mod._face_protection_box(raw)
        region = ProtectedRegion(0.0, 1800.0, box, "face")
        return [region], {"decoded": len(points), "attempted": len(points), "detected": 1}

    monkeypatch.setattr(geometry_mod, "sample_face_regions", faces)
    monkeypatch.setattr(title_mod, "sample_face_regions", faces)

    def background(path, at_s, *, crop=None, canvas=None, focus=None):
        if calls is not None:
            calls.append(focus)
        return _frame(letterbox=focus is not None)

    monkeypatch.setattr(background_mod, "sample_background", background)


def _style(job) -> None:
    brief = CreativeBrief(version=1, requirements=[R4])
    job.all_candidates["creator_request"] = "Turkish captions. No music."
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call


def _capture_faces(monkeypatch) -> dict:
    captured: dict = {}
    original = styling.remember_caption_bounds

    def remember(layers, *, canvas):
        original(layers, canvas=canvas)
        policy = styling._POLICY.get() or {}
        captured["faces"] = [dict(f) for f in policy.get("_face_boxes") or []]
        captured["frames"] = len(policy.get("_background_frames") or [])

    monkeypatch.setattr(styling, "remember_caption_bounds", remember)
    return captured


def _run(job) -> dict:
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    return job.assembly_plan["variants"][0]


def _talking(monkeypatch, *, landscape_fit, switch=True, take_face=T2_FACE, landscape=True):
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "talking_fit_caption_geometry_enabled", switch)
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
    calls: list = []
    _simulate_io(monkeypatch, take_face, calls=calls)
    if landscape:
        job = _setup_landscape_subtitled(monkeypatch, landscape_fit=landscape_fit)
    else:
        job, _snapshot, _session, _binding_ = _setup_subtitled(monkeypatch)
        job.all_candidates["landscape_fit"] = landscape_fit
    _style(job)
    captured = _capture_faces(monkeypatch)
    variant = _run(job)
    return variant, captured, calls


def _look(variant) -> tuple:
    style = variant["caption_editor_style"]
    return (
        variant["caption_size_px"],
        round(style["position_x_frac"], 4),
        round(style["position_y_frac"], 4),
        style["alignment"],
        variant["caption_text_color"],
    )


# -------------------------------------------------------------- the bug (items 1, 9, 10)


def test_a_letterboxed_take_is_measured_where_the_phone_draws_it(monkeypatch):
    """Items 1 and 9: the face and the background the captions are placed against are the take as
    drawn (inside the fitted band, over black), so the captions are not pushed into the top strip,
    keep a readable size and are never black."""
    variant, captured, calls = _talking(monkeypatch, landscape_fit="fit")

    faces = [f["box"] for f in captured["faces"]]
    assert faces, "the face was not sampled"
    for box in faces:
        assert BAND[0] - 0.02 <= box[1] and box[3] <= BAND[1] + 0.02, box
    assert calls and all(focus == {"x": 0.5, "y": 0.5, "zoom": FIT_SCALE} for focus in calls)

    size, _x, y, _align, colour = _look(variant)
    assert y > 0.5, f"captions still squeezed into the top strip at y {y}"
    assert size >= 48, f"captions shrank to {size} px"
    # The block now sits under the picture, mostly on the bar: its ink must read on black.
    assert styling._contrast(colour, "#000000") >= styling.READABLE_CONTRAST, colour


def test_the_switch_off_keeps_todays_top_strip(monkeypatch):
    """Item 10, and the control for the test above: with the switch off the same take reproduces
    the prod runs' position (faces over the whole frame, captions in the top strip). Prod also
    shrank them to 34-38 px, from more face samples than this one-face fake gives."""
    variant, captured, calls = _talking(monkeypatch, landscape_fit="fit", switch=False)

    assert any(f["box"][1] < BAND[0] - 0.05 for f in captured["faces"])
    assert all(focus is None for focus in calls)
    _size, _x, y, _align, _colour = _look(variant)
    assert y < 0.25, y


# ----------------------------------------------------- unchanged edits (items 2, 3, 4, 5)


@pytest.mark.parametrize(
    ("landscape_fit", "landscape"),
    [("fit", False), ("fill", False), ("fill", True)],
    ids=["vertical take", "vertical take, fill", "sideways take that fills"],
)
def test_takes_that_fill_the_canvas_are_drawn_exactly_as_before(
    monkeypatch, landscape_fit, landscape
):
    """Items 2-5: a vertical take, and a sideways take with no bars (asked for full screen, a
    landscape canvas and a face-filled take all compile with "fill"), are untouched."""
    on, on_faces, on_calls = _talking(monkeypatch, landscape_fit=landscape_fit, landscape=landscape)
    off, off_faces, off_calls = _talking(
        monkeypatch, landscape_fit=landscape_fit, landscape=landscape, switch=False
    )
    assert _look(on) == _look(off)
    assert on_faces["faces"] == off_faces["faces"]
    assert on_calls == off_calls and all(focus is None for focus in on_calls)


# ---------------------------------------------- the environment itself (items 6-9, 11, 13)


def _environment(monkeypatch, bindings, *, fit=None, windows=None, take_face=T2_FACE):
    monkeypatch.setattr(settings, "talking_fit_caption_geometry_enabled", True)
    calls: list = []
    _simulate_io(monkeypatch, take_face, calls=calls)
    with styling.phone_text_policy_scope({"version": 1, "preferences": {}, "requested": {}}):
        policy = styling._POLICY.get()
        policy["_source_bindings"] = [b.model_dump(mode="json") for b in bindings]
        if fit is not None:
            styling.register_generation_speaker_fit(fit)
        if windows is not None:
            policy["_sample_windows"] = windows
        styling._ensure_generation_environment(PORTRAIT)
        return [f["box"] for f in policy["_face_boxes"]], calls, policy


def _window(media_id, *, scale, x=0, rotation=0, backdrop=False):
    return {
        "media_id": media_id,
        "source_start": 0.0,
        "source_duration": 10.0,
        "start_s": 0.0,
        "end_s": 10.0,
        "scale": scale,
        "x": x,
        "y": 0,
        "rotation": rotation,
        "backdrop": backdrop,
    }


def test_each_take_gets_the_scale_of_its_own_size(monkeypatch):
    """Item 6: two takes, one sideways and one vertical, as the compiler gives extra speakers their
    own `fit_transform`: only the sideways one is shrunk."""
    wide = _binding("wide", duration_s=10.0, width=1920, height=1080)
    tall = _binding("tall", duration_s=10.0, width=1080, height=1920)
    faces, _calls, _policy = _environment(monkeypatch, [wide, tall], fit="fit")

    assert len(faces) == 2
    wide_face, tall_face = faces
    assert BAND[0] - 0.02 <= wide_face[1] and wide_face[3] <= BAND[1] + 0.02
    # A vertical take fills a vertical canvas exactly: its face is where it is in the take.
    assert tall_face == pytest.approx(_padded(T2_FACE), abs=1e-9)


def test_a_rotated_file_is_judged_by_how_it_is_displayed(monkeypatch):
    """Item 7: 1080x1920 pixels flagged 90 degrees is a sideways take on screen."""
    rotated = _binding("rot", duration_s=10.0, width=1080, height=1920, orientation_degrees=90)
    faces, calls, _policy = _environment(monkeypatch, [rotated], fit="fit")

    assert faces and BAND[0] - 0.02 <= faces[0][1] and faces[0][3] <= BAND[1] + 0.02
    assert calls and calls[0] == {"x": 0.5, "y": 0.5, "zoom": FIT_SCALE}


def test_windows_the_compiler_registered_first_are_used_and_sampled_as_drawn(monkeypatch):
    """Item 8: with no early caller the compile's windows are there before sampling. Their scale
    is used (as today), and the letterboxed take's background is now sampled over black (item 9)
    instead of not at all."""
    wide = _binding("wide", duration_s=10.0, width=1920, height=1080)
    faces, calls, policy = _environment(
        monkeypatch, [wide], windows=[_window("wide", scale=FIT_SCALE)]
    )

    assert faces and BAND[0] - 0.02 <= faces[0][1] and faces[0][3] <= BAND[1] + 0.02
    assert calls and all(focus == {"x": 0.5, "y": 0.5, "zoom": FIT_SCALE} for focus in calls)
    assert policy["_background_frames"], "the drawn background was not kept"


@pytest.mark.parametrize(
    "window",
    [
        {"backdrop": True},
        {"rotation": 4},
        {"x": 120},
    ],
    ids=["backdrop behind it", "tilted", "shifted"],
)
def test_a_scaled_take_not_drawn_over_black_is_not_sampled(monkeypatch, window):
    """Item 9: a backdrop, a tilt or a shift means the take is not over black; keep skipping its
    background as today."""
    wide = _binding("wide", duration_s=10.0, width=1920, height=1080)
    _faces, calls, policy = _environment(
        monkeypatch, [wide], windows=[_window("wide", scale=FIT_SCALE, **window)]
    )
    assert calls == []
    assert policy["_background_frames"] == []


def test_registering_after_the_sample_changes_nothing(monkeypatch):
    """Item 11: a caller that sampled first wins; a late registration is ignored, never a crash."""
    wide = _binding("wide", duration_s=10.0, width=1920, height=1080)
    monkeypatch.setattr(settings, "talking_fit_caption_geometry_enabled", True)
    _simulate_io(monkeypatch, T2_FACE)
    with styling.phone_text_policy_scope({"version": 1, "preferences": {}, "requested": {}}):
        policy = styling._POLICY.get()
        policy["_source_bindings"] = [wide.model_dump(mode="json")]
        styling._ensure_generation_environment(PORTRAIT)
        before = [dict(f) for f in policy["_face_boxes"]]
        styling.register_generation_speaker_fit("fit")
        styling._ensure_generation_environment(PORTRAIT)
        assert "_speaker_fit" not in policy
        assert policy["_face_boxes"] == before


def test_a_face_at_the_side_of_a_letterboxed_take_is_kept_whole(monkeypatch):
    """Item 13: the whole width of a letterboxed take is on screen. A face outside the middle third
    a cover crop keeps must be mapped where it is drawn, not trimmed to the crop or dropped."""
    wide = _binding("wide", duration_s=10.0, width=1920, height=1080)
    faces, _calls, _policy = _environment(monkeypatch, [wide], fit="fit", take_face=SIDE_FACE)

    assert faces, "the side face was dropped"
    left, top, right, bottom = faces[0]
    # 16:9 into 9:16 with bars: the picture spans the full width, so x is unchanged.
    padded = _padded(SIDE_FACE)
    assert left == pytest.approx(padded[0], abs=1e-6)
    assert right == pytest.approx(padded[2], abs=1e-6)
    assert BAND[0] - 0.02 <= top and bottom <= BAND[1] + 0.02


def test_the_switch_off_leaves_the_environment_byte_identical(monkeypatch):
    """Item 10 at the environment: off, the recorded fit and the new mapping are both ignored."""
    wide = _binding("wide", duration_s=10.0, width=1920, height=1080)
    on_faces, _on_calls, _ = _environment(monkeypatch, [wide], fit="fit", take_face=SIDE_FACE)
    monkeypatch.setattr(settings, "talking_fit_caption_geometry_enabled", False)
    _simulate_io(monkeypatch, SIDE_FACE)
    with styling.phone_text_policy_scope({"version": 1, "preferences": {}, "requested": {}}):
        policy = styling._POLICY.get()
        policy["_source_bindings"] = [wide.model_dump(mode="json")]
        styling.register_generation_speaker_fit("fit")
        styling._ensure_generation_environment(PORTRAIT)
        off_faces = [f["box"] for f in policy["_face_boxes"]]
    assert off_faces == [], "today's cover crop drops a face outside the middle third"
    assert on_faces


# --------------------------------------------------- a tilted take (review follow-up, 14-21)
#
# The phone turns the whole cover-filled take clockwise about the canvas centre (after its scale,
# before its shift: `KriaMediaEngine` `Composition.transform`). Every window below is registered the
# way a writer's compile registers it (`register_generation_timeline`, real `TimelineClip`s).

TILT_DEG = 8.0
# Just outside the cover crop's middle third of a sideways take: still off the canvas after 8 deg.
OFF_CROP_FACE = (0.05, 0.25, 0.22, 0.55)
# Straddles the cover crop's left edge (x 0.342 of the take): only its right part is on screen.
EDGE_FACE = (0.30, 0.30, 0.40, 0.50)
# Small and central: stays whole on screen through every tilt below, so no clipping hides the math.
CENTRE_FACE = (0.48, 0.42, 0.52, 0.50)
CUES = [
    {"start_s": 0.0, "end_s": 2.0, "text": "Evet, tabii şundan bahsettik."},
    {"start_s": 2.2, "end_s": 5.0, "text": "Esneklik mütemadiyen sertlikten daha güçlü."},
    {"start_s": 5.2, "end_s": 8.0, "text": "Kilitli sistemler yıkılmayı zorlaştırıyor."},
]


def _tilted_clip(media_id, *, rotation=TILT_DEG, scale=1.0, x=0.0, y=0.0):
    from app.kria.recipes import MediaTransform, TimelineClip

    return TimelineClip(
        id="clip-0",
        source_asset_id=media_id,
        source_start=0.0,
        source_duration=10.0,
        timeline_start=0.0,
        rate=1.0,
        transform=MediaTransform(
            scale=scale, rotation_degrees=rotation, position_x=x, position_y=y
        ),
    )


def _tilted_faces(monkeypatch, binding, clip, take_face, *, switch=True):
    monkeypatch.setattr(settings, "talking_fit_caption_geometry_enabled", switch)
    _simulate_io(monkeypatch, take_face)
    with styling.phone_text_policy_scope({"version": 1, "preferences": {}, "requested": {}}):
        policy = styling._POLICY.get()
        policy["_source_bindings"] = [binding.model_dump(mode="json")]
        styling.register_generation_timeline([clip])
        styling._ensure_generation_environment(PORTRAIT)
        return [f["box"] for f in policy["_face_boxes"]]


def _wide():
    return _binding("wide", duration_s=10.0, width=1920, height=1080)


def test_a_face_the_tilt_leaves_off_the_picture_is_not_protected(monkeypatch):
    """Item 14 (Tadic): an off-crop face on a tilted, cover-filled sideways take is not drawn, so
    it must not be protected at all, let alone as the whole canvas."""
    faces = _tilted_faces(monkeypatch, _wide(), _tilted_clip("wide"), OFF_CROP_FACE)
    assert faces == []


def test_the_switch_off_keeps_todays_tilted_take_byte_identical(monkeypatch):
    """Item 20, and the control for the test above. Today (main) the cover crop drops the off-crop
    face before the tilt is looked at, and a face inside the crop is protected as the whole canvas.
    The first version of this PR mapped every face from the whole take first, so the off-crop face
    reached the whole-canvas branch: that is the regression Tadic found."""
    off_crop = _tilted_faces(
        monkeypatch, _wide(), _tilted_clip("wide"), OFF_CROP_FACE, switch=False
    )
    in_crop = _tilted_faces(monkeypatch, _wide(), _tilted_clip("wide"), T2_FACE, switch=False)
    assert off_crop == []
    assert in_crop == [(0.0, 0.0, 1.0, 1.0)]


def test_a_tilted_face_is_protected_where_the_turn_draws_it(monkeypatch):
    """Items 15 and 17: a vertical take fills a vertical canvas exactly, so a quarter turn has a
    known answer. Clockwise on screen, a face just above the centre swings to just right of it
    (counter-clockwise would put it on the left)."""
    tall = _binding("tall", duration_s=10.0, width=1080, height=1920)
    face = (0.40, 0.42, 0.60, 0.50)
    faces = _tilted_faces(monkeypatch, tall, _tilted_clip("tall", rotation=90.0), face)

    left, top, right, bottom = _padded(face)
    width, height = PORTRAIT.width, PORTRAIT.height
    # A clockwise quarter turn about the centre (y down): (dx, dy) -> (-dy, dx), in pixels.
    expected = (
        0.5 - (bottom - 0.5) * height / width,
        0.5 + (left - 0.5) * width / height,
        0.5 - (top - 0.5) * height / width,
        0.5 + (right - 0.5) * width / height,
    )
    assert len(faces) == 1
    assert faces[0] == pytest.approx(expected, abs=1e-6)
    # The face's centre sits above the canvas centre, so clockwise swings it to the right.
    assert (faces[0][0] + faces[0][2]) / 2 > 0.5, "turned the wrong way"


def test_a_face_half_off_the_tilted_picture_keeps_its_visible_part(monkeypatch):
    """Item 16: clipped to the canvas, not dropped and not widened."""
    faces = _tilted_faces(monkeypatch, _wide(), _tilted_clip("wide"), EDGE_FACE)
    assert len(faces) == 1
    left, top, right, bottom = faces[0]
    assert left == 0.0 and 0.0 < right < 0.5
    assert 0.0 < top < bottom < 1.0
    assert (right - left) * (bottom - top) < 0.25


@pytest.mark.parametrize(
    ("scale", "x", "y"),
    [(FIT_SCALE, 0.0, 0.0), (FIT_SCALE * 1.4, 0.0, 0.0), (1.0, 140.0, -90.0)],
    ids=["tilted letterbox", "tilted letterbox with a zoom", "tilted and shifted"],
)
def test_a_tilt_composes_with_the_scale_and_the_shift(monkeypatch, scale, x, y):
    """Item 18: scale about the centre, then the turn, then the shift. With no turn the same
    window must land exactly where the untilted mapping puts it; with the turn its centre moves
    by the turn of its offset from the canvas centre."""
    import math

    flat = _tilted_faces(
        monkeypatch,
        _wide(),
        _tilted_clip("wide", rotation=0.0, scale=scale, x=x, y=y),
        CENTRE_FACE,
    )
    turned = _tilted_faces(
        monkeypatch,
        _wide(),
        _tilted_clip("wide", rotation=30.0, scale=scale, x=x, y=y),
        CENTRE_FACE,
    )
    assert len(flat) == len(turned) == 1
    assert all(0.0 < v < 1.0 for v in (*flat[0], *turned[0])), (flat, turned)
    w, h = PORTRAIT.width, PORTRAIT.height
    # Undo the shift to get the centre's offset from the canvas centre, in pixels, before the turn.
    cx = ((flat[0][0] + flat[0][2]) / 2 - 0.5) * w - x
    cy = ((flat[0][1] + flat[0][3]) / 2 - 0.5) * h - y
    angle = math.radians(30.0)
    want = (
        0.5 + (cx * math.cos(angle) - cy * math.sin(angle) + x) / w,
        0.5 + (cx * math.sin(angle) + cy * math.cos(angle) + y) / h,
    )
    got = ((turned[0][0] + turned[0][2]) / 2, (turned[0][1] + turned[0][3]) / 2)
    assert got == pytest.approx(want, abs=1e-6)


def _caption_and_title(monkeypatch, take_face, *, switch=True):
    """The caption look and the title a writer places on a tilted, cover-filled sideways take:
    the REAL environment, caption placement and title fit (`generation_caption_look`,
    `generation_style_rows`), with the caption block reserved for the title as the compile does."""
    from app.pipeline.phone_text_style import measure_box

    monkeypatch.setattr(settings, "talking_fit_caption_geometry_enabled", switch)
    _simulate_io(monkeypatch, take_face)
    policy = {
        "version": 1,
        "preferences": {},
        "requested": {},
        "selected_fonts": {"caption": "Inter", "title": "Inter"},
        "selection_roles": ["caption", "title"],
        "selection_calls": 2,
    }
    with styling.phone_text_policy_scope(policy):
        scoped = styling._POLICY.get()
        scoped["_source_bindings"] = [_wide().model_dump(mode="json")]
        styling.register_generation_timeline([_tilted_clip("wide")])
        look = styling.generation_caption_look(CUES, canvas=PORTRAIT)
        block = None
        for cue in CUES:
            row = {
                "id": "caption-measure",
                "text": cue["text"],
                "start_s": 0.0,
                "end_s": 8.0,
                "position": "custom",
                "font_family": look.font_family,
                "size_px": look.text_size_px,
                "x_frac": look.position_x_frac,
                "y_frac": look.position_y_frac,
                "alignment": look.text_anchor,
                "max_width_frac": look.max_width_frac,
                "stroke_width": look.outline_px,
                "shadow_enabled": look.shadow_enabled,
                "effect": "static",
            }
            box = measure_box(row, PORTRAIT)
            block = (
                box
                if block is None
                else (
                    min(block[0], box[0]),
                    min(block[1], box[1]),
                    max(block[2], box[2]),
                    max(block[3], box[3]),
                )
            )
        title = {
            "id": "title-hook",
            "text": "Cusco'da bir gün",
            "start_s": 0.0,
            "end_s": 3.0,
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.16,
            "font_family": "Inter",
            "size_px": 76,
            "color": "#FFFFFF",
            "stroke_width": 0,
            "shadow_enabled": False,
            "alignment": "center",
            "max_width_frac": 0.8,
            "effect": "static",
        }
        [placed] = styling.generation_style_rows(
            [title],
            canvas=PORTRAIT,
            excluded_boxes=[{"start_s": 0.0, "end_s": 8.0, "box": block, "kind": "caption"}],
        )
        return look, block, placed, measure_box(placed, PORTRAIT)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_an_off_picture_face_on_a_tilted_take_changes_neither_captions_nor_title(monkeypatch):
    """Item 19 (Tadic): readable captions that do not crowd the title. The invisible face must
    leave both exactly as on the same take with nobody in it."""
    look, block, title, title_box = _caption_and_title(monkeypatch, OFF_CROP_FACE)
    bare_look, bare_block, bare_title, _ = _caption_and_title(monkeypatch, None)

    assert look == bare_look
    assert block == bare_block
    assert title == bare_title
    assert look.text_size_px >= 48, f"captions shrank to {look.text_size_px} px"
    assert not _overlap(block, title_box), (block, title_box)


def test_a_visible_face_on_a_tilted_take_is_dodged_not_the_whole_frame(monkeypatch):
    """Items 15 and 19: a face the tilt keeps on screen is avoided where it is drawn. Today (switch
    off) the same face is the whole canvas, so the captions are fitted around a frame "full of
    face"; on, they stay readable, off the turned face and clear of the title. (A face that really
    fills the frame, like the cover-filled T2 close-up, leaves no clear spot tilted or not: a named
    gap.)"""
    look, block, _title, title_box = _caption_and_title(monkeypatch, CENTRE_FACE)
    off_look, *_ = _caption_and_title(monkeypatch, CENTRE_FACE, switch=False)
    faces = _tilted_faces(monkeypatch, _wide(), _tilted_clip("wide"), CENTRE_FACE)

    assert faces and faces[0] != (0.0, 0.0, 1.0, 1.0)
    assert look != off_look
    assert look.text_size_px >= 48, f"captions shrank to {look.text_size_px} px"
    assert not _overlap(block, title_box), (block, title_box)
    assert not _overlap(block, faces[0]), (block, faces[0])
