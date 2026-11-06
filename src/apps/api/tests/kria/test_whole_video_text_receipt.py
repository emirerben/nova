"""Qendresa case 9 (receipt half): "this text for the whole video" shown for 2.2 s still got "Done".

Ported from the Qendresa kit (`test_q3_whole_video_text_receipt.py`). The brief records
whole-video text as a `text/global` literal (Main Creator brief rule: "Text the creator wants
pinned in a corner or on screen for the whole video ... {kind: text, scope: global}"). When the
Main Creator files her line as `opening_title` (or emits no text field, so the plan uses the
brief's literal as the title), the phone montage draws it for `FAST_MONTAGE_TITLE_HOLD_S` = 2.2 s
of an 11 s video, and `_check_literal_text` only checked that the words appear, so the review
said "Done". Group B makes such a line render for the whole video; this receipt must be right
either way: judged by the time the line is really on screen.

The helper mirrors the render-ready review (`tasks/kria_runtime.py`): the worker's record
receipt is shown when judged (and not left to the render), else the finished-video facts
re-judge it. Pure functions; no model, DB or network. Failure modes:
`docs/reviews/says-done-receipts-failure-modes.md` (case 9).
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    is_judged,
    judged_at_render,
    plan_facts_from_rendered_montage,
    plan_facts_from_unified_montage,
)
from app.kria.reply_language import reply_language_for
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage

TEXT = "The prompt I run at the end of every Claude session"
WHOLE_VIDEO_LINE = BriefRequirement(
    id="r1",
    kind="text",
    scope="global",
    literal=TEXT,
    description="this text for the whole video",
    facts={},
)
BRIEF = CreativeBrief(version=1, requirements=[WHOLE_VIDEO_LINE])
BASE = {"edit_format": "montage", "audio_strategy": "licensed_music", "direction": "fast_montage"}
CLIP = UnifiedClip(
    media_id="clip-1",
    proxy_path="users/u/analysis-proxy-clip-1.mp4",
    generation="1",
    duration_s=11.0,
    width=1080,
    height=1920,
    analysis={"best_moments": []},  # the whole 11 s plays (isolates the text bug)
)


def _review(req: BriefRequirement, record: dict, variant: dict):
    """The render-ready review's verdict for ``req``: the record's when judged, else the
    finished video's (`tasks/kria_runtime.py` montage branch)."""
    stored = build_receipts([req], plan_facts_from_unified_montage(record), include_unchecked=True)[
        0
    ]
    if is_judged(req, stored) and not judged_at_render(req):
        return stored
    return build_receipts(
        [req], plan_facts_from_rendered_montage(variant, record), include_unchecked=True
    )[0]


def _shown_receipt(strategy: dict, req: BriefRequirement = WHOLE_VIDEO_LINE):
    brief = CreativeBrief(version=1, requirements=[req])
    plan = plan_unified_montage([CLIP], brief_view(brief), strategy=strategy)
    rows = compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]
    spans = [(r["id"], r["start_s"], r["end_s"]) for r in rows if req.literal in r.get("text", "")]
    variant = {"text_elements": rows, "duration_s": plan.duration_s}
    return _review(req, plan.record(), variant), spans, plan.duration_s


@pytest.mark.parametrize(
    "filing",
    [
        pytest.param({"opening_title": TEXT}, id="filed_as_opening_title"),
        pytest.param({}, id="no_text_field_brief_literal_becomes_title"),
    ],
)
def test_whole_video_line_shown_briefly_is_not_done(filing: dict) -> None:
    receipt, spans, total = _shown_receipt({**BASE, **filing})
    covered = sum(end - start for _id, start, end in spans)
    if covered >= total - 0.05:
        # Group B's render fix holds the line for the whole video: then it is done.
        assert receipt.status == "met", receipt.reason
        return
    assert receipt.status != "met", (
        f"her line is on screen {spans} of a {total}s video, yet the review says "
        f"{receipt.status} ({receipt.reason!r})"
    )
    assert is_judged(WHOLE_VIDEO_LINE, receipt)
    assert "not the whole video" in receipt.reason


def test_a_typed_short_title_hold_is_not_done() -> None:
    """9.6b: a title hold the Main Creator typed (`opening_title_duration_s`) stays short even
    after group B's render fix, so the whole-video line must still read "not done"."""
    receipt, spans, total = _shown_receipt(
        {**BASE, "opening_title": TEXT, "opening_title_duration_s": 2}
    )
    assert spans == [("guided-title", 0.0, 2.0)] and total == 11.0
    assert receipt.status != "met", receipt.reason
    assert is_judged(WHOLE_VIDEO_LINE, receipt)
    assert "first 2 s of the 11 s video" in receipt.reason
    assert "not the whole video" in receipt.reason


