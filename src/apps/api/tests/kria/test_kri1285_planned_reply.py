"""KRI-1285: a draft reply lists render promises as planned, not as a failure.

Q1 run 2 (2026-11-07 20:20Z): every promise receipt (pauses, five labels, the ending arrow)
read "Partly" under "Not everything you asked for made it in". Real reply writer, the
incident's own promise texts; the control keeps a genuine shortfall failing.
"""

from __future__ import annotations

from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import RequirementReceipt

_TURN = "17e9fb29-b7e4-453a-9d59-62410cefd660"


def _req(rid: str, text: str) -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind="text", scope="global", description=text, source_turn_id=_TURN
    )


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(reqs))


def _partial(rid: str, reason: str) -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id=rid, status="partial", reason=reason, verification="checked"
    )


def test_only_promises_is_not_a_failure():
    brief = _brief(_req("r1", "Show label one"), _req("r2", "Draw an arrow"))
    receipts = [
        _partial("r1", bc._TALKING_SPOKEN_TEXT_PLAN),
        _partial("r2", bc._CLOSING_ARROW_PLAN),
    ]
    text, lead = bc.reply_and_lead_from_receipts(brief, receipts, summary="All set.")
    assert "Not everything you asked for" not in text
    assert "Will do at render" in text
    assert "Partly" not in text
    assert "Partly done" not in lead


def test_real_shortfall_keeps_the_failure_heading():
    brief = _brief(_req("r1", "Show label one"), _req("r2", "Draw an arrow"))
    receipts = [
        _partial("r1", bc._TALKING_SPOKEN_TEXT_PLAN),
        _partial("r2", bc._CLOSING_ARROW_MISSING),
    ]
    text, lead = bc.reply_and_lead_from_receipts(brief, receipts, summary="All set.")
    assert text.startswith("Not everything you asked for made it in")
    assert "Will do at render: Show label one" in text
    assert "Partly: Draw an arrow" in text
    assert lead == "Partly done."


def test_compacted_reasons_end_on_a_whole_word():
    reqs = [_req(f"r{i}", f"Label {i}") for i in range(12)]
    reason = "I'll draw a curly arrow next to your ending text " * 3
    receipts = [_partial(r.id, reason.strip()) for r in reqs]
    receipts[0] = _partial("r0", bc._CLOSING_ARROW_MISSING)
    text, _ = bc.reply_and_lead_from_receipts(_brief(*reqs), receipts)
    assert "…" in text
    assert "nex…" not in text
