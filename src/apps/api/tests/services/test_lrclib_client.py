"""LRCLIB client tests — mocked httpx, no real network.

Covers: HTTP path (200/404/429/5xx/timeout), LRC parser edge cases
(multi-timestamp choruses, metadata tags, blank text lines, BOM,
hundredths vs thousandths), and retry behavior on 429.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.lrclib_client import (
    LrclibError,
    LrclibLyrics,
    LrclibNotFound,
    SyncedLine,
    _parse_plain_lyrics,
    _parse_synced_lyrics,
    search_lrclib,
)


def _resp(status: int, json_body: object | None = None, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = "" if json_body is None else "mocked"
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


# ── HTTP behavior ─────────────────────────────────────────────────────────────


@patch("app.services.lrclib_client.httpx.Client")
def test_happy_path_with_synced_lyrics(mock_client_cls: MagicMock) -> None:
    body = {
        "id": 12345,
        "trackName": "Test Song",
        "artistName": "Test Artist",
        "instrumental": False,
        "plainLyrics": "Hello world\nSecond line",
        "syncedLyrics": "[00:01.00]Hello world\n[00:03.50]Second line",
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    out = search_lrclib("Test Song", "Test Artist")
    assert isinstance(out, LrclibLyrics)
    assert out.title == "Test Song"
    assert out.artist == "Test Artist"
    assert out.lrclib_id == 12345
    assert out.instrumental is False
    assert out.plain_lines == ("Hello world", "Second line")
    assert out.synced_lines is not None
    assert len(out.synced_lines) == 2
    assert out.synced_lines[0] == SyncedLine(start_s=1.0, text="Hello world")
    assert out.synced_lines[1] == SyncedLine(start_s=3.5, text="Second line")


@patch("app.services.lrclib_client.httpx.Client")
def test_happy_path_with_plain_lyrics_only(mock_client_cls: MagicMock) -> None:
    body = {
        "id": 1,
        "trackName": "X",
        "artistName": "Y",
        "instrumental": False,
        "plainLyrics": "Just plain\nText only",
        "syncedLyrics": None,
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    out = search_lrclib("X", "Y")
    assert out.plain_lines == ("Just plain", "Text only")
    assert out.synced_lines is None


@patch("app.services.lrclib_client.httpx.Client")
def test_404_raises_not_found(mock_client_cls: MagicMock) -> None:
    client = MagicMock()
    client.get.return_value = _resp(404)
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibNotFound):
        search_lrclib("Obscure", "Unknown")


@patch("app.services.lrclib_client.httpx.Client")
def test_200_with_not_found_error_body_raises_not_found(mock_client_cls: MagicMock) -> None:
    """LRCLIB occasionally returns 200 with a NotFoundError envelope
    (regional CDN caching artifact). Treat as 404."""
    body = {"statusCode": 404, "name": "NotFoundError", "error": "Track not found"}
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibNotFound):
        search_lrclib("X", "Y")


@patch("app.services.lrclib_client.httpx.Client")
def test_200_with_empty_body_raises_not_found(mock_client_cls: MagicMock) -> None:
    body = {
        "id": 1,
        "trackName": "X",
        "artistName": "Y",
        "instrumental": False,
        "plainLyrics": "",
        "syncedLyrics": "",
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibNotFound):
        search_lrclib("X", "Y")


@patch("app.services.lrclib_client.httpx.Client")
def test_instrumental_returns_flag(mock_client_cls: MagicMock) -> None:
    """Instrumental tracks must NOT be treated as not-found — LRCLIB
    knows the track, it just has no lyrics. The agent reads this flag
    and routes to lyrics_status='unavailable'."""
    body = {
        "id": 99,
        "trackName": "Beat Only",
        "artistName": "DJ",
        "instrumental": True,
        "plainLyrics": "",
        "syncedLyrics": None,
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    out = search_lrclib("Beat Only", "DJ")
    assert out.instrumental is True
    assert out.plain_lines == ()
    assert out.synced_lines is None


@patch("app.services.lrclib_client.httpx.Client")
def test_500_raises_lrclib_error_no_retry(mock_client_cls: MagicMock) -> None:
    """5xx is a different failure mode than 429 — service broken vs
    throttled. Don't retry; fail fast so the agent falls back."""
    client = MagicMock()
    client.get.return_value = _resp(500)
    client.get.return_value.text = "internal"
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibError):
        search_lrclib("X", "Y")

    assert client.get.call_count == 1, "5xx should not retry"


