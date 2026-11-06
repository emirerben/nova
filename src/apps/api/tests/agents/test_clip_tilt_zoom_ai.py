"""KRI-1169: the chat copilot zooms clips through ``patch_slots.tilt.zoom`` / ``zoom_delta``.

The zoom is the twin of the angle: ``zoom`` replaces a clip's own zoom, ``zoom_delta`` adds to
it, both stay between 0.5x and 3x, and nothing changes for an edit whose zoom gate is closed.
"""

from __future__ import annotations

import pytest

from app.agents import editor_ops_v2 as v2
from app.agents.edit_copilot import _format_snapshot
from app.agents.editor_ops_v2 import timeline
from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
from tests.agents.test_clip_tilt_ai import (
    CLOSED,
    OPEN,
    _compile,
    _parse,
    _said,
    _snapshot,
    _three_clips,
    _tilt,
)
from tests.services.test_kria_editor_ops import _job

ZOOM_OPEN = {
    **OPEN,
    "clips": {
        "tilt": {"editable": True, "reason": None},
        "tilt_zoom": {"editable": True, "reason": None},
    },
}


def _second(tilt):
    return _tilt({"slot_indexes": [1]}, tilt)


def test_an_absolute_zoom_replaces_the_clip_zoom_and_keeps_its_angle_and_backdrop(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 8.0, "zoom": 1.5, "backdrop": {"kind": "blur"}})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    assert snapshot["slots"][1]["tilt"]["zoom"] == 1.5

    parsed = _parse(snapshot, _second({"zoom": 2}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)

    assert rows[1]["tilt"] == {
        "rotation_deg": 8.0,
        "zoom": 2.0,
        "backdrop": {"kind": "blur"},
    }
    assert _said(draft) == ["Zoomed clip 2 to 2x"]


def test_a_relative_zoom_adds_to_each_clips_own_zoom(monkeypatch):
    variant = _three_clips()
    variant["ai_timeline"]["slots"][1]["tilt"] = {"rotation_deg": 0.0, "zoom": 1.2}
    variant["ai_timeline"]["slots"][2]["tilt"] = {"rotation_deg": 5.0, "zoom": 2.0}
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)

    parsed = _parse(snapshot, _tilt({"media_kind": "image"}, {"zoom_delta": 0.3}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)

    assert [r["tilt"]["zoom"] for r in rows[1:]] == [1.5, 2.3]
    assert rows[2]["tilt"]["rotation_deg"] == 5.0
    assert _said(draft) == ["Changed the zoom of 2 clips"]


def test_a_relative_zoom_on_an_unzoomed_clip_starts_from_one(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom_delta": 0.2}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)

    assert rows[1]["tilt"] == {"rotation_deg": 0.0, "zoom": 1.2}
    assert _said(draft) == ["Zoomed clip 2 to 1.2x"]


def test_a_tilt_and_the_zoom_that_fills_its_corners_in_one_patch(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"rotation_deg": 8, "zoom": 1.24}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)

    assert rows[1]["tilt"] == {"rotation_deg": 8.0, "zoom": 1.24}
    assert _said(draft) == ["Tilted clip 2 8 degrees right, zoomed to 1.24x"]


def test_an_angle_keeps_the_zoom_the_clip_has(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 8.0, "zoom": 1.24})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"rotation_delta_deg": 3}))
    _draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 11.0, "zoom": 1.24}


def test_resetting_the_zoom_keeps_the_angle_and_a_zoom_alone_resets_to_no_tilt(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 8.0, "zoom": 1.24})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom": 1}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 8.0}
    assert _said(draft) == ["Zoomed clip 2 to 1x"]

    variant = _three_clips(tilt={"rotation_deg": 0.0, "zoom": 1.5})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom": 1}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] is None
    assert _said(draft) == ["Reset the zoom of clip 2"]


@pytest.mark.parametrize(("asked", "kept"), [(5, 3.0), (0.1, 0.5), (3.0, 3.0), (0.5, 0.5)])
def test_an_absolute_zoom_past_the_range_is_clamped_and_the_reply_says_so(monkeypatch, asked, kept):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom": asked}), reply="Zoomed.")
    assert parsed.ops == [_second({"zoom": kept})]
    if kept != asked:
        assert "0.5x and 3x" in parsed.reply
    else:
        assert "0.5x and 3x" not in parsed.reply


def test_a_relative_zoom_never_leaves_the_range(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 0.0, "zoom": 2.9})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    _draft, rows = _compile(
        monkeypatch, variant, *_parse(snapshot, _second({"zoom_delta": 1.0})).ops
    )
    assert rows[1]["tilt"]["zoom"] == 3.0

    variant = _three_clips(tilt={"rotation_deg": 0.0, "zoom": 0.6})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    _draft, rows = _compile(
        monkeypatch, variant, *_parse(snapshot, _second({"zoom_delta": -1.0})).ops
    )
    assert rows[1]["tilt"]["zoom"] == 0.5


