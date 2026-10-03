"""KRI-282: Creative Brief receipts for a spoken-excerpt montage."""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    SpeechSectionFact,
    build_receipts,
    check_requirement,
    is_judged,
    plan_facts_from_speech_montage,
    reply_from_receipts,
)

PATTERN = (
    "Play my espresso line over the other clips, then cut to me for the routine line, "
    "then back to fast cuts"
)


def _req(description: str = PATTERN, kind: str = "audio") -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, status="open"
    )


def _record(*sections: dict, dropped: tuple[str, ...] = ()) -> dict:
    return {
        "sections": list(sections),
        "duration_s": 24.0,
        "planned": {"dropped": [{"quote": q, "reason": "not_said"} for q in dropped]},
    }


MONTAGE = {"kind": "montage"}
OVER = {"kind": "speech", "visual": "cutaways", "quote": "never rush", "media_id": "talk"}
SPEAKER = {"kind": "speech", "visual": "speaker", "quote": "morning routine", "media_id": "talk"}


def _check(record: dict, description: str = PATTERN):
    return check_requirement(_req(description), plan_facts_from_speech_montage(record))


def test_full_pattern_is_met() -> None:
    receipt = _check(_record(MONTAGE, OVER, SPEAKER, MONTAGE))
    assert receipt.status == "met" and receipt.reason is None


def test_speech_never_over_broll_is_partial_and_says_so() -> None:
    receipt = _check(_record(MONTAGE, SPEAKER, MONTAGE))
    assert receipt.status == "partial"
    assert "never play over the other footage" in receipt.reason


def test_not_returning_to_the_speaker_is_caught() -> None:
    receipt = _check(_record(SPEAKER, MONTAGE, OVER))
    assert receipt.status == "partial" and "come back to you" in receipt.reason


def test_missing_fast_cuts_between_lines_is_caught() -> None:
    receipt = _check(_record(OVER, SPEAKER))
    assert receipt.status == "partial" and "no fast cuts" in receipt.reason


def test_dropped_quote_is_named() -> None:
    receipt = _check(_record(MONTAGE, OVER, SPEAKER, MONTAGE, dropped=("I flew to the moon",)))
    assert receipt.status == "partial" and "I flew to the moon" in receipt.reason


def test_no_grounded_excerpt_at_all_is_not_possible() -> None:
    receipt = _check(_record(MONTAGE, dropped=("a line nobody said",)))
    assert receipt.status == "not_possible" and "a line nobody said" in receipt.reason


def test_a_plan_that_is_not_a_speech_montage_claims_nothing() -> None:
    receipt = check_requirement(_req(), PlanFacts())
    assert not is_judged(_req(), receipt)


def test_receipts_flow_into_the_reply() -> None:
    brief = CreativeBrief(version=1, requirements=[_req()])
    facts = plan_facts_from_speech_montage(_record(MONTAGE, SPEAKER, MONTAGE))
    receipts = build_receipts(brief.live(), facts)
    assert len(receipts) == 1 and receipts[0].status == "partial"
    reply = reply_from_receipts(brief, receipts)
    assert "Partly" in reply and "never play over the other footage" in reply


def test_facts_read_the_compiled_sections() -> None:
    facts = plan_facts_from_speech_montage(_record(MONTAGE, OVER))
    assert facts.speech_sections == (
        SpeechSectionFact(kind="montage"),
        SpeechSectionFact(kind="speech", visual="cutaways", quote="never rush", media_id="talk"),
    )
    assert facts.duration_s == 24.0
    assert plan_facts_from_speech_montage(None).speech_sections == ()
