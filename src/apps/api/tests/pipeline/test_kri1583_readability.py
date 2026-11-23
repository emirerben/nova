"""KRI-1583: informative text keeps one colour per level for the whole video, and a row the
footage swallows gains a soft shadow instead of flipping to black.

Rows go through the real resolver (`style_rows` under the job's own text policy) with
background frames on the pipeline's own 108x192 grey grid, the frames `sample_background`
gives the resolver in production. The Kyoto proof uses the cached proxy frames of job 3930c822
(`scripts/kri1585_kyoto_fixture.py cache`); CI has no footage, so the same claims are also
made over synthetic frames.

Ways this fails, written before the code:
1. A stamped job still picks a fill per row (Kyoto shipped black, black, white).
2. A level takes the wrong colour: a heading goes white, a detail or hook goes yellow.
3. The row over the roof and the red pole stays unreadable: no shadow under 4.5:1.
4. Every row gets a shadow whatever is behind it, so the threshold decides nothing.
5. Her colour (an edit, a saved or asked colour) or her shadow (an edit, an ask this turn)
   is overwritten.
6. A job approved from a build below protocol 55 gets the design blur and drop, which mark
   the layer for design paint that build cannot decode.
7. A shadow added on one pass sticks after the footage under the row becomes readable.
8. A designed row (KRI-1581) is restyled twice: its own shadow or scrim is replaced, or its
   colour changes.
9. Captions and pins change: the rule is for informative text only.
10. The flag-off path changes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.pipeline.informative_text import CREATOR_FIELDS_KEY, POLICY_KEY, SHAPES_KEY
from app.pipeline.phone_text_background import HEIGHT, WIDTH
from app.pipeline.phone_title_legibility import LEGIBILITY_PARAM
from app.pipeline.text_designs import DETAIL_COLOR, HEADING_COLOR, HOOK_COLOR, SHADOW
from app.services.phone_text_styling import (
    PHONE_TEXT_STYLE_FIELD,
    phone_text_policy_scope,
    style_rows,
)

pytestmark = pytest.mark.journey("kri-1585-informative-text")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1585_kyoto"
CACHE = Path(__file__).resolve().parents[5] / ".cache" / "kri1585-kyoto"
ROOF_ROW = "creation-f286f919bed8-1"
# sha256 of `_flag_off_restyle()` computed on origin/main 12c59fec29, before this change.
FLAG_OFF_WITH_FRAMES_SHA256 = "7ba16b087e2c682f5f080cb40fdf733c5b6fd4f7239f9229dbae7301160c9c50"


def _load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _policy(*, stamped: bool, shapes: bool = False, requested: dict | None = None) -> dict:
    style = _load("plan.json")["variant"][PHONE_TEXT_STYLE_FIELD]
    policy = {key: copy.deepcopy(style[key]) for key in ("version", "preferences", "requested")}
    if requested is not None:
        policy["requested"] = requested
    if stamped:
        policy[POLICY_KEY] = True
        if shapes:
            policy[SHAPES_KEY] = True
    return policy


def _restyle(rows, frames, **policy_args) -> list[dict]:
    policy = _policy(**policy_args)
    selected = _load("plan.json")["variant"][PHONE_TEXT_STYLE_FIELD]["selected_fonts"]
    with phone_text_policy_scope(policy):
        return style_rows(
            copy.deepcopy(rows), policy, selected_fonts=selected, background_frames=frames
        )


def _flat(value: int, start_s: float = 0.0, end_s: float = 60.0) -> dict:
    return {"start_s": start_s, "end_s": end_s, "pixels": bytes([value]) * (WIDTH * HEIGHT)}


def _under_rows(values: list[int]) -> list[dict]:
    """One flat frame under each Kyoto row's own window."""
    return [
        _flat(value, row["start_s"], row["end_s"])
        for row, value in zip(_rows(), values, strict=True)
    ]


def _rows() -> list[dict]:
    return _load("text_rows.json")["text_elements"]


def _legibility(row: dict) -> dict:
    return row["source_params"][LEGIBILITY_PARAM]


def _layer(row: dict):
    from app.agents._schemas.text_element import TextElement
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.portable_text_layout import compile_text_overlay

    (overlay,) = build_overlays_from_text_elements(
        [TextElement.model_validate(row)], video_duration_s=61.6, independent_box_alignment=True
    )
    return compile_text_overlay(overlay, layer_id=f"text-{row['id']}", canvas=PORTRAIT)[0]


