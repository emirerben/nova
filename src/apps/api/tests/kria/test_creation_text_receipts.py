"""KRI-524: literal copy alone cannot settle independent text behavior."""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement


def _title(*, facts: dict[str, object] | None = None, description: str | None = None):
    return BriefRequirement(
        id="title",
        kind="text",
        scope="title",
        literal="Split the title text into words",
        description=description,
        facts=facts or {},
    )


def test_literal_title_with_typewriter_and_word_sequence_is_not_fully_met_by_copy() -> None:
    receipt = check_requirement(
        _title(
            facts={"animation": "typewriter"},
            description="animate each word one after another",
        ),
        PlanFacts(title="Split the title text into words", title_source="creator"),
    )
    assert receipt.status == "partial"
    assert receipt.reason == "I can't verify this one automatically yet."


def test_literal_title_with_position_is_not_fully_met_by_copy() -> None:
    receipt = check_requirement(
        _title(facts={"position": "bottom_left"}),
        PlanFacts(title="Split the title text into words", title_source="creator"),
    )
    assert receipt.status == "partial"
    assert receipt.reason == "I can't verify this one automatically yet."


def test_plain_literal_title_remains_met_when_copy_matches() -> None:
    receipt = check_requirement(
        _title(),
        PlanFacts(title="Split the title text into words", title_source="creator"),
    )
    assert receipt.status == "met"