@patch("app.services.lrclib_client.httpx.Client")
def test_network_error_raises_lrclib_error(mock_client_cls: MagicMock) -> None:
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("DNS failure")
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibError, match="network error"):
        search_lrclib("X", "Y")


@patch("app.services.lrclib_client.httpx.Client")
def test_malformed_json_raises_lrclib_error(mock_client_cls: MagicMock) -> None:
    resp = MagicMock(spec=httpx.Response, status_code=200, headers={}, text="not json")
    resp.json.side_effect = ValueError("Expecting value")
    client = MagicMock()
    client.get.return_value = resp
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibError, match="non-JSON"):
        search_lrclib("X", "Y")


def test_empty_title_raises_not_found() -> None:
    """Defense — never hit the network with an empty track_name."""
    with pytest.raises(LrclibNotFound):
        search_lrclib("", "Some Artist")


# ── 429 retry behavior ────────────────────────────────────────────────────────


@patch("app.services.lrclib_client.time.sleep")
@patch("app.services.lrclib_client.httpx.Client")
def test_429_then_200_succeeds_after_retry(
    mock_client_cls: MagicMock, mock_sleep: MagicMock
) -> None:
    """Common case: parallel Celery batch trips the rate limit once.
    A single retry should succeed without degrading to whisper-only."""
    success_body = {
        "id": 1,
        "trackName": "X",
        "artistName": "Y",
        "instrumental": False,
        "plainLyrics": "ok",
        "syncedLyrics": None,
    }
    client = MagicMock()
    client.get.side_effect = [_resp(429, headers={}), _resp(200, success_body)]
    mock_client_cls.return_value.__enter__.return_value = client

    out = search_lrclib("X", "Y")
    assert out.plain_lines == ("ok",)
    assert client.get.call_count == 2
    mock_sleep.assert_called_once()


@patch("app.services.lrclib_client.time.sleep")
@patch("app.services.lrclib_client.httpx.Client")
def test_429_exhausts_retries_raises_lrclib_error(
    mock_client_cls: MagicMock, mock_sleep: MagicMock
) -> None:
    """If LRCLIB stays 429 for all 4 attempts (initial + 3 retries),
    surface as LrclibError so the agent falls back to whisper-only."""
    client = MagicMock()
    client.get.return_value = _resp(429)
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibError, match="rate-limited"):
        search_lrclib("X", "Y")

    assert client.get.call_count == 4  # initial + 3 retries
    assert mock_sleep.call_count == 3


@patch("app.services.lrclib_client.time.sleep")
@patch("app.services.lrclib_client.httpx.Client")
def test_429_honors_retry_after_header(mock_client_cls: MagicMock, mock_sleep: MagicMock) -> None:
    """When LRCLIB sends Retry-After, use that delay (jittered) instead
    of the computed backoff."""
    success_body = {
        "id": 1,
        "trackName": "X",
        "artistName": "Y",
        "instrumental": False,
        "plainLyrics": "ok",
        "syncedLyrics": None,
    }
    client = MagicMock()
    client.get.side_effect = [
        _resp(429, headers={"Retry-After": "2"}),
        _resp(200, success_body),
    ]
    mock_client_cls.return_value.__enter__.return_value = client

    search_lrclib("X", "Y")

    # Retry-After=2 with ±20% jitter → sleep ∈ [1.6, 2.4]
    actual_sleep = mock_sleep.call_args.args[0]
    assert 1.6 <= actual_sleep <= 2.4


# ── LRC parser ────────────────────────────────────────────────────────────────


def test_parse_synced_single_timestamp() -> None:
    out = _parse_synced_lyrics("[00:12.50]Never gonna give you up")
    assert out == (SyncedLine(start_s=12.5, text="Never gonna give you up"),)


