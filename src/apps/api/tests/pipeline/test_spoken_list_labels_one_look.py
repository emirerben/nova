"""KRI-1284: a numbered list written as spoken-word texts keeps ONE readable look and ONE spot.

Qendresa Q2 run 2 (job 1d512b9c): "Add a label when each one starts: "1. Spec-kit", ...,
"5. Claude code game studio"". The brief gave each label its spoken name, so the labels rode the
spoken-text lane (KRI-707), not the list-label lane (item 19). There:

* each row was placed on its own: rows 1-4 at y 0.15, row 5 (two lines) moved to y 0.7166
  because the face was checked full-frame while she was drawn small in the bottom-right corner;
* the phone text restyle then gave every row the auto look -- Unbounded, mid-grey ``#767676``
  fill picked against the README page, no outline -- and its own size (104/84/67/104/58 px).
  Over white GitHub pages "1. Spec-kit" and "4. Agents" were faint and the kit OCR never found
  "5. Claude code game studio".

Ways this can fail (each asserted below, on the real take's words and the real placer):

* F1 the list rows get no outline / a grey auto fill: unreadable on a light page.
* F2 one row lands somewhere else (per-row placement), or at another size.
* F3 the shared spot covers her face or the captions in some row's window.
* F4 the face is checked full-frame while she is drawn small in a corner, pushing a row down.
* F5 a long label is cut or overflows: the whole text, inside the label band.
* F6 the restyle (`_protected_style_rows`) takes the look / spot away again.
* F7 a plain spoken text (not a numbered list) changes look: it must not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.kria.spoken_text_ask import SpokenTextAsk
from app.pipeline.canvas import Canvas
from app.pipeline.phone_list_labels import (
    LABEL_COLOR,
    LABEL_FONT_FAMILY,
    LABEL_STROKE_COLOR,
    LABEL_STROKE_WIDTH,
)
from app.pipeline.phone_spoken_text import share_list_spot, spoken_text_rows
from app.pipeline.phone_subtitled_title import _measure_title_box, place_talking_title
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion

CANVAS = Canvas(width=1080, height=1920)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1284_q2_run2_cues.json"
# Her face in the take (720x1280, same aspect as the canvas), from the run's trace face band.
FACE = NormalizedBox(0.207, 0.211, 0.845, 0.6175)
CORNER = SPEAKER_FRAME_PRESETS["small_bottom_right"]
FRAME = (CORNER["width_fraction"], CORNER["x_fraction"], CORNER["y_fraction"])
FULL_SCREEN_OPENING = ((0.0, 5.55),)

LABELS = [
    ("r4", "1. Spec-kit", "Spec Kit"),
    ("r5", "2. Impeccable", "Impeccable"),
    ("r6", "3. TradingAgents", "Trading Agents"),
    ("r7", "4. Agents", "The Agents Marketplace"),
    ("r8", "5. Claude code game studio", "Cloud Code Game Studios"),
]


def _cues() -> list[dict]:
    return json.loads(FIXTURE.read_text())["caption_cues"]


def _words() -> list[dict]:
    return [word for cue in _cues() for word in cue.get("words") or []]


def _sample(clip_path, anchors, **_kwargs):
    return [ProtectedRegion(a, a + 0.1, FACE, "face") for a in anchors], {"decoded": len(anchors)}


def _placer(*, speaker_frame=None, full_screen_windows=()):
    def place(row, avoid):
        return place_talking_title(
            row,
            clip_path="take.mp4",
            keep_segments=[(0.0, 40.431)],
            display_width=720,
            display_height=1280,
            canvas=CANVAS,
            sample=_sample,
            avoid=avoid,
            speaker_frame=speaker_frame,
            full_screen_windows=full_screen_windows,
        )

    return place


def _asks(labels=LABELS) -> list[SpokenTextAsk]:
    return [
        SpokenTextAsk(requirement_id=rid, text=text, triggers=(trigger,))
        for rid, text, trigger in labels
    ]


def _rows(place):
    return spoken_text_rows(
        _asks(),
        words=_words(),
        cues=_cues(),
        caption_style="word",
        timeline_duration_s=40.431,
        canvas=CANVAS,
        place=place,
    )


def _assert_list_look(rows):
    for row in rows:
        assert row["font_family"] == LABEL_FONT_FAMILY, row
        assert row["color"] == LABEL_COLOR, row
        assert row["stroke_color"] == LABEL_STROKE_COLOR, row
        assert float(row["stroke_width"]) == LABEL_STROKE_WIDTH, row
        assert not row.get("shadow_enabled"), row


def test_the_list_is_one_look_and_one_spot_before_the_corner_is_known():
    rows, receipt = _rows(_placer())
    assert [row["text"] for row in rows] == [text for _rid, text, _t in LABELS]  # F5: whole
    _assert_list_look(rows)  # F1
    assert len({row["y_frac"] for row in rows}) == 1, [row["y_frac"] for row in rows]  # F2
    assert len({row["size_px"] for row in rows}) == 1, [row["size_px"] for row in rows]  # F2
    for entry in receipt["rows"]:
        assert entry["status"] == "placed"
        assert entry["face_collision"] is False  # F3
        assert entry["placement"].get("text_overlap") is not True  # F3
        assert entry["list_spot"]["status"] == "shared"
    for row in rows:  # F5: inside the label band (width <= 0.9, about two lines)
        box = _measure_title_box(row, canvas=CANVAS)
        assert box.right - box.left <= 0.9 + 1e-6 and box.bottom - box.top <= 0.14 + 1e-6


def test_with_her_in_the_corner_the_list_takes_its_preset_spot():
    """F4: once the lanes say she is small in the bottom-right corner (full screen only in the
    opening), the list is laid out again against the face where it is drawn: every label at the
    preset top spot, none pushed under a full-frame face that isn't there."""
    rows, receipt = _rows(_placer())
    rows, receipt = share_list_spot(
        rows,
        receipt,
        place=_placer(speaker_frame=FRAME, full_screen_windows=FULL_SCREEN_OPENING),
        cues=_cues(),
        caption_style="word",
        canvas=CANVAS,
    )
    ys = {row["y_frac"] for row in rows}
    assert ys == {0.15}, ys
    _assert_list_look(rows)
    assert all(e["list_spot"]["status"] == "shared" for e in receipt["rows"])


