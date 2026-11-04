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


TIGHT = [
    "Cut out all my pauses so it's tight",
    "remove every pause",
    "cut each pause, I want it snappy",
    "remove the pauses and make it fast paced",
    "cut the silences, no dead air",
    "get rid of all the pauses",
    "zero pauses please, cut them",
    "cut no pauses, make it tighter",
    "tüm duraklamaları kes",
    "her boşluğu kes, sessizlikleri çıkar",
    "hiç boşluk kalmasın, sessizlikleri kes",
    "duraklamaları kes, hızlı tempo olsun",
    "Tüm sessizlikleri kes",
]
# The cleanup gate's noun list has no "gap"/"boşluk"/"duraksama": these need the recorded
# `speech_cleanup` fact (the model records it, KRI-667); the words then decide "every".
TIGHT_WITH_FACT = [
    "remove the gaps, no gaps between every sentence",
    "bütün boşlukları kes, seri olsun",
    "her duraksamayı çıkar",
]
CLEANUP_ONLY = [
    "cut the long pauses",
    "remove the ums and uhs",
    "cut the pauses",
    "uzun duraklamaları kes",
]
NOT_TIGHT = [
    "keep my pauses",
    "don't remove the pauses, keep it tight",
    "tight shot on my face",
    "keep it tight on my face",
    "tight framing please",
    "fast cuts between clips",
    "make the captions snappy",
    "duraklamalar kalsın",
    "cut out her long pauses",
    "remove her pauses",
    "cut her gaps",
    # "all her pauses": only "all/every + my/the/..." counts; "her" alone never makes it tight.
    "remove all her pauses",
]


@pytest.mark.parametrize("text", TIGHT)
def test_tight_asks_are_recognised(text):
    req = _req(text)
    assert wants_speech_cleanup(req), text
    assert wants_tight_pauses(req), text


@pytest.mark.parametrize("text", TIGHT_WITH_FACT)
def test_tight_asks_with_recorded_cleanup(text):
    req = _req(text, speech_cleanup=True)
    assert wants_tight_pauses(req), text


@pytest.mark.parametrize("text", CLEANUP_ONLY)
def test_plain_cleanup_is_not_tight(text):
    req = _req(text)
    assert wants_speech_cleanup(req), text
    assert not wants_tight_pauses(req), text


@pytest.mark.parametrize("text", NOT_TIGHT)
def test_non_asks_do_not_fire(text):
    assert not wants_tight_pauses(_req(text)), text


def test_recorded_fact_decides():
    req = _req("make it feel quick", speech_cleanup=True, pauses="tight")
    assert wants_tight_pauses(req)
    # A recorded tight pace with no cleanup consent in the ask is not a cleanup ask.
    assert not wants_tight_pauses(_req("make it feel quick", pauses="tight"))
    # A recorded "no cleanup" beats words.
    assert not wants_tight_pauses(_req("cut all my pauses", speech_cleanup=False, pauses="tight"))


def test_brief_level_helpers():
    plain = _req("cut the long pauses")
    tight = _req("cut every pause")
    brief = CreativeBrief(requirements=[plain])
    assert brief_wants_speech_cleanup(brief)
    assert not brief_tight_pauses(brief)
    both = CreativeBrief(requirements=[plain, tight.model_copy(update={"id": "t"})])
    assert brief_tight_pauses(both)
    gone = tight.model_copy(update={"id": "g", "status": "superseded"})
    assert not brief_tight_pauses(CreativeBrief(requirements=[plain, gone]))
