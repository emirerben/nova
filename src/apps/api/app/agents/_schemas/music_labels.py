"""MusicLabels — creative-direction labels attached to a MusicTrack.

Producer: ``nova.audio.song_classifier`` (admin-time, once per track).
Consumers: ``nova.audio.music_matcher`` (job-time match scoring) plus the
existing ``transition_picker`` / ``text_designer`` agents that read
``copy_tone`` + ``transition_style`` off the track when running in
auto-music mode.

This is intentionally a *creative* schema. Structural fields (beats,
tempo, energy curve) live on ``AudioTemplateOutput`` and are not
duplicated here — the song_classifier consumes that output as input.

Stored on ``MusicTrack.ai_labels`` (JSONB). ``MusicTrack.label_version``
mirrors ``MusicLabels.label_version`` so the matcher can refuse stale
labels when the schema or prompt evolves.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Bump when the schema shape OR the enum value lists change. The matcher
# filters tracks where ``label_version < CURRENT_LABEL_VERSION`` so a
# bump forces re-labeling. Keep in sync with ``song_classifier`` prompt
# version when semantics shift.
CURRENT_LABEL_VERSION = "2026-05-15"

Genre = Literal[
    "pop",
    "hip_hop",
    "electronic",
    "cinematic",
    "acoustic",
    "comedy",
    "other",
]

Energy = Literal["low", "medium", "high", "peaks_high"]

Pacing = Literal["slow", "medium", "fast", "frantic"]

CopyTone = Literal[
    "casual",
    "cinematic",
    "punchy",
    "sentimental",
    "comedic",
    "high_energy",
]

TransitionStyle = Literal[
    "hard_cut",
    "whip_pan",
    "dissolve",
    "beat_pulse",
    "mixed",
]


class MusicLabels(BaseModel):
    """Creative-direction labels for a single music track.

    Frozen contract — extending this requires bumping
    ``CURRENT_LABEL_VERSION`` and re-running ``song_classifier`` against
    the library (see ``scripts/backfill_song_classifier.py``).
    """

    label_version: str = Field(
        min_length=1,
        description="Schema/prompt version that produced these labels.",
    )
    genre: Genre
    vibe_tags: list[str] = Field(
        min_length=1,
        max_length=8,
        description="Short tokens like 'wistful', 'upbeat', 'sentimental'.",
    )
    energy: Energy
    pacing: Pacing
    mood: str = Field(min_length=1, description="One short phrase describing the mood.")
    ideal_content_profile: str = Field(
        min_length=1,
        description=(
            "1-2 sentences describing what user clips this song wants — "
            "subject types, energy, hook style."
        ),
    )
    copy_tone: CopyTone = Field(
        description="Consumed by text_designer + platform_copy in auto-music mode.",
    )
    transition_style: TransitionStyle = Field(
        description="Consumed by transition_picker in auto-music mode.",
    )
    color_grade: str = Field(
        default="none",
        description="Short freeform color-grade hint for downstream.",
    )
