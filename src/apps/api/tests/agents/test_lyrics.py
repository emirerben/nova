"""LyricsExtractionAgent.compute() integration tests with Genius + Whisper mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agents._runtime import TerminalError
from app.agents.lyrics import LyricsExtractionAgent, LyricsInput
from app.services.genius_client import GeniusLyrics, GeniusNotFound
from app.services.whisper_lyrics import WhisperLyricsError, WhisperLyricsResult, WhisperWord


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
