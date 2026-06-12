"""Guards for the lyric-language render gate (P6).

The song_lyrics variant must be skipped for tracks whose lyric language the
bundled Latin-script fonts cannot render (the prod Chinese-track incident:
variant attempted → alignment/tofu failure → opaque "try editing again").
"""

from app.pipeline.lyric_support import (
    RENDERABLE_LYRIC_LANGUAGES,
    lyric_language,
    lyrics_variant_renderable,
)


class TestLyricLanguage:
    def test_extracts_normalized_code(self):
        assert lyric_language({"language": "EN"}) == "en"

    def test_region_tags_collapse_to_primary_subtag(self):
        assert lyric_language({"language": "pt-BR"}) == "pt"
        assert lyric_language({"language": "zh_Hans"}) == "zh"

    def test_missing_or_empty_is_blank(self):
        assert lyric_language(None) == ""
        assert lyric_language({}) == ""
        assert lyric_language({"language": "  "}) == ""
        assert lyric_language("not-a-dict") == ""  # type: ignore[arg-type]


class TestLyricsVariantRenderable:
    def test_no_lyrics_is_not_renderable(self):
        assert lyrics_variant_renderable(None) is False
        assert lyrics_variant_renderable({}) is False

    def test_latin_language_is_renderable(self):
        assert lyrics_variant_renderable({"language": "en", "lines": [{"text": "hi"}]}) is True
        assert lyrics_variant_renderable({"language": "tr", "lines": [{"text": "selam"}]}) is True

    def test_cjk_is_not_renderable(self):
        # The prod incident track: 攬佬SKAI ISYOURGOD — 八方來財 (zh).
        zh = {"language": "zh", "lines": [{"text": "八方來財"}]}
        assert lyrics_variant_renderable(zh) is False
        assert lyrics_variant_renderable({"language": "ja", "lines": []}) is False
        assert lyrics_variant_renderable({"language": "ko", "lines": []}) is False

    def test_non_latin_scripts_not_renderable(self):
        for lang in ("ar", "ru", "uk", "el", "he", "th", "hi"):
            assert lyrics_variant_renderable({"language": lang}) is False, lang

    def test_unknown_language_fails_open(self):
        # Legacy extractions predate the language field — keep prior behavior
        # (attempt the variant); the glyph fail-fast is the backstop.
        assert lyrics_variant_renderable({"lines": [{"text": "hi"}]}) is True

    def test_vietnamese_deliberately_excluded(self):
        # Stacked diacritics most bundled display faces lack.
        assert "vi" not in RENDERABLE_LYRIC_LANGUAGES
        assert lyrics_variant_renderable({"language": "vi"}) is False