def test_a_same_words_title_ask_keeps_the_line_short_and_says_so() -> None:
    """9.6b: she also asked for a title with the same words, so the live `text/title` literal
    keeps the line short (2.2 s). The whole-video line is not done; the title ask is."""
    title_ask = BriefRequirement(
        id="r2",
        kind="text",
        scope="title",
        literal=TEXT,
        description="title it with the same words",
        facts={},
    )
    brief = CreativeBrief(version=1, requirements=[WHOLE_VIDEO_LINE, title_ask])
    plan = plan_unified_montage([CLIP], brief_view(brief), strategy=BASE)
    rows = compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]
    spans = [(r["id"], r["start_s"], r["end_s"]) for r in rows if TEXT in r.get("text", "")]
    assert spans == [("guided-title", 0.0, 2.2)] and plan.duration_s == 11.0
    variant = {"text_elements": rows, "duration_s": plan.duration_s}
    line = _review(WHOLE_VIDEO_LINE, plan.record(), variant)
    assert line.status != "met", line.reason
    assert is_judged(WHOLE_VIDEO_LINE, line)
    assert "first 2.2 s of the 11 s video" in line.reason
    assert "not the whole video" in line.reason
    assert _review(title_ask, plan.record(), variant).status == "met"


def test_control_a_pinned_whole_video_line_is_done() -> None:
    """Passes today (and must keep passing): the pin covers 0-11 s and is met."""
    receipt, spans, total = _shown_receipt(
        {**BASE, "pinned_texts": [{"text": TEXT, "corner": "top_center"}]}
    )
    assert spans == [("guided-pinned-0", 0.0, total)] and receipt.status == "met"


# ----------------------------------------------------------- synthetic finished videos


def _row(row_id: str, start: float, end: float, text: str = TEXT, role: str = "title") -> dict:
    return {"id": row_id, "role": role, "text": text, "start_s": start, "end_s": end}


TITLE_RECORD = {"clip_ids": ["clip-1"], "title": TEXT, "title_source": "creator", "duration_s": 11}


def test_a_title_held_for_the_whole_video_is_met() -> None:
    """9.3: the record cannot see the title's hold; a finished video that holds it 0-11 s is
    done (what group B's render fix produces)."""
    receipt = _review(
        WHOLE_VIDEO_LINE,
        TITLE_RECORD,
        {"text_elements": [_row("guided-title", 0.0, 11.0)], "duration_s": 11.0},
    )
    assert receipt.status == "met", receipt.reason


def test_line_split_across_two_rows_covering_the_video_is_met() -> None:
    """9.4: abutting / overlapping rows count once, as the union of their spans."""
    receipt = _review(
        WHOLE_VIDEO_LINE,
        TITLE_RECORD,
        {
            "text_elements": [
                _row("a", 0.0, 6.0),
                _row("b", 5.5, 11.0),
            ],
            "duration_s": 11.0,
        },
    )
    assert receipt.status == "met", receipt.reason


def test_ending_line_is_not_judged_as_whole_video() -> None:
    """9.1: an ending line (`closing` fact) is global text with its own time."""
    ending = BriefRequirement(
        id="r2",
        kind="text",
        scope="global",
        literal="comment READ",
        description='End with "comment READ"',
        facts={"closing": "comment READ"},
    )
    receipt = _review(
        ending,
        {"clip_ids": ["clip-1"], "closing_title": "comment READ", "duration_s": 11},
        {
            "text_elements": [_row("guided-closing", 8.0, 11.0, text="comment READ")],
            "duration_s": 11.0,
        },
    )
    assert receipt.status == "met", receipt.reason


def test_timed_pin_keeps_its_verdict() -> None:
    """9.2: a pin with a time the creator named covers part of the video on purpose."""
    record = {
        "clip_ids": ["clip-1"],
        "duration_s": 11,
        "pinned_texts": [{"text": TEXT, "corner": "top_left", "start_s": 5, "end_s": 10}],
    }
    receipt = _review(
        WHOLE_VIDEO_LINE,
        record,
        {"text_elements": [_row("guided-pinned-0", 5.0, 10.0)], "duration_s": 11.0},
    )
    assert receipt.status == "met", receipt.reason


def test_turkish_reason_for_brief_line() -> None:
    with reply_language_for("tr"):
        receipt = _review(
            WHOLE_VIDEO_LINE,
            TITLE_RECORD,
            {"text_elements": [_row("guided-title", 0.0, 2.2)], "duration_s": 11.0},
        )
    assert receipt.status == "partial"
    assert "2.2" in receipt.reason and "videonun tamamında değil" in receipt.reason
