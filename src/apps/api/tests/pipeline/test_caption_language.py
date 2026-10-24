"""Tests for the KRI-177 caption-language policy helpers.

Default rule: talking-to-camera captions follow the SPOKEN language. The only
override is the creator explicitly asking for captions in another language, in
their own words — a prompt merely *written* in Turkish/English must never move
the caption language on its own.

Guards:
  1. parse_caption_language_request — grounded, deterministic explicit-request
     extraction (positive phrasings + negatives that must stay None).
  2. infer_language_from_text — conservative EN/TR guess from transcript text.
  3. resolve_spoken_caption_language — detected -> transcript text -> fallback,
     with the source always reported.
  4. crosscheck_detected_language — whisper's detection vs Gemini's transcript.
"""

from __future__ import annotations

import pytest

from app.pipeline.caption_language import (
    CAPTION_LANGUAGES,
    LANGUAGE_NAMES,
    SUPPORTED_CAPTION_LANGUAGES,
    can_deliver_caption_language,
    coerce_caption_language_request,
    crosscheck_detected_language,
    infer_language_from_text,
    parse_caption_language_request,
    resolve_spoken_caption_language,
)

# ── 1. parse_caption_language_request ───────────────────────────────────────

# Prod thread 2ef61a47 (Cappadocia, phone Narrated): the creator's message, and
# the same ask as Kria's pinned brief renders it into the dispatched request.
_CAPPADOCIA_MESSAGE = (
    "The voiceover is in Turkish, but I want the subtitles in English so my foreign "
    "followers understand."
)
_CAPPADOCIA_BRIEF_REQUEST = (
    "Creative brief v1 (everything the creator has asked for, still in force):\n"
    "- [r1] [text/global] English subtitles with exact place name spellings: Göreme, "
    "Paşabağ, Avanos, Kızılçukur\n"
    f"Latest message: {_CAPPADOCIA_MESSAGE}"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Positive: explicit request, in the creator's own words.
        ("Türkçe konuşuyorum ama altyazılar İngilizce olsun", "en"),
        ("make the captions English", "en"),
        ("captions in Turkish", "tr"),
        ("I speak Turkish but English subtitles", "en"),
        ("altyazıyı İngilizceye çevir", "en"),
        ("turkish captions translated into english", "en"),
        ("ALTYAZILAR TÜRKÇE OLSUN", "tr"),
        (_CAPPADOCIA_MESSAGE, "en"),
        (_CAPPADOCIA_BRIEF_REQUEST, "en"),
        # KRI-838: every language the brief side knows is a request the worker hears.
        ("make the subtitles Spanish", "es"),
        ("subtitles in Spanish", "es"),
        ("Spanish subtitles please", "es"),
        ("İspanyolca altyazı", "es"),
        ("altyazılar İspanyolca olsun", "es"),
        ("ISPANYOLCA ALTYAZI", "es"),
        ("ispanyolca altyazi", "es"),
        ("add German captions", "de"),
        ("Fransızca altyazı ekle", "fr"),
        ("captions in Portuguese", "pt"),
        ("sottotitoli: italian subtitles", "it"),
        ("make the captions Dutch", "nl"),
        ("Arapça altyazı olsun", "ar"),
        # Turkish dative of a back-vowel exonym names the target ("-ya" as well as "-ye").
        ("Almanca altyazıyı İspanyolcaya çevir", "es"),
        ("İspanyolca altyazıyı Almancaya çevir", "de"),
        ("altyazıyı İngilizce'ye çevir", "en"),
        ("Turkish subtitles translated into Spanish", "es"),
        # A language named as the voice or the audience is not the captions' language.
        ("keep his English voice and make the subtitles Spanish", "es"),
        ("English subtitles for my Spanish speakers", "en"),
        ("subtitles for the Turkish audience in English", "en"),
        ("İngilizce konuşuyor altyazılar Türkçe olsun", "tr"),
        # Bound by adjacency or a copula, in either language's wording.
        ("captions: Spanish", "es"),
        ("Spanish-language subtitles", "es"),
        ("İspanyolca bir altyazı olsun", "es"),
        ("subtitles should be Spanish", "es"),
        ("add Spanish subtitles to the video", "es"),
        ("I want the subtitles in English so my foreign followers understand", "en"),
        # The two real kit prompts (café T2 and Don Julio T3).
        (
            "Caro'nun konuştuğu klipten kameraya konuşma videosu yap. İngilizce konuşuyor, "
            'altyazılar Türkçe olsun. "Ecuador" dediğinde Cotopaxi fotoğrafı köşede küçük '
            "bir kart olarak çıksın.",
            "tr",
        ),
        (
            "Talking video of Lucas for my Argentine followers: keep his English voice, but "
            "make the subtitles Spanish. While he names the sauces, cut away to my overhead "
            'video of the table. Spanish title: "Las salsas de Don Julio". No text on faces.',
            "es",
        ),
        # Negative: Turkish/English prose with no caption/subtitle keyword —
        # the prompt merely being *written* in a language must not count.
        ("Bu videoyu güzel düzenle, Türkçe konuşuyorum", None),
        ("Make this look nice and punchy, I'm speaking English throughout", None),
        # Negative: negated / declined requests.
        ("don't put english captions", None),
        ("altyazı istemiyorum, İngilizce konuşuyorum", None),
        # Negative: caption keyword present but no language named.
        ("add captions", None),
        # Negative (KRI-838 review): a language and a caption word in one clause are not an
        # ask; the language must be BOUND to the captions. Each of these fired once the table
        # held nine languages (the last two already fired on en/tr).
        ("make a video about Spanish food with subtitles", None),
        ("French toast recipe with captions", None),
        ("Show the Spanish steakhouse and add subtitles", None),
        ("German shepherd video with captions", None),
        ("add captions to my Portuguese trip video", None),
        ("Lucas speaks Spanish so add subtitles", None),
        ("Add Spanish music and subtitles", None),
        ("İspanyolca bir restoran videosu altyazılı olsun", None),
        ("Turkish coffee tutorial with subtitles", None),
        ("He speaks English and I want captions", None),
        ("subtitles Spanish food intro", None),
        ("captions Dutch oven", None),
        # Negative: a language named without captions, as the voice, or as the title's.
        ("Spanish speakers will love this", None),
        ("make a video for my Spanish speakers", None),
        ("keep his English voice, Spanish title", None),
        ("no subtitles in Spanish", None),
        ("Spanish and English subtitles", None),
        # Negative: empty / missing text.
        ("", None),
        (None, None),
    ],
)
def test_parse_caption_language_request(text: str | None, expected: str | None) -> None:
    assert parse_caption_language_request(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    # KRI-838: "fr" used to be None (the table was en/tr only). The table now carries every
    # language the brief side knows; whether one can be DELIVERED is a separate question.
    [
        ("en", "en"),
        (" TR ", "tr"),
        ("fr", "fr"),
        ("ES", "es"),
        ("ru", None),
        # Arabic is in the table (names, parser, language-ID) but never promised nor rendered.
        ("ar", None),
        ("", None),
        (None, None),
        (1, None),
    ],
)
def test_coerce_caption_language_request(value: object, expected: str | None) -> None:
    assert coerce_caption_language_request(value) == expected


