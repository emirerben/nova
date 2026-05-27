"""LyricsExtractionAgent.compute() integration tests with LRCLIB + Whisper mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agents._runtime import TerminalError
from app.agents.lyrics import (
    LyricsExtractionAgent,
    LyricsInput,
    LyricsOutput,
    _truncate_whisper_prompt,
)
from app.services.lrclib_client import LrclibError, LrclibLyrics, LrclibNotFound, SyncedLine
from app.services.whisper_lyrics import WhisperLyricsError, WhisperLyricsResult, WhisperWord


def _ww(text: str, start: float, end: float) -> WhisperWord:
    return WhisperWord(text=text, start_s=start, end_s=end)


def _lrclib_synced() -> LrclibLyrics:
    """LRCLIB result with syncedLyrics — the best-quality input shape."""
    return LrclibLyrics(
        title="Test Song",
        artist="Test Artist",
        plain_lines=("Hello world", "Bir gun gelir"),
        synced_lines=(
            SyncedLine(start_s=0.0, text="Hello world"),
            SyncedLine(start_s=1.5, text="Bir gun gelir"),
        ),
        instrumental=False,
        lrclib_id=99,
    )


def _lrclib_plain_only() -> LrclibLyrics:
    return LrclibLyrics(
        title="Test Song",
        artist="Test Artist",
        plain_lines=("Hello world", "Bir gun gelir"),
        synced_lines=None,
        instrumental=False,
        lrclib_id=99,
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


# ── Happy paths ───────────────────────────────────────────────────────────────


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_lrclib_synced_plus_whisper_happy_path(mock_lrclib, mock_whisper) -> None:
    mock_lrclib.return_value = _lrclib_synced()
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(
        LyricsInput(
            audio_path="/tmp/fake.m4a",
            track_title="Test Song",
            artist="Test Artist",
        )
    )

    assert out.source == "lrclib_synced+whisper"
    assert out.language == "en"
    assert out.track_title_matched == "Test Song"
    assert out.lrclib_id == 99
    assert out.genius_url == ""  # cleared on new LRCLIB extractions
    assert len(out.lines) == 2
    assert out.lines[0].text == "Hello world"
    # Whisper got the canonical text as a biasing prompt (50-word capped).
    assert "Hello world" in mock_whisper.call_args.kwargs["prompt"]


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_lrclib_plain_falls_back_to_fuzzy_align(mock_lrclib, mock_whisper) -> None:
    """LRCLIB row has plainLyrics but no syncedLyrics — agent uses the
    legacy fuzzy `align()` path with text-only canonical input."""
    mock_lrclib.return_value = _lrclib_plain_only()
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(
        LyricsInput(audio_path="/tmp/x.m4a", track_title="Test Song", artist="Test Artist")
    )

    assert out.source == "lrclib_plain+whisper"
    assert out.track_title_matched == "Test Song"
    assert out.lrclib_id == 99
    assert len(out.lines) >= 1


# ── Fallback paths ────────────────────────────────────────────────────────────


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_lrclib_miss_falls_back_to_whisper_only(mock_lrclib, mock_whisper) -> None:
    mock_lrclib.side_effect = LrclibNotFound("no entry")
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="Obscure"))

    assert out.source == "whisper_only"
    assert out.confidence == 0.5
    assert len(out.lines) >= 1
    # No prompt — nothing to bias Whisper with.
    assert mock_whisper.call_args.kwargs["prompt"] == ""


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_lrclib_error_falls_back_to_whisper_only(mock_lrclib, mock_whisper) -> None:
    """Network failure / 429-exhaustion / 5xx — all surface as LrclibError
    and the agent must continue with Whisper-only rather than raise."""
    mock_lrclib.side_effect = LrclibError("network broken")
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X"))

    assert out.source == "whisper_only"


# ── Version-mismatch defense (Hawai regression) ──────────────────────────────


def _lrclib_synced_wrong_recording() -> LrclibLyrics:
    """LRCLIB rows from a DIFFERENT recording of the same song. Synced
    anchors are clustered in the 0–10s range but the uploaded audio puts
    those lyrics 20s+ later. Mimics the production Hawai shape: many
    anchors none of which line up with where Whisper finds the words.

    Note on test geometry: `align_with_line_anchors` makes the LAST
    anchor's window extend to `whisper.words[-1].end_s`, so a 2-anchor
    test would accidentally let the final anchor capture all the Whisper
    words via Strategy 1 and boost confidence above the fallback
    threshold. We add a third decoy anchor whose text does NOT fuzzy-match
    any Whisper word so Strategy 2 produces zero matches there too —
    matching what happens in production where LRCLIB has 30+ anchors and
    only one of them happens to overlap a Whisper word.
    """
    return LrclibLyrics(
        title="Hawai",
        artist="Maluma",
        plain_lines=("Hello world", "so now he's your heaven yeah"),
        synced_lines=(
            SyncedLine(start_s=0.5, text="Hello world"),
            SyncedLine(start_s=4.59, text="so now he's your heaven yeah"),
            # Decoy: throwaway text in the trailing window. None of these
            # tokens fuzzy-match any Whisper word (similarity < 0.65), so
            # Strategy 2 produces zero matches and confidence stays low.
            SyncedLine(start_s=11.0, text="xkcd zzz qqq"),
        ),
        instrumental=False,
        lrclib_id=999,
    )


def _whisper_result_offset_22s() -> WhisperLyricsResult:
    """Whisper transcribed the uploaded audio correctly — the lyrics actually
    play at ~22s, NOT where LRCLIB's anchors say. Exact bug shape from the
    Hawai prod incident."""
    return WhisperLyricsResult(
        words=(
            # First line at 20-21s
            _ww("hello", 20.0, 20.5),
            _ww("world", 20.5, 21.0),
            # Bug line at 22-23s (NOT at LRCLIB's 4.59s)
            _ww("so", 22.0, 22.2),
            _ww("now", 22.2, 22.4),
            _ww("hes", 22.4, 22.6),
            _ww("your", 22.6, 22.9),
            _ww("heaven", 22.9, 23.4),
            _ww("yeah", 23.4, 23.8),
        ),
        full_text="hello world so now hes your heaven yeah",
        language="en",
    )


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_low_confidence_synced_falls_back_to_plain_whisper(mock_lrclib, mock_whisper) -> None:
    """The Hawai bug shape — LRCLIB returned a different recording's synced
    lyrics. align_with_line_anchors will run every line through Strategy 3
    (linear interpolation across the LRCLIB window) and produce a confidence
    near 0 with timestamps in the 0–10s range. The agent must detect this
    and fall back to the plain+whisper path so Whisper's real timings drive
    the line bounds, putting the bug line back at ~22s instead of 4.59s.
    """
    mock_lrclib.return_value = _lrclib_synced_wrong_recording()
    mock_whisper.return_value = _whisper_result_offset_22s()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(
        LyricsInput(
            audio_path="/tmp/hawai.m4a",
            track_title="Hawai",
            artist="Maluma",
            duration_s=211.6,  # passes through to LRCLIB as `duration`
        )
    )

    # Source flipped to plain+whisper — synced anchors rejected as bad.
    assert out.source == "lrclib_plain+whisper", (
        f"low-confidence synced should fall back to plain+whisper, got {out.source}"
    )
    # The bug line must now sit where the AUDIO has it, not where LRCLIB lied.
    bug_line = next(line for line in out.lines if "heaven" in line.text.lower())
    assert bug_line.start_s >= 20.0, (
        f"bug line should be at ~22s (real audio), not {bug_line.start_s}s (LRCLIB lie)"
    )
    assert bug_line.start_s < 25.0


@patch("app.config.settings")
@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_kill_switch_disabled_keeps_low_confidence_synced(
    mock_lrclib, mock_whisper, mock_settings
) -> None:
    """`LYRIC_SYNCED_ANCHOR_FALLBACK_ENABLED=false` reverts to pre-2026-05-27
    behavior: even when the synced alignment has near-zero confidence, the
    agent ships the synced result as-is instead of falling back. Reserved
    for emergency prod rollback if the 0.20 threshold demotes too many
    legitimate extractions. Pinning this test ensures the kill switch
    actually short-circuits the fallback gate."""
    mock_settings.lyric_synced_anchor_fallback_enabled = False
    mock_lrclib.return_value = _lrclib_synced_wrong_recording()
    mock_whisper.return_value = _whisper_result_offset_22s()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(LyricsInput(audio_path="/tmp/hawai.m4a", track_title="Hawai", artist="Maluma"))

    # Kill switch off → synced path wins even with bad confidence.
    assert out.source == "lrclib_synced+whisper"


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_low_confidence_synced_no_plain_falls_back_to_whisper_only(
    mock_lrclib, mock_whisper
) -> None:
    """Same version-mismatch shape, but LRCLIB row had no plainLyrics at all
    (rare — usually if synced exists, plain also does). Must still degrade
    gracefully to whisper_only rather than ship the wrong timestamps."""
    mock_lrclib.return_value = LrclibLyrics(
        title="X",
        artist="Y",
        plain_lines=(),
        synced_lines=(
            SyncedLine(start_s=0.5, text="anchor one"),
            SyncedLine(start_s=4.59, text="anchor two"),
        ),
        instrumental=False,
        lrclib_id=42,
    )
    mock_whisper.return_value = _whisper_result_offset_22s()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    out = agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X", artist="Y"))

    assert out.source == "whisper_only"
    assert len(out.lines) >= 1
    # Whisper-only timestamps are absolute — must be in the 20s range.
    assert out.lines[0].start_s >= 20.0


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_duration_s_passes_through_to_lrclib(mock_lrclib, mock_whisper) -> None:
    """LyricsInput.duration_s must reach search_lrclib so LRCLIB can pick
    the right recording at the lookup boundary (layer 1 of the defense)."""
    mock_lrclib.return_value = _lrclib_synced()
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    agent.run(
        LyricsInput(
            audio_path="/tmp/x.m4a",
            track_title="Test Song",
            artist="Test Artist",
            duration_s=211.6,
        )
    )

    # search_lrclib was called with duration_s kwarg matching the input.
    assert mock_lrclib.call_args.kwargs.get("duration_s") == 211.6


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_duration_s_zero_passes_none_to_lrclib(mock_lrclib, mock_whisper) -> None:
    """duration_s=0 (default, "unknown") must arrive at LRCLIB as None so
    the client omits the `duration` query param. Passing 0 verbatim would
    make LRCLIB filter for instant-length tracks and 404 everything."""
    mock_lrclib.return_value = _lrclib_synced()
    mock_whisper.return_value = _whisper_result()

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X"))

    # `duration_s or None` collapses 0.0 to None.
    assert mock_lrclib.call_args.kwargs.get("duration_s") is None


# ── Instrumental ──────────────────────────────────────────────────────────────


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_lrclib_instrumental_raises_terminal_with_instrumental_keyword(
    mock_lrclib, mock_whisper
) -> None:
    """LRCLIB-confirmed instrumental: skip Whisper entirely and raise a
    TerminalError whose message contains 'instrumental'. The orchestrator
    string-matches on this keyword to set lyrics_status='unavailable'
    rather than 'failed' (which would imply a real error)."""
    mock_lrclib.return_value = LrclibLyrics(
        title="Beat",
        artist="DJ",
        plain_lines=(),
        synced_lines=None,
        instrumental=True,
        lrclib_id=1,
    )

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="instrumental"):
        agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="Beat", artist="DJ"))

    # Whisper must NOT have been called — instrumental is decisive.
    mock_whisper.assert_not_called()


# ── Whisper failures ──────────────────────────────────────────────────────────


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_whisper_failure_raises_terminal(mock_lrclib, mock_whisper) -> None:
    mock_lrclib.return_value = _lrclib_synced()
    mock_whisper.side_effect = WhisperLyricsError("API down")

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="whisper"):
        agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X"))


@patch("app.agents.lyrics.transcribe_for_lyrics")
@patch("app.agents.lyrics.search_lrclib")
def test_zero_whisper_words_raises_terminal(mock_lrclib, mock_whisper) -> None:
    mock_lrclib.return_value = _lrclib_synced()
    mock_whisper.return_value = WhisperLyricsResult(words=(), full_text="", language="")

    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="zero words"):
        agent.run(LyricsInput(audio_path="/tmp/x.m4a", track_title="X"))


# ── _truncate_whisper_prompt ──────────────────────────────────────────────────


def test_truncate_prompt_caps_at_50_words() -> None:
    """Whisper's prompt has a ~224-token ceiling. Pasting a full lyric body
    silently end-truncates — dropping the chorus and risking repeat loops.
    Cap at 50 words upstream so the SONG OPENING is preserved (best
    section for vocabulary biasing)."""
    long_prompt = " ".join(f"word{i}" for i in range(200))
    out = _truncate_whisper_prompt(long_prompt)
    assert len(out.split()) == 50
    assert out.startswith("word0 word1")
    # Last word in the result must be from the beginning of the input,
    # NOT the end (which is what naive end-truncation would do).
    assert out.split()[-1] == "word49"


def test_truncate_prompt_preserves_short_input() -> None:
    """Input under 50 words should round-trip unchanged."""
    out = _truncate_whisper_prompt("Hello world\nshort song")
    assert out == "Hello world short song"


def test_truncate_prompt_empty_returns_empty_string() -> None:
    """transcribe_for_lyrics(prompt: str = '') treats '' as 'no prompt'."""
    assert _truncate_whisper_prompt("") == ""
    assert _truncate_whisper_prompt("   ") == ""


def test_truncate_prompt_chars_safety_cap() -> None:
    """One absurdly-long token shouldn't blow past the 800-char ceiling."""
    huge_word = "x" * 2000
    out = _truncate_whisper_prompt(f"{huge_word} second third")
    assert len(out) <= 800


