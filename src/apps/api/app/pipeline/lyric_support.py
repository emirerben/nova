"""Which lyric languages the renderer can actually burn.

`LyricsOutput.language` (ISO 639-1, lowercased) is extracted at track-analysis
time and stored inside `MusicTrack.lyrics_cached`. The bundled font set
(assets/fonts/) is Latin-script only — Bowlby One SC's "SC" means Small Caps,
not Simplified Chinese — so CJK/Arabic/Cyrillic lyrics would render as `.notdef`
tofu boxes, and the karaoke word-aligner additionally assumes
whitespace-separated words. Gate the song_lyrics VARIANT on language up front
instead of failing (or worse, rendering garbage) mid-burn.

Fail-open contract: a missing/empty language (legacy extractions predate the
field) attempts the variant exactly as before this gate existed — the
per-variant exception capture in generative_build stays the backstop, now with
`assert_lyric_glyphs` in the Skia burn path producing a typed error.
"""

from __future__ import annotations

# Latin-script languages whose alphabets the bundled display fonts cover.
# Deliberately excludes Vietnamese (stacked diacritics most display faces
# lack) and all non-Latin scripts. Extend together with the font bundle.
RENDERABLE_LYRIC_LANGUAGES: frozenset[str] = frozenset(
    {
        "en", "es", "pt", "fr", "de", "it", "nl", "sv", "no", "nb", "nn",
        "da", "fi", "is", "pl", "cs", "sk", "ro", "hu", "hr", "sl", "et",
        "lv", "lt", "sq", "eu", "ca", "gl", "tr", "az", "id", "ms", "tl",
        "sw",
    }
)


def lyric_language(lyrics_cached: dict | None) -> str:
    """Normalized ISO 639-1 code of the cached lyrics ("" when absent/unknown).

    Tolerates region-tagged codes ("pt-BR", "zh_Hans") by keeping the primary
    subtag only.
    """
    if not isinstance(lyrics_cached, dict):
        return ""
    raw = str(lyrics_cached.get("language") or "").strip().lower()
    if not raw:
        return ""
    return raw.replace("_", "-").split("-")[0]


def lyrics_variant_renderable(lyrics_cached: dict | None) -> bool:
    """True when the song_lyrics variant should be attempted for this track.

    False when there are no cached lyrics at all, or when the lyric language is
    known and outside the renderable set. Unknown/missing language fails OPEN
    (legacy behavior) — the glyph fail-fast in the burn path is the backstop.
    """
    if not lyrics_cached:
        return False
    lang = lyric_language(lyrics_cached)
    if not lang:
        return True
    return lang in RENDERABLE_LYRIC_LANGUAGES
