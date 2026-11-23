"""KRI-1579 on the Kyoto Torokko fixture (prod job 3930c822): research text follows the creator's
all_text spot, and a style field she sets survives every later style pass.

Ways this fails, written before the fix:
1. Research rows (``source_params.research``) are filed as titles, so the saved ``title: top``
   beats her ``all_text: bottom_left`` (what shipped: y 0.21 and 0.18 at the top).
2. An editor Save records nothing: the 11:53 Save left ``creator_style_fields`` empty on the
   rows she touched and absent on the hook she added, so a re-style takes her font and colour
   (Montserrat Bold, #FFF0A6) back to the automatic ones.
3. A chat edit commits through the same Save and records nothing either.
4. The resolver keeps only seven creator fields; her outline colour, shadow colour and opacity,
   tracking and case are not on that list.
5. The phone recipe drops what the resolver kept (font, fill, outline) on the way to the layer.
6. The flag-off path changes: any job stamped before the flip must restyle byte-identically.
7. A design writes over a field she set, or a design's fields lose to her saved preferences.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.informative_text import (
    CREATOR_FIELDS_KEY,
    DESIGN_FIELDS_KEY,
    POLICY_KEY,
    apply_design_values,
)
from app.pipeline.phone_text_style import text_role
from app.services.phone_text_styling import (
    PHONE_TEXT_STYLE_FIELD,
    phone_text_policy_scope,
    style_rows,
)

pytestmark = pytest.mark.journey("kri-1585-informative-text")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1585_kyoto"
HOOK_ID = "9BEF66D9-6920-47B5-8BC2-5AE58C27B7C2"
# sha256 of the unstamped restyle of text_rows.json and text_rows_after_editor_save.json
# (canonical JSON, `_restyle_both`), computed on origin/main d6e3c198 before this change.
FLAG_OFF_RESTYLE_SHA256 = "0b0aa6e1f7221571a922494cdeb0098a4d9770b878eb495026179756280d92c2"


def _load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _policy(*, stamped: bool) -> dict:
    style = _load("plan.json")["variant"][PHONE_TEXT_STYLE_FIELD]
    policy = {key: style[key] for key in ("version", "preferences", "requested")}
    if stamped:
        policy[POLICY_KEY] = True
    return policy


def _selected() -> dict:
    return _load("plan.json")["variant"][PHONE_TEXT_STYLE_FIELD]["selected_fonts"]


def _restyle(rows: list[dict], *, stamped: bool) -> list[dict]:
    policy = _policy(stamped=stamped)
    with phone_text_policy_scope(policy):
        return style_rows(rows, policy, selected_fonts=_selected())


def _restyle_both() -> str:
    out = {
        name: style_rows(
            _load(name)["text_elements"], _policy(stamped=False), selected_fonts=_selected()
        )
        for name in ("text_rows.json", "text_rows_after_editor_save.json")
    }
    return json.dumps(out, sort_keys=True, ensure_ascii=False)


def _save(previous: list[dict], saved: list[dict], *, stamped: bool) -> list[dict]:
    """What `_prepare_editor_commit` persists for this Save's text lane."""
    from app.routes.generative_jobs import _record_creator_text_edits

    variant = {
        "variant_id": "guided_story",
        "text_elements": previous,
        PHONE_TEXT_STYLE_FIELD: _policy(stamped=stamped),
    }
    job = SimpleNamespace(assembly_plan={"variants": [variant]})
    return _record_creator_text_edits(job, variant, copy.deepcopy(saved))


def _layer(row: dict):
    """The phone recipe's text layer for one row, the way the guided device plan builds it."""
    from app.agents._schemas.text_element import TextElement
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.portable_text_layout import compile_text_overlay

    (overlay,) = build_overlays_from_text_elements(
        [TextElement.model_validate(row)], video_duration_s=61.6, independent_box_alignment=True
    )
    return compile_text_overlay(overlay, layer_id=f"text-{row['id']}", canvas=PORTRAIT)[0]


def test_flag_off_restyle_is_byte_identical_to_main():
    digest = hashlib.sha256(_restyle_both().encode("utf-8")).hexdigest()
    assert digest == FLAG_OFF_RESTYLE_SHA256


