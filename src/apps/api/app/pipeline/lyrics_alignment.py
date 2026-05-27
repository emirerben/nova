"""Align canonical lyric text to Whisper word-level timings.

WHY THIS EXISTS
---------------
A canonical lyric source (LRCLIB, formerly Genius) gives accurate text but
either no timings or only line-level timings. Whisper gives word-grain
timings but mishears words (especially on Turkish + heavy-instrumental
tracks). We want:

  - The canonical text shown to the viewer (correct spelling, line breaks)
  - Whisper's timings driving the karaoke / per-word animation

Two entry points:

  align(canonical_lines, whisper_words)
      Used when the source provides text only (LRCLIB plainLyrics fallback,
      legacy Genius). Walks Whisper's word stream and pops one Whisper word
      per canonical word in order, using fuzzy character matching to
      recover from drift.

  align_with_line_anchors(anchor_lines, whisper_words, track_end_s)
      Used when the source provides line-level start times (LRCLIB
      syncedLyrics). Each anchor line defines a hard time window; per-word
      timing is distributed within the window using Whisper. Strictly
      higher quality than the unanchored path because the line bounds are
      exact rather than fuzzy-matched.

This is a deliberately simple O(N*W) Needleman-Wunsch-style alignment with
a small look-ahead window — it's pure Python, has no SciPy dependency, and
costs <50ms for typical 30-second sections.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

import structlog

from app.services.lrclib_client import SyncedLine
from app.services.whisper_lyrics import WhisperWord

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class AlignedWord:
    text: str  # canonical text (from Genius)
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class AlignedLine:
    text: str  # canonical line text (from Genius, original casing/punctuation)
    start_s: float
    end_s: float
    words: tuple[AlignedWord, ...]


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    lines: tuple[AlignedLine, ...]
    confidence: float  # 0..1 — fraction of canonical words matched within window


# Look-ahead used when a canonical word can't match Whisper at the current
# cursor — we peek up to N whisper words forward looking for a similar token.
# Larger window = more forgiving of Whisper hallucinations; smaller = faster.
_LOOKAHEAD_WINDOW = 4

# Minimum normalized similarity for a fuzzy match. 0.65 is permissive enough
# to absorb routine Whisper errors ("you're" → "your") without bridging
# completely unrelated tokens. Validated on hand-aligned Turkish + English
# fixtures.
_MIN_SIMILARITY = 0.65

# When we can't align a canonical word at all, we interpolate its start/end
# timing between the surrounding aligned words. This factor controls how
# much of the gap each unaligned run gets.
_INTERPOLATION_PADDING_S = 0.02

# Small visual spacer reserved at the trailing-unmatched spread's tail end so
# the last interpolated token doesn't land at the exact same timestamp as the
# next line's first accepted Whisper word. Distinct from the renderer's
# `_LINE_NEXT_LINE_GAP_S` (which gates audio-time gap, not interpolation room).
_NEXT_LINE_SAFETY_S = 0.05

# Default per-token budget multiplier when the caller cannot supply a
# tail-end cap (e.g. the unanchored `align()` path). Each unmatched token
# gets `_MAX_INTERP_SLICE_S * this factor` seconds, sized below normal
# karaoke pace so a noisy tail never runs long against the next line.
_TRAILING_SPREAD_CONSERVATIVE_FACTOR = 0.5


def align(
    canonical_lines: list[str],
    whisper_words: list[WhisperWord] | tuple[WhisperWord, ...],
) -> AlignmentResult:
    """Walk canonical line/word grid against Whisper words.

    Returns an AlignmentResult whose `lines` cover every canonical line.
    Each canonical word has a start/end_s — interpolated if Whisper had no
    matching token. Lines with zero matched words are skipped.

    Confidence = matched / total canonical words. Caller can store the
    number on MusicTrack.lyrics_cached["confidence"] for QA.
    """
    if not canonical_lines or not whisper_words:
        return AlignmentResult(lines=(), confidence=0.0)

    whisper_list = list(whisper_words)
    aligned_lines: list[AlignedLine] = []
    total_words = 0
    matched_words = 0
    cursor = 0  # index into whisper_list

    for canonical_line in canonical_lines:
        words_in_line = _tokenize(canonical_line)
        if not words_in_line:
            continue
        total_words += len(words_in_line)

        slots: list[tuple[str, float | None, float | None]] = []
        for canonical_word in words_in_line:
            match_idx = _find_match(canonical_word, whisper_list, cursor)
            if match_idx is None:
                slots.append((canonical_word, None, None))
            else:
                ww = whisper_list[match_idx]
                slots.append((canonical_word, ww.start_s, ww.end_s))
                matched_words += 1
                cursor = match_idx + 1

        line_aligned = _build_line(canonical_line, slots)
        if line_aligned is not None:
            aligned_lines.append(line_aligned)

    confidence = (matched_words / total_words) if total_words else 0.0
    log.info(
        "lyrics_alignment_done",
        canonical_lines=len(canonical_lines),
        aligned_lines=len(aligned_lines),
        whisper_words=len(whisper_list),
        matched_words=matched_words,
        total_canonical_words=total_words,
        confidence=round(confidence, 3),
    )
    return AlignmentResult(lines=tuple(aligned_lines), confidence=confidence)


def _tokenize(line: str) -> list[str]:
    """Split a canonical line into displayable words.

    Preserves apostrophes (don't, you're) which Whisper and Genius both keep
    as single tokens. Drops standalone punctuation runs.
    """
    tokens = re.findall(r"[\w']+", line, flags=re.UNICODE)
    return [t for t in tokens if any(c.isalnum() for c in t)]


def _normalize(token: str) -> str:
    """Lowercase + strip diacritics + strip apostrophes for matching only.

    Returned string is for SIMILARITY scoring, not display. Display strings
    always come from the canonical (Genius) source.
    """
    # NFKD splits "ü" → "u" + combining mark; we discard the marks.
    decomposed = unicodedata.normalize("NFKD", token)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def _similarity(a: str, b: str) -> float:
    """Levenshtein-style ratio in [0, 1] via stdlib `difflib.SequenceMatcher`.

    Tolerates single-character substitutions in the middle of tokens
    (`world` ↔ `wurld` → 0.8) which is the routine Whisper mishear we want
    to absorb. Tiny tokens (≤2 chars) require exact match — fuzzy matches
    on single letters are too noisy for alignment.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) <= 2:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _find_match(
    canonical_word: str,
    whisper_list: list[WhisperWord],
    cursor: int,
) -> int | None:
    """Find the best Whisper-word index for `canonical_word` near `cursor`.

    Scans cursor .. cursor+LOOKAHEAD. Returns None if no candidate clears
    `_MIN_SIMILARITY`. Returning the EXACT index is critical — we use it to
    advance the cursor past matched words and keep alignment monotonic.
    """
    norm_canonical = _normalize(canonical_word)
    if not norm_canonical:
        return None

    best_idx: int | None = None
    best_score = 0.0
    end = min(len(whisper_list), cursor + _LOOKAHEAD_WINDOW + 1)
    for idx in range(cursor, end):
        score = _similarity(norm_canonical, _normalize(whisper_list[idx].text))
        if score > best_score:
            best_score = score
            best_idx = idx
            # Exact match — no need to look further.
            if score >= 0.999:
                break

    if best_idx is None or best_score < _MIN_SIMILARITY:
        return None
    return best_idx


