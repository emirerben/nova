"""Schemas for nova.compose.text_alignment — stage E of the Layer-2 pipeline.

Inputs:  phrase list from stage D + Whisper transcript with per-word timestamps.
Outputs: same phrase list with OCR errors corrected via transcript alignment.

Phrases with no plausible transcript match are dropped (likely OCR hallucinations
on background graphics). The `dropped_count` field gives the caller an
observability handle without requiring log inspection.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents._schemas.text_overlay_pipeline import Phrase


class TranscriptWord(BaseModel):
    """One word from the Whisper / Gemini transcript with timestamps.

    Mirrors the `Word` schema in `app.agents.transcript` but is imported
    here (as a separate type) to keep the OCR pipeline schemas self-contained
    and avoid circular imports.  Both shapes share the same field names so
    callers can pass either a `Word` dict or a `TranscriptWord` dict without
    adaptation.
    """

    text: str
    start_s: float = Field(..., ge=0.0)
    end_s: float = Field(..., ge=0.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TextAlignmentInput(BaseModel):
    """Input to the text-alignment agent (stage E).

    `phrases` comes from `reconstruct_phrases()` (stage D).
    `transcript_words` is the per-word output from Whisper / the transcript
    agent — a list of dicts that each have at minimum `text`, `start_s`,
    `end_s` keys.  Callers may pass raw dicts; Pydantic validates them.
    `template_id` is optional and used only for log / trace correlation.

    `atomize_mode` tells the prompt how to interpret the input. When True
    (the prod default — Stage D ran with `atomize_per_event=True`), each
    phrase represents ONE OCR word at its first-appeared moment; the prompt
    must output exactly one transcript word per phrase. When False, the
    prompt may concatenate multiple transcript words spanning the phrase's
    visible window (legacy phrase-mode behavior).
    """

    phrases: list[Phrase]
    transcript_words: list[TranscriptWord] = Field(default_factory=list)
    template_id: str | None = None
    atomize_mode: bool = False


class TextAlignmentOutput(BaseModel):
    """Output of the text-alignment agent.

    `phrases` is the filtered + corrected phrase list:
      - Each phrase's `lines` field has been updated so that OCR character
        errors are replaced by the transcript's spelling / casing /
        punctuation.  Line breaks are preserved because they convey visual
        structure (build-up / typewriter captions) that the transcript does
        not encode.
      - Phrases that had no plausible transcript match are dropped.

    `dropped_count` records how many input phrases were dropped.  It equals
    `len(input.phrases) - len(output.phrases)` and is surfaced here so the
    caller can emit a single structured log event without needing to diff
    the two lists.
    """

    phrases: list[Phrase]
    dropped_count: int = 0
