"""nova.audio.lyrics — section-aware lyric extraction.

Orchestrates three providers:
  1. Genius API → canonical lyric text (correct spelling, line breaks).
  2. OpenAI Whisper API → word-level audio timings (with Genius lyrics as
     prompt for vocabulary biasing).
  3. lyrics_alignment → stitch Genius text + Whisper timings into one
     per-line + per-word structure.

This is a `rule_based` agent: there is no single LLM call, so we bypass the
GeminiClient + retry loop and implement everything in `compute()`. We still
inherit the Agent base class for: Pydantic input/output validation, the
canonical `agent_run` structlog event, Langfuse trace emission, and a slot
in the eval harness.

The agent runs at TRACK upload time (called from analyze_music_track_task),
not at job time. Its output is cached on MusicTrack.lyrics_cached and reused
by every music job that picks that track.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, TerminalError
from app.pipeline.lyrics_alignment import AlignmentResult, align
from app.services.genius_client import (
    GeniusError,
    GeniusLyrics,
    GeniusNotFound,
    search_lyrics,
)
from app.services.whisper_lyrics import (
    WhisperLyricsError,
    WhisperLyricsResult,
    transcribe_for_lyrics,
)

# ── I/O schema ────────────────────────────────────────────────────────────────


class LyricsInput(BaseModel):
    """Inputs the analyze task supplies to the lyrics agent.

    `audio_path` is a local filesystem path (already-downloaded m4a from GCS).
    The agent reads it for Whisper transcription; the file is not modified.

    `best_start_s`/`best_end_s` are advisory — Whisper still transcribes the
    whole file (cheap on a 3-min track), but the alignment uses them when
    deciding what to surface in the cached output. v1 stores all aligned
    lines and lets the lyric injector filter by section at job time, which
    means changing best_start_s after extraction doesn't require re-running
    Whisper.
    """

    audio_path: str
    track_title: str
    artist: str = ""
    # Best-section hints (currently not used to slice extraction — see above —
    # but threaded through so the agent_run log shows what section was active
    # at extraction time, which helps when debugging timing mismatches).
    best_start_s: float = 0.0
    best_end_s: float = 0.0
    language: str | None = None  # ISO 639-1; None → Whisper auto-detect


class LyricWord(BaseModel):
    text: str
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)


class LyricLine(BaseModel):
    text: str
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)
    words: list[LyricWord] = Field(default_factory=list)


class LyricsOutput(BaseModel):
    """Schema persisted into MusicTrack.lyrics_cached as JSONB."""

    source: str = "genius+whisper"  # see app.models.MusicTrack.lyrics_source
    language: str = ""
    track_title_matched: str = ""
    artist_matched: str = ""
    genius_url: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    lines: list[LyricLine] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.lines


# ── Agent ─────────────────────────────────────────────────────────────────────


class LyricsExtractionAgent(Agent[LyricsInput, LyricsOutput]):
    """Extract aligned lyrics for a music track. Rule-based (no LLM call).

    Run via:
        LyricsExtractionAgent(client).run(LyricsInput(...))
    where `client` is unused (rule_based path bypasses it). Pass the same
    GeminiClient the rest of the codebase uses; it makes the call sites
    uniform and costs nothing at runtime.
    """

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.lyrics",
        prompt_id="extract_lyrics",  # nominal — no template loaded
        prompt_version="2026-05-19",
        model="rule_based",
        # Genius + Whisper each have their own retry/timeout policy. The
        # agent runtime's retry loop doesn't apply to rule_based agents.
        max_attempts=1,
    )
    Input = LyricsInput
    Output = LyricsOutput

    def render_prompt(self, input: LyricsInput) -> str:  # noqa: ARG002
        # Unused: rule_based agents never hit the model client.
        return ""

    def parse(self, raw_text: str, input: LyricsInput) -> LyricsOutput:  # noqa: ARG002
        # Unused: rule_based agents skip the LLM path entirely. The
        # base class only calls `parse` when there is raw_text to parse.
        raise NotImplementedError("LyricsExtractionAgent is rule_based; parse() is not used")

    def compute(self, input: LyricsInput) -> LyricsOutput:  # noqa: A002
        """Run Genius search + Whisper transcription + alignment.

        Failures get partial-result fallbacks instead of raising whenever a
        useful answer is still possible:
          - Genius miss → Whisper-only output (no canonical text but timings
            are usable for per-word-pop animation).
          - Whisper miss → TerminalError (without timing, there's no usable
            output; the caller marks lyrics_status='failed' and continues).
        """
        genius_lyrics: GeniusLyrics | None = None
        try:
            genius_lyrics = search_lyrics(input.track_title, input.artist)
        except GeniusNotFound:
            # Expected for obscure tracks — proceed with Whisper-only.
            genius_lyrics = None
        except GeniusError:
            # Network / auth / rate limit — also proceed Whisper-only. The
            # caller's `lyrics_error_detail` will preserve the exception
            # when Whisper too fails.
            genius_lyrics = None

        try:
            whisper_result = transcribe_for_lyrics(
                input.audio_path,
                prompt=genius_lyrics.full_text if genius_lyrics else "",
                language=input.language,
            )
        except WhisperLyricsError as exc:
            raise TerminalError(f"nova.audio.lyrics: whisper transcription failed — {exc}") from exc

        if not whisper_result.words:
            raise TerminalError(
                "nova.audio.lyrics: whisper returned zero words — audio may be instrumental"
            )

        if genius_lyrics is not None:
            return _build_genius_plus_whisper(genius_lyrics, whisper_result)
        return _build_whisper_only(whisper_result)


# ── Output builders ───────────────────────────────────────────────────────────


def _build_genius_plus_whisper(
    genius: GeniusLyrics,
    whisper: WhisperLyricsResult,
) -> LyricsOutput:
    result: AlignmentResult = align(list(genius.lines), list(whisper.words))
    lines = [
        LyricLine(
            text=line.text,
            start_s=line.start_s,
            end_s=line.end_s,
            words=[LyricWord(text=w.text, start_s=w.start_s, end_s=w.end_s) for w in line.words],
        )
        for line in result.lines
    ]
    return LyricsOutput(
        source="genius+whisper",
        language=whisper.language,
        track_title_matched=genius.title,
        artist_matched=genius.artist,
        genius_url=genius.genius_url,
        confidence=round(result.confidence, 3),
        lines=lines,
    )


def _build_whisper_only(whisper: WhisperLyricsResult) -> LyricsOutput:
    """When Genius has no entry, group Whisper words into pseudo-lines.

    Strategy: break a new line every time Whisper's gap between consecutive
    words exceeds 0.9s. Keeps text readable; cheap; no NLP. Confidence is
    set to 0.5 to signal "no canonical source" — frontend can color this
    differently or hide it behind an "experimental" badge.
    """
    if not whisper.words:
        return LyricsOutput(source="whisper_only", language=whisper.language)

    line_gap_s = 0.9
    lines: list[list[LyricWord]] = [[]]
    last_end = whisper.words[0].start_s
    for w in whisper.words:
        if w.start_s - last_end > line_gap_s and lines[-1]:
            lines.append([])
        lines[-1].append(LyricWord(text=w.text, start_s=w.start_s, end_s=w.end_s))
        last_end = w.end_s

    lyric_lines = [
        LyricLine(
            text=" ".join(w.text for w in run),
            start_s=run[0].start_s,
            end_s=run[-1].end_s,
            words=run,
        )
        for run in lines
        if run
    ]
    return LyricsOutput(
        source="whisper_only",
        language=whisper.language,
        confidence=0.5,
        lines=lyric_lines,
    )