def _build_line(
    canonical_line: str,
    slots: list[tuple[str, float | None, float | None]],
    *,
    tail_end_cap_s: float | None = None,
) -> AlignedLine | None:
    """Stitch a per-word slot list into an AlignedLine.

    Words missing timings (None) are interpolated linearly between their
    surrounding aligned neighbors. If a line has ZERO aligned anchors, the
    line is dropped from the output — it can't be timed without at least
    one Whisper match.

    `tail_end_cap_s` is the upper bound for the trailing-unmatched spread —
    typically the caller's `window_end` (or the next line's first reliable
    Whisper start minus `_NEXT_LINE_SAFETY_S`). When supplied, several
    trailing unaligned tokens are spread across `[prev_end, tail_end_cap_s]`
    instead of being collapsed onto a single 0.25s window at `prev_end`.
    When not supplied (the unanchored `align()` path has no per-line window),
    a conservative half-budget formula is used. See plan §_build_line
    fallback guardrail.
    """
    timed_indices = [i for i, (_, s, e) in enumerate(slots) if s is not None and e is not None]
    if not timed_indices:
        return None

    # Pre-compute the trailing-unmatched-tail span so the spread formula
    # below sees the same `tail_count` for every word in the tail. Without
    # this, each iteration recomputes the index of the "next unmatched
    # slot" and the spread becomes order-dependent on iteration state.
    last_timed = max(timed_indices)
    tail_unmatched = [j for j, (_, s, _) in enumerate(slots) if j > last_timed and s is None]
    tail_count = len(tail_unmatched)

    # Emit guardrail logs so the rate of trailing-unmatched fallbacks is
    # visible in prod. The pre-fix code silently collapsed every such tail
    # onto a 250ms window at `prev_end`, masking 1-3 seconds of canonical
    # lyric. Two distinct events:
    #   - `lyrics_alignment_trailing_collapse` (WARN): the original ≥2-token
    #     collapse pattern — the bug class that motivated this fix.
    #   - `lyrics_alignment_trailing_single_no_cap` (WARN): a single trailing
    #     unmatched token with no caller-supplied tail_end_cap_s, falling
    #     back to the conservative budget. Low-confidence by construction;
    #     when AlignedLine gains a `line_alignment_status` field (follow-up
    #     PR), this site should also stamp `"low_conf"`.
    # Logging once per occurrence (not per token) keeps log volume reasonable.
    if tail_count >= 2:
        log.warning(
            "lyrics_alignment_trailing_collapse",
            line=canonical_line.strip()[:80],
            prev_end=round(slots[last_timed][2] or 0.0, 3),
            unmatched_count=tail_count,
            canonical_tail=[slots[j][0] for j in tail_unmatched],
            tail_end_cap_s=(round(tail_end_cap_s, 3) if tail_end_cap_s is not None else None),
        )
    elif tail_count == 1 and tail_end_cap_s is None:
        log.warning(
            "lyrics_alignment_trailing_single_no_cap",
            line=canonical_line.strip()[:80],
            prev_end=round(slots[last_timed][2] or 0.0, 3),
            canonical_tail=[slots[j][0] for j in tail_unmatched],
        )

    # Linear interpolation for runs of unaligned words.
    spans: list[tuple[float, float]] = []
    for i, (_, s, e) in enumerate(slots):
        if s is not None and e is not None:
            spans.append((s, e))
            continue

        # Find surrounding anchors.
        prev_idx = max((j for j in timed_indices if j < i), default=None)
        next_idx = min((j for j in timed_indices if j > i), default=None)

        if prev_idx is not None and next_idx is not None:
            prev_end = slots[prev_idx][2] or 0.0
            next_start = slots[next_idx][1] or prev_end
            gap = max(0.0, next_start - prev_end)
            # How many unaligned slots share this gap? Distribute uniformly.
            unaligned_run = sum(1 for j in range(prev_idx + 1, next_idx) if slots[j][1] is None)
            slice_dur = gap / unaligned_run if unaligned_run else 0.0
            position_in_run = sum(1 for j in range(prev_idx + 1, i + 1) if slots[j][1] is None) - 1
            start = prev_end + slice_dur * position_in_run + _INTERPOLATION_PADDING_S
            end = start + max(slice_dur - 2 * _INTERPOLATION_PADDING_S, 0.05)
        elif prev_idx is not None:
            # Trailing unaligned word — bounded spread across the available
            # tail window. Pre-fix code collapsed every trailing token onto
            # `prev_end + 0.02s` for 0.25s regardless of count, silently
            # dropping the back half of a long line when LRCLIB's next-line
            # anchor was earlier than the actual vocal. See plan §_build_line
            # fallback guardrail + the f65b5762 / Hawai regression test.
            prev_end = slots[prev_idx][2] or 0.0
            position_in_tail = sum(1 for j in range(prev_idx + 1, i + 1) if slots[j][1] is None) - 1

            # tail_end_cap_s = caller-supplied upper bound (window_end /
            # next-line-safe boundary). When None — or when the supplied cap
            # leaves no positive room past `prev_end` (the previous matched
            # Whisper word ran past the hard window already) — we fall back
            # to half of _MAX_INTERP_SLICE_S per token so a noisy tail never
            # overruns the next line nor lingers past sane karaoke pace.
            conservative_budget = (
                _MAX_INTERP_SLICE_S * tail_count * _TRAILING_SPREAD_CONSERVATIVE_FACTOR
            )
            if tail_end_cap_s is not None:
                effective_cap = tail_end_cap_s - _NEXT_LINE_SAFETY_S
                cap_budget = effective_cap - prev_end
                # If the cap leaves no usable budget, defer to the
                # conservative formula. Tail tokens will extend past the
                # cap, but the per-token cap (_MAX_INTERP_SLICE_S below)
                # still keeps each word from running long.
                budget = cap_budget if cap_budget > 0.05 else conservative_budget
            else:
                budget = conservative_budget

            # Per-token slice size, capped at the standard interp slice so a
            # very wide cap (e.g. the whole track tail) doesn't blow up
            # individual word durations.
            slice_dur = min(budget / max(1, tail_count), _MAX_INTERP_SLICE_S)
            slice_dur = max(slice_dur, 0.05)

            start = prev_end + _INTERPOLATION_PADDING_S + slice_dur * position_in_tail
            end = start + max(slice_dur - _INTERPOLATION_PADDING_S, 0.05)

            # Defense-in-depth clamp: when a usable cap was supplied AND
            # `prev_end` actually sits before the cap, never let `end` cross
            # the safety margin. When `prev_end` already overran the cap
            # (matched word's end exceeded the LRCLIB window), the
            # conservative formula above ran instead — no cap to clamp
            # against, the per-token slice cap is the only bound.
            if tail_end_cap_s is not None and prev_end < tail_end_cap_s - _NEXT_LINE_SAFETY_S:
                hard_cap = tail_end_cap_s - _NEXT_LINE_SAFETY_S
                end = min(end, hard_cap)
        else:
            # Leading unaligned word — sit it just before the next anchor.
            next_start = slots[next_idx][1] if next_idx is not None else 0.0  # type: ignore[index]
            start = max(0.0, (next_start or 0.0) - 0.25)
            end = max(start + 0.05, (next_start or 0.0) - _INTERPOLATION_PADDING_S)

        spans.append((start, end))

    # Build per-word objects (canonical text preserved).
    aligned_words = tuple(
        AlignedWord(
            text=word,
            start_s=round(spans[i][0], 3),
            end_s=round(max(spans[i][1], spans[i][0] + 0.05), 3),
        )
        for i, (word, _, _) in enumerate(slots)
    )

    line_start = aligned_words[0].start_s
    line_end = aligned_words[-1].end_s
    return AlignedLine(
        text=canonical_line.strip(),
        start_s=line_start,
        end_s=line_end,
        words=aligned_words,
    )