def _blur_layers(value) -> int:
    """Blur layers (shadow passes) anywhere in a compiled layer."""
    if isinstance(value, dict):
        return len(value.get("blur_layers") or []) + sum(map(_blur_layers, value.values()))
    if isinstance(value, list):
        return sum(map(_blur_layers, value))
    return 0


def _draws_a_shadow(row: dict) -> bool:
    """The phone layer of ``row`` carries more blur passes than the same row unshadowed."""
    bare = copy.deepcopy(row) | {"shadow_enabled": False}
    return _blur_layers(_layer(row).model_dump()) > _blur_layers(_layer(bare).model_dump())


def _flag_off_restyle() -> str:
    frames = [*_under_rows([235, 20, 128]), _flat(235, 0.0, 2.0)]
    out = {
        name: _restyle(_load(name)["text_elements"], frames, stamped=False)
        for name in ("text_rows.json", "text_rows_after_editor_save.json")
    }
    return json.dumps(out, sort_keys=True, ensure_ascii=False)


def test_flag_off_restyle_with_frames_is_byte_identical_to_main():
    digest = hashlib.sha256(_flag_off_restyle().encode("utf-8")).hexdigest()
    assert digest == FLAG_OFF_WITH_FRAMES_SHA256


def test_flag_off_still_flips_the_fill_per_row():
    shipped = _restyle(_rows(), _under_rows([235, 20, 235]), stamped=False)
    assert [row["color"] for row in shipped] == ["#000000", "#FFFFFF", "#000000"]
    assert all(LEGIBILITY_PARAM not in row["source_params"] for row in shipped)


def test_one_colour_and_a_shadow_only_where_the_footage_swallows_the_row():
    # Bright under rows 0 and 2, dark under row 1: per row the sampler picks black, white, black.
    styled = _restyle(_rows(), _under_rows([235, 20, 235]), stamped=True)
    assert {row["color"] for row in styled} == {DETAIL_COLOR}
    by_id = {row["id"]: row for row in styled}
    assert _legibility(by_id[ROOF_ROW])["treatment"] == "none"
    assert by_id[ROOF_ROW]["shadow_enabled"] is False
    for row_id in ("creation-f286f919bed8-0", "creation-f286f919bed8-2"):
        assert _legibility(by_id[row_id])["treatment"] == "shadow"
        assert _legibility(by_id[row_id])["contrast"] < 4.5
        assert by_id[row_id]["shadow_enabled"] is True


def test_a_build_below_the_designs_protocol_gets_a_shadow_it_can_decode():
    (row, *_) = _restyle(_rows(), [_flat(235)], stamped=True)
    assert "shadow_blur" not in row and "shadow_offset_y" not in row
    assert row["shadow_color"] == SHADOW["shadow_color"]
    assert row["shadow_opacity"] == SHADOW["shadow_opacity"]
    layer = _layer(row)
    assert layer.scrim is None and not layer.srgb_blend
    assert _draws_a_shadow(row)

    (designed, *_) = _restyle(_rows(), [_flat(235)], stamped=True, shapes=True)
    assert designed["shadow_blur"] == SHADOW["shadow_blur"]
    assert designed["shadow_offset_y"] == SHADOW["shadow_offset_y"]


def test_a_later_pass_over_readable_footage_drops_the_shadow_it_added():
    first = _restyle(_rows(), [_flat(235)], stamped=True)
    assert all(_legibility(row)["treatment"] == "shadow" for row in first)
    again = _restyle(first, [_flat(20)], stamped=True)
    for row in again:
        assert _legibility(row)["treatment"] == "none"
        assert row["shadow_enabled"] is False
        assert "shadow_color" not in row and "shadow_opacity" not in row


def test_a_row_no_frame_covers_gets_the_shadow():
    styled = _restyle(_rows(), [], stamped=True)
    for row in styled:
        assert _legibility(row) | {"contrast": None, "frames": 0} == _legibility(row)
        assert _legibility(row)["treatment"] == "shadow"


def test_her_colour_and_her_shadow_win():
    rows = _rows()
    rows[0]["color"] = "#FF00FF"
    rows[0]["shadow_enabled"] = False
    rows[0]["source_params"][CREATOR_FIELDS_KEY] = ["color", "shadow_enabled"]
    styled = _restyle(rows, [_flat(235)], stamped=True)
    assert styled[0]["color"] == "#FF00FF"
    assert styled[0]["shadow_enabled"] is False
    assert _legibility(styled[0])["treatment"] == "creator"
    assert styled[1]["color"] == DETAIL_COLOR and styled[1]["shadow_enabled"] is True

    asked = _restyle(
        _rows(),
        [_flat(235)],
        stamped=True,
        requested={"all_text": {"color": "#00FF00", "shadow_enabled": False}},
    )
    for row in asked:
        assert row["color"] == "#00FF00"
        assert row["shadow_enabled"] is False
        assert _legibility(row)["treatment"] == "creator"


