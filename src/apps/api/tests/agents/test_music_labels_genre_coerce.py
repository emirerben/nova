"""MusicLabels._coerce_genre — out-of-enum genres must not break labeling.

Before this validator, a Gemini-emitted genre outside the `Genre` literal
(rock, r&b, latin, …) raised a ValidationError, song_classifier refused, and the
track got NO labels — making it invisible to the music matcher (confirmed in prod
on a Bruno Mars cover and a Killers track). Unknown → 'other' (or a high-signal
alias) keeps the track labeled and matchable.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents._schemas.music_labels import MusicLabels

_BASE = {
    "label_version": "2026-05-15",
    "vibe_tags": ["upbeat", "energetic"],
    "energy": "high",
    "pacing": "fast",
    "mood": "test",
    "ideal_content_profile": "test profile",
    "copy_tone": "high_energy",
    "transition_style": "hard_cut",
}


def _labels(genre):
    return MusicLabels(genre=genre, **_BASE)


def test_valid_genre_passes_through():
    assert _labels("pop").genre == "pop"


def test_valid_genre_case_insensitive():
    assert _labels("Electronic").genre == "electronic"


@pytest.mark.parametrize("g", ["rock", "r&b", "soul", "latin", "reggaeton", "jazz", "metal"])
def test_unknown_genre_coerced_to_other(g):
    assert _labels(g).genre == "other"


@pytest.mark.parametrize(
    "g,expected",
    [
        ("rap", "hip_hop"),
        ("trap", "hip_hop"),
        ("EDM", "electronic"),
        ("house", "electronic"),
        ("orchestral", "cinematic"),
        ("folk", "acoustic"),
        ("country", "acoustic"),
    ],
)
def test_high_signal_aliases_mapped(g, expected):
    assert _labels(g).genre == expected


@pytest.mark.parametrize(
    "g,expected",
    [
        ("hip-hop", "hip_hop"),  # hyphen — the most common rap spelling
        ("hip hop", "hip_hop"),  # space
        ("HipHop", "hip_hop"),  # camel → "hiphop" alias
        ("Hip_Hop", "hip_hop"),
        ("pop/rock", "pop"),  # compound → first known token
        ("synth-pop", "pop"),
        ("film score", "cinematic"),  # token "score" → cinematic
        ("  Pop  ", "pop"),  # surrounding whitespace
    ],
)
def test_separator_and_compound_normalization(g, expected):
    assert _labels(g).genre == expected


def test_non_string_genre_still_raises():
    with pytest.raises(ValidationError):
        _labels(123)
