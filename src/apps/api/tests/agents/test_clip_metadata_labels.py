"""clip_metadata content_type/audio_type labels (format-aware edit engine, Lane A/D1).

No network. The new labels must default safely and coerce unknowns so a drifted
Gemini value never breaks parsing OR falsely promotes a clip to the talking-head
spine. The existing hook/transcript fields stay authoritative.
"""

from __future__ import annotations

from app.agents.clip_metadata import ClipMetadataOutput


def test_labels_default_safely_when_absent() -> None:
    # A response without the new fields (e.g. a cached/legacy payload) parses,
    # and defaults to the neutral, non-spine-promoting values.
    out = ClipMetadataOutput(hook_text="hey", hook_score=7.0)
    assert out.content_type == "broll"
    assert out.audio_type == "ambient"


def test_labels_passthrough_valid_values() -> None:
    out = ClipMetadataOutput(
        hook_text="hey",
        hook_score=7.0,
        content_type="talking_head",
        audio_type="dialogue",
    )
    assert out.content_type == "talking_head"
    assert out.audio_type == "dialogue"


def test_labels_coerce_unknown_to_safe_default() -> None:
    out = ClipMetadataOutput(
        hook_text="hey",
        hook_score=7.0,
        content_type="vlog-face",  # not in the vocabulary
        audio_type="asmr",  # not in the vocabulary
    )
    # Unknown content must NOT become talking_head (would wrongly win spine selection).
    assert out.content_type == "broll"
    assert out.audio_type == "ambient"


def test_label_case_and_separator_normalization() -> None:
    # "Talking Head" / "Day Vlog" etc. normalize space+case to the underscored
    # vocabulary token; "Voiceover" is case-only (the canonical token has no
    # separator). A hyphenated "voice-over" is NOT canonical and safely defaults.
    out = ClipMetadataOutput(
        hook_text="hey",
        hook_score=5.0,
        content_type="Talking Head",
        audio_type="Voiceover",
    )
    assert out.content_type == "talking_head"
    assert out.audio_type == "voiceover"