def test_research_rows_follow_all_text_bottom_left():
    rows = _load("text_rows.json")["text_elements"]
    shipped = _restyle(rows, stamped=False)
    assert [row["y_frac"] for row in shipped[:2]] == pytest.approx([0.20854, 0.18354], abs=0.03)

    with phone_text_policy_scope(_policy(stamped=True)):
        assert {text_role(row) for row in rows} == {"research"}
    styled = _restyle(rows, stamped=True)
    for row in styled:
        assert row["alignment"] == "left", row["id"]
        assert float(row["x_frac"]) < 0.3, row["id"]
        assert float(row["y_frac"]) > 0.5, row["id"]


def test_unstamped_save_records_nothing():
    previous = _load("text_rows.json")["text_elements"]
    saved = _load("text_rows_after_editor_save.json")["text_elements"]
    assert _save(previous, saved, stamped=False) == saved


def test_editor_save_marks_the_fields_she_set_and_a_restyle_keeps_them():
    previous = _load("text_rows.json")["text_elements"]
    saved = _load("text_rows_after_editor_save.json")["text_elements"]
    persisted = _save(previous, saved, stamped=True)
    by_id = {row["id"]: row for row in persisted}

    # Row 0 only moved in time: no style field is hers.
    assert by_id["creation-f286f919bed8-0"]["source_params"][CREATOR_FIELDS_KEY] == []
    hook_fields = set(by_id[HOOK_ID]["source_params"][CREATOR_FIELDS_KEY])
    assert {"color", "font_family", "size_px", "x_frac", "y_frac", "alignment"} <= hook_fields

    # Unrecorded, the restyle takes her font back; recorded, it keeps everything she set.
    lost = {row["id"]: row for row in _restyle(saved, stamped=True)}[HOOK_ID]
    assert lost["font_family"] != "Montserrat Bold"
    kept = {row["id"]: row for row in _restyle(persisted, stamped=True)}[HOOK_ID]
    assert kept["font_family"] == "Montserrat Bold"
    assert kept["color"] == "#FFF0A6"
    assert kept["size_px"] == 72
    assert kept["y_frac"] == pytest.approx(saved[3]["y_frac"])


def test_chat_edit_style_survives_restyle_and_reaches_the_phone_layer():
    """A chat edit's draft commits through the same Save: colour, position, outline, shadow and
    font on a research row, then a re-style, then the phone recipe layer."""
    previous = _load("text_rows.json")["text_elements"]
    edited = copy.deepcopy(previous)
    edited[2].update(
        color="#FFE7A0",
        font_family="Inter",
        stroke_width=4,
        stroke_color="#1A1A1A",
        shadow_enabled=True,
        shadow_color="#000000",
        shadow_opacity=0.6,
        x_frac=0.06,
        y_frac=0.16,
        alignment="left",
    )
    persisted = _save(previous, edited, stamped=True)
    assert set(persisted[2]["source_params"][CREATOR_FIELDS_KEY]) >= {
        "color",
        "font_family",
        "stroke_width",
        "stroke_color",
        "shadow_enabled",
        "shadow_color",
        "shadow_opacity",
        "x_frac",
        "y_frac",
        "alignment",
    }

    styled = _restyle(persisted, stamped=True)[2]
    for field in (
        "color",
        "font_family",
        "stroke_width",
        "stroke_color",
        "shadow_enabled",
        "shadow_color",
        "shadow_opacity",
        "alignment",
    ):
        assert styled[field] == edited[2][field], field
    assert styled["y_frac"] == pytest.approx(0.16)

    layer = _layer(styled)
    run = layer.runs[0]
    assert "Inter" in run.font_asset_id
    fill = (run.fill.red, run.fill.green, run.fill.blue)
    assert fill == pytest.approx((0xFF / 255, 0xE7 / 255, 0xA0 / 255), abs=1e-3)
    assert run.stroke_width > 0
    assert (run.stroke.red, run.stroke.green, run.stroke.blue) == pytest.approx(
        (0x1A / 255,) * 3, abs=1e-3
    )
    assert run.blur_layers, "the shadow she turned on is drawn"
    assert layer.anchor_y < 1920 * 0.5


def test_design_fields_beat_preferences_and_never_her_fields():
    previous = _load("text_rows.json")["text_elements"]
    row = copy.deepcopy(previous[1])
    row["source_params"][CREATOR_FIELDS_KEY] = ["color"]
    row["color"] = "#00FF00"
    apply_design_values(
        row,
        {"font_family": "Inter", "color": "#FFFFFF", "shadow_enabled": True, "size_px": 50},
        "B1",
    )
    assert row["color"] == "#00FF00"
    assert row["source_params"][DESIGN_FIELDS_KEY] == ["font_family", "shadow_enabled", "size_px"]

    # Her saved all_text says no shadow and a favourite font; the design's choice still holds.
    styled = _restyle([row], stamped=True)[0]
    assert styled["font_family"] == "Inter"
    assert styled["shadow_enabled"] is True
    assert styled["color"] == "#00FF00"


