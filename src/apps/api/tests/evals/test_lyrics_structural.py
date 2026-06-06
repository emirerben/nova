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


def test_repeated_chorus_prefix_lookback_beats_late_decoy() -> None:
    """Eval guard for lyrics-preview job f6637708's repeated chorus lag."""
    result = align_with_line_anchors(
        [
            SyncedLine(start_s=44.99, text="I can't feel my face when I'm with you"),
            SyncedLine(start_s=49.94, text="But I love it, but I love it, oh"),
            SyncedLine(start_s=53.06, text="I can't feel my face when I'm with you"),
            SyncedLine(start_s=59.16, text="But I love it, but I love it, oh"),
        ],
        [
            WhisperWord(text="I", start_s=44.00, end_s=44.62),
            WhisperWord(text="can", start_s=44.62, end_s=44.86),
            WhisperWord(text="feel", start_s=44.86, end_s=45.16),
            WhisperWord(text="my", start_s=45.16, end_s=45.40),
            WhisperWord(text="face", start_s=45.40, end_s=45.72),
            WhisperWord(text="when", start_s=45.72, end_s=45.98),
            WhisperWord(text="I'm", start_s=45.98, end_s=46.44),
            WhisperWord(text="with", start_s=46.44, end_s=46.54),
            WhisperWord(text="you", start_s=46.54, end_s=46.90),
            WhisperWord(text="But", start_s=47.82, end_s=48.44),
            WhisperWord(text="I", start_s=48.44, end_s=48.64),
            WhisperWord(text="love", start_s=48.64, end_s=48.94),
            WhisperWord(text="it", start_s=48.94, end_s=49.38),
            WhisperWord(text="why", start_s=49.56, end_s=50.70),
            WhisperWord(text="I", start_s=50.70, end_s=50.90),
            WhisperWord(text="love", start_s=50.90, end_s=51.24),
            WhisperWord(text="it", start_s=51.24, end_s=51.66),
            WhisperWord(text="I", start_s=51.66, end_s=52.60),
            WhisperWord(text="can", start_s=52.60, end_s=53.74),
            WhisperWord(text="feel", start_s=53.74, end_s=54.02),
            WhisperWord(text="my", start_s=54.02, end_s=54.26),
            WhisperWord(text="face", start_s=54.26, end_s=54.62),
            WhisperWord(text="when", start_s=54.62, end_s=54.86),
            WhisperWord(text="I'm", start_s=54.86, end_s=55.32),
            WhisperWord(text="with", start_s=55.32, end_s=55.42),
            WhisperWord(text="you", start_s=55.42, end_s=55.82),
            WhisperWord(text="But", start_s=56.74, end_s=57.36),
            WhisperWord(text="I", start_s=57.36, end_s=57.56),
            WhisperWord(text="love", start_s=57.56, end_s=57.98),
            WhisperWord(text="it", start_s=57.98, end_s=58.28),
            WhisperWord(text="why", start_s=58.58, end_s=59.58),
            WhisperWord(text="I", start_s=59.58, end_s=59.88),
            WhisperWord(text="love", start_s=59.88, end_s=60.14),
            WhisperWord(text="it", start_s=60.14, end_s=60.50),
        ],
        track_end_s=61.0,
    )

    but_lines = [line for line in result.lines if line.text == "But I love it, but I love it, oh"]

    assert [line.start_s for line in but_lines] == [47.82, 56.74]
    assert [word.start_s for word in but_lines[0].words[:4]] == [47.82, 48.44, 48.64, 48.94]
    assert [word.start_s for word in but_lines[1].words[:4]] == [56.74, 57.36, 57.56, 57.98]


def test_repeated_popup_chorus_repairs_bad_source_fragments() -> None:
    """Eval guard for lyrics-preview job 8c5793b6's repeated chorus cache."""
    result = align_with_line_anchors(
        [
            SyncedLine(start_s=0.0, text="Myself moving heart is open"),
            SyncedLine(start_s=3.85, text="moving heart is open"),
            SyncedLine(start_s=8.1, text="Body moving heart is open"),
            SyncedLine(start_s=11.45, text="Body moving heart is open"),
        ],
        [
            WhisperWord(text="body", start_s=0.0, end_s=0.35),
            WhisperWord(text="moving", start_s=1.35, end_s=1.75),
            WhisperWord(text="heart", start_s=2.05, end_s=2.35),
            WhisperWord(text="is", start_s=2.45, end_s=2.75),
            WhisperWord(text="open", start_s=3.3, end_s=3.7),
            WhisperWord(text="body", start_s=3.85, end_s=4.15),
            WhisperWord(text="moving", start_s=4.45, end_s=4.85),
            WhisperWord(text="heart", start_s=5.45, end_s=5.8),
            WhisperWord(text="is", start_s=6.0, end_s=6.25),
            WhisperWord(text="open", start_s=6.55, end_s=7.3),
            WhisperWord(text="body", start_s=8.1, end_s=8.45),
            WhisperWord(text="moving", start_s=8.65, end_s=9.0),
            WhisperWord(text="heart", start_s=9.35, end_s=9.7),
            WhisperWord(text="is", start_s=10.05, end_s=10.25),
            WhisperWord(text="open", start_s=10.5, end_s=11.1),
            WhisperWord(text="body", start_s=11.45, end_s=11.8),
            WhisperWord(text="moving", start_s=12.15, end_s=12.5),
            WhisperWord(text="heart", start_s=12.95, end_s=13.3),
            WhisperWord(text="is", start_s=13.7, end_s=13.92),
            WhisperWord(text="open", start_s=14.27, end_s=14.45),
        ],
        track_end_s=14.333,
    )

    assert [line.text for line in result.lines] == ["Body moving heart is open"] * 4
    assert [line.start_s for line in result.lines] == [0.0, 3.85, 8.1, 11.45]
    assert [line.words[0].text for line in result.lines] == ["Body"] * 4


def test_low_confidence_repeated_hook_tail_uses_whisper_tail() -> None:
    """Eval guard for repeated hooks whose canonical tail is stale."""
    result = align_with_line_anchors(
        [
            SyncedLine(start_s=10.0, text="previous line"),
            SyncedLine(start_s=12.0, text="one two gamma stale stale"),
            SyncedLine(start_s=15.5, text="next line"),
        ],
        [
            WhisperWord(text="previous", start_s=10.05, end_s=10.3),
            WhisperWord(text="line", start_s=10.3, end_s=10.65),
            WhisperWord(text="gamma", start_s=12.2, end_s=12.5),
            WhisperWord(text="delta", start_s=12.6, end_s=13.0),
            WhisperWord(text="epsilon", start_s=13.0, end_s=13.6),
            WhisperWord(text="zeta", start_s=13.6, end_s=14.7),
            WhisperWord(text="next", start_s=15.6, end_s=15.9),
            WhisperWord(text="line", start_s=15.9, end_s=16.2),
        ],
        track_end_s=17.0,
    )

    repaired = next(line for line in result.lines if line.text.startswith("one two gamma"))

    assert repaired.text == "one two gamma delta epsilon zeta"
    assert [word.text for word in repaired.words] == [
        "one",
        "two",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
    ]
    assert "stale" not in {word.text for word in repaired.words}
    assert repaired.end_s <= 14.7
