"""KRI-1501: a chat ask for two caption lines (spoken + a smaller translated line) is read.

Contract: docs/pipelines/dual-captions.md ("How a creator turns it on"). The real parser
(`caption_language_decision`) is run on the creator's own words; nothing is mocked.

Ways this can go wrong, written down before the code:

 1. A two-language ask is read as a single caption language ("Turkish captions with English
    under it" -> target "tr", or worse "en", and the whole video is translated).
 2. The languages come back in the wrong order: the line bound to "under" / "darunter" /
    "altına" must come first, so the worker translates INTO it.
 3. Only English wordings work; a Turkish or German creator's ask is silently dropped.
 4. German language names that inflect ("englische", "Englischen") or the German caption word
    ("Untertitel", "Untertiteln") are missed, so German single-language asks stay unread.
 5. KRI-1015 regressions: "English captions for the English, Turkish captions for my Turkish"
    and "subtitles in English and Turkish" (no "under" marker) stop being keep_spoken, or a plain
    "captions in English" becomes a two-language ask.
 6. A dual decision still carries a target or keep_spoken, so the worker translates the spoken
    line itself.
 7. "no translation" / "remove the English line" after a dual ask leaves the line on.
 8. A later single-language message does not correct an earlier dual ask (or vice versa).
 9. A marker that is not about captions ("under 30 seconds") turns a normal ask into two lines.
10. `resolve_dual_translation_language` returns the spoken language itself, a language outside
    the four, or a line for a spoken language the feature does not cover.
"""

from __future__ import annotations

import pytest

from app.pipeline.caption_language import (
    CaptionLanguageDecision,
    caption_language_decision,
    parse_caption_language_request,
    resolve_dual_translation_language,
)

# -- 1, 2, 3, 6: two-language asks in EN / TR / DE, for all four languages ----------------------


@pytest.mark.parametrize(
    ("text", "languages"),
    [
        # English
        ("Turkish captions with English under it", ("en", "tr")),
        ("add an English translation under the captions", ("en",)),
        ("English captions with a German translation underneath", ("de", "en")),
        ("captions in Portuguese, and English below them", ("en", "pt")),
        ("put a Portuguese line under the subtitles", ("pt",)),
        ("bilingual captions, Turkish and English", ("tr", "en")),
        ("dual-language subtitles in German and Portuguese", ("de", "pt")),
        ("captions in two languages: English and Turkish", ("en", "tr")),
        ("subtitles with a second line in German", ("de",)),
        ("translate the captions into English underneath", ("en",)),
        # Turkish
        ("altyazının altına İngilizce çeviri", ("en",)),
        ("Türkçe altyazı, altında İngilizcesi olsun", ("en", "tr")),
        ("altyazıların altında Almanca çevirisi olsun", ("de",)),
        ("altyazı ve altına Portekizce çevirisi", ("pt",)),
        ("iki dilli altyazı: Türkçe ve İngilizce", ("tr", "en")),
        ("çift dilli altyazı istiyorum, Almanca ve Türkçe", ("de", "tr")),
        # German
        ("zweisprachige Untertitel, Englisch darunter", ("en",)),
        ("deutsche Untertitel mit englischer Übersetzung darunter", ("en", "de")),
        ("Untertitel auf Deutsch, darunter auf Türkisch", ("tr", "de")),
        ("Untertitel mit portugiesischer Übersetzung", ("pt",)),
        ("unter den Untertiteln bitte eine englische Zeile", ("en",)),
    ],
)
def test_a_two_language_ask_names_its_languages_line_first(text, languages):
    decision = caption_language_decision(text)
    assert decision == CaptionLanguageDecision(translation_languages=languages), text
    # 6: a dual decision never translates the spoken line itself.
    assert decision.target is None
    assert decision.keep_spoken is False
    assert parse_caption_language_request(text) is None


# -- 5: KRI-1015 and single-language asks stay byte-identical -----------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "English captions for the English, Turkish captions for my Turkish",
            CaptionLanguageDecision(keep_spoken=True),
        ),
        ("subtitles in English and Turkish", CaptionLanguageDecision(keep_spoken=True)),
        ("Spanish and English subtitles", CaptionLanguageDecision(keep_spoken=True)),
        ("captions in English", CaptionLanguageDecision(target="en")),
        ("altyazılar Türkçe olsun", CaptionLanguageDecision(target="tr")),
        ("English subtitles translated from the Turkish voiceover", CaptionLanguageDecision("en")),
        ("translate the subtitles into Spanish", CaptionLanguageDecision(target="es")),
        ("altyazıyı İngilizceye çevir", CaptionLanguageDecision(target="en")),
        ("add captions", CaptionLanguageDecision()),
        ("no captions", CaptionLanguageDecision()),
        # 9: "under" that is not about a caption line
        ("English captions, keep it under 30 seconds", CaptionLanguageDecision(target="en")),
        ("captions in English, video under a minute", CaptionLanguageDecision(target="en")),
        # 9: "under" that says where the (single-language) captions go
        ("captions in English below the title", CaptionLanguageDecision(target="en")),
        ("put the captions below my chin in English", CaptionLanguageDecision(target="en")),
        ("add captions under the title in Turkish", CaptionLanguageDecision(target="tr")),
        ("English captions below the face", CaptionLanguageDecision(target="en")),
        ("Spanish food under 10 dollars with captions", CaptionLanguageDecision()),
    ],
)
def test_non_dual_asks_are_unchanged(text, expected):
    assert caption_language_decision(text) == expected, text