# Safety fallback for the very last anchor line when neither track_end_s nor
# any Whisper words are available to bound it. 3 seconds is long enough that
# karaoke renders the line at a readable pace, short enough that nothing
# beyond it gets eaten.
_FALLBACK_TRAILING_WINDOW_S = 3.0

# Padding added past the last whisper word when track_end_s isn't supplied.
# Whisper sometimes truncates the final word early; this prevents the last
# karaoke line from clipping.
_LAST_WORD_TAIL_PAD_S = 0.5

# Maximum per-word slice when Strategy 3 (linear interpolation) is forced.
# Without a cap, a long instrumental break — a melodic build-up in an
# Artbat set, the bridge in a power ballad — stretches `window_end` until
# the next sung anchor, and dividing 60s by 5 words would highlight each
# word for 12 SECONDS. That kills the snappy karaoke pacing the per-word
# animation is built around. 0.8s caps highlights at a comfortable reading
# pace; after the final capped word, the karaoke line clears the screen
# and the instrumental gap plays out clean (no stale highlight stuck on
# the last token).
_MAX_INTERP_SLICE_S = 0.8


def align_with_line_anchors(
    anchor_lines: Sequence[SyncedLine],
    whisper_words: Sequence[WhisperWord] | tuple[WhisperWord, ...],
    track_end_s: float | None = None,
) -> AlignmentResult:
    """Align canonical lines using LRC line timestamps as hard bounds.

    Each `anchor_lines[i]` provides exact start time `t_i`; the line's window
    is `[t_i, t_{i+1})` (last line uses `track_end_s` or
    `whisper_words[-1].end_s + 0.5`). Per-word timing is distributed within
    each window using Whisper's words.

    Strictly higher quality than `align()`: line bounds are exact rather
    than recovered via fuzzy matching, so we don't bleed timing across
    line breaks.

    Args:
        anchor_lines: SyncedLine list from `lrclib_client._parse_synced_lyrics`,
            sorted by `start_s` ascending. Multi-timestamp lines have
            already been expanded into one anchor per timestamp.
        whisper_words: Whisper's per-word timings for the full track.
        track_end_s: Optional hard upper bound for the final line's window.
            Defaults to `whisper_words[-1].end_s + 0.5` if Whisper data
            exists, otherwise a 3s fallback.
    """
    if not anchor_lines:
        return AlignmentResult(lines=(), confidence=0.0)

    whisper_list = list(whisper_words)
    aligned_lines: list[AlignedLine] = []
    total_words = 0
    matched_words = 0

    for i, anchor in enumerate(anchor_lines):
        window_start = anchor.start_s
        if i + 1 < len(anchor_lines):
            window_end = anchor_lines[i + 1].start_s
        elif track_end_s is not None:
            window_end = track_end_s
        elif whisper_list:
            window_end = whisper_list[-1].end_s + _LAST_WORD_TAIL_PAD_S
        else:
            window_end = window_start + _FALLBACK_TRAILING_WINDOW_S

        # Guard against malformed input — anchors out of order would yield a
        # negative window. Skip rather than emit nonsense timings.
        if window_end <= window_start:
            continue

        expected = _tokenize(anchor.text)
        if not expected:
            continue
        total_words += len(expected)

        words_in_window = [w for w in whisper_list if window_start <= w.start_s < window_end]

        line, matched_in_line = _align_within_window(
            anchor.text, expected, words_in_window, window_start, window_end
        )
        if line is not None:
            aligned_lines.append(line)
            matched_words += matched_in_line

    confidence = (matched_words / total_words) if total_words else 0.0
    log.info(
        "lyrics_alignment_anchored_done",
        anchor_lines=len(anchor_lines),
        aligned_lines=len(aligned_lines),
        whisper_words=len(whisper_list),
        matched_words=matched_words,
        total_canonical_words=total_words,
        confidence=round(confidence, 3),
    )
    return AlignmentResult(lines=tuple(aligned_lines), confidence=confidence)