# ── Legacy schema deserialization (REGRESSION) ────────────────────────────────


def test_legacy_genius_plus_whisper_blob_deserializes() -> None:
    """Existing MusicTrack.lyrics_cached rows have source='genius+whisper'
    and genius_url='https://genius.com/...'. The schema-widening must NOT
    break old rows — the music_job renderer reads them with model_validate
    and feeds them into lyric_injector unchanged.

    This is the IRON RULE regression lock for the schema change.
    """
    legacy_blob = {
        "source": "genius+whisper",
        "language": "tr",
        "track_title_matched": "Old Song",
        "artist_matched": "Old Artist",
        "genius_url": "https://genius.com/old-song",
        "confidence": 0.87,
        "lines": [
            {
                "text": "Hello world",
                "start_s": 0.0,
                "end_s": 1.5,
                "words": [
                    {"text": "Hello", "start_s": 0.0, "end_s": 0.5},
                    {"text": "world", "start_s": 0.6, "end_s": 1.5},
                ],
            }
        ],
    }

    parsed = LyricsOutput.model_validate(legacy_blob)
    assert parsed.source == "genius+whisper"
    assert parsed.genius_url == "https://genius.com/old-song"
    assert parsed.lrclib_id is None  # new field, defaults gracefully
    assert len(parsed.lines) == 1
    assert parsed.lines[0].words[0].text == "Hello"
