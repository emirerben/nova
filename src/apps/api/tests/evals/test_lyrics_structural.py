"""Structural evals for the lyrics cache contract.

The lyrics agent is rule-based, so this suite pins the persisted JSONB shape
that downstream admin tooling and backfills depend on.
"""

from __future__ import annotations

from app.agents.lyrics import (
    LyricsExtractionAgent,
    LyricsOutput,
    _attach_diagnostic,
    _Diagnostic,
)
from app.pipeline.lyrics_alignment import SyncedLine, WhisperWord, align_with_line_anchors


def _diagnostic() -> _Diagnostic:
    return _Diagnostic(
        query_title="Test Track",
        query_artist="Test Artist",
        query_duration_s=120.0,
        forced_lrclib_id=None,
    )


def test_lyrics_output_stamps_prompt_version_for_cache_invalidation() -> None:
    out = LyricsOutput(source="lrclib_synced+whisper")

    stamped = _attach_diagnostic(out, _diagnostic())

    assert stamped.prompt_version == LyricsExtractionAgent.spec.prompt_version
    assert stamped.prompt_version
    assert stamped.lyrics_diagnostic["query"]["title"] == "Test Track"


def test_legacy_cached_lyrics_without_prompt_version_remain_parseable() -> None:
    parsed = LyricsOutput.model_validate(
        {
            "source": "lrclib_synced+whisper",
            "language": "en",
            "lines": [],
        }
    )

    assert parsed.prompt_version == ""
    assert parsed.is_publishable


def test_late_lrclib_anchor_prefix_repair_keeps_following_anchor_local() -> None:
    """Eval guard for the Again/Roger Sanchez late-anchor cache regression."""
    line_text = "I swear to God, I don't even know why I put up with you"
    result = align_with_line_anchors(
        [
            SyncedLine(start_s=232.44, text=line_text),
            SyncedLine(start_s=235.63, text="Ok stop"),
        ],
        [
            WhisperWord(text="I", start_s=231.28, end_s=231.38),
            WhisperWord(text="swear", start_s=231.38, end_s=231.88),
            WhisperWord(text="to", start_s=231.88, end_s=232.08),
            WhisperWord(text="God", start_s=232.08, end_s=232.32),
            WhisperWord(text="I", start_s=232.80, end_s=232.85),
            WhisperWord(text="don't", start_s=232.85, end_s=233.12),
            WhisperWord(text="even", start_s=233.12, end_s=233.35),
            WhisperWord(text="know", start_s=233.35, end_s=233.57),
            WhisperWord(text="why", start_s=233.57, end_s=233.69),
            WhisperWord(text="I", start_s=233.76, end_s=233.79),
            WhisperWord(text="put", start_s=233.90, end_s=234.18),
            WhisperWord(text="up", start_s=234.18, end_s=234.40),
            WhisperWord(text="with", start_s=234.80, end_s=235.36),
            WhisperWord(text="you", start_s=235.36, end_s=235.58),
            WhisperWord(text="Ok", start_s=235.70, end_s=235.98),
            WhisperWord(text="stop", start_s=236.05, end_s=236.35),
        ],
        track_end_s=237.0,
    )

    repaired = next(line for line in result.lines if line.text == line_text)
    following = next(line for line in result.lines if line.text == "Ok stop")

    assert repaired.start_s == 231.28
    assert [word.start_s for word in repaired.words[:4]] == [231.28, 231.38, 231.88, 232.08]
    assert following.start_s == 235.7