def test_parse_synced_multi_timestamp_chorus_expands() -> None:
    """Standard LRC chorus shorthand: one text line, multiple timestamps."""
    text = "[00:12.00][01:12.00][02:30.50]Never gonna give you up"
    out = _parse_synced_lyrics(text)
    assert out is not None
    assert len(out) == 3
    # All three timestamps share the same text body.
    assert all(line.text == "Never gonna give you up" for line in out)
    # Sorted by start_s ascending.
    assert [line.start_s for line in out] == [12.0, 72.0, 150.5]


def test_parse_synced_multi_timestamp_mixed_with_singles() -> None:
    """Multi-timestamp lines mixed with single-timestamp lines all get
    sorted into one ordered sequence."""
    text = "[00:05.00]Verse line one\n[00:30.00][01:30.00]Chorus shared\n[00:50.00]Verse line two"
    out = _parse_synced_lyrics(text)
    assert out is not None
    times = [line.start_s for line in out]
    assert times == [5.0, 30.0, 50.0, 90.0]


def test_parse_synced_skips_metadata_tags() -> None:
    """LRC files put [ar:Artist], [ti:Title], [length:03:21] at the top.
    Distinguishable from timestamps by an alpha prefix — skip them."""
    text = "[ar:The Weeknd]\n[ti:Blinding Lights]\n[length:03:21]\n[00:12.00]Real lyric line"
    out = _parse_synced_lyrics(text)
    assert out == (SyncedLine(start_s=12.0, text="Real lyric line"),)


def test_parse_synced_skips_blank_text_lines() -> None:
    """`[01:23.45]` with no body = instrumental break. Skip — the
    alignment layer will interpolate around it."""
    text = "[00:10.00]first\n[00:20.00]\n[00:30.00]third"
    out = _parse_synced_lyrics(text)
    assert out is not None
    assert len(out) == 2
    assert [line.text for line in out] == ["first", "third"]


def test_parse_synced_skips_section_markers() -> None:
    """[Verse 1] and [Chorus] markers don't match the timestamp regex
    and aren't caught by the metadata regex (no colon). Skip silently."""
    text = "[Verse 1]\n[00:12.00]Real line\n[Chorus]\n[00:30.00]Another"
    out = _parse_synced_lyrics(text)
    assert out is not None
    assert [line.text for line in out] == ["Real line", "Another"]


def test_parse_synced_thousandths_precision() -> None:
    """LRCLIB occasionally emits `.xxx` (millisecond precision). Parse
    correctly — `.123` should be 123ms, not 12.3ms."""
    out = _parse_synced_lyrics("[00:00.123]test")
    assert out == (SyncedLine(start_s=0.123, text="test"),)


def test_parse_synced_hundredths_precision() -> None:
    """`.xx` (centisecond) is the LRC standard. Pad to milliseconds —
    `.50` is 500ms."""
    out = _parse_synced_lyrics("[00:00.50]test")
    assert out == (SyncedLine(start_s=0.5, text="test"),)


def test_parse_synced_single_digit_fractional() -> None:
    """`.5` is left-aligned per LRC convention — 500ms, not 5ms."""
    out = _parse_synced_lyrics("[00:00.5]test")
    assert out == (SyncedLine(start_s=0.5, text="test"),)


def test_parse_synced_no_fractional() -> None:
    out = _parse_synced_lyrics("[01:23]no fraction")
    assert out == (SyncedLine(start_s=83.0, text="no fraction"),)


def test_parse_synced_strips_bom() -> None:
    out = _parse_synced_lyrics("﻿[00:01.00]bom test")
    assert out == (SyncedLine(start_s=1.0, text="bom test"),)


def test_parse_synced_returns_none_on_empty_input() -> None:
    assert _parse_synced_lyrics("") is None
    assert _parse_synced_lyrics("[ar:Only metadata]") is None


def test_parse_plain_strips_bom_and_drops_blanks() -> None:
    out = _parse_plain_lyrics("﻿first\n\nsecond\n   \nthird")
    assert out == ("first", "second", "third")


def test_parse_plain_empty_returns_empty_tuple() -> None:
    assert _parse_plain_lyrics("") == ()