def test_the_restyle_keeps_the_list_look_and_spot():
    """F6: the phone text restyle (her policy from the job, a white README page sampled behind
    the text) leaves the list rows exactly as placed."""
    from app.services.phone_text_styling import phone_text_policy_scope
    from app.tasks.generative_build import _protected_style_rows

    rows, _receipt = _rows(_placer(speaker_frame=FRAME, full_screen_windows=FULL_SCREEN_OPENING))
    # README: white page, dark text every other band of rows (what made the auto fill grey).
    pixels = bytes(255 if (i // 108) % 6 else 30 for i in range(108 * 192))
    policy = {
        "version": 1,
        "requested": {},
        "caption_ask": {"box": "#E5E5E5", "position": "bottom"},
        "preferences": {"all_text": {}},
        "selected_fonts": {"title": "Unbounded"},
        "selection_calls": 2,
        "selection_roles": ["title"],
        "_environment_sampled": True,
        "_background_frames": [{"start_s": 0.0, "end_s": 40.431, "pixels": pixels}],
    }
    with phone_text_policy_scope(policy):
        styled = _protected_style_rows(rows, job_id="kri1284", protected=None, canvas=CANVAS)
    for before, after in zip(rows, styled, strict=True):
        for key in ("font_family", "color", "stroke_color", "stroke_width", "y_frac", "x_frac"):
            assert after.get(key) == before.get(key), (key, before, after)
        assert after["size_px"] == before["size_px"]


@pytest.mark.parametrize(
    "labels",
    [
        [("r1", "ATAKAN", "Atakan")],
        [("r1", "Spec Kit", "Spec Kit"), ("r2", "Impeccable", "Impeccable")],
        [("r1", "1. Spec-kit", "Spec Kit")],  # one numbered text alone is not a list
    ],
)
def test_a_text_that_is_not_a_list_keeps_todays_look(labels):
    """F7"""
    rows, receipt = spoken_text_rows(
        _asks(labels),
        words=_words() + [{"text": "Atakan", "start_s": 36.0, "end_s": 36.4}],
        cues=_cues(),
        caption_style="word",
        timeline_duration_s=40.431,
        canvas=CANVAS,
        place=_placer(),
    )
    assert rows
    for row in rows:
        assert "stroke_width" not in row and row["font_family"] != LABEL_FONT_FAMILY
        assert "list_label_look" not in row["source_params"]
    assert all("list_spot" not in entry for entry in receipt["rows"])


def test_the_worker_lays_the_list_out_again_on_the_final_lanes(monkeypatch):
    """F4 through the worker's own wiring: `_phone_talking_spoken_text_rows` places the rows
    before the lanes exist; `_phone_spoken_list_on_lanes` lays the list out again with the corner
    frame and the full-screen opening the lanes carry. Inside each pass the face sampler runs
    once per window (trying the shared spot reuses what the first placement saw)."""
    from types import SimpleNamespace

    from app.pipeline import phone_subtitled_title as title_mod
    from app.pipeline.phone_subtitled_lanes import SubtitledFullScreenWindow, SubtitledSpeakerFrame
    from app.tasks.generative_build import (
        _phone_spoken_list_on_lanes,
        _phone_talking_spoken_text_rows,
    )

    calls: list[tuple] = []

    def sample(clip_path, anchors, **kwargs):
        calls.append(tuple(anchors))
        return _sample(clip_path, anchors, **kwargs)

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)
    binding = SimpleNamespace(
        original=SimpleNamespace(width=720, height=1280, orientation_degrees=0)
    )
    common = dict(
        binding=binding,
        clip_path="take.mp4",
        keep_segments=[(0.0, 40.431)],
        landscape_fit="fill",
        canvas=CANVAS,
    )
    rows, receipt = _phone_talking_spoken_text_rows(
        _asks(),
        words=_words(),
        cues=_cues(),
        caption_style="word",
        shown_rows=[],
        timeline_duration_s=40.431,
        **common,
    )
    assert len(calls) == len(set(calls)), "a window was sampled twice while placing"
    calls.clear()
    lanes = SimpleNamespace(
        background=SimpleNamespace(
            speaker_frame=SubtitledSpeakerFrame(**CORNER),
            full_screen_windows=(SubtitledFullScreenWindow(start_s=0.0, end_s=5.55),),
        )
    )
    rows, receipt = _phone_spoken_list_on_lanes(
        rows,
        receipt,
        lanes=lanes,
        cues=_cues(),
        caption_style="word",
        caption_look=None,
        shown_rows=[],
        **common,
    )
    assert {row["y_frac"] for row in rows} == {0.15}
    assert len({row["size_px"] for row in rows}) == 1
    _assert_list_look(rows)
    assert all(e["list_spot"]["status"] == "shared" for e in receipt["rows"])
    assert len(calls) == len(set(calls)), "a window was sampled twice on the lanes"
