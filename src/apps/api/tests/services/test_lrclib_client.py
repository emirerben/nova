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
    LrclibSearchCandidate,
    SyncedLine,
    _parse_plain_lyrics,
    _parse_synced_lyrics,
    get_lrclib_by_id,
    search_lrclib,
    search_lrclib_fuzzy,
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
def test_search_lrclib_passes_duration_when_supplied(mock_client_cls: MagicMock) -> None:
    """LRCLIB `/api/get` accepts a `duration` (integer seconds, ±2s tolerance)
    that disambiguates between recordings of the same song (radio edit vs.
    remix vs. extended). Without it, LRCLIB returns whatever row matches
    title+artist first — its syncedLyrics line anchors then come from the
    wrong recording and the line-anchored alignment writes wildly wrong
    timestamps (the "Hawai" bug). When duration_s is supplied, it must
    appear in the outgoing query as `duration=<int>` (no millis, no float).
    """
    body = {
        "id": 1,
        "trackName": "Hawai",
        "artistName": "Maluma",
        "instrumental": False,
        "plainLyrics": "x",
        "syncedLyrics": "[00:01.00]x",
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    search_lrclib("Hawai", "Maluma", duration_s=211.6)

    # Inspect what was actually sent to LRCLIB.
    args, kwargs = client.get.call_args
    sent_params = kwargs.get("params") if "params" in kwargs else args[1]
    assert sent_params["duration"] == "212", (
        f"duration must round to nearest int, got {sent_params.get('duration')!r}"
    )
    assert sent_params["track_name"] == "Hawai"
    assert sent_params["artist_name"] == "Maluma"


@patch("app.services.lrclib_client.httpx.Client")
def test_search_lrclib_omits_duration_when_zero_or_none(mock_client_cls: MagicMock) -> None:
    """`duration` param must NOT be sent when caller passes None or 0 —
    that's the legacy "unknown duration" path. Sending `duration=0` would
    cause LRCLIB to filter for ~instant tracks and 404 everything."""
    body = {
        "id": 1,
        "trackName": "X",
        "artistName": "Y",
        "instrumental": False,
        "plainLyrics": "x",
        "syncedLyrics": None,
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    # No duration arg → no duration param.
    search_lrclib("X", "Y")
    args, kwargs = client.get.call_args
    sent_params = kwargs.get("params") if "params" in kwargs else args[1]
    assert "duration" not in sent_params

    # duration_s=0 → also no duration param (None and 0 are both "unknown").
    client.get.reset_mock()
    search_lrclib("X", "Y", duration_s=0.0)
    args, kwargs = client.get.call_args
    sent_params = kwargs.get("params") if "params" in kwargs else args[1]
    assert "duration" not in sent_params


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


# ── get_lrclib_by_id ──────────────────────────────────────────────────────────


@patch("app.services.lrclib_client.httpx.Client")
def test_get_by_id_happy_path(mock_client_cls: MagicMock) -> None:
    """Admin force-ID flow: paste numeric ID, agent re-fetches that exact row."""
    body = {
        "id": 12345,
        "trackName": "Beauty And A Beat",
        "artistName": "Justin Bieber",
        "instrumental": False,
        "plainLyrics": "Show me off, show me off",
        "syncedLyrics": "[00:12.00]Show me off, show me off",
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    out = get_lrclib_by_id(12345)
    assert isinstance(out, LrclibLyrics)
    assert out.lrclib_id == 12345
    assert out.title == "Beauty And A Beat"
    assert out.synced_lines is not None
    assert len(out.synced_lines) == 1

    # Verify the URL was the path-param form, not a query-param one.
    args, _ = client.get.call_args
    assert args[0] == "https://lrclib.net/api/get/12345"


@patch("app.services.lrclib_client.httpx.Client")
def test_get_by_id_404_raises_not_found(mock_client_cls: MagicMock) -> None:
    client = MagicMock()
    client.get.return_value = _resp(404)
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibNotFound):
        get_lrclib_by_id(99999999)


def test_get_by_id_rejects_zero() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        get_lrclib_by_id(0)


def test_get_by_id_rejects_negative() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        get_lrclib_by_id(-5)


def test_get_by_id_rejects_non_int() -> None:
    with pytest.raises(ValueError):
        get_lrclib_by_id("12345")  # type: ignore[arg-type]


@patch("app.services.lrclib_client.httpx.Client")
def test_get_by_id_instrumental_returns_flag(mock_client_cls: MagicMock) -> None:
    body = {
        "id": 7,
        "trackName": "Beat Only",
        "artistName": "DJ",
        "instrumental": True,
        "plainLyrics": "",
        "syncedLyrics": None,
    }
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    out = get_lrclib_by_id(7)
    assert out.instrumental is True


# ── search_lrclib_fuzzy ───────────────────────────────────────────────────────


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_strong_match_top_result(mock_client_cls: MagicMock) -> None:
    """Common case after /api/get 404: /api/search returns the right row
    as top result. Should score above the combined-score gate."""
    body = [
        {
            "id": 50001,
            "trackName": "Beauty And A Beat",
            "artistName": "Justin Bieber",
            "duration": 211,
            "instrumental": False,
        },
        {
            "id": 50002,
            "trackName": "Beauty And A Beat (Karaoke)",
            "artistName": "Karaoke Library",
            "duration": 215,
            "instrumental": False,
        },
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    candidates = search_lrclib_fuzzy("Beauty And A Beat", "Justin Bieber", duration_s=212.0)
    assert len(candidates) >= 1
    top = candidates[0]
    assert isinstance(top, LrclibSearchCandidate)
    assert top.lrclib_id == 50001
    assert top.title == "Beauty And A Beat"
    assert top.combined_score >= 0.85, f"top score was {top.combined_score}"
    # The karaoke row should be either rejected (wrong artist) or scored lower.
    if len(candidates) > 1:
        assert candidates[1].combined_score < top.combined_score


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_does_not_pass_duration_param(mock_client_cls: MagicMock) -> None:
    """`/api/search` accepts a duration param with the SAME ±2s hard gate
    as `/api/get`. Sending it would defeat the purpose of /search as a
    relaxed fallback — music-video uploads with intro/outro padding
    would 404 all over again. The function must NOT pass duration."""
    body = [
        {
            "id": 1,
            "trackName": "X",
            "artistName": "Y",
            "duration": 200,
        }
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    search_lrclib_fuzzy("X", "Y", duration_s=215.0)

    args, kwargs = client.get.call_args
    sent_params = kwargs.get("params") if "params" in kwargs else args[1]
    assert "duration" not in sent_params, (
        "Duration param sent to /api/search; this would re-introduce the strict gate"
    )


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_wrong_artist_filtered(mock_client_cls: MagicMock) -> None:
    """Title can collide between unrelated artists ('Hello' by Adele vs
    'Hello' by Lionel Richie). Artist mismatch must drop the candidate
    entirely, not just penalize it."""
    body = [
        {"id": 1, "trackName": "Hello", "artistName": "Lionel Richie", "duration": 250},
        {"id": 2, "trackName": "Hello", "artistName": "Adele", "duration": 295},
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    candidates = search_lrclib_fuzzy("Hello", "Adele", duration_s=295.0)
    assert len(candidates) == 1
    assert candidates[0].lrclib_id == 2
    assert candidates[0].artist == "Adele"


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_weak_title_filtered(mock_client_cls: MagicMock) -> None:
    """If the only word a candidate shares with the request is a stop-word
    style token, title similarity stays below the hard gate."""
    body = [
        {
            "id": 1,
            "trackName": "The Day The World Ended",
            "artistName": "Some Band",
            "duration": 200,
        },
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    candidates = search_lrclib_fuzzy("The Sound Of Silence", "Simon Garfunkel")
    assert candidates == []


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_duration_soft_signal_tolerates_intro_padding(
    mock_client_cls: MagicMock,
) -> None:
    """Music-video uploads commonly carry 5-15s of spoken intro that the
    LRCLIB recording does not. A 12s delta must NOT reject the candidate;
    the title+artist gates should still admit it (combined score above
    threshold)."""
    body = [
        {
            "id": 1,
            "trackName": "Beauty And A Beat",
            "artistName": "Justin Bieber",
            "duration": 200,  # 12s less than the user's audio
        }
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    candidates = search_lrclib_fuzzy("Beauty And A Beat", "Justin Bieber", duration_s=212.0)
    assert len(candidates) == 1
    assert candidates[0].duration_delta_s == 12.0
    # Title match (1.0) + artist match (1.0) + duration_score ~0.2 → ~0.84-0.85.
    # Should still cross the combined-score gate for a strong title+artist match.
    assert candidates[0].combined_score >= 0.83


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_drops_extreme_duration_delta(mock_client_cls: MagicMock) -> None:
    """A 90s duration delta means this isn't the same recording (extended
    mix or different song entirely). Hard-drop, don't even score."""
    body = [
        {
            "id": 1,
            "trackName": "Beauty And A Beat",
            "artistName": "Justin Bieber",
            "duration": 120,  # 92s less than 212
        }
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    candidates = search_lrclib_fuzzy("Beauty And A Beat", "Justin Bieber", duration_s=212.0)
    assert candidates == []


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_empty_result_raises_not_found(mock_client_cls: MagicMock) -> None:
    """Treating an empty JSON array as not-found makes the fallback path
    consistent with /api/get (also raises LrclibNotFound on empty)."""
    client = MagicMock()
    client.get.return_value = _resp(200, [])
    mock_client_cls.return_value.__enter__.return_value = client

    with pytest.raises(LrclibNotFound):
        search_lrclib_fuzzy("Obscure Indie Track 12345", "Unknown Artist")


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_artist_with_feat_normalized(mock_client_cls: MagicMock) -> None:
    """Admin uploads sometimes have the artist field already including
    'ft. Other'. Match the canonical artist by stripping the tail on
    both sides."""
    body = [
        {
            "id": 1,
            "trackName": "Some Song",
            "artistName": "Main Artist",
            "duration": 200,
        }
    ]
    client = MagicMock()
    client.get.return_value = _resp(200, body)
    mock_client_cls.return_value.__enter__.return_value = client

    candidates = search_lrclib_fuzzy(
        "Some Song", "Main Artist ft. Featured Artist", duration_s=200.0
    )
    assert len(candidates) == 1


@patch("app.services.lrclib_client.httpx.Client")
def test_fuzzy_search_reuses_429_retry(mock_client_cls: MagicMock) -> None:
    """Same retry behavior as search_lrclib — keyless rate limit is shared."""
    body = [{"id": 1, "trackName": "X", "artistName": "Y", "duration": 200}]
    client = MagicMock()
    client.get.side_effect = [_resp(429, headers={}), _resp(200, body)]
    mock_client_cls.return_value.__enter__.return_value = client

    with patch("app.services.lrclib_client.time.sleep"):
        candidates = search_lrclib_fuzzy("X", "Y")
    assert len(candidates) == 1
    assert client.get.call_count == 2


def test_fuzzy_search_empty_title_raises_not_found() -> None:
    with pytest.raises(LrclibNotFound):
        search_lrclib_fuzzy("", "Some Artist")
