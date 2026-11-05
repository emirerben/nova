"""KRI-1192: a caption in any of the new scripts is drawn in ONE face that has every letter.

Failure modes this guards (written before the code, docs/reviews/kri-1192-caption-languages.md):

F6  Korean or Arabic in any bundled face: empty boxes (no registry face had Hangul or
    Arabic), Greek/Cyrillic in most faces: boxes or a per-letter DejaVu patch.
F11 the cloud and the phone pick different faces: the rule is partner -> script list,
    both read from glyph-fallbacks.json, so the cloud's same-category walk is never
    reached for a non-Latin text.
F15 English and Turkish text change face: a Latin-only text must get exactly what the
    KRI-785 rule gave it (partner -> same category -> display default).

The sample texts are real FLEURS sentences (CC BY 4.0) from the KRI-1192 fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline import font_coverage
from app.pipeline.font_coverage import glyph_safe_family, missing_glyphs

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "kri1192_caption_languages"
    / "fleurs_whisper_turbo.json"
)
CLIPS = json.loads(FIXTURE.read_text(encoding="utf-8"))["clips"]
NEW = ("pt", "es", "el", "de", "ko", "it", "fr", "ar", "da", "sr")


def _texts(language: str) -> list[str]:
    return [clip["reference"] for clip in CLIPS if clip["language"] == language]


def _choosable_fonts() -> list[str]:
    fonts = font_coverage._registry()["fonts"]
    return [name for name, entry in fonts.items() if entry.get("deprecated") is not True]


def _legacy_family(family: str, text: str) -> str:
    """The KRI-785 rule as it was before KRI-1192 (the oracle for Latin text)."""
    resolved = font_coverage._entry(family)
    if resolved is None or font_coverage.font_covers_text(family, text):
        return family
    name, entry = resolved
    fonts = font_coverage._registry()["fonts"]
    for candidate in font_coverage._candidates(name, entry):
        cand = fonts.get(candidate)
        if not cand or cand.get("deprecated") is True:
            continue
        if font_coverage.font_covers_text(candidate, text):
            return candidate
    return family


@pytest.mark.parametrize("language", NEW)
def test_every_choosable_font_draws_every_new_language_in_one_face(language):
    failures = []
    for family in _choosable_fonts():
        for text in _texts(language):
            face = glyph_safe_family(family, text)
            missing = missing_glyphs(face, text)
            if missing:
                failures.append((family, face, "".join(missing)))
    assert not failures, failures[:10]


@pytest.mark.parametrize("language", ("en", "tr", "pt", "es", "de", "it", "fr", "da"))
def test_latin_text_keeps_the_kri785_face(language):
    # Latin-script languages never reach the script list: same face as before KRI-1192.
    for family in _choosable_fonts():
        for text in _texts(language):
            assert glyph_safe_family(family, text) == _legacy_family(family, text), (family, text)


def test_serbian_latin_keeps_the_kri785_face():
    latin_serbian = "U njemu se navodi da je najupečatljivije mesto za glavni predmet đak ćevap."
    for family in _choosable_fonts():
        assert glyph_safe_family(family, latin_serbian) == _legacy_family(family, latin_serbian)


@pytest.mark.parametrize(
    ("family", "language", "expected"),
    [
        # Closest registry weight among the covering script faces.
        ("Montserrat", "el", "TikTok Sans Bold"),  # 800 -> 700
        ("Montserrat Regular", "sr-cyrl", "Montserrat Regular"),  # Montserrat has Cyrillic
        ("Poppins Black", "sr-cyrl", "TikTok Sans Bold"),  # 900 -> 700, no Cyrillic
        ("Anton", "ko", "Noto Sans KR"),  # 400 -> 400
        ("TikTok Sans", "ko", "Noto Sans KR"),  # 400 -> 400
        ("TikTok Sans Bold", "ko", "Noto Sans KR Bold"),
        ("Montserrat", "ar", "Noto Sans Arabic Bold"),
        ("Playfair Display", "el", "TikTok Sans Bold"),  # serif 700: script list, not the walk
        ("TikTok Sans", "el", "TikTok Sans"),  # covers Greek itself
    ],
)
def test_script_face_choice(family, language, expected):
    text = {
        "el": _texts("el")[0],
        "ko": _texts("ko")[0],
        "ar": _texts("ar")[0],
        "sr-cyrl": _texts("sr")[0],  # FLEURS Serbian is Cyrillic
    }[language]
    face = glyph_safe_family(family, text)
    if expected is None:
        assert face in {"TikTok Sans", "TikTok Sans Bold"}
    else:
        assert face == expected
    assert not missing_glyphs(face, text)


def test_script_faces_are_never_offered():
    # deprecated keeps them out of every picker, prompt and the CLIP matcher; the
    # registry hash leaves them out so registry-embeddings.npz stays valid.
    fonts = font_coverage._registry()["fonts"]
    script_faces = [name for name, entry in fonts.items() if entry.get("script_fallback")]
    assert sorted(script_faces) == [
        "Noto Sans Arabic",
        "Noto Sans Arabic Bold",
        "Noto Sans KR",
        "Noto Sans KR Bold",
    ]
    assert all(fonts[name].get("deprecated") is True for name in script_faces)
    listed = json.loads(
        (Path(font_coverage.FONTS_DIR) / "glyph-fallbacks.json").read_text(encoding="utf-8")
    )["_script_fallbacks"]
    assert set(script_faces) <= set(listed)


def test_registry_hash_still_matches_the_committed_embeddings():
    import numpy as np

    from app.pipeline.font_identification import registry_sha256

    data = np.load(Path(font_coverage.FONTS_DIR) / "registry-embeddings.npz", allow_pickle=True)
    assert json.loads(str(data["meta"]))["registry_sha256"] == registry_sha256()


def test_the_shared_phone_parity_fixture_is_what_the_server_picks():
    # F11: the iOS NativeGlyphFallback asserts the same file
    # (tests/fixtures/caption-glyph-parity/script_fallback_faces.json); this keeps the
    # server from drifting away from it silently.
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[5]
            / "tests"
            / "fixtures"
            / "caption-glyph-parity"
            / "script_fallback_faces.json"
        ).read_text(encoding="utf-8")
    )
    mismatches = [
        (row["id"], family, fixture["faces"].get(row["id"], {}).get(family, family), face)
        for row in fixture["texts"]
        for family in fixture["families"]
        if (face := glyph_safe_family(family, row["text"]))
        != fixture["faces"].get(row["id"], {}).get(family, family)
    ]
    assert not mismatches, mismatches[:10]
