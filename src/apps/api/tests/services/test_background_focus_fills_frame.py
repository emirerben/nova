"""KRI-1307: an automatically aimed screen recording fills the whole 9:16 frame.

Qendresa Q2 run 2 (job 1d512b9c): "put my screen recordings full screen ... zoom into the
recordings so the text is readable". The item-18 focus zoomed each 1920x1080 README recording
OUT below the cover fit (0.70-0.87x) so the whole text column fit the width; the phone draws
``max(cover * zoom, contain)``, so every recording showed 130-286 px black bars above and below
(cropdetect on the device MP4 at 12/20/27/34 s).

Ways this can fail, each checked below on the engine's own geometry (`_drawn`, the
`VisualMediaPlacement` rule: base cover, drawn scale ``max(cover * zoom, contain)``, focal x/y
aligns the picture):

* F1 the auto zoom goes below cover: a black band above/below the recording (the bug).
* F2 filling the frame crops the column so its lines start off screen: the view must start at
  the column's left edge (lines read from their first word).
* F3 the text gets too small to read: a text line stays >= ~40 px of the 1920 px canvas,
  unless the 2.5x cap is reached.
* F4 a picture that already fits the canvas (a portrait screenshot) gets an aim: still none.
* F5 a creator's own zoom (the lane's, below 1x) is overridden: only the measured part is
  floored; the lane's zoom still wins (`_FocusResolver`).
"""

from __future__ import annotations

import pytest

from app.services.background_text_focus import (
    MAX_AUTO_ZOOM,
    TARGET_LINE_PITCH_PX,
    TextColumn,
    focus_for_text_column,
)

CANVAS_W, CANVAS_H = 1080, 1920

# Her five Q2 recordings (1920x1080), measured with `measure_text_column` on the kit files
# ("Screen Recording 1..5"), plus the trending page: (left, right, line_pitch of height).
HER_RECORDINGS = {
    "spec-kit": (0.2002, 0.6006, 0.03333),
    "impeccable": (0.1960, 0.6350, 0.02407),
    "tradingagents": (0.1981, 0.6257, 0.02963),
    "agents": (0.1971, 0.6194, 0.02778),
    "game-studios": (0.2002, 0.5829, 0.02593),
    "trending": (0.2492, 0.4546, 0.02593),
}


def _drawn(focus, source_w: int, source_h: int) -> dict[str, float]:
    """Where the phone draws the picture on the canvas (KriaMediaEngine VisualMediaPlacement)."""
    cover = max(CANVAS_W / source_w, CANVAS_H / source_h)
    contain = min(CANVAS_W / source_w, CANVAS_H / source_h)
    zoom = focus.zoom if focus is not None else 1.0
    fx = focus.x if focus is not None else 0.5
    fy = focus.y if focus is not None else 0.5
    scale = max(cover * zoom, contain)
    w, h = source_w * scale, source_h * scale
    left = (CANVAS_W - w) * fx
    top = (CANVAS_H - h) * fy
    return {
        "scale": scale,
        "left": left,
        "top": top,
        "right": left + w,
        "bottom": top + h,
        # The part of the source width in view, as fractions of the source width.
        "view_left": -left / w,
        "view_right": (CANVAS_W - left) / w,
    }


@pytest.mark.parametrize("name", sorted(HER_RECORDINGS))
def test_her_recordings_fill_the_frame_and_read_from_the_line_start(name):
    left, right, pitch = HER_RECORDINGS[name]
    column = TextColumn(left=left, right=right, line_pitch=pitch)
    focus = focus_for_text_column(column, source_w=1920, source_h=1080)
    drawn = _drawn(focus, 1920, 1080)
    # F1: no black band anywhere (allow half a pixel of rounding).
    assert drawn["top"] <= 0.5 and drawn["bottom"] >= CANVAS_H - 0.5, drawn
    assert drawn["left"] <= 0.5 and drawn["right"] >= CANVAS_W - 0.5, drawn
    # F2: the column's first word is in view (its left edge, less the 2 % pad, at most).
    assert drawn["view_left"] <= left + 1e-3 <= drawn["view_right"], drawn
    # F3: a text line is at least the target size on the canvas, or the cap is reached.
    line_px = pitch * 1080 * drawn["scale"]
    assert line_px >= TARGET_LINE_PITCH_PX - 0.5 or focus.zoom == pytest.approx(MAX_AUTO_ZOOM)


@pytest.mark.parametrize(
    "size,column",
    [
        ((1920, 1080), (0.18, 0.60, 26 / 1080)),  # GitHub 1x
        ((3024, 1964), (0.09, 0.67, 52 / 1964)),  # retina
        ((2560, 1080), (0.04, 0.47, 0.02)),  # README left, terminal right
        ((4000, 1000), (0.30, 0.35, 0.005)),  # narrow column, tiny text: capped
    ],
)
def test_no_measured_focus_leaves_a_bar(size, column):
    left, right, pitch = column
    focus = focus_for_text_column(
        TextColumn(left=left, right=right, line_pitch=pitch), source_w=size[0], source_h=size[1]
    )
    drawn = _drawn(focus, *size)
    assert drawn["top"] <= 0.5 and drawn["bottom"] >= CANVAS_H - 0.5, (focus, drawn)
    assert focus is None or focus.zoom <= MAX_AUTO_ZOOM


def test_a_picture_that_fits_gets_no_aim():
    # F4
    column = TextColumn(left=0.1, right=0.9, line_pitch=0.02)
    assert focus_for_text_column(column, source_w=1080, source_h=1920) is None
    assert focus_for_text_column(column, source_w=1080, source_h=2340) is None


def test_a_creators_own_zoom_out_still_wins():
    # F5: the floor is on the measured zoom only; the lane's zoom is the creator's.
    from app.pipeline.canvas import Canvas
    from app.pipeline.phone_subtitled_lanes import SubtitledBackgroundFocus
    from app.pipeline.phone_subtitled_plan import _FocusResolver

    class _Visual:
        media_id = "rec"
        text_column = TextColumn(
            left=0.2, right=0.6, line_pitch=0.03, source_width=1920, source_height=1080
        )

    resolver = _FocusResolver((_Visual(),), Canvas(width=CANVAS_W, height=CANVAS_H), open_=True)
    auto = resolver("rec", None)
    assert auto is not None and auto.zoom >= 1.0
    asked = resolver("rec", SubtitledBackgroundFocus(zoom=0.6))
    assert asked is not None and asked.zoom == pytest.approx(0.6)
