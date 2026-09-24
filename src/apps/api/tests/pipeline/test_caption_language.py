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
"""

from __future__ import annotations

import pytest

from app.pipeline.caption_language import (
    infer_language_from_text,
    parse_caption_language_request,
    resolve_spoken_caption_language,
)

# ── 1. parse_caption_language_request ───────────────────────────────────────


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
        # Negative: Turkish/English prose with no caption/subtitle keyword —
        # the prompt merely being *written* in a language must not count.
        ("Bu videoyu güzel düzenle, Türkçe konuşuyorum", None),
        ("Make this look nice and punchy, I'm speaking English throughout", None),
        # Negative: negated / declined requests.
        ("don't put english captions", None),
        ("altyazı istemiyorum, İngilizce konuşuyorum", None),
        # Negative: caption keyword present but no language named.
        ("add captions", None),
        # Negative: empty / missing text.
        ("", None),
        (None, None),
    ],
)
def test_parse_caption_language_request(text: str | None, expected: str | None) -> None:
    assert parse_caption_language_request(text) == expected


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
