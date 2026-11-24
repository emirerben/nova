"""KRI-1611 review round 2: the creator's edits and chat ops override an intro scene.

A scene draws her slashless title as 2-5 rows (`opening-title`, `opening-title-line-N`). The
Review Plan title field and a chat "make the title X" both rewrite the WHOLE title, but each
addresses rows one at a time, so before the fix a whole-title rewrite left the old words on the
other lines or repeated the new title once per row.

Ways it could fail (written before the fix):
E1 manual title edit on the first line leaves the old words on lines 2-N;
E2 a chat `rewrite_text` on `group: "title"` repeats the new title on every line;
E3 the surviving row keeps the scene's capitals, so her typed words draw re-cased;
E4 an edit to ONE line (a subset of the scene) is wrongly widened into a whole-title rewrite;
E5 a replace-inside-the-line edit, or any row that is not a scene, is touched;
E6 a typed " / " stack, the same shape, still repeats the new title per line.
"""

from __future__ import annotations

import pytest

from app.kria.plan_contract import ManualEdit
from app.kria.plan_review import manual_edits_to_ops, parse_ops_without_model
from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from app.pipeline.canvas import PORTRAIT
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.pipeline.test_intro_scene_pick import _place_with
from tests.services.test_kria_editor_clip_context import (
    _caps,  # noqa: F401  (autouse fixture)
    _job_and_variant,
    _saved_text,
)

EN = "the biggest design trend right now"
NEW = "my morning coffee ritual"


def _scene(font_pair: str = "grotesk_hand") -> list[dict]:
    menu = scene_mod.issue_menu(EN)
    ask = {
        "version": pick.INTRO_SCENE_VERSION,
        "seed": "e",
        "inks": list(scene_mod.INKS),
        "candidates": [
            {
                "voices": list(pick.accent_splits(EN)[0]),
                "font_pair": font_pair,
                "entrance": menu.entrances_for(font_pair)[0],
                "zones": ["above_head"],
            }
        ],
    }
    rows, _receipt, info = pick.fit_intro_scene(
        ask,
        EN,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=None,
        place=_place_with(),
    )
    assert rows is not None, info
    return rows


def _scene_job(rows: list[dict]):
    job, variant = _job_and_variant()
    others = [r for r in variant["text_elements"] if r["id"] != "guided-title"]
    variant["text_elements"] = [*rows, *others]
    return job, variant, build_editor_snapshot(job, variant), others


def _compile(job, variant, snapshot, raw_ops):
    ops, rejection = parse_ops_without_model(raw_ops, snapshot, "edit the title")
    assert ops, rejection
    return compile_editor_ops(job, variant, ops)


def _title_rows(compiled) -> list[dict]:
    return [r for r in _saved_text(compiled) if r["id"].startswith("opening-title")]


@pytest.mark.parametrize("font_pair", ["grotesk_hand", "serif_italic"])
def test_the_review_plan_title_edit_replaces_the_whole_scene(font_pair):
    rows = _scene(font_pair)
    assert len(rows) >= 2
    job, variant, snapshot, others = _scene_job(rows)
    raw = manual_edits_to_ops(
        [ManualEdit(kind="rewrite_text", target_id="opening-title", text=NEW)], snapshot
    )
    compiled = _compile(job, variant, snapshot, raw)
    [title] = _title_rows(compiled)  # E1: exactly one row, none of the old lines
    assert title["text"] == NEW
    assert "text_case" not in title or title["text_case"] in (None, "none")  # E3
    assert {r["id"] for r in _saved_text(compiled)} >= {r["id"] for r in others}  # E5
    assert [r["text"] for r in _saved_text(compiled) if r["id"] in {o["id"] for o in others}] == [
        o["text"] for o in others
    ]


def test_a_chat_rewrite_of_the_title_group_does_not_repeat_it_per_line():
    job, variant, snapshot, _others = _scene_job(_scene())
    raw = [{"op": "rewrite_text", "selector": {"group": "title"}, "text": NEW}]
    compiled = _compile(job, variant, snapshot, raw)
    [title] = _title_rows(compiled)  # E2
    assert title["text"] == NEW
    assert "text_case" not in title or title["text_case"] in (None, "none")


def test_an_edit_to_one_line_stays_one_line():
    rows = _scene()
    job, variant, snapshot, _others = _scene_job(rows)
    second = rows[1]["id"]
    raw = [{"op": "rewrite_text", "selector": {"ids": [second]}, "text": "fresh"}]
    compiled = _compile(job, variant, snapshot, raw)
    after = _title_rows(compiled)  # E4
    assert len(after) == len(rows)
    assert {r["id"]: r["text"] for r in after}[second] == "fresh"
    assert [r["text"] for r in after if r["id"] != second] == [
        r["text"] for r in rows if r["id"] != second
    ]


def test_a_replace_inside_a_line_leaves_the_scene_alone():
    rows = _scene()
    job, variant, snapshot, _others = _scene_job(rows)
    word = rows[0]["text"].split()[0]
    raw = [
        {
            "op": "rewrite_text",
            "selector": {"group": "title"},
            "replace": {"find": word, "with": "zzz"},
        }
    ]
    compiled = _compile(job, variant, snapshot, raw)
    after = _title_rows(compiled)  # E5
    assert len(after) == len(rows)
    assert after[0]["text"].startswith("zzz")


def test_her_typed_slash_stack_follows_the_same_rule():
    """E6: the same shape, so the same fix: a whole-title rewrite is one new title, not the new
    words on each typed line."""
    from app.pipeline.phone_title_look import TitleLookAsk, title_look_rows

    rows = title_look_rows(
        "5 PROJECTS / the AI builder community",
        duration_s=None,
        first_word_end_s=1.0,
        timeline_duration_s=30.0,
        canvas=PORTRAIT,
        ask=TitleLookAsk(),
    )
    assert rows and len(rows) == 2
    job, variant, snapshot, _others = _scene_job(rows)
    raw = [{"op": "rewrite_text", "selector": {"group": "title"}, "text": NEW}]
    compiled = _compile(job, variant, snapshot, raw)
    assert [r["text"] for r in _title_rows(compiled)] == [NEW]
