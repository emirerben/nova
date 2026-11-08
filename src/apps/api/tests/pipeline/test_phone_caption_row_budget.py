"""KRI-632 production path: the row budget on a long Talking cue set, switched on and off.

The off-state goldens (`single_word_fixture.origin_main_caption_rows`) run with the kill switch
off, so they say nothing about the default path. This file compiles the same ~190 s cue set the
goldens use with the switch ON (production default) and checks, without pinning any font-engine
dependent line break or split decision:

- the kill switch off is the pre-KRI-632 compile: one layer per cue, ids `caption-0..n-1`;
- a cue the pre-KRI-632 compile already kept to two rows is untouched by the new code;
- a cue the mark forced elsewhere may come back as consecutive pieces of at most two rows (at
  a custom size, no taller than the cue), with the same words, the same overall window and no
  gap;
- nothing inks the Kria mark.

Ways this can fail, written before the code: the switch-on path changes cues nobody asked it to;
a split loses a word, leaves a gap, or leaves a piece taller than two rows; the switch off still
splits.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.pipeline.phone_captions import compile_caption_layers
from tests.pipeline.single_word_fixture import OFF_STATE_CASES, talk_cues
from tests.pipeline.test_phone_captions import _ink_pixels_under_the_mark


def _rows(layer) -> int:
    return len({run.baseline_y for run in layer.runs})


def _text(layers) -> list[str]:
    return " ".join(run.text for layer in layers for run in layer.runs).split()


def _body(layer) -> dict:
    return {key: value for key, value in layer.model_dump(mode="json").items() if key != "id"}


def _compile(style: str, look, *, enabled: bool, monkeypatch):
    monkeypatch.setattr(settings, "phone_caption_row_budget_enabled", enabled)
    return compile_caption_layers(
        talk_cues(), canvas_width=1080, canvas_height=1920, style=style, look=look
    )


@pytest.mark.parametrize("name", sorted(OFF_STATE_CASES))
def test_switch_on_changes_only_cues_the_mark_forced_past_two_rows(name, monkeypatch):
    from app.pipeline.phone_captions import PhoneCaptionLook

    style, look_fields = OFF_STATE_CASES[name]
    look = PhoneCaptionLook(**look_fields) if look_fields else None
    cues = talk_cues()
    off = _compile(style, look, enabled=False, monkeypatch=monkeypatch)
    on = _compile(style, look, enabled=True, monkeypatch=monkeypatch)

    assert [layer.id for layer in off] == [f"caption-{k}" for k in range(len(cues))]
    assert _ink_pixels_under_the_mark(on) == 0
    for cue, before in zip(cues, off, strict=True):
        pieces = [layer for layer in on if cue["start_s"] <= layer.start < cue["end_s"]]
        assert _text(pieces) == cue["text"].split(), cue["text"]
        assert (pieces[0].start, pieces[-1].end) == pytest.approx((before.start, before.end))
        assert all(a.end == b.start for a, b in zip(pieces, pieces[1:], strict=False))
        if len(pieces) == 1:
            if _rows(before) <= 2:
                assert _body(pieces[0]) == _body(before), cue["text"]
        else:
            # A custom size (the 96 px case) can need the last round's gained rows; the pieces
            # are still never taller than the cue they came from.
            limit = _rows(before) if "text_size_px" in look_fields else 2
            assert all(_rows(piece) <= limit for piece in pieces), cue["text"]
