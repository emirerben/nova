"""KRI-884: a creator's words print from the place the intent quoted, not the first match.

"Salsa + chips" for clip 1 and "salsa & chips" for clip 2 are the same words. The span
search took the first place the words appear in the whole request, so clip 2 could print
clip 1's version.

Ways this can fail, written down before the code:

 1. A label whose value has no symbols prints the first occurrence for every clip.
 2. A caption (no stored-casing preference) prints the first occurrence's casing.
 3. The intent's quote is lost between the planner and the resolver, so nothing anchors.
 4. A quote that is not in the request (an older stored intent, a stale chat) breaks
    grounding instead of falling back to the first match.
 5. A quote that holds the words twice, or sits in a later message, picks the wrong one.
 6. Intents stored before this change re-dump differently.
"""

from __future__ import annotations

import pytest

from app.schemas.clip_intents import (
    ClipIntent,
    ClipUnderstanding,
    creator_literal_span,
    ground_caption,
    ground_label,
)
from tests.kria.test_kri804_label_text_reply import BOWLS, PLUS, _plan, _prompt, _row

REQUEST = "Put Salsa + chips on the first clip. Put salsa & chips on the second clip."
FIRST_QUOTE = "Put Salsa + chips on the first clip"
SECOND_QUOTE = "Put salsa & chips on the second clip"
RECORD = ClipUnderstanding(subject="two bowls of salsa and chips on a table")


def _label(value: str, quote: str | None, request: str = REQUEST):
    return ground_label(
        media_id="m1",
        value=value,
        confidence=1.0,
        creator_request=request,
        record=RECORD,
        source_quote=quote,
    )


@pytest.mark.parametrize(
    ("quote", "printed"),
    [(FIRST_QUOTE, "Salsa + chips"), (SECOND_QUOTE, "salsa & chips"), (None, "Salsa + chips")],
)
def test_a_label_prints_the_version_its_own_quote_holds(quote, printed) -> None:
    label = _label("salsa chips", quote)
    assert label is not None and label.text == printed


@pytest.mark.parametrize(
    ("quote", "printed"),
    [(FIRST_QUOTE, "Salsa + chips"), (None, "Salsa + chips"), (SECOND_QUOTE, "salsa + chips")],
)
def test_a_caption_prints_the_casing_its_own_quote_holds(quote, printed) -> None:
    request = "Put Salsa + chips on the first clip. Put salsa + chips on the second clip."
    caption = ground_caption(
        value="salsa + chips",
        confidence=1.0,
        creator_request=request,
        records=[RECORD],
        creator_copy=True,
        source_quote=(quote.replace("& ", "+ ") if quote else None),
    )
    assert caption is not None and caption.text == printed


def test_a_quote_that_is_not_in_the_request_falls_back_to_the_first_match() -> None:
    label = _label("salsa chips", "Put salsa & chips on the THIRD clip")
    assert label is not None and label.text == "Salsa + chips"


def test_a_quote_with_smart_punctuation_still_anchors() -> None:
    request = "Put Salsa + chips on the first clip. Put salsa & chips on Dad’s clip."
    label = _label("salsa chips", "Put salsa & chips on Dad's clip", request)
    assert label is not None and label.text == "salsa & chips"


def test_the_match_inside_the_quote_wins_over_an_earlier_one() -> None:
    request = "first Salsa + chips, then a long gap of other words, then salsa & chips"
    assert creator_literal_span(request, "salsa chips", near="then salsa & chips") == (
        "salsa & chips"
    )


@pytest.mark.asyncio
async def test_the_planner_quote_survives_onto_the_resolved_intent(monkeypatch) -> None:
    _models, planned = await _plan(monkeypatch, [_row("label", PLUS)], messages=[_prompt(PLUS)])
    (label,) = planned.resolution.intents
    assert label.source_quote == f'Put "{PLUS}" on the sauce bowls'
    assert [a.media_id for a in label.assignments] == [BOWLS]


def test_intents_stored_without_a_quote_redump_byte_identically() -> None:
    row = {"intent_id": "text_bowls", "op": "label", "attribute": "the sauce bowls"}
    intent = ClipIntent.model_validate(row)
    assert intent.source_quote is None
    assert "source_quote" not in intent.model_dump(mode="json")
