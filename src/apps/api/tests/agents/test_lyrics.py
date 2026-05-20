"""LyricsExtractionAgent.compute() integration tests with Genius + Whisper mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog
from structlog.testing import capture_logs

from app.agents._runtime import TerminalError
from app.agents.lyrics import LyricsExtractionAgent, LyricsInput
from app.services.genius_client import GeniusError, GeniusLyrics, GeniusNotFound
from app.services.whisper_lyrics import WhisperLyricsError, WhisperLyricsResult, WhisperWord


@pytest.fixture(autouse=True)
def _reset_structlog():
    """capture_logs() depends on structlog defaults — reset to clear any
    test-side patching of context vars or processors."""
    structlog.reset_defaults()
    yield


def _ww(text: str, start: float, end: float) -> WhisperWord:
    return WhisperWord(text=text, start_s=start, end_s=end)


def _genius_result() -> GeniusLyrics:
    return GeniusLyrics(
        title="Test Song",
        artist="Test Artist",
        lines=("Hello world", "Bir gün gelir"),
        genius_url="https://genius.com/test",
    )


def _whisper_result(language: str = "en") -> WhisperLyricsResult:
    return WhisperLyricsResult(
        words=(
            _ww("hello", 0.0, 0.4),
            _ww("world", 0.4, 0.9),
            _ww("bir", 1.5, 1.7),
            _ww("gun", 1.7, 1.9),
            _ww("gelir", 1.9, 2.3),
        ),
        full_text="hello world bir gun gelir",
        language=language,
    )


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lyrics")
def test_genius_plus_whisper_happy_path(mock_genius, mock_whisper) -> None:
    mock_genius.return_value = _genius_result()
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(
        LyricsInput(
            audio_path="/tmp/fake.m4a",
            track_title="Test Song",
            artist="Test Artist",
        )
    )

    assert out.source == "genius+whisper"
    assert out.language == "en"
    assert out.track_title_matched == "Test Song"
    assert out.genius_url == "https://genius.com/test"
    assert len(out.lines) == 2
    assert out.lines[0].text == "Hello world"
    assert out.confidence > 0.9
    # Whisper got the Genius lyric text as biasing prompt
    assert mock_whisper.call_args.kwargs["prompt"] == "Hello world\nBir gün gelir"


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lyrics")
def test_genius_miss_falls_back_to_whisper_only(mock_genius, mock_whisper) -> None:
    mock_genius.side_effect = GeniusNotFound("no hits")
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="Obscure"))

    assert out.source == "whisper_only"
    # whisper_only confidence is fixed; no canonical text to compare against
    assert out.confidence == 0.5
    # Lines should be grouped from Whisper words (gap-based segmentation)
    assert len(out.lines) >= 1
    # Whisper was called with empty prompt (no Genius text to bias on)
    assert mock_whisper.call_args.kwargs["prompt"] == ""


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lyrics")
def test_genius_not_found_emits_observability_log(mock_genius, mock_whisper) -> None:
    """Without this log, prod tracks dropping to whisper_only have no
    breadcrumb — same observability hole that hid the 2026-05-19 Genius
    CDN-block incident for an hour."""
    mock_genius.side_effect = GeniusNotFound("no hits for cleaned query")
    mock_whisper.return_value = _whisper_result()
    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]

    with capture_logs() as logs:
        agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="Obscure"))

    miss_events = [e for e in logs if e["event"] == "lyrics_genius_miss"]
    assert len(miss_events) == 1
    assert miss_events[0]["track_title"] == "Obscure"


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lyrics")
def test_genius_error_emits_observability_log(mock_genius, mock_whisper) -> None:
    """GeniusError covers token missing, 401, 429, network, scrape 4xx,
    scrape network. Every one of those must leave a `lyrics_genius_error`
    breadcrumb before falling back to whisper_only."""
    mock_genius.side_effect = GeniusError("genius page returned 403: https://genius.com/x")
    mock_whisper.return_value = _whisper_result()
    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]

    with capture_logs() as logs:
        out = agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="Y", artist="Z"))

    assert out.source == "whisper_only"
    err_events = [e for e in logs if e["event"] == "lyrics_genius_error"]
    assert len(err_events) == 1
    assert "403" in err_events[0]["error"]
    assert err_events[0]["track_title"] == "Y"


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lyrics")
def test_whisper_failure_raises_terminal(mock_genius, mock_whisper) -> None:
    mock_genius.return_value = _genius_result()
    mock_whisper.side_effect = WhisperLyricsError("API down")

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="whisper"):
        agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X"))


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lyrics")
def test_zero_whisper_words_raises_terminal(mock_genius, mock_whisper) -> None:
    mock_genius.return_value = _genius_result()
    mock_whisper.return_value = WhisperLyricsResult(words=(), full_text="", language="")

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="zero words"):
        agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X"))
