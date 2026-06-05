"""nova.audio.lyrics — section-aware lyric extraction.

Orchestrates three providers:
  1. LRCLIB API → canonical lyric text + line-level timestamps.
     - `/api/get` exact match by title+artist+duration (primary)
     - `/api/search` fuzzy fallback when /api/get 404s (since 2026-05-27)
     - `/api/get/{id}` admin-forced row (since 2026-05-27)
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

**Publishability policy (since 2026-05-27).** Only outputs whose `source` is
in `PUBLISHABLE_LYRICS_SOURCES` are treated as production-ready by the
orchestrator. `whisper_only` outputs are stored on `MusicTrack.lyrics_whisper_draft`
as admin reference and `lyrics_status='needs_manual_lyrics'` is set — the
admin must paste an LRCLIB row ID via the force-id endpoint to recover.
The agent itself does not gate persistence; the orchestrator's
`_apply_lyrics_result` reads `LyricsOutput.source` against this allowlist.

History: replaced Genius (PR replacing #251) with LRCLIB after Genius's
public lyric body became unscrapeable. LRCLIB's syncedLyrics gives EXACT
line bounds, which is strictly higher quality than the fuzzy text-matched
line bounds the Genius path produced. Legacy rows with
`source="genius+whisper"` continue to deserialize unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import structlog
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
    get_lrclib_by_id,
    search_lrclib,
    search_lrclib_fuzzy,
)
from app.services.lyrics_search_query import build_lyrics_search_query
from app.services.whisper_lyrics import (
    WhisperLyricsError,
    WhisperLyricsResult,
    transcribe_for_lyrics,
)

log = structlog.get_logger()

# ── Publishability allowlist ─────────────────────────────────────────────────

# Single source of truth for which `LyricsOutput.source` values count as a
# production-ready extraction. The orchestrator's `_apply_lyrics_result`
# checks membership here when deciding between
# `lyrics_status='ready'` (write to lyrics_cached) and
# `lyrics_status='needs_manual_lyrics'` (write to lyrics_whisper_draft, keep
# lyrics_cached null). The injector's Layer-2 gate also reads this set.
#
# Adding a future source (e.g. a Genius revival) requires explicitly editing
# this constant — that forces a conscious quality decision the same way the
# `_encoding_args` allowlist works for FFmpeg presets.
PUBLISHABLE_LYRICS_SOURCES: frozenset[str] = frozenset(
    {
        "lrclib_synced+whisper",
        "lrclib_plain+whisper",
    }
)

# Cached blobs that are safe for the renderer to burn. This is intentionally
# wider than the forward publishability set: legacy `genius+whisper` rows still
# exist in prod until re-extracted, and `manual` is reserved for the future admin
# override. `whisper_only` stays excluded.
RENDERABLE_CACHED_LYRICS_SOURCES: frozenset[str] = PUBLISHABLE_LYRICS_SOURCES | frozenset(
    {
        "genius+whisper",
        "manual",
    }
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
    # Full-track duration in seconds. Passed to LRCLIB as the `duration` query
    # param so its `/api/get` only returns a row whose recording length matches
    # the uploaded audio (±2s tolerance). Songs with multiple recordings
    # (original vs remix, radio edit vs extended) share title+artist; without
    # this hint LRCLIB happily returns a different recording's syncedLyrics
    # and the line-anchored alignment writes wrong absolute timestamps. 0.0
    # disables duration disambiguation.
    duration_s: float = 0.0
    language: str | None = None  # ISO 639-1; None → Whisper auto-detect
    # Admin manual override: when set, the agent skips title/artist search
    # entirely and fetches the exact LRCLIB row by ID. Use the
    # `app.services.lrclib_id_parse.parse_lrclib_id` helper to validate
    # admin input before storing it on `track_config.lyrics_config.forced_lrclib_id`.
    forced_lrclib_id: int | None = None


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
        per-word (current best path; PUBLISHABLE)
      - "lrclib_plain+whisper"  — LRCLIB plainLyrics text + Whisper fuzzy
        alignment (used when LRCLIB row has no syncedLyrics; PUBLISHABLE)
      - "whisper_only"          — no canonical text source matched
                                   (NOT PUBLISHABLE; written to whisper_draft)
      - "genius+whisper"        — legacy rows from the pre-LRCLIB pipeline
      - "manual"                — admin override (future)

    See `PUBLISHABLE_LYRICS_SOURCES` for the authoritative production-ready set.

    Kept as `str` (not Literal) so historic rows with values outside this
    list still deserialize. New writes go through `_build_*` helpers below
    which set the source explicitly.

    `genius_url` is preserved (defaulted empty) so legacy cached blobs
    deserialize unchanged. New LRCLIB extractions leave it empty and use
    `lrclib_id` instead.

    `prompt_version` is the LyricsExtractionAgent.spec.prompt_version that
    produced this cache blob. Backfill tooling compares this to the live
    version to find stale alignment data after timing fixes.

    `lyrics_diagnostic` is the structured trace of every LRCLIB lookup
    attempt this extraction ran. Populated on every terminal state (success
    AND failure) so the admin debug UI never has a "why did this fail?"
    blind spot. Stored separately on `MusicTrack.lyrics_diagnostic` by the
    orchestrator; included on the agent output to keep the agent self-contained.
    """

    source: str = "lrclib_synced+whisper"
    prompt_version: str = ""
    language: str = ""
    track_title_matched: str = ""
    artist_matched: str = ""
    genius_url: str = ""  # legacy, retained for backwards-compat deserialization
    lrclib_id: int | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    lines: list[LyricLine] = Field(default_factory=list)
    lyrics_diagnostic: dict | None = None

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def is_publishable(self) -> bool:
        """True when this output is fit for production rendering.

        Cross-check against `PUBLISHABLE_LYRICS_SOURCES`. The orchestrator
        uses this to decide whether to write the blob to `lyrics_cached`
        (publishable) or `lyrics_whisper_draft` (non-publishable).
        """
        return self.source in PUBLISHABLE_LYRICS_SOURCES


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

