"""KRI-1015: the review of a render whose captions were TRANSLATED says so.

Incident (Plaza T2, maximo 2): a phone Talking render captioned an English speaker in
Turkish because a caption language ("tr") was requested by mistake. The creator's real ask was
"Keep every language as it was spoken with no translation". The post-render review never
admitted what happened: that requirement mentions "language", so its captions check returned
the neutral "can't check", which the reply listed under "Have a look at these in the video".
A creator who told us not to translate was shown a translated video and no sentence said so.

The worker now stamps `caption_translated_from` (the spoken language the captions were
translated out of) on the variant, only when it did translate. The review reads it.

Failure modes, written before the code (each is a test below):

 1. A captions requirement that names no language ("keep every language as it was spoken")
    on translated captions is unchecked -> "Have a look", no mention of the translation.
    Now: a JUDGED "partly" receipt, "I translated the English speech into Turkish captions.",
    in English and Turkish, listed under "Partly", never under "Have a look".
 2. The same requirement on captions that were NOT translated must not start claiming
    anything (no `caption_translated_from`): it stays as it was.
 3. A language ask the render met by translating (Turkish captions asked, English spoken) is
    "met" but must say where the words came from ("translated from the English speech"), in
    both judges of a language ask (the rendered-lines judge and the plan-facts judge).
 4. A plain "add captions" is held to the spoken language too: translated captions are partly.
 5. A stale or odd stamp must never produce a false claim: a stamp equal to the rendered
    language, a stamp with no rendered language, or captions the creator turned off.
 6. The Voiceover wording ("translated from the Turkish voiceover") is unchanged when the
    spoken language is the voiceover's.
 7. A requirement that captions TWO languages each as itself ("English captions for the English
    parts and Turkish captions for the Turkish parts") was read by the first-match regexes as an
    English ask: the draft promised English captions and the review judged the render against
    English. It now asks for no language (`caption_language_decision(...).keep_spoken`); a
    recorded `caption_language` fact still decides; its review is unchecked ("Have a look")
    when the captions were not translated and "partly, I translated ..." when they were.
 8. A plain "Turkish captions" requirement is judged exactly as before.
 9. A plain "add captions" next to a separate LIVE "Turkish captions" requirement must NOT say
    "I translated the English speech into Turkish captions" as a miss: the translation was
    asked for, and the language row owns saying it ("translated from the English speech").
    With that language requirement superseded, or absent (the T2 incident), it still does.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    _asked_caption_language,
    _requested_caption_language,
    asks_caption_language,
    build_receipts,
    check_requirement,
    plan_facts_from_phone_variant,
    reply_from_receipts,
)
from app.kria.reply_language import bind_reply_language, release_reply_language

_TURKISH_CUES = [
    {
        "text": "Merhaba, ben Ekvador'dan Carolina ve bu Oblea de la Casa.",
        "start_s": 0.0,
        "end_s": 4.0,
    },
    {"text": "Geleneksel bir Kolombiya tatlısı.", "start_s": 4.0, "end_s": 6.0},
]
_ENGLISH_CUES = [
    {
        "text": "Hi, I'm Carolina from Ecuador and this is the Oblea de la Casa.",
        "start_s": 0.0,
        "end_s": 4.0,
    },
    {"text": "It's a traditional Colombian dessert.", "start_s": 4.0, "end_s": 6.0},
]
KEEP_SPOKEN = "Keep every language as it was spoken with no translation"


def _variant(language: str, cues: list[dict], **extra) -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_language": language,
        "voiceover_caption_style": "sentence",
        "caption_cues": cues,
        **extra,
    }


def _req(description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r2", kind="style", scope="global", description=description, facts=dict(facts)
    )


def _review(variant: dict, description: str, **facts):
    [receipt] = build_receipts(
        [_req(description, **facts)],
        plan_facts_from_phone_variant(variant),
        include_unchecked=True,
    )
    return receipt


def _in_turkish(call):
    token = bind_reply_language("tr")
    try:
        return call()
    finally:
        release_reply_language(token)


# ------------------------------------------------- [1] the incident: "keep every language"


def test_1_translated_captions_are_admitted_when_the_creator_said_not_to_translate():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    receipt = _review(variant, KEEP_SPOKEN, captions="on")

    assert (receipt.status, receipt.verification) == ("partial", "checked")  # judged, not unchecked
    assert receipt.reason == "I translated the English speech into Turkish captions."


def test_1_the_same_receipt_in_a_turkish_chat():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    receipt = _in_turkish(lambda: _review(variant, KEEP_SPOKEN, captions="on"))

    assert receipt.status == "partial"
    assert receipt.reason == "İngilizce konuşmayı Türkçe altyazıya çevirdim."


def test_1_the_reply_lists_it_under_partly_never_under_have_a_look():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")
    req = _req(KEEP_SPOKEN, captions="on")
    facts = plan_facts_from_phone_variant(variant)
    brief = CreativeBrief(version=1, requirements=[req])

    def reply_of() -> str:
        # receipts are built in the chat's language, then the reply is composed from them
        receipts = build_receipts([req], facts, include_unchecked=True)
        return reply_from_receipts(brief, receipts)

    reply = reply_of()
    reply_tr = _in_turkish(reply_of)

    assert "Partly: " in reply
    assert "I translated the English speech into Turkish captions" in reply
    assert "Have a look" not in reply
    assert "Kısmen: " in reply_tr
    assert "İngilizce konuşmayı Türkçe altyazıya çevirdim" in reply_tr
    assert "göz at" not in reply_tr


# ----------------------------------------------- [2] captions that were not translated


def test_2_untranslated_captions_keep_the_old_answer():
    variant = _variant("en", _ENGLISH_CUES)

    receipt = _review(variant, KEEP_SPOKEN, captions="on")

    assert "translat" not in (receipt.reason or "").lower()
    assert (receipt.status, receipt.verification) == ("partial", "unchecked")  # as before


# ----------------------------------------------------- [3] a language ask met by translating


def test_3_a_met_language_ask_says_the_captions_were_translated():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    receipt = _review(variant, "Turkish captions", captions="on", caption_language="tr")

    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == "The captions are in Turkish, translated from the English speech."


def test_3_the_same_in_turkish():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    receipt = _in_turkish(
        lambda: _review(variant, "Turkish captions", captions="on", caption_language="tr")
    )

    assert receipt.status == "met"
    assert receipt.reason == "Altyazılar Türkçe, İngilizce konuşmadan çevrildi."


def test_3_a_met_language_ask_without_a_translation_stays_plain():
    variant = _variant("tr", _TURKISH_CUES)

    receipt = _review(variant, "Turkish captions", captions="on", caption_language="tr")

    assert receipt.status == "met"
    assert receipt.reason == "The captions are in Turkish."


def test_3_the_plan_facts_judge_says_it_too():
    """`_check_caption_language` (plan facts that are not an editor payload) shares the wording."""
    facts = PlanFacts(caption_language="tr", caption_translated_from="en")

    receipt = check_requirement(
        _req("Turkish captions", captions="on", caption_language="tr"), facts
    )

    assert receipt.status == "met"
    assert receipt.reason == "The captions are in Turkish, translated from the English speech."


# --------------------------------------------------------------- [4] a plain captions ask


@pytest.mark.parametrize("description", ["Add captions", "Add captions to the video"])
def test_4_a_plain_captions_ask_is_held_to_the_spoken_language(description):
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    receipt = _review(variant, description, captions="on")

    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == "I translated the English speech into Turkish captions."


def test_4_a_plain_captions_ask_on_untranslated_captions_is_met():
    receipt = _review(_variant("en", _ENGLISH_CUES), "Add captions", captions="on")

    assert receipt.status == "met"


# ----------------------------------------------------- [5] a stale stamp never claims a thing


def test_5_a_stamp_equal_to_the_rendered_language_claims_nothing():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="tr")

    receipt = _review(variant, KEEP_SPOKEN, captions="on")

    assert "translated the" not in (receipt.reason or "")


def test_5_a_stamp_without_a_rendered_language_claims_nothing():
    variant = _variant("", _TURKISH_CUES, caption_translated_from="en")

    receipt = _review(variant, KEEP_SPOKEN, captions="on")

    assert "translated the" not in (receipt.reason or "")


def test_5_captions_the_creator_turned_off_claim_nothing():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en", captions_enabled=False)

    facts = plan_facts_from_phone_variant(variant)

    assert facts.caption_translated_from is None


def test_5_the_stamp_is_read_as_a_trimmed_lower_case_code():
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from=" EN ")

    assert plan_facts_from_phone_variant(variant).caption_translated_from == "en"
    assert (
        plan_facts_from_phone_variant(_variant("tr", _TURKISH_CUES)).caption_translated_from is None
    )
    assert (
        plan_facts_from_phone_variant(
            _variant("tr", _TURKISH_CUES, caption_translated_from=7)
        ).caption_translated_from
        is None
    )


# ------------------------------------------------------- [6] the Voiceover wording is unchanged


def test_6_a_voiceover_translation_keeps_its_own_wording():
    facts = dataclasses.replace(
        PlanFacts(caption_language="en", spoken_language="tr"), caption_translated_from="tr"
    )

    receipt = check_requirement(
        _req("English captions", captions="on", caption_language="en"), facts
    )

    assert receipt.status == "met"
    assert receipt.reason == "The captions are in English, translated from the Turkish voiceover."


# --------------------------------- [7] two languages, each captioned as itself ("keep spoken")

TWO_LANGUAGES = {
    "en": "English captions for the English parts and Turkish captions for the Turkish parts",
    "tr": "İngilizce kısımlara İngilizce altyazı, Türkçe kısımlara Türkçe altyazı",
}


@pytest.mark.parametrize("description", list(TWO_LANGUAGES.values()), ids=list(TWO_LANGUAGES))
def test_7_a_requirement_that_captions_each_language_as_itself_asks_for_no_language(description):
    req = _req(description, captions="on")

    assert asks_caption_language(req) is False
    assert _requested_caption_language(req) is None
    assert _asked_caption_language(req) is None


def test_7_a_recorded_caption_language_fact_still_decides():
    req = _req(TWO_LANGUAGES["en"], captions="on", caption_language="tr")

    assert _requested_caption_language(req) == "tr"
    assert _asked_caption_language(req) == "tr"
    assert asks_caption_language(req) is True


@pytest.mark.parametrize("language,cues", [("en", _ENGLISH_CUES), ("tr", _TURKISH_CUES)])
@pytest.mark.parametrize("description", list(TWO_LANGUAGES.values()), ids=list(TWO_LANGUAGES))
def test_7_untranslated_captions_of_that_requirement_are_unchecked_never_met(
    description, language, cues
):
    receipt = _review(_variant(language, cues), description, captions="on")

    assert (receipt.status, receipt.verification) == ("partial", "unchecked")
    assert receipt.reason == "I can't check the captions on this draft yet."
    assert "translat" not in receipt.reason


def test_7_that_requirement_is_listed_under_have_a_look_in_the_reply():
    req = _req(TWO_LANGUAGES["en"], captions="on")
    variant = _variant("en", _ENGLISH_CUES)
    receipts = build_receipts([req], plan_facts_from_phone_variant(variant), include_unchecked=True)

    reply = reply_from_receipts(CreativeBrief(version=1, requirements=[req]), receipts)

    assert "Have a look" in reply
    assert "Partly" not in reply and "Done" not in reply


@pytest.mark.parametrize("description", list(TWO_LANGUAGES.values()), ids=list(TWO_LANGUAGES))
def test_7_translated_captions_of_that_requirement_get_the_translated_row(description):
    variant = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    receipt = _review(variant, description, captions="on")

    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == "I translated the English speech into Turkish captions."
    assert _in_turkish(lambda: _review(variant, description, captions="on")).reason == (
        "İngilizce konuşmayı Türkçe altyazıya çevirdim."
    )


# ------------------------------------------------ [8] a plain "Turkish captions" is unchanged


def test_8_a_plain_turkish_captions_ask_is_judged_as_before():
    req = _req("Turkish captions", captions="on")
    assert asks_caption_language(req) is True
    assert _requested_caption_language(req) == "tr"

    met = _review(_variant("tr", _TURKISH_CUES), "Turkish captions", captions="on")
    miss = _review(_variant("en", _ENGLISH_CUES), "Turkish captions", captions="on")

    assert (met.status, met.reason) == ("met", "The captions are in Turkish.")
    assert (miss.status, miss.reason) == ("partial", "The captions are in English, not Turkish.")


# --------------- [9] a translation another requirement asked for is no miss on a plain ask


def _review_all(variant: dict, *reqs: BriefRequirement) -> dict:
    receipts = build_receipts(
        list(reqs), plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return {receipt.requirement_id: receipt for receipt in receipts}


def _plain() -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="style", scope="global", description="Add captions", facts={"captions": "on"}
    )


def _turkish_ask(**overrides) -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="style",
        scope="global",
        description="Turkish captions",
        facts={"captions": "on", "caption_language": "tr"},
        **overrides,
    )


def test_9a_a_live_language_ask_owns_the_translation_the_plain_ask_says_nothing():
    translated = _variant("tr", _TURKISH_CUES, caption_translated_from="en")
    before = _review_all(_variant("tr", _TURKISH_CUES), _plain(), _turkish_ask())

    rows = _review_all(translated, _plain(), _turkish_ask())

    assert "translated the" not in (rows["r1"].reason or "")
    assert (rows["r1"].status, rows["r1"].reason) == (before["r1"].status, before["r1"].reason)
    assert rows["r1"].status == "met"
    assert (rows["r2"].status, rows["r2"].verification) == ("met", "checked")
    assert rows["r2"].reason == "The captions are in Turkish, translated from the English speech."


def test_9a_the_order_of_the_requirements_does_not_matter():
    translated = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    rows = _review_all(translated, _turkish_ask(), _plain())

    assert rows["r1"].status == "met" and "translated the" not in (rows["r1"].reason or "")
    assert rows["r2"].reason == "The captions are in Turkish, translated from the English speech."


def test_9a_a_language_ask_for_another_language_does_not_silence_the_plain_ask():
    translated = _variant("tr", _TURKISH_CUES, caption_translated_from="en")
    english_ask = BriefRequirement(
        id="r2",
        kind="style",
        scope="global",
        description="English captions",
        facts={"captions": "on", "caption_language": "en"},
    )

    rows = _review_all(translated, _plain(), english_ask)

    assert rows["r1"].status == "partial"
    assert rows["r1"].reason == "I translated the English speech into Turkish captions."


def test_9b_a_superseded_language_ask_does_not_count():
    translated = _variant("tr", _TURKISH_CUES, caption_translated_from="en")

    rows = _review_all(translated, _plain(), _turkish_ask(status="superseded"))

    assert list(rows) == ["r1"]  # a superseded requirement gets no receipt
    assert (rows["r1"].status, rows["r1"].verification) == ("partial", "checked")
    assert rows["r1"].reason == "I translated the English speech into Turkish captions."


def test_9c_the_t2_requirement_alone_or_beside_unrelated_ones_still_says_it():
    translated = _variant("tr", _TURKISH_CUES, caption_translated_from="en")
    calm = BriefRequirement(
        id="r3", kind="style", scope="global", description="Make it calm", facts={}
    )

    alone = _review_all(translated, _req(KEEP_SPOKEN, captions="on"))
    beside = _review_all(translated, _req(KEEP_SPOKEN, captions="on"), calm)

    for rows in (alone, beside):
        assert (rows["r2"].status, rows["r2"].verification) == ("partial", "checked")
        assert rows["r2"].reason == "I translated the English speech into Turkish captions."