@pytest.mark.parametrize("zoom", [0, -1, "1.5", True, None, float("inf")])
def test_an_unusable_zoom_is_refused_in_plain_words(monkeypatch, zoom):
    snapshot = _snapshot(monkeypatch, _three_clips(), ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom": zoom}), reply="Zoomed clip 2.")
    assert parsed.ops == []
    assert "Zoomed" not in parsed.reply
    assert "zoom" in parsed.reply.lower()


def test_an_absolute_and_a_relative_zoom_together_are_ambiguous(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips(), ZOOM_OPEN)
    assert _parse(snapshot, _second({"zoom": 1.5, "zoom_delta": 0.2})).ops == []


def test_a_clip_with_a_look_and_a_card_photo_cannot_zoom(monkeypatch):
    variant = _three_clips(look_preset="golden_hour")
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom": 1.5}))
    with pytest.raises(KriaEditorOpError, match="look"):
        compile_editor_ops(_job(variant), variant, parsed.ops)

    variant = _three_clips(layout="supporting_card")
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    parsed = _parse(snapshot, _second({"zoom": 1.5}))
    with pytest.raises(KriaEditorOpError, match="can't be zoomed"):
        compile_editor_ops(_job(variant), variant, parsed.ops)


def test_straightening_clears_the_zoom_with_everything_else(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 8.0, "zoom": 1.24})
    snapshot = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    _draft, rows = _compile(monkeypatch, variant, *_parse(snapshot, _second(None)).ops)
    assert rows[1]["tilt"] is None


def test_the_row_lists_its_zoom_to_the_model_only_while_the_zoom_gate_is_open(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 8.0, "zoom": 1.24})
    opened = _snapshot(monkeypatch, variant, ZOOM_OPEN)
    assert "tilt=(rotation_deg=8.00, zoom=1.24)" in _format_snapshot(opened)
    assert "CLIP ZOOM" in v2.family_fragments(opened)

    tilt_only = _snapshot(monkeypatch, variant, OPEN)
    assert tilt_only["slots"][1]["tilt"] == {"rotation_deg": 8.0}
    assert "tilt=(rotation_deg=8.00)" in _format_snapshot(tilt_only)
    assert "zoom" not in _format_snapshot(tilt_only)
    assert "CLIP ZOOM" not in v2.family_fragments(tilt_only)
    assert v2.family_fragments(opened).startswith(v2.family_fragments(tilt_only))


def test_a_zoom_only_row_is_hidden_from_a_snapshot_whose_zoom_gate_is_closed(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 0.0, "zoom": 1.5})
    tilt_only = _snapshot(monkeypatch, variant, OPEN)
    assert "tilt" not in tilt_only["slots"][1]
    assert "tilt_zoom" not in tilt_only["timeline_patch_capabilities"]
    closed = _snapshot(monkeypatch, variant, CLOSED)
    assert "tilt" not in closed["timeline_patch_capabilities"]


def test_a_closed_zoom_gate_refuses_the_ask_instead_of_claiming_it(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips(), OPEN)
    parsed = _parse(snapshot, _second({"zoom": 1.3}), reply="Zoomed clip 2 in.")
    assert parsed.ops == []
    assert parsed.outcome == "unsupported"
    assert "Zoomed" not in parsed.reply
    assert "can't zoom" in parsed.reply

    parsed = _parse(snapshot, _second({"rotation_deg": 10}), reply="Tilted clip 2.")
    assert parsed.ops == [_second({"rotation_deg": 10.0})]


def test_a_closed_zoom_gate_also_stops_an_op_that_skips_the_parser(monkeypatch):
    variant = _three_clips()
    monkeypatch.setattr("app.services.kria_editor_ops._editor_capabilities", lambda *_: OPEN)
    with pytest.raises(KriaEditorOpError, match="Zooming clips isn't available"):
        compile_editor_ops(_job(variant), variant, [_second({"zoom": 1.3})])


def test_a_closed_zoom_gate_makes_a_zoomed_variant_read_like_an_unzoomed_one(monkeypatch):
    """The prompt and the snapshot of a tilt-open edit do not depend on a stored zoom."""
    tilt = {"rotation_deg": 10.0, "backdrop": {"kind": "blur"}}
    plain = _three_clips(tilt=tilt)
    zoomed = _three_clips(tilt={**tilt, "zoom": 1.5})

    def render(variant):
        snapshot = _snapshot(monkeypatch, variant, OPEN)
        return snapshot, _format_snapshot(snapshot), v2.family_fragments(snapshot)

    assert render(zoomed) == render(plain)


def test_the_zoom_prompt_teaches_the_range_the_delta_rule_and_the_cover_table():
    fragment = v2.read_fragment("clip_tilt_zoom.txt")
    assert "zoom_delta" in fragment and "0.5 to 3" in fragment
    assert "cos(A)" in fragment and "1.78" in fragment
    assert "8 is 1.24" in fragment and "never answer a relative ask" in fragment.lower()
    assert "slot_indexes" in fragment


def test_the_zoom_keys_are_part_of_the_tilt_contract():
    assert {"zoom", "zoom_delta"} <= timeline._TILT_KEYS