# ── 2. infer_language_from_text ─────────────────────────────────────────────


def test_infer_language_from_text_turkish() -> None:
    assert infer_language_from_text("Bu videoyu çok güzel çektim bugün") == "tr"


def test_infer_language_from_text_english() -> None:
    assert infer_language_from_text("I really love how this video turned out today") == "en"


def test_infer_language_from_text_undecidable() -> None:
    assert infer_language_from_text("ok") == ""


# ── 3. resolve_spoken_caption_language ──────────────────────────────────────


def test_resolve_prefers_detected_language() -> None:
    lang, source = resolve_spoken_caption_language("EN", transcript_text="whatever", fallback="tr")
    assert (lang, source) == ("en", "detected")


def test_resolve_falls_back_to_transcript_text_when_undetected() -> None:
    lang, source = resolve_spoken_caption_language(
        None, transcript_text="Bu videoyu çok güzel çektim bugün", fallback=None
    )
    assert (lang, source) == ("tr", "transcript_text")


def test_resolve_falls_back_to_deliberate_fallback_when_undecidable() -> None:
    lang, source = resolve_spoken_caption_language(None, transcript_text="ok", fallback="tr")
    assert (lang, source) == ("tr", "fallback")


def test_resolve_fallback_defaults_to_english_when_none_given() -> None:
    lang, source = resolve_spoken_caption_language(None, transcript_text=None, fallback=None)
    assert (lang, source) == ("en", "fallback")


