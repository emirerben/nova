"""KRI-1035: an agent-authored line break survives the ops and is laid out the same everywhere.

Ways this can break: the copilot cleaner or an op flattens the break to a space; the row keeps
wrap_lines=true so a renderer re-wraps and shrinks it; one renderer (cloud Skia vs the portable
compiler the phone draws with) keeps the rows and the other does not; placement measures a
different layout than the one drawn.
"""

from types import SimpleNamespace

import pytest

from app.agents.edit_copilot import _clean_user_text
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_text_style import measure_box, resolve_phone_text_style
from app.pipeline.portable_text_layout import compile_text_overlay
from app.services.kria_editor_ops import _op_add_text, _op_edit_text
from app.services.kria_editor_ops_text import clean_authored_text, keep_authored_breaks

TEXT = "Bu güzel vatan,\nçağdaş imkanlar için,\n29 Ekim kutlu olsun"


def test_cleaner_keeps_breaks_and_strips_everything_else():
    assert clean_authored_text(" a \x00 b\r\n\r\n\r\n\r\nc \n") == "a b\n\nc"
    assert clean_authored_text("\n\n x \n") == "x"
    assert _clean_user_text("a\nb", keep_breaks=True) == "a\nb"
    assert _clean_user_text("a\nb") == "a b"


def test_ops_keep_the_break_and_mark_the_row_unwrapped():
    state = SimpleNamespace(
        text=[], explicit_text_ids=set(), changed=set(), text_bar=lambda _i: bar
    )
    bar = {"text": "old"}
    _op_edit_text(state, {"bar_index": 0, "text": TEXT})
    assert bar["text"] == TEXT and bar["wrap_lines"] is False
    _op_add_text(state, {"text": TEXT, "start_s": 0, "end_s": 2})
    assert state.text[0]["text"] == TEXT and state.text[0]["wrap_lines"] is False
    plain = {"text": "one line"}
    keep_authored_breaks(plain)
    assert "wrap_lines" not in plain


def _row():
    return {
        "id": "guided-title",
        "text": TEXT,
        "role": "generative_intro",
        "start_s": 0,
        "end_s": 3,
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.16,
        "font_family": "Inter",
        "size_px": 76,
        "color": "#FFFFFF",
        "alignment": "center",
        "max_width_frac": 0.84,
        "effect": "static",
        "wrap_lines": False,
    }


def test_placement_and_both_renderers_draw_the_authored_rows():
    placed = resolve_phone_text_style([_row()])[0]
    assert placed["text"] == TEXT and placed["wrap_lines"] is False
    overlay = {
        "text": placed["text"],
        "start_s": 0,
        "end_s": 3,
        "font_family": placed["font_family"],
        "text_size_px": placed["size_px"],
        "max_width_frac": placed["max_width_frac"],
        "wrap_lines": False,
        "effect": "static",
    }
    layer, _ = compile_text_overlay(overlay, layer_id="t", canvas=PORTRAIT)
    wanted = TEXT.split("\n")
    assert [run.text for run in layer.runs] == wanted
    face = cloud._typeface_for_overlay(overlay)
    _font, size, lines = cloud._authored_lines(TEXT, face, placed["size_px"], 0.9 * PORTRAIT.width)
    assert lines == wanted and size == layer.runs[0].font_size
    # The box placement reserved holds exactly the rows both renderers draw.
    left, top, right, bottom = measure_box(placed)
    assert 0 < left < right <= 1 and 0 < top < bottom <= 1


@pytest.mark.parametrize("flag", [True, False])
def test_wrapped_text_keeps_legacy_layout(flag):
    row = {**_row(), "text": "one two three", "wrap_lines": flag}
    assert resolve_phone_text_style([row])[0]["text"] == "one two three"
