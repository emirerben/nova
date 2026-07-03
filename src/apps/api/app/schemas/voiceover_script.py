"""Pydantic schema for `PlanItem.voiceover_script` — the AI-authored voiceover
script the creator reads aloud in the "Get a transcript" helper.

Stored as raw JSONB (no side table) but ALWAYS validated through this model on
read and write, so routes never touch the raw dict (eng-review: explicit over
clever). `version` bumps on every Rewrite; the plan item separately records the
version a voiceover take was captured against (`voiceover_script_recorded_version`)
so the Script step can warn when a Rewrite invalidates an existing take.

The spoken read-time is a rough ESTIMATE (guidance on the badge), never a gate —
the narrated pipeline reflows clips to the voice, so length precision is not
required. See the plan's eng-review "metronome" decision.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Conversational speaking rate for a read-aloud voiceover. Used only to size the
# script (target word count) and to render the "≈ M:SS to read" badge. A fixed
# constant is deliberate: it is guidance, not a gate (per-user calibration is a
# deferred TODO). Real rates span ~1.7–3.3 wps; 2.3 is a calm middle.
VOICEOVER_WORDS_PER_SECOND = 2.3


def estimate_read_time_s(text: str) -> int:
    """Rough spoken read-time for `text`, in whole seconds (min 1)."""
    words = len(text.split())
    return max(1, round(words / VOICEOVER_WORDS_PER_SECOND))


def target_word_count(duration_s: float) -> int:
    """Target script length for footage of this duration (min 1 word)."""
    return max(1, round(duration_s * VOICEOVER_WORDS_PER_SECOND))


class VoiceoverScriptTurn(BaseModel):
    """One interview turn persisted with the script (for resume + regeneration)."""

    role: Literal["agent", "user"]
    content: str


class VoiceoverScript(BaseModel):
    """The persisted voiceover script document (PlanItem.voiceover_script)."""

    version: int = Field(ge=1)
    text: str = Field(min_length=1)
    # Rough spoken read-time estimate in seconds (badge only, never a gate).
    read_time_s: int = Field(ge=1)
    # The one-line context the creator gave in the Brief step.
    brief: str = ""
    # Light single-pass footage summary the script was grounded in (None when the
    # analyze step fell back to brief-only, e.g. Gemini unavailable).
    footage_summary: str | None = None
    # The clarifying-question turns (empty when the creator skipped the questions).
    interview_turns: list[VoiceoverScriptTurn] = Field(default_factory=list)
    # Phrase/clause line breaks for the teleprompter (speech-appropriate, NOT the
    # 6-word on-screen cap from phrase_sequence). Derived from `text`.
    lines: list[str] = Field(default_factory=list)
    source: Literal["generated", "edited"] = "generated"