def test_each_level_keeps_its_own_colour():
    """A hook, an A2-style pair the research writer tagged, and a guide line, on an older build
    (no design layout): hook white, heading #FFE7A0, detail and line white."""
    base = _rows()[0]
    hook = copy.deepcopy(base) | {"id": "hook", "start_s": 0.0, "end_s": 3.4}
    hook["source_params"] = {"phone_text_role": "hook"}
    heading, detail, line = (copy.deepcopy(base) for _ in range(3))
    for row, level, row_id, window in (
        (heading, "heading", "heading", (4.0, 9.0)),
        (detail, "detail", "detail", (4.0, 9.0)),
        (line, "line", "line", (10.0, 15.0)),
    ):
        row.update(id=row_id, start_s=window[0], end_s=window[1], color="#000000")
        research = row["source_params"]["research"]
        research["design"] = {"id": "A2", "level": level, "pair": 0}
    styled = {
        row["id"]: row for row in _restyle([hook, heading, detail, line], [_flat(20)], stamped=True)
    }
    assert styled["hook"]["color"] == HOOK_COLOR
    assert styled["heading"]["color"] == HEADING_COLOR
    assert styled["detail"]["color"] == DETAIL_COLOR
    assert styled["line"]["color"] == DETAIL_COLOR
    assert {_legibility(row)["level"] for row in styled.values()} == {"hook", "heading", "detail"}


@pytest.mark.parametrize("design", ["A1", "A2", "A3", "B1"])
def test_a_designed_row_keeps_its_own_look(design):
    from app.agents._schemas.text_element import TextElement

    rows = [
        TextElement.model_validate(row).model_dump(mode="json")
        for row in _load("designed_rows.json")["designs"][design]
    ]
    bright, dark = (
        _restyle(rows, [_flat(value)], stamped=True, shapes=True) for value in (235, 20)
    )
    strip = lambda row: {k: v for k, v in row.items() if k != "source_params"}  # noqa: E731
    assert [strip(row) for row in bright] == [strip(row) for row in dark]
    for row in bright:
        assert _legibility(row)["treatment"] == "design"
        assert row["shadow_enabled"] is True


@pytest.mark.parametrize(
    ("row_id", "source_params"),
    [
        ("caption-0", {"source": "caption_cue"}),
        ("pin-0", {}),
        ("title-0", {}),
    ],
)
def test_captions_pins_and_titles_keep_their_own_fill(row_id, source_params):
    row = copy.deepcopy(_rows()[0])
    row.update(id=row_id, source_params=source_params)
    stamped = _restyle([row], [_flat(235)], stamped=True)
    plain = _restyle([row], [_flat(235)], stamped=False)
    assert stamped == plain
    assert LEGIBILITY_PARAM not in stamped[0]["source_params"]


@pytest.mark.skipif(
    not (CACHE / "frames.json").exists(),
    reason="needs footage frames: python scripts/kri1585_kyoto_fixture.py cache",
)
def test_kyoto_replay_gives_one_colour_and_shadows_the_roof_and_pole_row():
    """Production's rows over the cached proxy frames, keyed to the clips as the sampler keys
    them. Shipped: black, black, white. Stamped: white on every row, each under 4.5:1 with
    white too, so each gains the shadow; the roof/pole row is the second."""
    index = json.loads((CACHE / "frames.json").read_text(encoding="utf-8"))["frames"]
    frames = [
        {"start_s": f["start_s"], "end_s": f["end_s"], "pixels": (CACHE / f["gray"]).read_bytes()}
        for f in index
        if f["window"].startswith("clip-")
    ]
    styled = {row["id"]: row for row in _restyle(_rows(), frames, stamped=True)}
    assert {row["color"] for row in styled.values()} == {DETAIL_COLOR}
    roof = styled[ROOF_ROW]
    assert roof["shadow_enabled"] is True
    assert _legibility(roof)["treatment"] == "shadow"
    assert _legibility(roof)["frames"] > 0
    assert _legibility(roof)["contrast"] < 4.5
    assert _draws_a_shadow(roof)
