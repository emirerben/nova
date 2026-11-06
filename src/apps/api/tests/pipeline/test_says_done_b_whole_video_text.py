"""Qendresa Q3 case 9 (render half): a "for the whole video" line shown for 2.2 s.

Ported from the kit (`kria-qendresa-test/tests/bugs/test_q3_whole_video_text_receipt.py`). The
brief records whole-video text as a `text/global` literal. When the planner files her line as
`opening_title` (or writes no text field, so `unified_montage._title` uses the brief literal),
the phone montage drew it as the opening title for `FAST_MONTAGE_TITLE_HOLD_S` = 2.2 s of an
11 s video. The fix pins it for the whole video, the shape the brief rule asked for.

The kit asserts the scenario (`covered < total`) and then a "not met" receipt (group C's half).
This port asserts the render half's stronger result: the line covers the whole video, and both
the worker's stored receipt and the finished-video re-judge (the render-ready review in
`tasks/kria_runtime.py`) say met. Pure functions; no model, DB or network.
Failure modes: docs/reviews/qendresa-says-done-b-failure-modes.md (case 9).
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_rendered_montage,
    plan_facts_from_unified_montage,
)
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_plan_repair import _shows
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.creator_render_contract import build_render_contract

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
    analysis={"best_moments": []},  # the whole 11 s plays (isolates the text)
)
FILINGS = [
    pytest.param({"opening_title": TEXT}, id="filed_as_opening_title"),
    pytest.param({}, id="no_text_field_brief_literal_becomes_title"),
]


def _render(strategy: dict, brief: CreativeBrief = BRIEF):
    plan = plan_unified_montage([CLIP], brief_view(brief), strategy=strategy)
    rows = compile_execution_plan(plan.guided_edit(), track=None)["text_elements"]
    spans = [(r["id"], r["start_s"], r["end_s"]) for r in rows if TEXT in r.get("text", "")]
    return plan, rows, spans


def _receipts(plan, rows, brief: CreativeBrief = BRIEF):
    record = plan.record()
    stored = build_receipts(
        brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
    )[0]
    variant = {"text_elements": rows, "duration_s": plan.duration_s}
    rendered = build_receipts(
        brief.live(), plan_facts_from_rendered_montage(variant, record), include_unchecked=True
    )[0]
    return stored, rendered


@pytest.mark.parametrize("filing", FILINGS)
def test_whole_video_line_stays_on_screen(filing: dict) -> None:
    plan, rows, spans = _render({**BASE, **filing})
    total = plan.duration_s
    assert len(spans) == 1, spans
    _id, start, end = spans[0]
    assert start <= 0.05 and end >= total - 0.05, f"her line is on screen {spans} of {total}s"
    stored, rendered = _receipts(plan, rows)
    assert (stored.status, rendered.status) == ("met", "met"), (stored.reason, rendered.reason)


@pytest.mark.parametrize("filing", FILINGS)
def test_the_pinned_line_keeps_the_contract_opening_role(filing: dict) -> None:
    """The approval's contract still names the line as the opening text when the planner filed
    it as `opening_title`; a pin drawn from 0 s satisfies that role (no "missing text")."""
    strategy = {**BASE, **filing}
    plan, _rows, _spans = _render(strategy)
    contract = build_render_contract(
        {**strategy, "target_duration_s": plan.duration_s},
        generation_id="g1",
        brief=BRIEF,
    )
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    for requirement in contract.exact_texts if contract is not None else ():
        assert _shows(execution, requirement, 1 / 30), requirement


def test_control_a_pinned_whole_video_line_is_done() -> None:
    plan, rows, spans = _render({**BASE, "pinned_texts": [{"text": TEXT, "corner": "top_center"}]})
    assert spans == [("guided-pinned-0", 0.0, plan.duration_s)]
    assert _receipts(plan, rows)[1].status == "met"


def test_title_literal_stays_a_title() -> None:
    """A title-scope literal with the same words is an opening title: it keeps its short hold."""
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="t1", kind="text", scope="title", literal=TEXT, facts={}),
        ],
    )
    plan, _rows, spans = _render({**BASE, "opening_title": TEXT}, brief)
    assert len(spans) == 1 and spans[0][2] < plan.duration_s - 1.0, spans


def test_a_typed_title_hold_stays_a_title() -> None:
    plan, _rows, spans = _render({**BASE, "opening_title": TEXT, "opening_title_duration_s": 3})
    assert len(spans) == 1 and abs(spans[0][2] - 3.0) < 0.05, spans


def test_switch_off_keeps_the_short_title(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "whole_video_text_pin_enabled", False)
    plan, _rows, spans = _render({**BASE, "opening_title": TEXT})
    assert len(spans) == 1 and spans[0][2] < plan.duration_s - 1.0, spans
