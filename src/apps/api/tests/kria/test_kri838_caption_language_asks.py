"""KRI-838: how the brief side reads a caption-language ask, and what it promises.

Failure modes:
 1. "make the subtitles Spanish" / "subtitles should be Spanish" (no preposition) was missed by
    every brief regex, so the draft said "I can't check the captions on this draft yet".
 2. A language named as the audience or the voice ("subtitles for Spanish speakers") must not
    become the captions' language.
 3. Every language of the shared table is a language the brief side can ask for, in both
    languages' wording (names come from ONE table now).
 4. The record-based Voiceover receipt (`_check_caption_language`) reads the cue text when the
    facts carry it, so a stale `tr` label over English lines is not "met".
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    _asked_caption_language,
    _check_caption_language,
    _language_name,
    _requested_caption_language,
    asks_caption_language,
)
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.pipeline.caption_language import LANGUAGE_NAMES


def _req(description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="c", kind="style", scope="global", description=description, facts=dict(facts)
    )


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        ("make the subtitles Spanish", "es"),
        ("subtitles Spanish", "es"),
        ("Make the captions Dutch please", "nl"),
        ("subtitles should be Spanish", "es"),
        ("the captions are German", "de"),
        ("Spanish subtitles", "es"),
        ("subtitles in Spanish", "es"),
        ("translate the subtitles to Portuguese", "pt"),
        ("İspanyolca altyazı", "es"),
        ("altyazılar İspanyolca olsun", "es"),
        ("altyazılar Fransızca", "fr"),
        ("English subtitles for my Spanish speakers", "en"),
        # the bare "captions <language>" is the ask only when the language closes it
        ("make the subtitles Spanish.", "es"),
        ("subtitles Spanish, please", "es"),
        ("subtitles Spanish and make them yellow", "es"),
        ("subtitles Italian restaurant style", None),
        ("captions Spanish food intro", None),
        ("subtitles French toast scene", None),
        ("captions Dutch oven", None),
        ("captions Turkish coffee scene", None),
        # a language that names the audience or the voice is not the captions' language
        ("subtitles Spanish speakers will enjoy", None),
        ("subtitles for Spanish speakers", None),
        ("keep his English voice", None),
        ("Spanish title: Las salsas", None),
        ("captions", None),
    ],
)
def test_the_brief_reads_the_language_of_a_caption_ask(wording, expected):
    assert _asked_caption_language(_req(wording)) == expected


def test_a_bare_wording_is_a_caption_language_ask():
    assert asks_caption_language(_req("make the subtitles Spanish"))
    assert _requested_caption_language(_req("make the subtitles Spanish")) == "es"


def test_the_structured_fact_still_wins_over_the_wording():
    assert _asked_caption_language(_req("subtitles", caption_language="es")) == "es"
    assert _asked_caption_language(_req("subtitles", caption_language="Türkçe")) == "tr"
    assert _asked_caption_language(_req("subtitles", caption_language="pt-BR")) == "pt"


@pytest.mark.parametrize(("code", "names"), sorted(LANGUAGE_NAMES.items()))
def test_every_table_language_has_an_english_and_a_turkish_name(code, names):
    english, turkish = names
    assert _language_name(code) == english
    token = bind_reply_language("tr")
    try:
        assert _language_name(code) == turkish
    finally:
        release_reply_language(token)
    assert english and turkish


def test_the_record_receipt_reads_the_cue_text_when_it_has_it():
    english_lines = (
        "Hi, I'm Carolina from Ecuador and this is the traditional dessert.",
        "It's a waffle cookie with cheese and whipped cream.",
    )
    facts = PlanFacts(caption_language="tr", caption_texts=english_lines)
    receipt = _check_caption_language(_req("Turkish subtitles"), facts, "tr")
    assert receipt.status == "partial"
    assert receipt.reason == "The captions are in English, not Turkish."

    # no cue text on the record (the compile-time Voiceover receipt): the stamp stands, as before
    stamped_only = _check_caption_language(
        _req("Turkish subtitles"), PlanFacts(caption_language="tr", spoken_language="en"), "tr"
    )
    assert stamped_only.status == "met"
    assert (
        stamped_only.reason == "The captions are in Turkish, translated from the English voiceover."
    )
