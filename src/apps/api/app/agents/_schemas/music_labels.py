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

import re
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

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

# Map common out-of-enum genres the classifier emits onto the closest in-enum
# value, preserving matching signal where there's an obvious home. Everything
# else falls back to "other" (see `_coerce_genre`). Keys are in the SAME
# normalized form `_coerce_genre` produces: lowercased, separators (space, -, /,
# &, ,) collapsed to "_". So "hip-hop", "hip hop", "hiphop" all normalize before
# lookup — don't add hyphen/space spellings here, they'd never be hit.
_GENRE_ALIASES: dict[str, str] = {
    "hiphop": "hip_hop",
    "rap": "hip_hop",
    "trap": "hip_hop",
    "drill": "hip_hop",
    "edm": "electronic",
    "house": "electronic",
    "techno": "electronic",
    "dance": "electronic",
    "dubstep": "electronic",
    "classical": "cinematic",
    "orchestral": "cinematic",
    "score": "cinematic",
    "soundtrack": "cinematic",
    "ambient": "cinematic",
    "folk": "acoustic",
    "singer_songwriter": "acoustic",
    "country": "acoustic",
}

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

    @field_validator("genre", mode="before")
    @classmethod
    def _coerce_genre(cls, v: object) -> object:
        """Coerce an out-of-enum genre to its closest in-enum value (or 'other').

        The classifier (Gemini) freely emits genres like 'rock', 'r&b', 'latin',
        'jazz' that aren't in `Genre`. Before this, an out-of-enum value raised a
        ValidationError → song_classifier refused → the track got NO labels and
        stayed invisible to the matcher (confirmed in prod: a Bruno Mars cover and
        a Killers track both failed to label). Mapping unknown → 'other' (with a
        few high-signal aliases) keeps the track labeled and matchable; the matcher
        leans on vibe_tags/mood/energy anyway, so 'other' is a fine fallback. Only
        a genuinely non-string value falls through to pydantic's normal error.
        """
        if not isinstance(v, str):
            return v
        valid = set(get_args(Genre))
        # Normalize separators so "hip-hop", "hip hop", "hip_hop", "HipHop" all
        # collapse to the same key — the model emits genres in free natural form.
        g = re.sub(r"[\s/&,_-]+", "_", v.strip().lower()).strip("_")
        if g in valid:
            return g
        if g in _GENRE_ALIASES:
            return _GENRE_ALIASES[g]
        # Compound like "pop_rock" / "synth_pop" / "film_score": take the first
        # token that maps to a real genre before defaulting to "other".
        for tok in g.split("_"):
            if tok in valid:
                return tok
            if tok in _GENRE_ALIASES:
                return _GENRE_ALIASES[tok]
        return "other"
