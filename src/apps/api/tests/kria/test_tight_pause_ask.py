"""Ways the "cut every pause so it's tight" recognizer can fail (Qendresa 13).

1. False negative: "Cut out all my pauses so it's tight", "every gap", "no dead air", the
   Turkish forms ("tüm boşlukları kes", "boşluksuz", "seri olsun") stay long-pause-only.
2. False positive: "keep my pauses", "tight shot", "keep it tight on my face", "fast cuts
   between clips" start promising 0.3 s pauses.
3. A plain "cut the long pauses" / "remove the ums" is a cleanup ask but NOT a tight ask.
4. A recorded fact (``facts["pauses"] == "tight"``) must decide over the words, but only on
   a cleanup requirement.
5. A withdrawn (superseded) requirement still counts for the brief-level helpers.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.tight_pause_ask import (
    brief_tight_pauses,
    brief_wants_speech_cleanup,
    wants_speech_cleanup,
    wants_tight_pauses,
)


def _req(text: str, **facts) -> BriefRequirement:
    return BriefRequirement(id="r", kind="audio", scope="global", description=text, facts=facts)


CLEANUP_ONLY = [
    "cut the long pauses",
    "remove the ums and uhs",
    "uzun duraklamaları kes",
    # Words never decide (KRI-667): a tight-sounding ask without the recorded fact is plain cleanup.
    "Cut out all my pauses so it's tight",
    "bütün boşlukları kes, sıkı olsun",
    "cut out her long pauses",
]
TIGHT_FACT = {"speech_cleanup": True, "pauses": "tight"}


def test_recorded_tight_fact_is_tight():
    for text in ("Cut out all my pauses so it's tight", "bütün boşlukları kes, sıkı olsun", "x"):
        req = _req(text, **TIGHT_FACT)
        assert wants_speech_cleanup(req)
        assert wants_tight_pauses(req)
    assert wants_tight_pauses(_req("x", speech_cleanup=True, pauses=" Tight "))


@pytest.mark.parametrize("pauses", ["long", "", None, True, 1, ["tight"], "natural"])
def test_other_pause_facts_are_not_tight(pauses):
    assert not wants_tight_pauses(_req("cut all my pauses", speech_cleanup=True, pauses=pauses))


def test_keep_my_pauses_is_not_tight_even_with_the_fact():
    assert not wants_tight_pauses(_req("keep my pauses", speech_cleanup=False, pauses="tight"))
    assert not wants_tight_pauses(_req("keep my pauses", pauses="tight"))


def test_tight_fact_needs_a_cleanup_ask():
    assert not wants_tight_pauses(_req("make it feel quick", pauses="tight"))


@pytest.mark.parametrize("text", CLEANUP_ONLY)
def test_plain_cleanup_is_not_tight(text):
    req = _req(text, speech_cleanup=True)
    assert wants_speech_cleanup(req), text
    assert not wants_tight_pauses(req), text


def test_brief_level_helpers():
    plain = _req("cut the long pauses", speech_cleanup=True)
    tight = _req("cut every pause", **TIGHT_FACT)
    brief = CreativeBrief(requirements=[plain])
    assert brief_wants_speech_cleanup(brief)
    assert not brief_tight_pauses(brief)
    both = CreativeBrief(requirements=[plain, tight.model_copy(update={"id": "t"})])
    assert brief_tight_pauses(both)
    gone = tight.model_copy(update={"id": "g", "status": "superseded"})
    assert not brief_tight_pauses(CreativeBrief(requirements=[plain, gone]))


def test_requested_in_chat_is_fact_based():
    # The word fallback would read "no need to cut the pauses" as a cleanup ask.
    assert not wants_speech_cleanup(_req("No need to cut the pauses"))
    assert not brief_wants_speech_cleanup(
        CreativeBrief(requirements=[_req("No need to cut the pauses")])
    )
    assert wants_speech_cleanup(_req("whatever", speech_cleanup=True))
    assert not wants_speech_cleanup(_req("cut the pauses", speech_cleanup=False))
