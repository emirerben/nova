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


# ── Trailing-feature stripping (the Beauty And A Beat fix) ────────────────────


def test_strips_trailing_ft_with_dot() -> None:
    """Admin-uploaded filename 'Song ft. Other' must lose the suffix —
    LRCLIB's index keys on the canonical title without feature credits."""
    title, artist = build_lyrics_search_query("Beauty And A Beat ft. Nicki Minaj", "Justin Bieber")
    assert title == "Beauty And A Beat"
    assert artist == "Justin Bieber"


def test_strips_trailing_feat_with_dot() -> None:
    title, _ = build_lyrics_search_query("Song Title feat. Artist X", "Other")
    assert title == "Song Title"


def test_strips_trailing_feat_without_dot() -> None:
    title, _ = build_lyrics_search_query("Song Title feat Artist X", "Other")
    assert title == "Song Title"


def test_strips_trailing_featuring() -> None:
    title, _ = build_lyrics_search_query("Song Title featuring Some Person", "Artist")
    assert title == "Song Title"


def test_trailing_feature_case_insensitive() -> None:
    """yt-dlp + admin uploads emit varied casing — FT./Feat./Featuring."""
    for raw in (
        "Song Title FT. Other",
        "Song Title Feat. Other",
        "Song Title FEATURING Other",
    ):
        title, _ = build_lyrics_search_query(raw, "Artist")
        assert title == "Song Title", f"failed for {raw!r}"


def test_full_beauty_and_a_beat_filename() -> None:
    """Regression test for the exact failing case: admin uploads
    'Justin Bieber - Beauty And A Beat (Official Music Video) ft. Nicki Minaj.mp3'
    (the .mp3 is gone by the time it reaches here, but the rest is the
    track title field). LRCLIB needs `Beauty And A Beat` / `Justin Bieber`."""
    title, artist = build_lyrics_search_query(
        "Justin Bieber - Beauty And A Beat (Official Music Video) ft. Nicki Minaj",
        "Justin Bieber",
    )
    assert title == "Beauty And A Beat"
    assert artist == "Justin Bieber"


def test_preserves_parenthetical_feat() -> None:
    """The `(feat. ...)` form stays — that's the canonical title shape on
    many services (Apple Music, Spotify). LRCLIB indexes both, and the
    parenthetical variant is more likely to match older recordings."""
    title, _ = build_lyrics_search_query("Save Your Tears (feat. Ariana Grande)", "The Weeknd")
    assert title == "Save Your Tears (feat. Ariana Grande)"


def test_does_not_strip_leading_featuring() -> None:
    """A title that legitimately STARTS with 'Featuring' (e.g. a track
    literally named 'Featuring You') must NOT be stripped — the regex is
    anchored to require leading whitespace, so a token at position 0 of
    the string doesn't match."""
    title, _ = build_lyrics_search_query("Featuring You", "Some Artist")
    assert title == "Featuring You"


def test_does_not_strip_ft_inside_word() -> None:
    """A title with 'ft' as a substring of a longer word (e.g. 'Software')
    must not be touched."""
    title, _ = build_lyrics_search_query("Software Update Blues", "Artist")
    assert title == "Software Update Blues"
