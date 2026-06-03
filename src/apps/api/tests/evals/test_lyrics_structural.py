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
