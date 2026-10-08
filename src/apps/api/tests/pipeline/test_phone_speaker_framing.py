"""KRI-547: face-filled vertical crop for a sideways phone Talking speaker.

The window math (`face_fill_window`), the decision over a sampled clip
(`decide_speaker_framing`, with an injected face sampler), the receipt, the
card-geometry mapper and the editor Save rule.
"""

from __future__ import annotations

import pytest

import app.pipeline.phone_speaker_framing as framing_mod
from app.kria.recipes import Canvas, MediaTransform
from app.pipeline.phone_recipe_shared import face_fill_transform, max_cover_shift_px
from app.pipeline.phone_speaker_framing import (
    FACE_BOTTOM_LIMIT_FRAC,
    decide_speaker_framing,
    editor_speaker_framing,
    face_box_mapper,
    face_fill_window,
    sample_anchors,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion

CANVAS = Canvas(width=1080, height=1920)
# 1920x1080 cover-filled into 1080x1920: 3413.33 px wide, 1080 of it shows.
SHOWN_W = 1920 * 1920 / 1080
WINDOW = 1080 / SHOWN_W  # 0.31640625 of the source width
MAX_SHIFT = (SHOWN_W - 1080) / 2


def _box(left: float, right: float, top: float = 0.15, bottom: float = 0.6) -> NormalizedBox:
    return NormalizedBox(left, top, right, bottom)


def _window(boxes, width=1920, height=1080):
    return face_fill_window(boxes, display_width=width, display_height=height, canvas=CANVAS)


# ── window math ─────────────────────────────────────────────────────────────


def test_a_face_in_the_left_third_gets_a_window_centred_on_it():
    # The T3 Kadıköy speaker: Haar boxes ~0.25 wide drifting between 0.19 and 0.51.
    window = _window([_box(0.19, 0.44), _box(0.25, 0.50), _box(0.22, 0.47)])
    assert window is not None and window.fits
    centre = (0.19 + 0.50) / 2
    assert window.left == pytest.approx(centre - WINDOW / 2, abs=1e-4)
    assert window.right - window.left == pytest.approx(WINDOW, abs=1e-4)
    # The engine draws source x at 540 + (x - 0.5) * 3413.33 + position_x.
    assert window.position_x == pytest.approx((0.5 - centre) * SHOWN_W, abs=0.01)
    assert window.position_x > 0  # the picture slides right to bring the left third in
    assert window.worst_visible == 1.0
    assert window.margin_px == pytest.approx(((WINDOW - (0.50 - 0.19)) / 2) * SHOWN_W, abs=0.2)


def test_a_face_at_the_frame_edge_clamps_the_window_inside_the_source():
    left = _window([_box(0.0, 0.18)])
    assert left.left == 0.0 and left.fits
    assert left.position_x == pytest.approx(MAX_SHIFT, abs=0.01)
    right = _window([_box(0.85, 1.0)])
    assert right.right == pytest.approx(1.0) and right.fits
    assert right.position_x == pytest.approx(-MAX_SHIFT, abs=0.01)


def test_a_face_that_moves_across_more_than_one_window_does_not_fit():
    window = _window([_box(0.10, 0.30), _box(0.45, 0.65)])
    assert window is not None
    assert not window.fits
    assert window.worst_visible < 0.95
    assert window.margin_px < 0


def test_a_face_wider_than_the_window_does_not_fit():
    window = _window([_box(0.2, 0.6)])
    assert not window.fits
    assert window.worst_visible == pytest.approx(WINDOW / 0.4, abs=1e-3)


def test_a_slight_overhang_inside_the_box_margin_still_fits():
    # Union 0.33 wide vs a 0.316 window: each extreme box loses ~3% of its width.
    window = _window([_box(0.18, 0.43), _box(0.26, 0.51)])
    assert window.fits
    assert 0.95 <= window.worst_visible < 1.0
    assert window.margin_px < 0


def test_no_usable_box_or_size_is_no_window():
    assert _window([]) is None
    assert _window([NormalizedBox(0.3, 0.2, 0.3, 0.5)]) is None
    assert _window([_box(0.2, 0.4)], width=0) is None


def test_face_fill_transform_clamps_and_is_identity_without_overflow():
    assert face_fill_transform(1920, 1080, CANVAS, 508.44) == MediaTransform(position_x=508.44)
    clamped = face_fill_transform(1920, 1080, CANVAS, 99999)
    assert clamped.position_x <= MAX_SHIFT
    assert clamped.position_x == pytest.approx(MAX_SHIFT, abs=0.01)
    assert face_fill_transform(1920, 1080, CANVAS, 0.0) == MediaTransform()
    # Portrait 9:16 on the story canvas has nothing to slide.
    assert max_cover_shift_px(1080, 1920, CANVAS) == 0
    assert face_fill_transform(1080, 1920, CANVAS, 300.0) == MediaTransform()


# ── anchors ─────────────────────────────────────────────────────────────────


def test_anchors_cover_the_whole_take_once_per_1_25_s_capped():
    anchors = sample_anchors([(0.0, 33.6)])
    assert len(anchors) == 24
    assert anchors[0] < 1.0 and anchors[-1] > 32.5
    assert len(sample_anchors([(0.0, 4.0)])) == 6
    assert sample_anchors([(0.0, 0.0)]) == []


def test_anchors_skip_what_the_cleanup_cut_removed():
    anchors = sample_anchors([(0.0, 3.0), (7.0, 10.0)])
    assert anchors
    assert not any(3.0 < at < 7.0 for at in anchors)


# ── decision ────────────────────────────────────────────────────────────────


def _sampler(boxes_by_anchor=None, *, receipt=None, raises=None, calls=None):
    def sample(path, anchors, **kwargs):
        if calls is not None:
            calls.append((path, list(anchors), kwargs))
        if raises is not None:
            raise raises
        regions = []
        for index, at_s in enumerate(anchors):
            box = boxes_by_anchor(index, at_s) if boxes_by_anchor else None
            if box is not None:
                regions.append(ProtectedRegion(at_s - 0.5, at_s + 0.5, box, "face"))
        out = {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}
        out.update(receipt or {})
        return regions, out

    return sample


def _decide(sample, *, fit="fit", width=1920, height=1080, keep=((0.0, 30.0),), clip="/tmp/c.mp4"):
    return decide_speaker_framing(
        clip,
        keep_segments=list(keep),
        display_width=width,
        display_height=height,
        canvas=CANVAS,
        landscape_fit=fit,
        asked_by=["r1"],
        sample=sample,
    )


def _steady(index, at_s):
    shift = 0.03 if index % 2 else 0.0
    return _box(0.2 + shift, 0.45 + shift)


def test_a_steady_face_is_face_filled_from_raw_boxes():
    calls = []
    framing = _decide(_sampler(_steady, calls=calls))
    assert framing.mode == "face_fill"
    assert framing.reason == "face_in_window"
    assert framing.eligible
    assert framing.position_x == framing.window.position_x > 0
    [(path, anchors, kwargs)] = calls
    assert path == "/tmp/c.mp4" and len(anchors) == 24
    assert kwargs["raw_boxes"] is True and kwargs["count_decoded"] is True
    receipt = framing.receipt()
    assert receipt["version"] == 1
    assert receipt["mode"] == "face_fill" and receipt["asked_by"] == ["r1"]
    assert receipt["window"]["fits"] is True
    assert receipt["faces"]["used"] == 24 and receipt["faces"]["face_sampling"] == "ok"


def test_a_wandering_face_keeps_the_letterbox_and_says_why():
    def wander(index, at_s):
        return _box(0.05, 0.3) if index < 12 else _box(0.55, 0.8)

    framing = _decide(_sampler(wander))
    assert (framing.mode, framing.reason) == ("letterbox", "face_moves_too_much")
    assert framing.position_x is None
    assert not framing.eligible
    assert framing.receipt()["window"]["fits"] is False


def test_with_crop_chosen_the_fallback_is_the_centre_crop():
    framing = _decide(_sampler(lambda i, t: None), fit="fill")
    assert (framing.mode, framing.reason) == ("centre_fill", "no_face")


def test_too_few_detections_is_no_face():
    framing = _decide(_sampler(lambda i, t: _box(0.2, 0.45) if i < 10 else None))
    assert (framing.mode, framing.reason) == ("letterbox", "no_face")
    assert framing.receipt()["faces"]["used"] == 10


@pytest.mark.parametrize(
    "sample",
    [
        _sampler(raises=RuntimeError("cv2 missing")),
        _sampler(_steady, receipt={"worker_error": "rc_1:no cascade"}),
        _sampler(_steady, receipt={"timed_out": True, "partial": True}),
        _sampler(_steady, receipt={"decoded": 5}),
    ],
)
def test_an_unconfirmed_sampler_never_guesses_a_crop(sample):
    framing = _decide(sample)
    assert (framing.mode, framing.reason) == ("letterbox", "face_unconfirmed")
    assert framing.position_x is None


def test_no_clip_is_unconfirmed_without_sampling():
    calls = []
    framing = _decide(_sampler(_steady, calls=calls), clip=None)
    assert framing.reason == "face_unconfirmed" and calls == []


def test_a_face_reaching_the_caption_block_keeps_the_letterbox():
    framing = _decide(_sampler(lambda i, t: _box(0.2, 0.45, top=0.3, bottom=0.8)))
    assert (framing.mode, framing.reason) == ("letterbox", "face_under_captions")
    assert framing.receipt()["faces"]["bottom_on_canvas"] > FACE_BOTTOM_LIMIT_FRAC
    assert framing.receipt()["window"]["fits"] is True
    assert not framing.eligible


def test_a_giant_background_merge_is_ignored_as_an_outlier():
    def merge(index, at_s):
        return _box(0.05, 0.75) if index == 3 else _box(0.2, 0.45)

    framing = _decide(_sampler(merge))
    assert framing.mode == "face_fill"
    assert framing.receipt()["faces"]["ignored"] == 1


@pytest.mark.parametrize("dims", [(1080, 1920), (1080, 1080), (1080, 1350)])
def test_portrait_and_square_clips_are_a_no_op_without_sampling(dims):
    calls = []
    framing = _decide(_sampler(_steady, calls=calls), width=dims[0], height=dims[1])
    assert (framing.mode, framing.reason) == ("centre_fill", "not_landscape")
    assert framing.position_x is None and calls == []


def test_the_default_sampler_is_looked_up_per_call(monkeypatch):
    calls = []
    monkeypatch.setattr(framing_mod, "sample_face_regions", _sampler(_steady, calls=calls))
    framing = decide_speaker_framing(
        "/tmp/c.mp4",
        keep_segments=[(0.0, 10.0)],
        display_width=1920,
        display_height=1080,
        canvas=CANVAS,
        landscape_fit="fit",
    )
    assert framing.mode == "face_fill" and calls
    assert "asked_by" not in framing.receipt()


# ── downstream geometry ─────────────────────────────────────────────────────


def test_the_card_mapper_puts_the_face_where_the_crop_draws_it():
    window = _window([_box(0.2, 0.45)])
    mapped = face_box_mapper(
        display_width=1920, display_height=1080, canvas=CANVAS, position_x=window.position_x
    )(_box(0.2, 0.45, top=0.15, bottom=0.6))
    # The face spans most of the crop, centred; padded in canvas units afterwards.
    raw_left = 0.5 + (0.2 - 0.5) * SHOWN_W / 1080 + window.position_x / 1080
    raw_right = 0.5 + (0.45 - 0.5) * SHOWN_W / 1080 + window.position_x / 1080
    assert mapped.left == pytest.approx(max(0.0, raw_left - 0.06), abs=1e-6)
    assert mapped.right == pytest.approx(min(1.0, raw_right + 0.06), abs=1e-6)
    assert mapped.top == pytest.approx(0.13, abs=1e-6)
    assert mapped.bottom == pytest.approx(0.68, abs=1e-6)
    # Unlike the source-frame protection box, a wide face on the zoomed crop is
    # not narrowed to a 0.55-wide "background merge".
    assert mapped.width > 0.55


def test_the_card_mapper_drops_a_face_outside_the_crop():
    mapper = face_box_mapper(display_width=1920, display_height=1080, canvas=CANVAS, position_x=0.0)
    assert mapper(_box(0.0, 0.1)) is None


# ── editor Save ─────────────────────────────────────────────────────────────


def _receipt(mode="face_fill", reason="face_in_window", eligible=True, position=508.44):
    return {
        "version": 1,
        "mode": mode,
        "reason": reason,
        "eligible": eligible,
        "window": {"left": 0.19, "right": 0.51, "position_x": position, "fits": True},
    }


def test_editor_without_a_framing_receipt_is_unchanged():
    assert editor_speaker_framing(None, landscape_fit="fill") == (None, None)
    assert editor_speaker_framing({"mode": "weird"}, landscape_fit="fit") == (None, None)


def test_editor_save_keeps_the_face_crop():
    position, receipt = editor_speaker_framing(_receipt(), landscape_fit="fill")
    assert position == 508.44
    assert receipt == _receipt()


def test_editor_bars_drop_the_crop_and_crop_brings_it_back():
    position, bars = editor_speaker_framing(_receipt(), landscape_fit="fit")
    assert position is None
    assert (bars["mode"], bars["reason"]) == ("letterbox", "creator_chose_bars")
    assert bars["window"]["position_x"] == 508.44
    position, back = editor_speaker_framing(bars, landscape_fit="fill")
    assert position == 508.44
    assert (back["mode"], back["reason"]) == ("face_fill", "face_in_window")


def test_editor_crop_over_a_fallback_letterbox_is_the_centre_crop():
    fallback = _receipt(mode="letterbox", reason="face_moves_too_much", eligible=False)
    position, receipt = editor_speaker_framing(fallback, landscape_fit="fill")
    assert position is None
    assert (receipt["mode"], receipt["reason"]) == ("centre_fill", "creator_chose_crop")
    position, receipt = editor_speaker_framing(fallback, landscape_fit="fit")
    assert position is None and receipt == fallback


def test_editor_never_touches_a_portrait_receipt():
    portrait = {"version": 1, "mode": "centre_fill", "reason": "not_landscape", "eligible": False}
    for fit in ("fit", "fill"):
        assert editor_speaker_framing(portrait, landscape_fit=fit) == (None, portrait)


def test_a_speaker_who_leans_out_still_has_to_fit_the_window():
    # Four frames lean far right: same size as the speaker, so still the speaker
    # (not dropped as "another cluster"), and no single window holds both.
    def lean(index, at_s):
        return _box(0.45, 0.7) if index in (5, 6, 7, 8) else _box(0.2, 0.45)

    framing = _decide(_sampler(lean))
    assert (framing.mode, framing.reason) == ("letterbox", "face_moves_too_much")
    assert framing.receipt()["faces"]["used"] == 24


def test_a_small_background_face_on_a_missed_frame_is_ignored():
    def background(index, at_s):
        return _box(0.85, 0.89) if index == 2 else _box(0.2, 0.45)

    framing = _decide(_sampler(background))
    assert framing.mode == "face_fill"
    assert framing.receipt()["faces"]["ignored"] == 1