@pytest.mark.parametrize("stamped", [False, True])
def test_a_real_guided_save_persists_her_fields_on_the_revision_and_the_variant(
    monkeypatch, stamped
):
    """Through `prepare_editor_commit` (the iPhone Save and a chat draft's commit) on a guided
    story, the same path the Kyoto job saved through."""
    import app.routes.generative_jobs as gj
    from app.config import settings
    from tests.routes.test_editor_commit import _arm, _narrated_guided_job

    _arm(monkeypatch)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj, "_TEXT_ELEMENTS_ENABLED", True)
    job = _narrated_guided_job()
    variant = job.assembly_plan["variants"][0]
    variant[PHONE_TEXT_STYLE_FIELD] = _policy(stamped=stamped)
    revision = gj._guided_v2_revision(job, variant)
    elements = copy.deepcopy(revision["text_elements"])
    edited_id = elements[0]["id"]
    elements[0].update(color="#FFF0A6", font_family="Montserrat Bold")
    gj.prepare_editor_commit(
        job,
        variant["variant_id"],
        gj.EditorCommitRequest(
            base_generation=revision["base_generation"],
            guided_revision_number=1,
            text_elements=elements,
        ),
    )
    saved_variant = job.assembly_plan["variants"][0]
    for rows in (
        saved_variant["guided_edit_revision"]["text_elements"],
        saved_variant["text_elements"],
    ):
        row = next(row for row in rows if row["id"] == edited_id)
        fields = (row.get("source_params") or {}).get(CREATOR_FIELDS_KEY)
        if stamped:
            assert fields == ["color", "font_family"]
        else:
            assert not fields
        untouched = [r for r in rows if r["id"] != edited_id]
        assert all(not (r.get("source_params") or {}).get(CREATOR_FIELDS_KEY) for r in untouched)


def test_a_chat_add_text_makes_only_the_style_she_named_hers(monkeypatch):
    """ "Add 'Hi' in red": the colour is hers; the font, size and spot it copied from the title
    (or the Playfair default) stay automatic, so the resolver may still pick them."""
    import app.routes.generative_jobs as gj
    from app.config import settings
    from app.services.kria_editor_ops import compile_editor_ops
    from tests.routes.test_editor_commit import _arm, _narrated_guided_job

    _arm(monkeypatch)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj, "_TEXT_ELEMENTS_ENABLED", True)
    job = _narrated_guided_job()
    variant = job.assembly_plan["variants"][0]
    variant[PHONE_TEXT_STYLE_FIELD] = _policy(stamped=True)
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "add_text",
                "text": "Hi",
                "start_s": 0.0,
                "end_s": 2.0,
                "patch": {"color": "#FF0000"},
            }
        ],
    )
    saved = gj._record_creator_text_edits(job, variant, compiled.payload.text_elements)
    (added,) = [row for row in saved if str(row["id"]).startswith("kria-")]
    assert added["source_params"][CREATOR_FIELDS_KEY] == ["color"]
    assert "font_family" in added["source_params"]["default_style_fields"]


def test_every_design_level_is_a_drawable_text_row():
    """The registry names registry fonts and valid row values, and matches the fixture targets."""
    from app.agents._schemas.text_element import _ALLOWED_FONTS, TextElement
    from app.pipeline.text_designs import TEXT_DESIGNS

    targets = _load("expected_rows.json")["target"]["designs"]
    for design in TEXT_DESIGNS.values():
        for name in ("hook", "heading", "detail"):
            level = getattr(design, name)
            if level is None:
                continue
            assert level.font_family in _ALLOWED_FONTS, (design.id, name)
            TextElement.model_validate(
                {"id": "x", "text": "t", "start_s": 0, "end_s": 1, **level.style_values()}
            )
        if "hook_size_px" in targets[design.id]:
            assert design.hook.size_px == targets[design.id]["hook_size_px"]
    assert TEXT_DESIGNS["B1"].detail.size_px == targets["B1"]["line_size_px"]
