"""KRI-1633 F13: chat ops and her edits override a picked intro look, on every route.

A look is 2-5 rows of one title family (`opening-title`, `narrated-title` or `guided-title`, each
with `-line-N`). Before, only `opening-title` was known as a stack, and montage code keyed on the
literal `guided-title`, so an op aimed at the title restyled or removed only line 1.

Ways it could fail (written before the change):
O1 a style patch aimed at the title changes line 1 only;
O2 a whole-title rewrite leaves old lines or repeats the new words per line;
O3 a remove takes line 1 and leaves the other lines on screen;
O4 the Review Plan title field (`manual_edits_to_ops`) addresses one family only;
O5 an op aimed at one line stays one line (a subset is a line edit);
O6 a typed " / " title of two families at once is mixed (lines of two families are never one stack).
Each is tested per route with ops passed straight into `compile_editor_ops` (no model).
"""

from __future__ import annotations

import pytest

from app.kria.plan_contract import ManualEdit
from app.kria.plan_review import manual_edits_to_ops, parse_ops_without_model
from app.pipeline import intro_styles as styles
from app.pipeline.phone_title_look import title_family_of, title_look_line_ids
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.services.test_intro_styles_contract import EN, TR, stack
from tests.services.test_kria_editor_clip_context import (
    _caps,  # noqa: F401  (autouse fixture)
    _job_and_variant,
    _saved_text,
)

ROUTES = ("talking", "narrated", "montage")
NEW = "my morning coffee ritual"


def _job(route: str, title: str = EN, language: str | None = None):
    job, variant = _job_and_variant()
    rows = styles.remap_to_route(stack(title, language), route)
    others = [r for r in variant["text_elements"] if r["id"] != "guided-title"]
    variant["text_elements"] = [*rows, *others]
    return job, variant, build_editor_snapshot(job, variant), rows, others


def _compile(job, variant, snapshot, raw_ops):
    ops, rejection = parse_ops_without_model(raw_ops, snapshot, "edit the title")
    assert ops, rejection
    return compile_editor_ops(job, variant, ops)


def _title(compiled, route: str) -> list[dict]:
    family = styles.ROUTE_FAMILIES[route]
    return [r for r in _saved_text(compiled) if title_family_of(r["id"]) == family]


def test_the_family_ids_are_known_and_never_mixed():
    ids = ["opening-title", "opening-title-line-2", "narrated-title", "x", "guided-title-line-3"]
    assert title_look_line_ids(ids) == ["opening-title", "opening-title-line-2"]
    assert title_look_line_ids(ids, "guided-title") == ["guided-title-line-3"]  # O6
    assert title_family_of("guided-title-line-x") is None and title_family_of("kria-1") is None


@pytest.mark.parametrize("route", ROUTES)
def test_a_style_patch_aimed_at_the_title_changes_every_line(route):  # O1
    job, variant, snapshot, rows, _others = _job(route)
    raw = [{"op": "patch_text", "selector": {"group": "title"}, "patch": {"color": "#112233"}}]
    title = _title(_compile(job, variant, snapshot, raw), route)
    assert len(title) == len(rows) >= 2
    assert {r["color"] for r in title} == {"#112233"}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(("lang", "words"), [(None, EN), ("tr", TR)], ids=["en", "tr"])
def test_a_whole_title_rewrite_collapses_the_stack(route, lang, words):  # O2
    job, variant, snapshot, _rows, others = _job(route, words, lang)
    raw = [{"op": "rewrite_text", "selector": {"group": "title"}, "text": NEW}]
    compiled = _compile(job, variant, snapshot, raw)
    [title] = _title(compiled, route)
    assert title["text"] == NEW
    assert title.get("text_case") in (None, "none")
    assert {r["id"] for r in _saved_text(compiled)} >= {r["id"] for r in others}


@pytest.mark.parametrize("route", ROUTES)
def test_a_remove_takes_every_line(route):  # O3
    job, variant, snapshot, _rows, _others = _job(route)
    raw = [{"op": "remove_texts", "selector": {"group": "title"}}]
    assert _title(_compile(job, variant, snapshot, raw), route) == []


@pytest.mark.parametrize("route", ROUTES)
def test_the_review_plan_title_field_addresses_the_whole_stack(route):  # O4
    job, variant, snapshot, rows, _others = _job(route)
    family = styles.ROUTE_FAMILIES[route]
    raw = manual_edits_to_ops(
        [ManualEdit(kind="rewrite_text", target_id=family, text=NEW)], snapshot
    )
    [title] = _title(_compile(job, variant, snapshot, raw), route)
    assert title["text"] == NEW and title["id"] == family


@pytest.mark.parametrize("route", ROUTES)
def test_an_edit_to_one_line_stays_one_line(route):  # O5
    job, variant, snapshot, rows, _others = _job(route)
    second = rows[1]["id"]
    raw = [{"op": "rewrite_text", "selector": {"ids": [second]}, "text": "fresh"}]
    after = _title(_compile(job, variant, snapshot, raw), route)
    assert len(after) == len(rows)
    assert {r["id"]: r["text"] for r in after}[second] == "fresh"
