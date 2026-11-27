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


# --- KRI-1618: a late accent line moves with its title ---------------------------------------
#
# M5 a chat retime or a Range cut bends the accent's delay into one the approval check refuses;
# a window too short for the delay leaves a line starting at or after its end.


def _late_accent_scene(*, accent_first: bool = False) -> list[dict]:
    """ref 1's late accent; ``accent_first`` is ref 5's shape, a script line tucked onto the serif
    line under it, so line 1 is the line that enters late."""
    look = (
        {"voices": ("accent",) * 2 + ("solid",) * 4, "font_pair": "script_serif", "ink": "white"}
        if accent_first
        else {
            "voices": ("solid",) * 4 + ("accent",) * 2,
            "font_pair": "grotesk_hand",
            "ink": "oxblood",
        }
    )
    scene = scene_mod.IntroScene(
        decision="compose",
        zone="above_head",
        entrance="fade" if accent_first else "ink",
        sound="none",
        stagger="accent_after",
        **({"stack": "script_over_serif"} if accent_first else {}),
        **look,
    )
    out = scene_mod.compile_intro_scene(scene, EN, canvas=PORTRAIT, timeline_duration_s=30.0)
    assert out.rows is not None, out.reason
    placed, _receipt = _place_with()(out.rows, out.position)
    return placed


def _times(rows: list[dict]) -> dict[str, tuple[float, float]]:
    return {r["source_params"]["title_look"]["style"]: (r["start_s"], r["end_s"]) for r in rows}


@pytest.mark.parametrize(
    ("start", "end", "accent_start"),
    [(0.5, 2.5, 0.5 + scene_mod.STAGGERS["accent_after"]), (0.5, 0.9, 0.5)],
    ids=["moves-as-one-title", "too-short-for-the-delay-shows-together"],
)
def test_a_chat_retime_moves_a_late_accent_title_as_one(start, end, accent_start):
    job, variant, snapshot, _others = _scene_job(_late_accent_scene())
    raw = [{"op": "set_text_timing", "bar_index": 0, "start_s": start, "end_s": end}]
    times = _times(_title_rows(_compile(job, variant, snapshot, raw)))
    assert times["intro_solid"] == (start, end)
    assert times["intro_accent"] == (pytest.approx(accent_start), end)


def test_a_range_cut_that_bent_the_delay_is_undone_and_other_rows_are_left_alone():
    from app.pipeline.title_family import retime_title_stacks

    scene = _late_accent_scene()
    caption = {"id": "caption-1", "text": "so", "start_s": 0.31, "end_s": 0.9}
    accent = {"intro_accent": 0.4, "intro_solid": 0.0}  # a 0.2 s cut inside the first 0.6 s
    bent = [
        {**row, "start_s": accent[row["source_params"]["title_look"]["style"]], "end_s": 2.8}
        for row in scene
    ]
    out = retime_title_stacks([*bent, caption])
    assert _times(out[:-1]) == {
        "intro_solid": (0.0, 2.8),
        "intro_accent": (pytest.approx(scene_mod.STAGGERS["accent_after"]), 2.8),
    }
    assert out[-1] is caption
    plain = _scene()  # a scene that declares no stagger is returned as it was
    assert retime_title_stacks(plain) == plain


def test_a_whole_title_rewrite_of_a_late_accent_first_title_starts_with_the_title():
    rows = _late_accent_scene(accent_first=True)
    assert rows[0]["start_s"] == pytest.approx(scene_mod.STAGGERS["accent_after"])
    job, variant, snapshot, _others = _scene_job(rows)
    raw = manual_edits_to_ops(
        [ManualEdit(kind="rewrite_text", target_id="opening-title", text=NEW)], snapshot
    )
    [title] = _title_rows(_compile(job, variant, snapshot, raw))
    assert (title["text"], title["start_s"], title["end_s"]) == (NEW, 0.0, rows[0]["end_s"])


def test_the_render_contract_reads_a_late_accent_first_title_as_one_text_from_its_start():
    from app.pipeline.phone_title_look import intro_stack_as_one_row

    rows = _late_accent_scene(accent_first=True)
    [one] = intro_stack_as_one_row(rows)
    assert (one["text"], one["start_s"]) == (EN, 0.0)
