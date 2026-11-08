"""KRI-1305 / KRI-1303: her opening title never sits on her captions, and has room for her logo.

Qendresa Q2 run 2 (job 1d512b9c): a three-line title ("5 PROJECTS / the AI builder community /
can't stop talking about!!!!") over a close selfie whose face fills 0.21-0.62 of the frame, and
word-by-word captions in a grey box "near the bottom" (her spot) at 0.77-0.81. No size or spot
cleared both, so the title placer took text on text (``text_overlap: true``); #2196's fix (move
the captions) then refused because the captions' spot is hers (``creator_spot_kept``) and the
title rendered as is: at 0.8 s the grey "Five projects" box sat on title lines 2-3. Run 1 asked
for the GitHub logo above the title too and got ``no_room_above_title``.

Ways this can fail (each asserted below, on her title, her take's words, her face band and the
title-look placer):

* F1 the overlap is rendered as is (``text_overlap`` true, a caption box on the title block).
* F2 the fix moves her captions off the spot she asked for.
* F3 the title is pushed onto her face, off the frame, or shrunk although the captions wait.
* F4 captions vanish for longer than the title shows (words after it lose their captions).
* F5 the receipt says clear while the drawn captions still touch the title.
* F6 a logo asked above the title finds no room although a small one fits between her chin and
  the title (then it must sit off her face, above the block, inside the frame).
* F7 nothing overlaps: nothing changes (cues and rows byte-identical).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline.canvas import Canvas
from app.pipeline.phone_spoken_text import caption_boxes
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.phone_text_styling import phone_text_policy_scope

CANVAS = Canvas(width=1080, height=1920)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1284_q2_run2_cues.json"
TITLE = "5 PROJECTS / the AI builder community / can't stop talking about!!!!"
FACE = NormalizedBox(0.207, 0.211, 0.845, 0.6175)  # the run's face band (full-frame take)
POLICY = {
    "version": 1,
    "requested": {},
    "caption_ask": {"box": "#E5E5E5", "position": "bottom"},  # her spot: hers to keep
    "preferences": {"all_text": {}},
    "selected_fonts": {"title": "Unbounded"},
    "selection_calls": 2,
    "selection_roles": ["title"],
    "_environment_sampled": True,
}


def _cues() -> list[dict]:
    return json.loads(FIXTURE.read_text())["caption_cues"]


def _sample(clip_path, anchors, **_kwargs):
    return [ProtectedRegion(a, a + 0.1, FACE, "face") for a in anchors], {"decoded": len(anchors)}


@pytest.fixture(autouse=True)
def _her_setup(monkeypatch):
    monkeypatch.setattr(settings, "title_look_enabled", True)
    monkeypatch.setattr(title_mod, "sample_face_regions", _sample)
    with phone_text_policy_scope(POLICY):
        yield


def _place(*, reserve_logo: bool = False, logo_below_band: bool = False, avoid=None):
    from app.tasks.generative_build import _phone_talking_title_rows, _title_look_caption_avoid

    look_avoid = (
        avoid
        if avoid is not None
        else _title_look_caption_avoid(_cues(), caption_style="word", canvas=CANVAS)
    )
    return _phone_talking_title_rows(
        TITLE,
        duration_s=None,
        cues=_cues(),
        binding=SimpleNamespace(
            original=SimpleNamespace(width=720, height=1280, orientation_degrees=0)
        ),
        clip_path="take.mp4",
        keep_segments=[(0.0, 40.431)],
        landscape_fit="fill",
        canvas=CANVAS,
        reserve_logo=reserve_logo,
        look_caption_avoid=look_avoid,
        logo_below_band=logo_below_band,
    )


def _resolve(rows, receipt, *, reserve_logo: bool = False):
    from app.tasks.generative_build import _title_clear_of_captions

    return _title_clear_of_captions(
        rows,
        receipt,
        cues=_cues(),
        framed_compile={},
        caption_style="word",
        protected_caption_look=None,
        replace=lambda: _place(
            reserve_logo=reserve_logo, logo_below_band=True, avoid=lambda _window: []
        ),
        canvas=CANVAS,
    )


def _block(rows) -> NormalizedBox:
    from app.pipeline.phone_title_look import title_block_box

    return title_block_box(rows, canvas=CANVAS)


def _window(rows) -> tuple[float, float]:
    return (min(float(r["start_s"]) for r in rows), max(float(r["end_s"]) for r in rows))


def test_the_title_and_her_captions_never_overlap():
    rows, receipt = _place()
    assert receipt["text_overlap"] is True  # the run's situation: no spot clears both
    first_scale = receipt["scale"]
    rows, receipt, cues = _resolve(rows, receipt)

    block, window = _block(rows), _window(rows)
    # F1 / F5: what is drawn, measured again: no caption line touches the title while it shows.
    drawn = caption_boxes(cues, window=window, caption_style="word", canvas=CANVAS)
    assert all(
        line.coverage_by(block) <= 0.05 and block.coverage_by(line) <= 0.05 for line in drawn
    ), drawn
    assert receipt["text_overlap"] is False
    assert receipt["caption_move"]["status"] == "captions_wait"
    # F3: off her face, inside the frame, and never smaller for the captions' sake (at 1x her
    # third line wraps and the block no longer fits under her face, so 0.85 is her face's limit).
    assert block.coverage_by(FACE) <= 0.05 and FACE.coverage_by(block) <= 0.05
    assert 0.06 <= block.top and block.bottom <= 0.95
    assert receipt["scale"] >= first_scale
    # F4: only what is said while the title shows waits: every later line is untouched, and a
    # line running past the title keeps its later words ("stop talking" -> "talking").
    after = [cue for cue in _cues() if float(cue["start_s"]) >= window[1]]
    assert all(cue in cues for cue in after)
    assert all(float(cue["start_s"]) >= window[1] - 1e-6 for cue in cues)
    words_after = [
        w["text"] for cue in _cues() for w in cue["words"] if float(w["start_s"]) >= window[1]
    ]
    assert [w["text"] for cue in cues for w in cue["words"]] == words_after


def test_her_caption_spot_is_kept():
    # F2: the captions after the title stay where she asked (no caption look was moved).
    rows, receipt = _place()
    framed: dict = {}
    from app.tasks.generative_build import _title_clear_of_captions

    _title_clear_of_captions(
        rows,
        receipt,
        cues=_cues(),
        framed_compile=framed,
        caption_style="word",
        protected_caption_look=None,
        replace=None,
        canvas=CANVAS,
    )
    assert "caption_look" not in framed


def test_her_logo_fits_between_her_chin_and_the_title():
    """F6: with the captions waiting, a logo asked above the title gets room: the title sits
    under the face with a small logo between, both off her face and inside the frame."""
    from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC
    from app.pipeline.phone_title_look import place_title_logo

    rows, receipt = _place(reserve_logo=True)
    rows, receipt, _cues_out = _resolve(rows, receipt, reserve_logo=True)
    assert receipt["text_overlap"] is False
    assert receipt["logo_room"] is True
    block = _block(rows)
    # Her GitHub logo is 1024x1024; the worker places it against the receipt's boxes.
    geometry, miss = place_title_logo(
        block,
        aspect=1.0,
        faces=[NormalizedBox(**box) for box in receipt["face_boxes"]],
        canvas=CANVAS,
        avoid=[NormalizedBox(**box) for box in receipt["caption_boxes"]],
        below_band=receipt.get("captions_hidden") is True
        and receipt.get("logo_below_band") is True,
    )
    assert miss is None and geometry is not None
    half_w = geometry["scale"] / 2
    half_h = geometry["scale"] * CANVAS.width / CANVAS.height / 2
    logo = NormalizedBox(
        geometry["x_frac"] - half_w,
        geometry["y_frac"] - half_h,
        geometry["x_frac"] + half_w,
        geometry["y_frac"] + half_h,
    )
    assert logo.bottom <= block.top + 1e-6  # above the title
    assert logo.coverage_by(FACE) <= 0.05  # off her face
    assert logo.top >= 0.06 and block.bottom <= 0.95
    # Below the caption-band line the compiler would pull it up onto her chin: it says so.
    if geometry["y_frac"] > CAPTION_BAND_TOP_FRAC:
        assert geometry.get("caption_clear") is True


def test_a_logo_card_below_the_band_keeps_its_spot_in_the_recipe():
    """F6, compiled: a card marked ``caption_clear`` keeps its y (the compiler would otherwise
    clamp it up to the caption-band line, onto her chin)."""
    from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC, SubtitledOverlayCard
    from app.pipeline.phone_subtitled_plan import _card_placement

    card = SubtitledOverlayCard(
        id="beat-logo-1",
        media_id="asset-logo",
        gcs_path="p/logo.png",
        generation="1",
        start_s=0.0,
        end_s=3.0,
        x_frac=0.5,
        y_frac=0.68,
        scale=0.17,
        caption_clear=True,
    )
    _w, _x, y = _card_placement(card, SimpleNamespace(), CANVAS)
    assert y == pytest.approx(0.68) and y > CAPTION_BAND_TOP_FRAC


def test_nothing_changes_when_nothing_overlaps():
    # F7
    from app.tasks.generative_build import _title_clear_of_captions

    rows = [{"id": "opening-title", "start_s": 0.0, "end_s": 3.0, "y_frac": 0.15}]
    receipt = {
        "text_overlap": False,
        "block": {"top": 0.1, "left": 0.1, "right": 0.9, "bottom": 0.2},
    }
    cues = _cues()
    out = _title_clear_of_captions(
        rows,
        receipt,
        cues=cues,
        framed_compile={},
        caption_style="word",
        protected_caption_look=None,
        replace=None,
        canvas=CANVAS,
    )
    assert out == (rows, receipt, cues)