def _align_within_window(
    anchor_text: str,
    expected_words: list[str],
    whisper_words_in_window: list[WhisperWord],
    window_start: float,
    window_end: float,
) -> tuple[AlignedLine | None, int]:
    """Distribute `expected_words` across `[window_start, window_end)`.

    Three strategies, in priority order:

    1. **Exact-count zip** (fast path, common case): if Whisper produced
       exactly the right number of words in the window, zip directly. Every
       canonical word gets a real Whisper timing — no interpolation.

    2. **Fuzzy align within window**: Whisper produced some but not the
       right count. Run the cursor-based fuzzy matcher restricted to this
       window, then interpolate gaps via `_build_line`. Same logic the
       unanchored path uses, just scoped.

    3. **Linear interpolation**: zero Whisper words landed in the window
       (rare — instrumental gap, or Whisper missed a quiet line). Distribute
       canonical words uniformly across the window, but CAP each word's
       duration at `_MAX_INTERP_SLICE_S`. Without the cap, a 60s
       instrumental break would hold a single word on screen for 12s and
       kill the per-word karaoke pacing; with it, the line plays out at a
       readable pace and the rest of the gap clears the screen.

    Returns `(line, matched_count)` where `matched_count` counts words with
    real Whisper timings (strategy 1 → all, strategy 2 → variable,
    strategy 3 → 0). This drives `AlignmentResult.confidence`.
    """
    # Strategy 1 — exact-count fast path.
    if len(whisper_words_in_window) == len(expected_words):
        words = tuple(
            AlignedWord(
                text=expected_words[k],
                start_s=round(whisper_words_in_window[k].start_s, 3),
                end_s=round(whisper_words_in_window[k].end_s, 3),
            )
            for k in range(len(expected_words))
        )
        return (
            AlignedLine(
                text=anchor_text.strip(),
                start_s=words[0].start_s,
                end_s=words[-1].end_s,
                words=words,
            ),
            len(expected_words),
        )

    # Strategy 2 — fuzzy align within the window using the existing matcher.
    if whisper_words_in_window:
        slots: list[tuple[str, float | None, float | None]] = []
        cursor = 0
        matched_count = 0
        for canonical_word in expected_words:
            match_idx = _find_match(canonical_word, whisper_words_in_window, cursor)
            if match_idx is None:
                slots.append((canonical_word, None, None))
            else:
                ww = whisper_words_in_window[match_idx]
                slots.append((canonical_word, ww.start_s, ww.end_s))
                matched_count += 1
                cursor = match_idx + 1
        # Pass the anchored window's upper bound so the trailing-unmatched
        # guardrail in `_build_line` can spread (not collapse) tokens up to
        # `window_end - _NEXT_LINE_SAFETY_S`.
        line = _build_line(anchor_text, slots, tail_end_cap_s=window_end)
        if line is not None:
            return line, matched_count
        # `_build_line` only returns None when zero words matched — fall
        # through to interpolation so the line still renders.

    # Strategy 3 — linear interpolation across the window.
    # Clamp to [0.05, _MAX_INTERP_SLICE_S] — see constant docstring above
    # for why the cap matters (instrumental-break pacing).
    slice_dur = min(
        max((window_end - window_start) / len(expected_words), 0.05),
        _MAX_INTERP_SLICE_S,
    )
    words = tuple(
        AlignedWord(
            text=expected_words[k],
            start_s=round(window_start + k * slice_dur, 3),
            end_s=round(window_start + (k + 1) * slice_dur, 3),
        )
        for k in range(len(expected_words))
    )
    return (
        AlignedLine(
            text=anchor_text.strip(),
            start_s=words[0].start_s,
            end_s=words[-1].end_s,
            words=words,
        ),
        0,
    )