# Below this fraction of canonical-word matches, the LRCLIB synced anchors
# are almost certainly for a different recording than the uploaded audio.
# `align_with_line_anchors.confidence` is `matched_words / total_words`; a
# correct recording typically scores >0.7 (Strategy 1 fast path or Strategy
# 2 fuzzy match). A wrong recording scores ~0.0 (Strategy 3 across every
# window — no Whisper words fall in any LRCLIB-defined window). 0.20 is a
# wide safety margin; even rough alignment on a noisy track stays above it.
_SYNCED_CONFIDENCE_MIN = 0.20

# /api/search candidates must score above this combined threshold to be
# treated as a real match worth re-fetching by ID. Below this the agent
# routes to whisper_only (orchestrator persists as needs_manual_lyrics).
# Same value as `lrclib_client._FUZZY_MIN_COMBINED_SCORE` — duplicated here
# so the agent logic is readable without jumping modules.
_FUZZY_PROMOTE_MIN_SCORE = 0.85


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
        # 2026-05-27 (PR Instant Crush): LRC-anchor re-anchor when the
        # detected audio-vs-LRC shift exceeds the threshold. Rewrites every
        # AlignedLine.start_s / end_s to LRC_anchor[i] + shift so line
        # bounds track the actual audio cut even when LRC indexed a
        # different cut (e.g. Instant Crush 339.79s official-video cut vs
        # LRCLIB album cut at 338.00s). Per-word AlignedWord timings stay
        # as Whisper produced them — karaoke `\kf` + per-word-pop are
        # unaffected. Bumped to force re-analyze of existing cached blobs
        # on next preview / render.
        # Previous: 2026-05-27.instant-crush (PR #363): single-L0 LRC
        # re-anchor when |shift| > 1.0s for wrong-cut audio.
        # Previous: 2026-05-27.beauty (PR Beauty And A Beat): forced-ID
        # admin override, /api/search fuzzy fallback, diagnostic blob,
        # whisper_only demoted to non-publishable draft.
        # Previous: 2026-05-28.median: multi-line median re-anchor layered
        # above single-L0 to catch sub-second consistent drift (Overnight,
        # The Bay class).
        # Previous: 2026-05-31.linear-reanchor: linear re-anchor layered
        # above uniform paths to catch progressively growing audio-vs-LRCLIB
        # drift.
        # Previous: 2026-06-05.collapsed-word-runs: repair dense Whisper
        # word-timing clusters that exact-count alignment would otherwise
        # cache as one 50ms karaoke flash.
        # Current: repair isolated late LRC anchors by admitting a strong
        # matching line prefix from the pre-anchor lookback window.
        prompt_version="2026-06-05.anchor-prefix-lookback",
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

        Decision tree:
          1. If `forced_lrclib_id` is set → fetch via /api/get/{id} and align.
             Hit → publishable. Miss → whisper_only (orchestrator persists
             as needs_manual_lyrics with the forced_id failure on the diagnostic).
          2. Try /api/get?title+artist+duration. Hit → align. Miss → step 3.
          3. Try /api/search?title+artist (no duration; soft signal).
             Top candidate above `_FUZZY_PROMOTE_MIN_SCORE` → re-fetch by
             ID and align. Else → whisper_only.
          4. Wrong-recording defense (Hawai, 2026-05-27 #358): if synced
             alignment confidence < `_SYNCED_CONFIDENCE_MIN`, fall through
             to plain or whisper_only so LRCLIB's wrong anchors don't win.
          5. Instrumental flag from LRCLIB → TerminalError with the
             "instrumental" keyword → orchestrator maps to status=unavailable.

        Failures get partial-result fallbacks instead of raising whenever a
        useful answer is still possible:
          - LRCLIB miss → Whisper-only output (NOT publishable, stored as draft).
          - Whisper miss → TerminalError (without timing, there's no usable
            output; the caller marks lyrics_status='failed' and continues).

        The diagnostic blob is always populated on the returned LyricsOutput
        so the admin UI can render WHY a particular path was taken.
        """
        diag = _Diagnostic(
            query_title="",  # filled in below after sanitization
            query_artist="",
            query_duration_s=input.duration_s or None,
            forced_lrclib_id=input.forced_lrclib_id,
        )

        # ── Forced-ID path (admin manual override) ──────────────────────
        if input.forced_lrclib_id is not None:
            lrclib_lyrics = _lookup_by_forced_id(input.forced_lrclib_id, diag=diag)
            if lrclib_lyrics is not None and lrclib_lyrics.instrumental:
                raise TerminalError("nova.audio.lyrics: LRCLIB reports track is instrumental")
            return _finish_extraction(input, lrclib_lyrics, diag=diag)

        # ── Normal path: /api/get → /api/search → whisper_only ──────────
        clean_title, clean_artist = build_lyrics_search_query(input.track_title, input.artist)
        diag.query_title = clean_title
        diag.query_artist = clean_artist

        lrclib_lyrics = _lookup_via_get(clean_title, clean_artist, input.duration_s, diag=diag)

        if lrclib_lyrics is None:
            # /get missed — try /search fuzzy fallback.
            lrclib_lyrics = _lookup_via_search(
                clean_title, clean_artist, input.duration_s, diag=diag
            )

        if lrclib_lyrics is not None and lrclib_lyrics.instrumental:
            # LRCLIB knows this track is instrumental — don't waste a
            # Whisper call. The orchestrator routes "instrumental" in the
            # error message to lyrics_status="unavailable".
            raise TerminalError("nova.audio.lyrics: LRCLIB reports track is instrumental")

        return _finish_extraction(input, lrclib_lyrics, diag=diag)


# ── Internal: diagnostic blob accumulator ────────────────────────────────────


class _Diagnostic:
    """Builds the structured diagnostic blob threaded through the compute path.

    Plain class (not Pydantic) — performance is irrelevant here, and keeping
    it mutable lets each lookup stage update its slice without copying.
    Serialized via `to_dict()` at the very end.
    """

    __slots__ = (
        "query_title",
        "query_artist",
        "query_duration_s",
        "forced_lrclib_id",
        "get_status",
        "search_status",
        "search_top_score",
        "lrclib_id_matched",
        "fallback_path",
        "duration_delta_s",
        "lrclib_error",
    )

    def __init__(
        self,
        *,
        query_title: str,
        query_artist: str,
        query_duration_s: float | None,
        forced_lrclib_id: int | None,
    ) -> None:
        self.query_title = query_title
        self.query_artist = query_artist
        self.query_duration_s = query_duration_s
        self.forced_lrclib_id = forced_lrclib_id
        self.get_status: str = "skipped" if forced_lrclib_id else "pending"
        self.search_status: str = "skipped"
        self.search_top_score: float | None = None
        self.lrclib_id_matched: int | None = None
        self.fallback_path: str = "pending"
        self.duration_delta_s: float | None = None
        self.lrclib_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "query": {
                "title": self.query_title,
                "artist": self.query_artist,
                "duration_s": self.query_duration_s,
                "forced_lrclib_id": self.forced_lrclib_id,
            },
            "get_status": self.get_status,
            "search_status": self.search_status,
            "search_top_score": self.search_top_score,
            "lrclib_id_matched": self.lrclib_id_matched,
            "fallback_path": self.fallback_path,
            "duration_delta_s": self.duration_delta_s,
            "lrclib_error": self.lrclib_error,
            "attempted_at": datetime.now(UTC).isoformat(),
        }


# ── Internal: lookup stages ──────────────────────────────────────────────────


def _lookup_by_forced_id(forced_id: int, *, diag: _Diagnostic) -> LrclibLyrics | None:
    try:
        result = get_lrclib_by_id(forced_id)
    except LrclibNotFound:
        diag.get_status = "forced_id_not_found"
        diag.fallback_path = "needs_manual_lyrics"
        log.info("lyrics_forced_id_not_found", lrclib_id=forced_id)
        return None
    except LrclibError as exc:
        diag.get_status = "forced_id_error"
        diag.lrclib_error = str(exc)[:200]
        diag.fallback_path = "needs_manual_lyrics"
        log.warning("lyrics_forced_id_error", lrclib_id=forced_id, error=str(exc))
        return None

    diag.get_status = "forced_id_hit"
    diag.lrclib_id_matched = result.lrclib_id or forced_id
    return result


def _lookup_via_get(
    title: str, artist: str, duration_s: float, *, diag: _Diagnostic
) -> LrclibLyrics | None:
    try:
        result = search_lrclib(title, artist, duration_s=duration_s or None)
    except LrclibNotFound:
        diag.get_status = "not_found"
        return None
    except LrclibError as exc:
        diag.get_status = "error"
        diag.lrclib_error = str(exc)[:200]
        return None

    diag.get_status = "hit"
    diag.lrclib_id_matched = result.lrclib_id or None
    return result


def _lookup_via_search(
    title: str, artist: str, duration_s: float, *, diag: _Diagnostic
) -> LrclibLyrics | None:
    """/api/search fuzzy fallback when /api/get 404s.

    Returns the highest-scoring candidate fetched via /api/get/{id}, OR
    None if no candidate cleared the combined-score threshold (admin
    must paste an ID manually).
    """
    try:
        candidates = search_lrclib_fuzzy(title, artist, duration_s=duration_s or None)
    except LrclibNotFound:
        diag.search_status = "not_found"
        return None
    except LrclibError as exc:
        diag.search_status = "error"
        # Preserve the more recent error on the diagnostic, but only if /get
        # didn't already record its own.
        if diag.lrclib_error is None:
            diag.lrclib_error = str(exc)[:200]
        return None

    if not candidates:
        diag.search_status = "no_strong_match"
        return None

    top = candidates[0]
    diag.search_top_score = top.combined_score
    if top.combined_score < _FUZZY_PROMOTE_MIN_SCORE:
        diag.search_status = "no_strong_match"
        log.info(
            "lyrics_search_top_below_threshold",
            top_score=top.combined_score,
            threshold=_FUZZY_PROMOTE_MIN_SCORE,
            top_id=top.lrclib_id,
            top_title=top.title,
        )
        return None

    # Re-fetch by ID to get the lyric body (the /search endpoint returns
    # metadata only, not lyrics). Same retry/error handling as forced-ID.
    try:
        result = get_lrclib_by_id(top.lrclib_id)
    except LrclibNotFound:
        # Race: top result existed in /search but is gone by /get. Extremely
        # rare but possible. Treat as no strong match.
        diag.search_status = "fetched_404"
        return None
    except LrclibError as exc:
        diag.search_status = "fetched_error"
        if diag.lrclib_error is None:
            diag.lrclib_error = str(exc)[:200]
        return None

    diag.search_status = "hit"
    diag.lrclib_id_matched = result.lrclib_id or top.lrclib_id
    diag.duration_delta_s = top.duration_delta_s
    log.info(
        "lyrics_search_promoted_candidate",
        top_score=top.combined_score,
        top_id=top.lrclib_id,
        top_title=result.title,
        top_artist=result.artist,
        duration_delta_s=top.duration_delta_s,
    )
    return result


def _finish_extraction(
    input_: LyricsInput, lrclib_lyrics: LrclibLyrics | None, *, diag: _Diagnostic
) -> LyricsOutput:
    """Run Whisper, align, and assemble the final LyricsOutput.

    Whisper is ALWAYS run, even when LRCLIB missed entirely — the resulting
    whisper_only output becomes the admin's draft reference (persisted to
    `lyrics_whisper_draft` by the orchestrator).
    """
    prompt_hint = _truncate_whisper_prompt(_choose_prompt_text(lrclib_lyrics))

    try:
        whisper_result = transcribe_for_lyrics(
            input_.audio_path,
            prompt=prompt_hint,
            language=input_.language,
        )
    except WhisperLyricsError as exc:
        raise TerminalError(f"nova.audio.lyrics: whisper transcription failed — {exc}") from exc

    if not whisper_result.words:
        raise TerminalError(
            "nova.audio.lyrics: whisper returned zero words — audio may be instrumental"
        )

    if lrclib_lyrics is not None and lrclib_lyrics.synced_lines:
        synced_output = _build_lrclib_synced_plus_whisper(lrclib_lyrics, whisper_result)
        # Kill switch: when disabled, always trust the synced anchors
        # regardless of confidence (pre-2026-05-27 behavior). Reserved
        # for emergency rollback if the 0.20 threshold demotes too many
        # legitimate extractions in prod. See settings docstring.
        from app.config import settings as _app_settings  # noqa: PLC0415

        fallback_enabled = getattr(_app_settings, "lyric_synced_anchor_fallback_enabled", True)
        if not fallback_enabled or synced_output.confidence >= _SYNCED_CONFIDENCE_MIN:
            diag.fallback_path = "ready_synced"
            return _attach_diagnostic(synced_output, diag)
        # Synced anchors look like a different recording's. Fall through
        # to the plain-text path so Whisper's timestamps win. This is
        # the layer-2 safety net behind LRCLIB's `duration` disambiguation
        # — covers tracks where LRCLIB has no duration metadata or
        # duration matched ±2s but the recording is still different.
        log.info(
            "lyrics_synced_anchor_low_confidence_fallback",
            confidence=synced_output.confidence,
            threshold=_SYNCED_CONFIDENCE_MIN,
            lrclib_id=lrclib_lyrics.lrclib_id,
            track_title=input_.track_title,
            artist=input_.artist,
            duration_s=input_.duration_s,
            has_plain_lines=bool(lrclib_lyrics.plain_lines),
        )
        if lrclib_lyrics.plain_lines:
            diag.fallback_path = "hawai_fallback_plain"
            return _attach_diagnostic(
                _build_lrclib_plain_plus_whisper(lrclib_lyrics, whisper_result), diag
            )
        diag.fallback_path = "hawai_fallback_whisper"
        return _attach_diagnostic(_build_whisper_only(whisper_result), diag)

    if lrclib_lyrics is not None and lrclib_lyrics.plain_lines:
        diag.fallback_path = "ready_plain"
        return _attach_diagnostic(
            _build_lrclib_plain_plus_whisper(lrclib_lyrics, whisper_result), diag
        )

    # No LRCLIB result at all OR LRCLIB hit but row had no lines.
    diag.fallback_path = "needs_manual_lyrics"
    return _attach_diagnostic(_build_whisper_only(whisper_result), diag)


def _attach_diagnostic(out: LyricsOutput, diag: _Diagnostic) -> LyricsOutput:
    """Stamp runtime metadata onto the output before returning."""
    out.prompt_version = LyricsExtractionAgent.spec.prompt_version
    out.lyrics_diagnostic = diag.to_dict()
    return out


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

    The result is NOT publishable (see PUBLISHABLE_LYRICS_SOURCES). The
    orchestrator routes it to `lyrics_whisper_draft` and sets
    `lyrics_status='needs_manual_lyrics'` so admin sees it as draft-only
    and can paste a real LRCLIB row ID to recover.

    Strategy: break a new line every time Whisper's gap between consecutive
    words exceeds 0.9s. Keeps text readable; cheap; no NLP. Confidence is
    set to 0.5 to signal "no canonical source" — the source field plus
    the publishable allowlist do the publishability gating.
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
