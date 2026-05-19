"""Genius client tests — mocked httpx, no real network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.genius_client import (
    GeniusError,
    GeniusLyrics,
    GeniusNotFound,
    _build_search_query,
    _extract_lyric_lines,
    _pick_best_hit,
    search_lyrics,
)


def _hit(title: str, primary_artist: str, url: str = "https://genius.com/x") -> dict:
    return {
        "result": {
            "title": title,
            "url": url,
            "primary_artist": {"name": primary_artist},
        }
    }


def test_pick_best_hit_prefers_artist_match() -> None:
    hits = [
        _hit("Hello", "Adele Cover Band"),
        _hit("Hello", "Adele"),
        _hit("Hello", "Lionel Richie"),
    ]
    assert _pick_best_hit(hits, "Hello", "Adele")["result"]["primary_artist"]["name"] == "Adele"


def test_build_search_query_strips_youtube_artist_prefix() -> None:
    """yt-dlp passes 'Artist - Title' as the title field. Strip that prefix when
    the artist is already known so the query is just 'Title Artist', not
    'Artist - Title Artist'."""
    q = _build_search_query("The Weeknd - Can't Feel My Face", "The Weeknd")
    assert q == "Can't Feel My Face The Weeknd"


def test_build_search_query_strips_official_video_tag() -> None:
    """Parenthetical noise like '(Official Video)' tanks Genius relevance."""
    q = _build_search_query(
        "The Weeknd - Can't Feel My Face (Official Video)", "The Weeknd"
    )
    assert q == "Can't Feel My Face The Weeknd"


def test_build_search_query_strips_brackets_and_multiple_tags() -> None:
    q = _build_search_query("Blinding Lights [Official Music Video] (HD)", "The Weeknd")
    assert q == "Blinding Lights The Weeknd"


def test_build_search_query_keeps_legitimate_parens() -> None:
    """'(feat. ...)' is part of the canonical title — don't strip it."""
    q = _build_search_query("Save Your Tears (feat. Ariana Grande)", "The Weeknd")
    assert "feat. Ariana Grande" in q
    assert "The Weeknd" in q


def test_build_search_query_handles_missing_artist() -> None:
    assert _build_search_query("Bohemian Rhapsody", "") == "Bohemian Rhapsody"
    assert _build_search_query("", "Queen") == "Queen"
    assert _build_search_query("", "") == ""


def test_build_search_query_handles_case_mismatch_in_prefix() -> None:
    """Title from YouTube may use different capitalization than the stored artist."""
    q = _build_search_query("THE WEEKND - Can't Feel My Face", "The Weeknd")
    assert q == "Can't Feel My Face The Weeknd"


def test_pick_best_hit_no_artist_returns_first() -> None:
    hits = [_hit("Yesterday", "The Beatles"), _hit("Yesterday", "Boyce Avenue")]
    assert (
        _pick_best_hit(hits, "Yesterday", "")["result"]["primary_artist"]["name"] == "The Beatles"
    )


def test_extract_lyric_lines_handles_modern_div() -> None:
    html = """
    <html><body>
      <div data-lyrics-container="true">
        [Verse 1]<br>
        Bir gün gelir bulurum seni<br>
        Bekle beni
      </div>
      <div data-lyrics-container="true">
        [Chorus]<br>
        Sen ve ben
      </div>
    </body></html>
    """
    lines = _extract_lyric_lines(html)
    assert lines == ["Bir gün gelir bulurum seni", "Bekle beni", "Sen ve ben"]


def test_extract_lyric_lines_strips_html_entities_and_tags() -> None:
    html = '<div data-lyrics-container="true">You&#x27;re my <i>only</i> one<br>I &amp; you</div>'
    assert _extract_lyric_lines(html) == ["You're my only one", "I & you"]


def test_extract_lyric_lines_returns_empty_when_no_container() -> None:
    assert _extract_lyric_lines("<html><body>nothing</body></html>") == []


@patch("app.services.genius_client.settings")
def test_search_lyrics_raises_when_token_missing(mock_settings: MagicMock) -> None:
    mock_settings.genius_access_token = ""
    with pytest.raises(GeniusError, match="GENIUS_ACCESS_TOKEN"):
        search_lyrics("Anything", "")


@patch("app.services.genius_client.httpx.Client")
@patch("app.services.genius_client.settings")
def test_search_lyrics_raises_not_found_on_empty_hits(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.genius_access_token = "fake"
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"response": {"hits": []}}
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client_cls.return_value.__enter__.return_value = mock_client

    with pytest.raises(GeniusNotFound):
        search_lyrics("Obscure", "Unknown")


@patch("app.services.genius_client.httpx.Client")
@patch("app.services.genius_client.settings")
def test_search_lyrics_returns_parsed_lyrics(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.genius_access_token = "fake"

    # First call: search returns one hit
    search_resp = MagicMock(status_code=200)
    search_resp.json.return_value = {
        "response": {"hits": [_hit("Test Song", "Test Artist", "https://genius.com/test")]}
    }
    # Second call: page HTML
    page_resp = MagicMock(status_code=200)
    page_resp.text = '<div data-lyrics-container="true">[Verse]<br>Hello world<br>Foo bar</div>'

    mock_search_client = MagicMock()
    mock_search_client.get.return_value = search_resp
    mock_page_client = MagicMock()
    mock_page_client.get.return_value = page_resp

    # httpx.Client used twice — first for search, then for page fetch
    mock_client_cls.return_value.__enter__.side_effect = [
        mock_search_client,
        mock_page_client,
    ]

    result = search_lyrics("Test Song", "Test Artist")
    assert isinstance(result, GeniusLyrics)
    assert result.lines == ("Hello world", "Foo bar")
    assert result.title == "Test Song"
    assert result.artist == "Test Artist"
    assert result.genius_url == "https://genius.com/test"


@patch("app.services.genius_client.httpx.Client")
@patch("app.services.genius_client.settings")
def test_search_lyrics_propagates_429_as_error(
    mock_settings: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    mock_settings.genius_access_token = "fake"
    mock_resp = MagicMock(status_code=429, text="rate limited")
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client_cls.return_value.__enter__.return_value = mock_client

    with pytest.raises(GeniusError, match="rate-limited"):
        search_lyrics("X", "Y")
