"""Title/artist hygiene tests for the shared lyrics-search helper.

Ported from the former test_genius_client.py — the underlying regex and
artist-deduplication logic are identical to what PR #251 added; the
contract just changed from "return a single concatenated query string" to
"return (clean_title, clean_artist) separately" so backends that take
named params (LRCLIB) get the right fields.
"""

from __future__ import annotations

from app.services.lyrics_search_query import build_lyrics_search_query


def test_strips_youtube_artist_prefix() -> None:
    """yt-dlp passes 'Artist - Title' as the title field. Strip that prefix
    when the artist is already known so the title is just the song name."""
    title, artist = build_lyrics_search_query("The Weeknd - Can't Feel My Face", "The Weeknd")
    assert title == "Can't Feel My Face"
    assert artist == "The Weeknd"


def test_strips_official_video_tag() -> None:
    """Parenthetical noise like '(Official Video)' tanks lookup relevance."""
    title, artist = build_lyrics_search_query(
        "The Weeknd - Can't Feel My Face (Official Video)", "The Weeknd"
    )
    assert title == "Can't Feel My Face"
    assert artist == "The Weeknd"


def test_strips_brackets_and_multiple_tags() -> None:
    title, artist = build_lyrics_search_query(
        "Blinding Lights [Official Music Video] (HD)", "The Weeknd"
    )
    assert title == "Blinding Lights"
    assert artist == "The Weeknd"


def test_keeps_legitimate_parens() -> None:
    """'(feat. ...)' is part of the canonical title — don't strip it."""
    title, _ = build_lyrics_search_query("Save Your Tears (feat. Ariana Grande)", "The Weeknd")
    assert title == "Save Your Tears (feat. Ariana Grande)"


def test_handles_missing_artist() -> None:
    title, artist = build_lyrics_search_query("Bohemian Rhapsody", "")
    assert (title, artist) == ("Bohemian Rhapsody", "")

    title, artist = build_lyrics_search_query("", "Queen")
    assert (title, artist) == ("", "Queen")

    title, artist = build_lyrics_search_query("", "")
    assert (title, artist) == ("", "")


def test_handles_case_mismatch_in_prefix() -> None:
    """Title from YouTube may use different capitalization than the
    stored artist."""
    title, artist = build_lyrics_search_query("THE WEEKND - Can't Feel My Face", "The Weeknd")
    assert title == "Can't Feel My Face"
    assert artist == "The Weeknd"


def test_strips_artist_prefix_without_space() -> None:
    """Rare yt-dlp variant: 'Artist-Title' with no spaces around the dash."""
    title, artist = build_lyrics_search_query("Adele-Hello", "Adele")
    assert title == "Hello"
    assert artist == "Adele"


def test_collapses_extra_whitespace_after_noise_strip() -> None:
    """When noise tags get removed, leftover double-spaces should collapse."""
    title, _ = build_lyrics_search_query("Hello  (Official)  World", "Adele")
    assert title == "Hello World"


def test_strips_trailing_dash_after_noise_removal() -> None:
    """After stripping '(Official Video)', a dangling hyphen would look
    weird in the lookup string."""
    title, _ = build_lyrics_search_query("Song Title - (Official Video)", "Artist")
    assert title == "Song Title"
