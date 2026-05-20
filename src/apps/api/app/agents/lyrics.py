"""nova.audio.lyrics — section-aware lyric extraction.

Orchestrates three providers:
  1. LRCLIB API → canonical lyric text + line-level timestamps.
  2. OpenAI Whisper API → word-level audio timings (with LRCLIB lyrics as
     prompt for vocabulary biasing).
  3. lyrics_alignment → stitch canonical text + line anchors + Whisper
     timings into one per-line + per-word structure.

This is a `rule_based` agent: there is no single LLM call, so we bypass the
GeminiClient + retry loop and implement everything in `compute()`. We still
inherit the Agent base class for: Pydantic input/output validation, the
canonical `agent_run` structlog event, Langfuse trace emission, and a slot
in the eval harness.

The agent runs at TRACK upload time (called from analyze_music_track_task),
not at job time. Its output is cached on MusicTrack.lyrics_cached and reused
by every music job that picks that track.

History: replaced Genius (PR replacing #251) with LRCLIB after Genius's
public lyric body became unscrapeable. LRCLIB's syncedLyrics gives EXACT
line bounds, which is strictly higher quality than the fuzzy text-matched
line bounds the Genius path produced. Legacy rows with
`source="genius+whisper"` continue to deserialize unchanged.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, TerminalError
from app.pipeline.lyrics_alignment import (
    AlignmentResult,
    align,
    align_with_line_anchors,
)
from app.services.lrclib_client import (
    LrclibError,
    LrclibLyrics,
    LrclibNotFound,
    search_lrclib,
)
from app.services.lyrics_search_query import build_lyrics_search_query
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
    """Schema persisted into MusicTrack.lyrics_cached as JSONB.

    `source` canonical values:
      - "lrclib_synced+whisper" — LRCLIB syncedLyrics line anchors + Whisper
        per-word (current best path)
      - "lrclib_plain+whisper"  — LRCLIB plainLyrics text + Whisper fuzzy
        alignment (used when LRCLIB row has no syncedLyrics)
      - "whisper_only"          — no canonical text source matched
      - "genius+whisper"        — legacy rows from the pre-LRCLIB pipeline
      - "manual"                — admin override (future)

    Kept as `str` (not Literal) so historic rows with values outside this
    list still deserialize. New writes go through `_build_*` helpers below
    which set the source explicitly.

    `genius_url` is preserved (defaulted empty) so legacy cached blobs
    deserialize unchanged. New LRCLIB extractions leave it empty and use
    `lrclib_id` instead.
    """

    source: str = "lrclib_synced+whisper"
    language: str = ""
    track_title_matched: str = ""
    artist_matched: str = ""
    genius_url: str = ""  # legacy, retained for backwards-compat deserialization
    lrclib_id: int | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    lines: list[LyricLine] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.lines


# ── Agent ─────────────────────────────────────────────────────────────────────


# OpenAI Whisper's `prompt` field is capped at ~224 tokens. Passing a full
# lyric body silently truncates from the END, which drops chorus/outro hints
# AND can prime the model into a repeat loop on the verse. We cap at 50
# words upstream (≈100 tokens worst case, well under the ceiling) so the
# beginning of the song — the most reliable section for vocabulary biasing —
# is always intact.
_WHISPER_PROMPT_MAX_WORDS = 50

# Defensive char cap if the first 50 words happen to be one absurdly long
# token. `whisper_lyrics` also enforces 800 chars, but doing it here keeps
# the truncation point predictable when reading agent logs.
_WHISPER_PROMPT_MAX_CHARS = 800


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
        prompt_version="2026-05-20",  # bump on LRCLIB swap
        model="rule_based",
        # LRCLIB + Whisper each have their own retry/timeout policy. The
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
        """Run LRCLIB lookup + Whisper transcription + alignment.

        Failures get partial-result fallbacks instead of raising whenever a
        useful answer is still possible:
          - LRCLIB miss → Whisper-only output (no canonical text but timings
            are usable for per-word-pop animation).
          - LRCLIB reports `instrumental: true` → raise TerminalError with
            "instrumental" in the message. The orchestrator string-matches
            this and persists `lyrics_status="unavailable"`.
          - Whisper miss → TerminalError (without timing, there's no usable
            output; the caller marks lyrics_status='failed' and continues).
        """
        clean_title, clean_artist = build_lyrics_search_query(input.track_title, input.artist)

        lrclib_lyrics: LrclibLyrics | None = None
        try:
            lrclib_lyrics = search_lrclib(clean_title, clean_artist)
        except LrclibNotFound:
            # Expected for obscure tracks — proceed with Whisper-only.
            lrclib_lyrics = None
        except LrclibError:
            # Network / rate limit / 5xx — also proceed Whisper-only. The
            # caller's `lyrics_error_detail` will preserve the exception
            # when Whisper too fails.
            lrclib_lyrics = None

        if lrclib_lyrics is not None and lrclib_lyrics.instrumental:
            # LRCLIB knows this track is instrumental — don't waste a
            # Whisper call. The orchestrator routes "instrumental" in the
            # error message to lyrics_status="unavailable".
            raise TerminalError("nova.audio.lyrics: LRCLIB reports track is instrumental")

        prompt_hint = _truncate_whisper_prompt(_choose_prompt_text(lrclib_lyrics))

        try:
            whisper_result = transcribe_for_lyrics(
                input.audio_path,
                prompt=prompt_hint,
                language=input.language,
            )
        except WhisperLyricsError as exc:
            raise TerminalError(f"nova.audio.lyrics: whisper transcription failed — {exc}") from exc

        if not whisper_result.words:
            raise TerminalError(
                "nova.audio.lyrics: whisper returned zero words — audio may be instrumental"
            )

        if lrclib_lyrics is not None and lrclib_lyrics.synced_lines:
            return _build_lrclib_synced_plus_whisper(lrclib_lyrics, whisper_result)
        if lrclib_lyrics is not None and lrclib_lyrics.plain_lines:
            return _build_lrclib_plain_plus_whisper(lrclib_lyrics, whisper_result)
        return _build_whisper_only(whisper_result)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _choose_prompt_text(lrclib: LrclibLyrics | None) -> str:
    """Pick the cleanest text hint for Whisper's vocabulary prompt.

    Synced text is preferred — it has no section markers ("[Chorus]") that
    would otherwise prime Whisper to emit bracket tokens. Falls back to
    plain text if synced isn't available, then to empty string.
    """
    if lrclib is None:
        return ""
    if lrclib.synced_lines:
        return lrclib.synced_text
    return lrclib.full_text


def _truncate_whisper_prompt(text: str) -> str:
    """Cap the Whisper vocabulary prompt at the first 50 words.

    Whisper's `prompt` field has a ~224-token ceiling. Passing the full
    lyric body causes silent end-truncation, dropping the chorus and
    risking a repeat-loop hallucination on the verse. Truncating from the
    FRONT keeps the song's opening intact — that's the most reliable
    section for biasing vocabulary because Whisper's first-window
    transcription benefits most from a strong prior.

    Returns "" (not None) for empty/whitespace input — matches the
    `transcribe_for_lyrics(prompt: str = "")` signature, which treats
    empty strings as "no prompt".
    """
    if not text:
        return ""
    words = text.split()
    if not words:
        return ""

    capped = " ".join(words[:_WHISPER_PROMPT_MAX_WORDS])
    if len(capped) > _WHISPER_PROMPT_MAX_CHARS:
        # One pathological mega-word slipped past the word cap; trim at
        # a word boundary just under the char limit.
        truncated = capped[:_WHISPER_PROMPT_MAX_CHARS]
        last_space = truncated.rfind(" ")
        capped = truncated[:last_space] if last_space > 0 else truncated
    return capped


# ── Output builders ───────────────────────────────────────────────────────────


def _aligned_lines_to_pydantic(result: AlignmentResult) -> list[LyricLine]:
    return [
        LyricLine(
            text=line.text,
            start_s=line.start_s,
            end_s=line.end_s,
            words=[LyricWord(text=w.text, start_s=w.start_s, end_s=w.end_s) for w in line.words],
        )
        for line in result.lines
    ]


def _build_lrclib_synced_plus_whisper(
    lrclib: LrclibLyrics,
    whisper: WhisperLyricsResult,
) -> LyricsOutput:
    """Best-quality path — LRC line anchors + Whisper per-word."""
    track_end_s = whisper.words[-1].end_s if whisper.words else None
    result = align_with_line_anchors(
        lrclib.synced_lines or (),
        list(whisper.words),
        track_end_s=track_end_s,
    )
    return LyricsOutput(
        source="lrclib_synced+whisper",
        language=whisper.language,
        track_title_matched=lrclib.title,
        artist_matched=lrclib.artist,
        lrclib_id=lrclib.lrclib_id or None,
        confidence=round(result.confidence, 3),
        lines=_aligned_lines_to_pydantic(result),
    )


def _build_lrclib_plain_plus_whisper(
    lrclib: LrclibLyrics,
    whisper: WhisperLyricsResult,
) -> LyricsOutput:
    """Fallback when LRCLIB row has plainLyrics but no syncedLyrics.

    Same shape as the historical Genius+Whisper path: canonical text from
    LRCLIB, line bounds recovered via the fuzzy `align()` matcher.
    """
    result = align(list(lrclib.plain_lines), list(whisper.words))
    return LyricsOutput(
        source="lrclib_plain+whisper",
        language=whisper.language,
        track_title_matched=lrclib.title,
        artist_matched=lrclib.artist,
        lrclib_id=lrclib.lrclib_id or None,
        confidence=round(result.confidence, 3),
        lines=_aligned_lines_to_pydantic(result),
    )


def _build_whisper_only(whisper: WhisperLyricsResult) -> LyricsOutput:
    """When LRCLIB has no entry, group Whisper words into pseudo-lines.

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
