"""Q1 run 4 (2026-11-17): the stacked title stays readable on the footage under it.

Written before the module. Ways the legibility pass could go wrong, and the test for each:

* L1: cream lines over her cream t-shirt get no outline (the incident):
  `test_cream_title_on_a_cream_frame_gets_a_dark_outline`.
* L2: a dark frame gets an outline it does not need (rows must be returned untouched):
  `test_a_dark_frame_changes_nothing`.
* L3: it overrides a colour or outline she set: `test_her_colour_or_outline_is_never_overridden`.
* L4: the switch off still changes rows: `test_switch_off_returns_the_same_rows`.
* L5: a second compile re-decides wrongly (its own outline mistaken for hers):
  `test_a_second_pass_gives_the_same_rows`.
* L6: the outline fields do not survive the TextElement round trip a Save takes:
  `test_the_outline_survives_the_text_element_round_trip`.
* L7: no sampled frame under the title: nothing is invented: `test_no_frames_no_receipt`.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.recipes import Canvas
from app.pipeline.caption_legibility import GreyFrame
from app.pipeline.phone_title_legibility import title_legibility_pass
from app.pipeline.phone_title_look import TitleLookAsk, title_look_rows

CANVAS = Canvas(width=1080, height=1920)
TITLE = "5 repos / I'd build with / this weekend!!"


def _rows(ask=None):
    rows = title_look_rows(
        TITLE,
        duration_s=3.0,
        first_word_end_s=0.5,
        timeline_duration_s=30.0,
        canvas=CANVAS,
        ask=ask or TitleLookAsk(),
    )
    assert rows is not None
    # Where run 4 drew them: low on the shirt.
    for row, y in zip(rows, (0.7338, 0.8132, 0.8856), strict=True):
        row["y_frac"] = y
    return rows


def _frame(grey, start=0.0, end=30.0):
    return GreyFrame(bytes([grey]) * (108 * 192), 108, 192, start, end)


def test_cream_title_on_a_cream_frame_gets_a_dark_outline():
    rows = _rows()
    treated, receipt = title_legibility_pass(rows, [_frame(235)], canvas=CANVAS)

    assert [row["stroke_color"] for row in treated] == ["#000000"] * 3
    assert all(row["stroke_width"] >= 4 for row in treated)
    assert [row["text"] for row in treated] == [row["text"] for row in rows]
    assert [line["treated"] for line in receipt["lines"]] == [True, True, True]
    assert all(line["contrast"] < 4.5 for line in receipt["lines"])
    assert "stroke_width" not in rows[0], "the input rows are not edited in place"


def test_a_dark_frame_changes_nothing():
    rows = _rows()
    treated, receipt = title_legibility_pass(rows, [_frame(30)], canvas=CANVAS)

    assert treated == rows
    assert all(not line["treated"] for line in receipt["lines"])
    assert all("stroke_width" not in row for row in treated)


def test_her_colour_or_outline_is_never_overridden():
    asked, receipt = title_legibility_pass(
        _rows(TitleLookAsk(color="#FFFFFF")), [_frame(235)], canvas=CANVAS
    )
    assert all("stroke_width" not in row for row in asked)
    assert all(line.get("skipped") == "creator_styled" for line in receipt["lines"])

    own = _rows()
    own[0]["stroke_width"], own[0]["stroke_color"] = 3.0, "#222222"
    out, _ = title_legibility_pass(own, [_frame(235)], canvas=CANVAS)
    assert (out[0]["stroke_width"], out[0]["stroke_color"]) == (3.0, "#222222")
    assert out[1]["stroke_width"] > 0


def test_switch_off_returns_the_same_rows(monkeypatch):
    monkeypatch.setattr(settings, "title_legibility_enabled", False)
    rows = _rows()
    treated, receipt = title_legibility_pass(rows, [_frame(235)], canvas=CANVAS)

    assert treated is rows and receipt is None


def test_a_second_pass_gives_the_same_rows():
    once, _ = title_legibility_pass(_rows(), [_frame(235)], canvas=CANVAS)
    twice, receipt = title_legibility_pass(once, [_frame(235)], canvas=CANVAS)

    assert twice == once
    assert all(line["treated"] for line in receipt["lines"])


def test_the_outline_survives_the_text_element_round_trip():
    treated, _ = title_legibility_pass(_rows(), [_frame(235)], canvas=CANVAS)

    for row in treated:
        dumped = TextElement.model_validate(row).model_dump(mode="json", exclude_none=True)
        assert dumped["stroke_color"] == row["stroke_color"]
        assert dumped["stroke_width"] == row["stroke_width"]


@pytest.mark.parametrize("frames", [[], [_frame(235, start=10.0, end=20.0)]])
def test_no_frames_no_receipt(frames):
    rows = _rows()
    treated, receipt = title_legibility_pass(rows, frames, canvas=CANVAS)

    assert treated == rows
    assert receipt is None or all(line["contrast"] is None for line in receipt["lines"])


def test_the_worker_hook_outlines_the_title_and_records_the_receipt(monkeypatch):
    # The real hook (`_style_rows_keeping_title_look`) inside a generation scope that holds the
    # sampled take frames: the rows come back outlined and the variant gets `title_legibility`.
    from app.services.phone_text_styling import generation_style_fields, phone_text_policy_scope
    from app.tasks.generative_build import _style_rows_keeping_title_look

    policy = {
        "version": 1,
        "_background_frames": [
            {"pixels": bytes([235]) * (108 * 192), "start_s": 0.0, "end_s": 30.0}
        ],
    }
    rows = _rows()
    with phone_text_policy_scope(policy):
        styled = _style_rows_keeping_title_look(rows, job_id="j", canvas=CANVAS)
        fields = generation_style_fields()

    assert all(row["stroke_color"] == "#000000" and row["stroke_width"] > 0 for row in styled)
    assert [line["treated"] for line in fields["title_legibility"]["lines"]] == [True] * 3

    monkeypatch.setattr(settings, "title_legibility_enabled", False)
    with phone_text_policy_scope(policy):
        off = _style_rows_keeping_title_look(rows, job_id="j", canvas=CANVAS)
        assert "title_legibility" not in generation_style_fields()
    assert off == rows


def test_the_outline_goes_once_the_line_reads_without_it():
    """Self-review of the run-4 fix: an outline this pass drew is its own to remove. A title
    outlined on a light frame and re-compiled on a dark one must not keep a stale outline."""
    outlined, _ = title_legibility_pass(_rows(), [_frame(235)], canvas=CANVAS)
    assert all("stroke_width" in row for row in outlined)

    again, receipt = title_legibility_pass(outlined, [_frame(30)], canvas=CANVAS)

    assert again == _rows()
    assert all(not line["treated"] for line in receipt["lines"])


def test_her_own_outline_stays_on_a_dark_frame():
    rows = _rows()
    rows[0] = {**rows[0], "stroke_color": "#FF0000", "stroke_width": 5.0}

    again, _ = title_legibility_pass(rows, [_frame(30)], canvas=CANVAS)

    assert (again[0]["stroke_color"], again[0]["stroke_width"]) == ("#FF0000", 5.0)