# -- 4: German single-language asks (new: they used to parse as nothing) -------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("englische Untertitel bitte", "en"),
        ("Untertitel auf Englisch", "en"),
        ("die Untertitel ins Englische übersetzen", "en"),
        ("deutsche Untertitel", "de"),
        ("Untertitel auf Türkisch", "tr"),
        ("portugiesische Untertitel", "pt"),
        ("Ich spreche Türkisch, aber die Untertitel auf Englisch", "en"),
    ],
)
def test_german_single_language_asks_parse(text, code):
    assert caption_language_decision(text) == CaptionLanguageDecision(target=code), text


@pytest.mark.parametrize(
    "text",
    [
        # KRI-1015 parity (D37c): German elides the noun in the second half.
        "Englische Untertitel für die englischen Teile, türkische für die türkischen",
        "English captions for the English parts, Turkish for the Turkish",
    ],
)
def test_an_elided_mixed_ask_keeps_each_language_as_spoken(text):
    assert caption_language_decision(text) == CaptionLanguageDecision(keep_spoken=True)


def test_german_negated_caption_ask_requests_nothing():
    assert caption_language_decision("keine englischen Untertitel") == CaptionLanguageDecision()


# -- 7, 8: negation and later-message correction -------------------------------------------------


@pytest.mark.parametrize(
    "later",
    [
        "no translation",
        "remove the English line",
        "actually, no English translation under the captions",
        "çeviri olmasın",
        "İngilizce satırı kaldır",
        "keine Übersetzung",
        "ohne die englische Zeile",
    ],
)
def test_a_later_negation_clears_the_dual_ask(later):
    text = f"Turkish captions with English under it\n{later}"
    assert caption_language_decision(text) == CaptionLanguageDecision()


def test_a_negated_dual_sentence_is_no_dual_ask():
    assert caption_language_decision("captions please, no English translation under them") == (
        CaptionLanguageDecision()
    )


def test_a_negation_with_no_dual_ask_changes_nothing():
    # Byte-identical to before: "don't translate" after a single-language ask keeps it.
    assert caption_language_decision("Turkish captions\ndon't translate them") == (
        CaptionLanguageDecision(target="tr")
    )


def test_a_later_single_language_message_corrects_a_dual_ask():
    text = "Turkish captions with English under it\nactually just captions in English"
    assert caption_language_decision(text) == CaptionLanguageDecision(target="en")


def test_a_later_dual_ask_corrects_a_single_language_one():
    text = "captions in English\nwait, Turkish captions with English under it"
    assert caption_language_decision(text) == CaptionLanguageDecision(
        translation_languages=("en", "tr")
    )


def test_an_unrelated_later_message_keeps_the_dual_ask():
    text = "Turkish captions with English under it\nmake the captions bigger"
    assert caption_language_decision(text).translation_languages == ("en", "tr")


def test_a_marker_with_no_language_is_no_dual_ask():
    assert caption_language_decision("bilingual captions please") == CaptionLanguageDecision()


# -- 10: resolving the translated line once the spoken language is known -----------------------


@pytest.mark.parametrize(
    ("requested", "spoken", "line"),
    [
        (("en", "tr"), "tr", "en"),
        # spoken == the line asked for: the next named language, never the spoken one
        (("en", "tr"), "en", "tr"),
        (("en",), "en", None),
        (("de",), "tr", "de"),
        (("pt",), "de", "pt"),
        # a requested language outside the four is skipped
        (("es", "en"), "tr", "en"),
        (("es",), "tr", None),
        # a spoken language outside the four: no line at all
        (("en",), "es", None),
        (("en",), None, None),
        (("en",), "", None),
        # spoken spellings are normalised ("tr-TR", "TR")
        (("en",), "tr-TR", "en"),
        (("EN",), "TR", "en"),
        ((), "tr", None),
    ],
)
def test_resolve_dual_translation_language(requested, spoken, line):
    assert resolve_dual_translation_language(requested, spoken) == line


def test_the_four_languages_make_twelve_pairs():
    four = ("en", "de", "tr", "pt")
    pairs = {
        (spoken, line)
        for spoken in four
        for line in four
        if resolve_dual_translation_language((line,), spoken) == line
    }
    assert len(pairs) == 12
