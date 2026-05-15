"""SongSections — top-K edit-worthy sections of a music track.

Producer: ``nova.audio.song_sections`` (admin-time, once per track).
Consumers: ``nova.audio.music_matcher`` + the variant orchestrator pick a
specific section per ranked track when assembling auto-music edits. The
brief gives 1-3 ranked candidates; rank 1 is the strongest section, rank
N is the Nth-best. Phase 6 variant diversity uses ranks 2/3 to differ
between variants of the same track.

Stored on ``MusicTrack.best_sections`` (JSONB list).
``MusicTrack.section_version`` mirrors ``CURRENT_SECTION_VERSION`` so the
matcher can refuse stale rows without parsing the JSONB.

This schema lives in ``_schemas/`` for the same reason ``music_labels``
does: ``tests/evals/runners/structural.py`` imports the version constant
and the Pydantic shapes directly, so they must not live behind the agent
module (which carries Gemini-client imports the eval harness shouldn't
need).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Bump when the schema shape OR the enum value lists change. The matcher
# filters tracks where ``section_version != CURRENT_SECTION_VERSION`` so a
# bump forces re-sectioning via ``scripts/backfill_song_sections.py
# --include-stale``. Keep in sync with ``song_sections`` prompt version
# when semantics shift.
CURRENT_SECTION_VERSION = "2026-05-15"

SectionLabel = Literal[
    "intro",
    "verse",
    "pre_chorus",
    "chorus",
    "drop",
    "bridge",
    "outro",
    "hook",
    "build",
]

SectionEnergy = Literal["low", "medium", "high", "peaks_high"]

SectionUse = Literal["hook", "build", "climax", "ambient", "transition"]


class SongSection(BaseModel):
    """One ranked edit-worthy section of a track.

    Pydantic enforces rank range and enum values. Cross-field rules
    (start < end, duration band, no overlap with siblings) live in the
    agent's ``parse()`` because they reference the surrounding list.
    """

    rank: int = Field(ge=1, le=3, description="1 = best, 3 = third-best. Unique across list.")
    start_s: float = Field(ge=0.0, description="Window start (seconds from track start).")
    end_s: float = Field(gt=0.0, description="Window end. Must be > start_s.")
    label: SectionLabel = Field(description="Musical-structure label.")
    energy: SectionEnergy = Field(description="Section energy level.")
    suggested_use: SectionUse = Field(description="What the section is best as in an edit.")
    rationale: str = Field(min_length=1, description="1-2 sentences on why edit-worthy.")


class SongSectionsOutput(BaseModel):
    """Ordered list of 1-3 ranked sections for a music track."""

    sections: list[SongSection] = Field(min_length=1, max_length=3)
    section_version: str = Field(min_length=1)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()
