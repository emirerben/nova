"""KRI-1192: spoken captions stay in the spoken language for ten more languages.

Real speech, not authored sentences: 12 FLEURS clips per language (CC BY 4.0) for pt, es,
el, de, ko, it, fr, ar, da, sr, plus 12 English and 12 Turkish as controls, transcribed
by whisper.cpp large-v3-turbo with language auto-detect (fixture
``kri1192_caption_languages/fleurs_whisper_turbo.json``). The FLEURS human transcript
stands in for Gemini's independent transcript in the cross-check.

Failure modes (docs/reviews/kri-1192-caption-languages.md, written before the code):

F1  the cross-check re-runs a correctly heard clip in another language. Measured on main:
    29 of 60 pt/es/de/it/fr clips re-run in Turkish, 5 of 12 Danish in English, and a
    hinted re-run is a translation.
F2  whisper-1's full language names ("greek", "danish", "serbian", "croatian") are not
    mapped, so the caption language is stored as a name nothing recognises.
F3  whisper labels half the Serbian speakers Croatian: one language family, one code.
F4  the text language-ID calls real text a WRONG table language (Danish -> French/German,
    Serbian -> Portuguese/Dutch on main).
F5  Serbian captions mix Cyrillic and Latin.
F13 the creator's "Greek captions" / "Yunanca altyazı" ask is not understood.
F15 English and Turkish decisions change.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

from app.pipeline.caption_language import (
    LANGUAGE_NAMES,
    SUPPORTED_CAPTION_LANGUAGES,
    canonical_caption_language,
    caption_language_verdict,
    crosscheck_detected_language,
    detect_text_language,
    infer_language_from_text,
    parse_caption_language_request,
    resolve_spoken_caption_language,
    serbian_latin,
)
from app.pipeline.transcribe import _normalize_lang

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "kri1192_caption_languages"
    / "fleurs_whisper_turbo.json"
)
CLIPS = json.loads(FIXTURE.read_text(encoding="utf-8"))["clips"]
NEW = ("pt", "es", "el", "de", "ko", "it", "fr", "ar", "da", "sr")
# whisper-1's verbose_json reports a full lowercase language name, not the ISO code.
WHISPER_NAMES = {
    "pt": "portuguese",
    "es": "spanish",
    "el": "greek",
    "de": "german",
    "ko": "korean",
    "it": "italian",
    "fr": "french",
    "ar": "arabic",
    "da": "danish",
    "sr": "serbian",
    "hr": "croatian",
    "bs": "bosnian",
    "en": "english",
    "tr": "turkish",
}


def _clips(language: str) -> list[dict]:
    return [clip for clip in CLIPS if clip["language"] == language]


def test_the_fixture_is_the_measured_one():
    assert Counter(clip["language"] for clip in CLIPS) == {code: 12 for code in (*NEW, "en", "tr")}
    serbian = Counter(clip["whisper_language"] for clip in _clips("sr"))
    assert serbian == {"sr": 6, "hr": 6}  # F3 as measured


def test_the_ten_languages_are_table_languages_with_english_and_turkish_names():
    for code in NEW:
        assert code in SUPPORTED_CAPTION_LANGUAGES
        english, turkish = LANGUAGE_NAMES[code]
        assert english and turkish and english != code.upper()


@pytest.mark.parametrize("code", [*NEW, "hr", "bs", "en", "tr"])
def test_whisper_language_names_and_codes_map_to_the_table_code(code):
    expected = "sr" if code in {"hr", "bs"} else code
    assert _normalize_lang(WHISPER_NAMES[code]) == expected  # whisper-1 (prod)
    assert _normalize_lang(code) == expected  # faster-whisper / whisper.cpp


def test_serbian_croatian_bosnian_are_one_caption_language():
    for code in ("sr", "hr", "bs", "sr-Latn", "SR", "sh"):
        assert canonical_caption_language(code) == "sr"
    assert canonical_caption_language("el") == "el"
    assert canonical_caption_language("ru") == "ru"  # outside the table: kept as is
    assert canonical_caption_language("") == ""


@pytest.mark.parametrize("language", [*NEW, "en", "tr"])
def test_the_cross_check_never_re_runs_a_correctly_heard_clip(language):
    redone = []
    for clip in _clips(language):
        detected = _normalize_lang(WHISPER_NAMES[clip["whisper_language"]])
        for reference in (clip["reference"], clip["whisper_text"]):
            redo = crosscheck_detected_language(detected, reference_text=reference)
            if redo is not None:
                redone.append((clip["fleurs_id"], detected, redo))
    assert not redone


@pytest.mark.parametrize("language", NEW)
def test_the_cross_check_still_corrects_a_misheard_clip_into_the_spoken_language(language):
    # Accented speech heard as English/Turkish: the reference's language wins.
    reference = _clips(language)[0]["reference"]
    wrong = "tr" if language != "tr" else "en"
    assert crosscheck_detected_language(wrong, reference_text=reference) == language


@pytest.mark.parametrize("language", [*NEW, "en", "tr"])
def test_text_language_id_is_never_wrong_on_real_speech(language):
    wrong = []
    for clip in _clips(language):
        for text in (clip["reference"], clip["whisper_text"]):
            found = detect_text_language(text)
            if found is not None and found != language:
                wrong.append((found, text[:60]))
    assert not wrong


@pytest.mark.parametrize("language", ("el", "ko", "ar", "sr"))
def test_script_languages_are_always_identified(language):
    # Greek, Hangul and Arabic letters, and Serbian Cyrillic (the FLEURS references).
    for clip in _clips(language):
        assert detect_text_language(clip["reference"]) == language
        if language != "sr":  # whisper writes Serbian in Latin: next test
            assert detect_text_language(clip["whisper_text"]) == language


def test_latin_serbian_is_mostly_identified():
    # Latin Serbian shares short words with Dutch and Portuguese; measured 11/12 (whisper)
    # and 10/12 (references written Latin). The misses are "unknown", never wrong.
    whisper = [detect_text_language(clip["whisper_text"]) for clip in _clips("sr")]
    latin = [detect_text_language(serbian_latin(clip["reference"])) for clip in _clips("sr")]
    assert whisper.count("sr") >= 10 and latin.count("sr") >= 10, (whisper, latin)
    assert set(whisper) | set(latin) <= {"sr", None}


@pytest.mark.parametrize("language", ("pt", "es", "de", "it", "fr", "da"))
def test_latin_languages_are_mostly_identified(language):
    found = [detect_text_language(clip["whisper_text"]) for clip in _clips(language)]
    assert found.count(language) >= 9, found


@pytest.mark.parametrize("language", [*NEW, "en", "tr"])
def test_the_transcript_guess_is_the_spoken_language_or_nothing(language):
    for clip in _clips(language):
        guess = infer_language_from_text(clip["reference"])
        assert guess in {language, ""}, (guess, clip["reference"][:60])


def test_a_transcript_guess_names_the_spoken_language_before_any_fallback():
    greek = _clips("el")[0]["whisper_text"]
    assert resolve_spoken_caption_language("", transcript_text=greek, fallback="tr") == (
        "el",
        "transcript_text",
    )
    assert resolve_spoken_caption_language("greek", transcript_text="", fallback="tr") == (
        "el",
        "detected",
    )
    assert resolve_spoken_caption_language("hr", transcript_text="", fallback="en") == (
        "sr",
        "detected",
    )


@pytest.mark.parametrize("language", NEW)
def test_requesting_the_spoken_language_is_never_contradicted(language):
    for clip in _clips(language):
        text = clip["whisper_text"]
        if language == "sr":
            text = serbian_latin(text)
        verdict, _found = caption_language_verdict([text], language)
        assert verdict in {"confirmed", "unknown"}


# --- F5: Serbian is captioned in ONE alphabet (Latin, what whisper writes) -----------


def test_serbian_cyrillic_becomes_latin_and_latin_is_untouched():
    assert serbian_latin("Љубав њега џеп ђак ћерка Ј") == "Ljubav njega džep đak ćerka J"
    assert serbian_latin("ЉУБАВ ЊЕГА") == "LJUBAV NJEGA"
    assert serbian_latin("Љубав") == "Ljubav"
    latin = "U njemu se navodi da je najupečatljivije mesto"
    assert serbian_latin(latin) == latin


def test_every_serbian_reference_turns_fully_latin():
    for clip in _clips("sr"):
        latin = serbian_latin(clip["reference"])
        assert not any("CYRILLIC" in unicodedata.name(ch, "") for ch in latin), latin


# --- F13: the creator's own words name the new languages --------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Greek captions please", "el"),
        ("make the subtitles Korean", "ko"),
        ("captions in Danish", "da"),
        ("Serbian subtitles", "sr"),
        ("altyazılar Yunanca olsun", "el"),
        ("Korece altyazı", "ko"),
        ("altyazıyı Danca yap", "da"),
        ("Sırpça altyazı ekle", "sr"),
        # Not asks: the language names the voice or a topic.
        ("he speaks Greek so add subtitles", None),
        ("Korean food with captions", None),
    ],
)
def test_the_new_languages_are_understood_in_a_caption_ask(text, expected):
    assert parse_caption_language_request(text) == expected


# --- F5 / F14: Serbian compared in one alphabet; mixed takes see the new languages ----


def test_a_cyrillic_reference_does_not_make_latin_serbian_suspect():
    # Gemini may write Serbian in Cyrillic while whisper writes Latin: before KRI-1192 the
    # script mismatch made every such clip "suspect" and its words were dropped.
    from app.services.clip_speech_check import suspect_reason

    for clip in _clips("sr"):
        for detected in ("sr", "hr"):
            assert suspect_reason(clip["whisper_text"], detected, clip["reference"]) is None


@pytest.mark.parametrize(
    ("second", "language"),
    [
        (_clips("el")[0]["reference"], "el"),
        (_clips("ko")[0]["reference"], "ko"),
        (_clips("ar")[0]["reference"], "ar"),
        (_clips("da")[0]["reference"], "da"),
        (_clips("sr")[0]["reference"], "sr"),
        (_clips("sr")[1]["whisper_text"] + " " + _clips("sr")[2]["whisper_text"], "sr"),
    ],
    ids=["el", "ko", "ar", "da", "sr-cyrillic", "sr-latin"],
)
def test_a_mixed_take_names_the_new_language_next_to_english(second, language):
    from app.services.mixed_language_transcript import reference_languages

    take = f"Hello everyone, thank you so much for watching my channel today. {second}"
    assert set(reference_languages(take)) == {"en", language}


def test_english_and_turkish_takes_still_name_only_english_and_turkish():
    from app.services.mixed_language_transcript import reference_languages

    for language in ("en", "tr"):
        for clip in _clips(language):
            assert set(reference_languages(clip["reference"])) <= {language}


# --- F13: the brief side understands and checks the new languages -------------------


def _brief_req(description: str, **facts):
    from app.kria.brief import BriefRequirement

    return BriefRequirement(
        id="c", kind="style", scope="global", description=description, facts=dict(facts)
    )


@pytest.mark.parametrize(
    ("wording", "facts", "expected"),
    [
        ("Greek captions please", {}, "el"),
        ("altyazılar Yunanca olsun", {}, "el"),
        ("make the subtitles Korean", {}, "ko"),
        ("Danish subtitles", {}, "da"),
        ("Sırpça altyazı", {}, "sr"),
        ("subtitles", {"caption_language": "el"}, "el"),
        ("subtitles", {"caption_language": "hr"}, "sr"),
    ],
)
def test_the_brief_reads_a_new_language_ask(wording, facts, expected):
    from app.kria.brief_checks import _asked_caption_language

    assert _asked_caption_language(_brief_req(wording, **facts)) == expected


def test_the_brief_checks_greek_captions_by_their_text():
    from app.kria.brief_checks import PlanFacts, _check_caption_language

    greek = tuple(clip["reference"] for clip in _clips("el")[:2])
    met = _check_caption_language(
        _brief_req("Greek captions"), PlanFacts(caption_language="el", caption_texts=greek), "el"
    )
    assert met.status == "met"
    english = tuple(clip["reference"] for clip in _clips("en")[:2])
    wrong = _check_caption_language(
        _brief_req("Greek captions"), PlanFacts(caption_language="el", caption_texts=english), "el"
    )
    assert wrong.status == "partial"
    assert wrong.reason == "The captions are in English, not Greek."


# --- F11: the phone's caption keep-out boxes are measured in the face that draws them --


@pytest.mark.parametrize("language", ["ko", "ar", "en"])
def test_phone_caption_bounds_are_measured_in_the_face_that_draws_the_text(monkeypatch, language):
    # Hangul/Arabic runs carry glyph ids (or shaped text) of the script face; measuring them
    # in the caption look's own face reads other glyphs' outlines, so the reserved caption
    # area (the watermark/title keep-out) was wrong for Korean and Arabic.
    from app.config import settings
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.phone_captions import PhoneCaptionLook, compile_caption_layers
    from app.services import phone_text_styling as styling

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "scriptFonts"],
    )
    text = _clips(language)[0]["reference"]
    layers = compile_caption_layers(
        [{"text": text, "start_s": 0.0, "end_s": 4.0}], canvas_width=1080, canvas_height=1920
    )
    import app.pipeline.phone_captions as phone_captions

    measured_in: set[str] = set()
    real_ink_box = phone_captions.caption_ink_box

    def spy(run, typeface):  # noqa: ANN001, ANN202
        measured_in.add(typeface.getFamilyName())
        return real_ink_box(run, typeface)

    monkeypatch.setattr(phone_captions, "caption_ink_box", spy)
    look = PhoneCaptionLook()
    token = styling._POLICY.set({"caption_look": look})
    try:
        styling.remember_caption_bounds(layers, canvas=PORTRAIT)
        boxes = styling._POLICY.get()["_caption_boxes"]
    finally:
        styling._POLICY.reset(token)
    expected = {"ko": "Noto Sans KR", "ar": "Noto Sans Arabic", "en": "TikTok Sans"}[language]
    assert boxes
    assert measured_in == {expected}


# --- F10: Smart captions close a phrase on Arabic and Greek sentence marks ----------


@pytest.mark.parametrize(
    ("words", "first_cue"),
    [
        # Arabic question mark "؟" ends a sentence; the next sentence starts a new cue.
        (["هل", "أنت", "بخير؟", "نعم", "أنا", "بخير", "جدا", "اليوم"], "هل أنت بخير؟"),
        # Greek question mark is ";" (and U+037E).
        (
            ["Τι", "κάνεις", "σήμερα;", "Καλά", "είμαι", "πολύ", "ευχαριστώ", "φίλε"],
            "Τι κάνεις σήμερα;",
        ),
    ],
    ids=["ar", "el"],
)
def test_smart_captions_close_on_arabic_and_greek_question_marks(words, first_cue):
    from app.smart_edit.captions import build_smart_caption_cues
    from app.smart_edit.presets import CaptionPolicy

    policy = CaptionPolicy(
        min_words=3, max_words=7, max_lines=2, max_chars=44, font_family="Montserrat Bold",
        font_size_px=64, y_frac=0.705, width_frac=0.88, color="&H00FFFFFF",
        stroke_color="&H00000000", stroke_width=6,
    )  # fmt: skip
    cue = {
        "text": " ".join(words),
        "start_s": 0.0,
        "end_s": 4.0,
        "words": [
            {"text": w, "start_s": i * 0.5, "end_s": i * 0.5 + 0.4} for i, w in enumerate(words)
        ],
    }
    cues = build_smart_caption_cues([cue], policy)
    assert cues[0]["text"] == first_cue
