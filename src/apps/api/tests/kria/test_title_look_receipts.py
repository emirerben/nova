"""Item 20: receipts for the stacked title look and the logo that sits above it.

Ways this can fail (written before the code):
 1. Her three title rows are on screen but the ask "A / B / C" is reported missing,
    because no single row holds the whole literal.
 2. A changed word still passes (negative control).
 3. A logo placed above the title is quoted as a word she said, or its miss reads
    "I never heard ..." although nothing was listened for.
 4. A title-anchored miss ("no_title", "no_room_above_title") gets the generic hiccup line.
 5. A draft says "when you say ..." for a beat that goes above the title.
"""

from __future__ import annotations

from typing import Any

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    BeatFact,
    check_requirement,
    plan_facts_from_phone_variant,
)
from app.services.nova_steps import beat_miss_sentence

_LINES = ("5 PROJECTS", "the AI builder community", "can't stop talking about!!!!")
_WANTED = " / ".join(_LINES)


def _rows(lines: tuple[str, ...] = _LINES) -> list[dict[str, Any]]:
    out = []
    for i, text in enumerate(lines, start=1):
        out.append(
            {
                "id": "opening-title" if i == 1 else f"opening-title-line-{i}",
                "text": text,
                "role": "generative_intro",
                "start_s": 0.0,
                "end_s": 3.0,
                "source_params": {
                    "source": "opening_title",
                    "title_look": {"line": i, "lines": len(lines), "style": "bold"},
                },
            }
        )
    return out


def _variant(lines: tuple[str, ...] = _LINES, receipt: Any = None) -> dict[str, Any]:
    variant: dict[str, Any] = {
        "text_elements": _rows(lines),
        "resolved_archetype": "subtitled",
        "render_destination": "device",
    }
    if receipt is not None:
        variant["phone_beat_receipt"] = receipt
    return variant


def _title_req(literal: str) -> BriefRequirement:
    return BriefRequirement(id="t1", kind="text", scope="title", literal=literal)


def test_her_stacked_rows_meet_her_slash_title() -> None:
    facts = plan_facts_from_phone_variant(_variant())
    assert check_requirement(_title_req(_WANTED), facts).status == "met"


def test_one_changed_word_is_not_met() -> None:
    facts = plan_facts_from_phone_variant(
        _variant(("5 PROJECTS", "the AI builder group", "can't stop talking about!!!!"))
    )
    assert check_requirement(_title_req(_WANTED), facts).status != "met"


def _logo_req() -> BriefRequirement:
    return BriefRequirement(
        id="b1",
        scope="global",
        kind="style",
        description="Show the GitHub logo above the title",
        facts={"triggers": ["GitHub logo"]},
    )


def _receipt(placed=(), unplaced=()) -> dict[str, Any]:
    return {
        "version": 1,
        "matcher": "phrase",
        "placed": list(placed),
        "unplaced": list(unplaced),
        "closing": {"status": "none", "badge": "none"},
    }


def test_a_placed_title_beat_is_met_and_never_unheard() -> None:
    placed = {
        "beat_id": "gh",
        "trigger": "GitHub logo",
        "at_s": 0.0,
        "end_s": 3.0,
        "visual_label": "GitHub",
        "anchor": "title",
    }
    facts = plan_facts_from_phone_variant(_variant(receipt=_receipt(placed=[placed])))
    assert facts.unheard_beat_triggers == ()
    receipt = check_requirement(_logo_req(), facts)
    assert receipt.status == "met"
    assert "never heard" not in (receipt.reason or "").lower()


def test_an_unplaced_title_beat_is_not_an_unheard_word() -> None:
    miss = {"beat_id": "gh", "trigger": "GitHub logo", "reason": "no_room_above_title"}
    miss["anchor"] = "title"
    facts = plan_facts_from_phone_variant(_variant(receipt=_receipt(unplaced=[miss])))
    assert facts.unheard_beat_triggers == ()
    receipt = check_requirement(_logo_req(), facts)
    assert receipt.status != "met"
    reason = (receipt.reason or "").lower()
    assert "never heard" not in reason and "in your voice" not in reason
    assert "room above your title" in reason


def test_no_title_reason_is_plain() -> None:
    miss = {"beat_id": "gh", "trigger": "GitHub logo", "reason": "no_title", "anchor": "title"}
    facts = plan_facts_from_phone_variant(_variant(receipt=_receipt(unplaced=[miss])))
    reason = (check_requirement(_logo_req(), facts).reason or "").lower()
    assert "no opening title" in reason
    assert "never heard" not in reason


def test_miss_sentences_for_title_reasons() -> None:
    assert "never heard" not in beat_miss_sentence("GitHub logo", "no_title")
    assert "no opening title" in beat_miss_sentence("GitHub logo", "no_title")
    sentence = beat_miss_sentence("GitHub logo", "no_room_above_title")
    assert "room above your title" in sentence and "face" in sentence


def test_a_draft_title_beat_says_above_your_title() -> None:
    from app.kria.brief_checks import PlanFacts

    facts = PlanFacts(
        reaction_beats_available=True,
        reaction_beats=(BeatFact(trigger="GitHub logo", visual_id="GitHub", anchor="title"),),
    )
    reason = check_requirement(_logo_req(), facts).reason or ""
    assert "above your title" in reason
    assert "when you say" not in reason