# ── 4. crosscheck_detected_language ─────────────────────────────────────────

# Job 385e3b13 (2026-09-24): whisper-1 auto-detected "tr" on Turkish-accented
# English and wrote a Turkish translation; Gemini's transcript of the same clip
# was English.
_GEMINI_EN = (
    "Let's talk about the best football players in Turkish Super League this season. "
    "Number three, Mac Ingram? No, no, no, no, no. Number three, Rafael Leao. Number two. "
    "No, no, no, no, no. Leandro Trossard from Beşiktaş. Number one. Ya Allah, Bismillah. "
    "Mohammed Salah from Trabzonspor."
)


def test_crosscheck_flags_accented_english_misdetected_as_turkish() -> None:
    assert crosscheck_detected_language("tr", reference_text=_GEMINI_EN) == "en"


def test_crosscheck_flags_turkish_misdetected_as_english() -> None:
    assert (
        crosscheck_detected_language("en", reference_text="Bu videoyu çok güzel çektim bugün")
        == "tr"
    )


@pytest.mark.parametrize(
    ("detected", "reference"),
    [
        ("en", _GEMINI_EN),  # agreement
        ("tr", "ok"),  # reference too short to classify
        ("tr", ""),  # no Gemini transcript
        ("", _GEMINI_EN),  # nothing detected — resolve_spoken_caption_language owns that
    ],
)
def test_crosscheck_is_silent_without_a_clear_disagreement(detected: str, reference: str) -> None:
    assert crosscheck_detected_language(detected, reference_text=reference) is None


# ── 5. the one language table (KRI-838) ─────────────────────────────────────


def test_the_table_covers_the_nine_languages_the_brief_side_knows() -> None:
    assert SUPPORTED_CAPTION_LANGUAGES == {"en", "tr", "de", "es", "fr", "it", "ar", "nl", "pt"}
    assert LANGUAGE_NAMES["es"] == ("Spanish", "İspanyolca")
    assert {lang.code for lang in CAPTION_LANGUAGES} == SUPPORTED_CAPTION_LANGUAGES


@pytest.mark.parametrize(
    ("flag", "code", "expected"),
    [
        (True, "es", True),
        # Arabic: the cloud renderer's fonts and right-to-left shaping were never verified.
        (True, "ar", False),
        (False, "ar", False),
        (True, "en", True),
        (True, "ru", False),
        (True, None, False),
        # Translation off: only what the whisper hint alone has always delivered.
        (False, "tr", True),
        (False, "en", True),
        (False, "es", False),
        (False, "de", False),
    ],
)
def test_can_deliver_caption_language_is_one_rule(
    monkeypatch: pytest.MonkeyPatch, flag: bool, code: str | None, expected: bool
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "caption_translation_enabled", flag, raising=False)
    assert can_deliver_caption_language(code) is expected


def test_coerce_honours_the_same_rule_at_render_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """KRI-838 review: with translation off the draft says "I can't make Spanish captions
    yet", so the worker must not hint Spanish either -- the legacy en/tr world exactly."""
    from app.config import settings

    monkeypatch.setattr(settings, "caption_translation_enabled", False, raising=False)
    assert coerce_caption_language_request("es") is None
    assert coerce_caption_language_request("fr") is None
    assert coerce_caption_language_request("tr") == "tr"
    assert coerce_caption_language_request("EN") == "en"
    # the parser stays general: the dispatcher still records the ask it heard
    assert parse_caption_language_request("make the subtitles Spanish") == "es"
