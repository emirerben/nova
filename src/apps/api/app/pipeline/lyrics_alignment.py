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
      syncedLyrics). Each anchor line defines a primary time window;
      per-word timing is distributed within the window using Whisper. A
      narrow prefix lookback repairs isolated late anchors without turning
      that local correction into a global audio-vs-LRC drift.

This is a deliberately simple O(N*W) Needleman-Wunsch-style alignment with
a small look-ahead window — it's pure Python, has no SciPy dependency, and
costs <50ms for typical 30-second sections.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

import structlog

from app.config import settings
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

# Whisper occasionally returns a dense cluster of exact-count words with
# effectively identical 50ms timings. If we cache those verbatim, karaoke
# stays on the previous word and then flashes the clustered words all at once.
_COLLAPSED_WORD_MAX_DUR_S = 0.08
_COLLAPSED_RUN_MAX_SPAN_S = 0.12
_COLLAPSED_RUN_MIN_WORDS = 2
_COLLAPSED_EDGE_GAP_S = 0.08
_COLLAPSED_EDGE_MIN_DUR_S = 0.25
_COLLAPSED_MIN_SLICE_S = 0.05

# LRCLIB syncedLyrics anchors are usually good line starts, but a single late
# anchor can exclude the real opening words before alignment ever sees them.
# Allow a narrow pre-anchor lookback only when the canonical line prefix is
# clearly present before the anchor.
_ANCHOR_PREFIX_LOOKBACK_S = 1.5
_ANCHOR_PREFIX_LOOKBACK_MAX_WORDS = 5
_ANCHOR_PREFIX_LOOKBACK_MIN_MATCHES = 3
_ANCHOR_PREFIX_LOOKBACK_MIN_SHIFT_S = 0.2

# If a synced LRCLIB line only matches a prefix and Whisper has a substantial
# unused tail in the same line window, the canonical row is probably wrong for
# this recording/repeat. Preserve the trusted canonical prefix, then let the
# audio-backed Whisper tail win.
_LOW_CONFIDENCE_WHISPER_TAIL_MAX_MATCH_RATIO = 0.50
_LOW_CONFIDENCE_WHISPER_TAIL_MAX_PREFIX_MISSES = 2
_LOW_CONFIDENCE_WHISPER_TAIL_MIN_PREFIX_SIMILARITY = 0.95
_LOW_CONFIDENCE_WHISPER_TAIL_MIN_UNUSED_WORDS = 2
_LOW_CONFIDENCE_WHISPER_TAIL_MAX_END_GAP_S = 1.25
_LOW_CONFIDENCE_WHISPER_TAIL_MAX_SIMILARITY = 0.80


@dataclass(frozen=True, slots=True)
class _PrefixLookbackMatch:
    start_s: float
    matched_count: int
    matched_words: tuple[str, ...]


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


def _count_ordered_prefix_matches(
    expected_words: list[str],
    whisper_words: list[WhisperWord],
    *,
    cursor: int = 0,
) -> tuple[int, int | None, tuple[str, ...]]:
    """Count how many canonical prefix words match in order from `cursor`."""
    matched: list[str] = []
    first_idx: int | None = None
    next_cursor = cursor
    for canonical_word in expected_words[:_ANCHOR_PREFIX_LOOKBACK_MAX_WORDS]:
        match_idx = _find_match(canonical_word, whisper_words, next_cursor)
        if match_idx is None:
            break
        if first_idx is None:
            first_idx = match_idx
        matched.append(canonical_word)
        next_cursor = match_idx + 1
    return len(matched), first_idx, tuple(matched)


def _find_pre_anchor_prefix_match(
    expected_words: list[str],
    lookback_words: list[WhisperWord],
    *,
    anchor_start_s: float,
) -> _PrefixLookbackMatch | None:
    """Find a strong canonical prefix match just before a late LRC anchor."""
    if len(expected_words) < _ANCHOR_PREFIX_LOOKBACK_MIN_MATCHES:
        return None
    if len(lookback_words) < _ANCHOR_PREFIX_LOOKBACK_MIN_MATCHES:
        return None

    best: _PrefixLookbackMatch | None = None
    for cursor in range(len(lookback_words)):
        matched_count, first_idx, matched_words = _count_ordered_prefix_matches(
            expected_words,
            lookback_words,
            cursor=cursor,
        )
        if matched_count < _ANCHOR_PREFIX_LOOKBACK_MIN_MATCHES or first_idx is None:
            continue

        start_s = lookback_words[first_idx].start_s
        if anchor_start_s - start_s < _ANCHOR_PREFIX_LOOKBACK_MIN_SHIFT_S:
            continue

        candidate = _PrefixLookbackMatch(
            start_s=start_s,
            matched_count=matched_count,
            matched_words=matched_words,
        )
        if best is None:
            best = candidate
            continue
        if candidate.matched_count > best.matched_count:
            best = candidate
        elif candidate.matched_count == best.matched_count and candidate.start_s < best.start_s:
            best = candidate

    return best


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
    aligned_words = _repair_collapsed_word_runs(
        tuple(
            AlignedWord(
                text=word,
                start_s=round(spans[i][0], 3),
                end_s=round(max(spans[i][1], spans[i][0] + 0.05), 3),
            )
            for i, (word, _, _) in enumerate(slots)
        ),
        line=canonical_line,
    )

    line_start = aligned_words[0].start_s
    line_end = aligned_words[-1].end_s
    return AlignedLine(
        text=canonical_line.strip(),
        start_s=line_start,
        end_s=line_end,
        words=aligned_words,
    )


def _repair_collapsed_word_runs(
    words: tuple[AlignedWord, ...],
    *,
    line: str,
) -> tuple[AlignedWord, ...]:
    """Spread dense Whisper timestamp clusters across their local phrase."""
    if len(words) < _COLLAPSED_RUN_MIN_WORDS:
        return words

    def _dur(word: AlignedWord) -> float:
        return max(0.0, word.end_s - word.start_s)

    def _run_span(items: list[AlignedWord]) -> float:
        return max(w.end_s for w in items) - min(w.start_s for w in items)

    out = list(words)
    changed = False
    i = 0
    while i < len(out):
        if _dur(out[i]) > _COLLAPSED_WORD_MAX_DUR_S:
            i += 1
            continue

        run_start = i
        run_end = i
        run_items = [out[i]]
        j = i + 1
        while j < len(out) and _dur(out[j]) <= _COLLAPSED_WORD_MAX_DUR_S:
            candidate = [*run_items, out[j]]
            if _run_span(candidate) > _COLLAPSED_RUN_MAX_SPAN_S:
                break
            run_items.append(out[j])
            run_end = j
            j += 1

        if run_end - run_start + 1 < _COLLAPSED_RUN_MIN_WORDS:
            i = run_end + 1
            continue

        repair_start = run_start
        repair_end = run_end
        left_edge_added = False
        right_boundary_s: float | None = None
        run_min_start = min(w.start_s for w in run_items)
        run_max_end = max(w.end_s for w in run_items)

        if repair_start > 0:
            prev = out[repair_start - 1]
            if (
                _dur(prev) >= _COLLAPSED_EDGE_MIN_DUR_S
                and run_min_start - prev.end_s <= _COLLAPSED_EDGE_GAP_S
            ):
                repair_start -= 1
                left_edge_added = True

        if repair_end + 1 < len(out):
            nxt = out[repair_end + 1]
            if (
                _dur(nxt) >= _COLLAPSED_EDGE_MIN_DUR_S
                and nxt.start_s - run_max_end <= _COLLAPSED_EDGE_GAP_S
            ):
                if left_edge_added:
                    right_boundary_s = nxt.start_s
                else:
                    repair_end += 1

        count = repair_end - repair_start + 1
        budget_start = out[repair_start].start_s
        budget_end = right_boundary_s or out[repair_end].end_s
        budget_s = budget_end - budget_start
        if count <= 0 or budget_s < count * _COLLAPSED_MIN_SLICE_S:
            i = run_end + 1
            continue

        slice_s = min(budget_s / count, _MAX_INTERP_SLICE_S)
        rebuilt: list[AlignedWord] = []
        for offset, original in enumerate(out[repair_start : repair_end + 1]):
            start_s = budget_start + slice_s * offset
            end_s = min(budget_end, start_s + slice_s)
            if end_s - start_s < _COLLAPSED_MIN_SLICE_S:
                end_s = min(budget_end, start_s + _COLLAPSED_MIN_SLICE_S)
            rebuilt.append(
                AlignedWord(
                    text=original.text,
                    start_s=round(start_s, 3),
                    end_s=round(end_s, 3),
                )
            )

        out[repair_start : repair_end + 1] = rebuilt
        changed = True
        log.warning(
            "lyrics_alignment_collapsed_word_run_repaired",
            line=line.strip()[:80],
            collapsed_count=run_end - run_start + 1,
            repaired_count=count,
            old_start_s=round(budget_start, 3),
            old_end_s=round(budget_end, 3),
        )
        i = repair_end + 1

    return tuple(out) if changed else words


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


# LRC-anchor re-anchor threshold. When the detected audio-vs-LRC shift on
# the first matched line exceeds this value, we conclude the audio cut
# does not match the LRC-indexed cut (e.g. the "official video" cut of
# Instant Crush is +2.57s vs the album cut LRCLIB indexed), and Whisper's
# per-line `start_s` / `end_s` cannot be trusted as line bounds — Whisper
# stacks tokens on instrumental moments and produces line spans 2-3s too
# short relative to the actual audio. In that case we re-anchor every
# `AlignedLine.start_s` / `end_s` to `LRC_anchor[i] + shift`, leaving the
# per-word Whisper timings untouched (karaoke `\kf` + per-word-pop still
# get the original Whisper word stream — they consume words, not line
# bounds, so they're unaffected).
#
# Threshold sized so well-aligned tracks (Whisper accuracy ~100-300ms on
# clean studio recordings) are unaffected. Only fires when there's a real
# systematic misalignment.
_AUDIO_SHIFT_THRESHOLD_S = 1.0

# Safety gap subtracted from the next anchor when computing a re-anchored
# line's end. Mirrors the renderer's `_LINE_NEXT_LINE_GAP_S` but kept
# separate so the alignment-layer value can be tuned independently of the
# renderer's gap_cap.
_REANCHOR_NEXT_LINE_SAFETY_S = 0.05

# Last-line fallback duration when re-anchoring and there's no `next_anchor`
# (the final LRC line). Used in conjunction with `track_end_s` — we cap the
# re-anchored end at `track_end_s + shift` (in case the shift extends past
# track end) and floor at `start + this`. Sized to comfortably cover a
# sung last line (most songs end with a 2-4s sustain on the final word).
_REANCHOR_LAST_LINE_MIN_DUR_S = 3.0


# Multi-line median re-anchor — secondary path layered above the single-L0
# `_AUDIO_SHIFT_THRESHOLD_S` check. The single-L0 path only fires for
# `|shift| > 1.0s` because Whisper's L0 detection has ~100-300ms jitter on
# clean tracks; a tighter L0-only threshold would re-anchor tracks that
# are already correct. Multi-line median uses the consistency of the first
# N aligned lines as evidence that a sub-second shift is real drift (not
# noise), which catches the Overnight + The Bay class where the audio cut
# differs from the LRC-indexed cut by ~0.4-0.7s — too small to trigger
# single-L0 but consistent across every aligned line.
#
# When triggered: rewrite every line's `start_s` / `end_s` to
# `LRC_anchor[i] + median_shift` via the same `_apply_uniform_shift` helper
# the single-L0 path uses (DRY: identical bounded-extension guard for
# trailing-interpolation overshoot applies to both).
#
# Eligibility: a line counts toward the median only if its alignment had
# at least `_MULTILINE_MATCHED_COUNT_THRESHOLD` real Whisper word matches —
# Strategy 3 (pure linear interpolation, matched_count = 0) emits
# `start_s = window_start = anchor.start_s` by construction, so including
# Strategy 3 lines would force `shift = 0` and pull the median toward
# zero. The eligibility filter is the only thing keeping the multi-line
# median from silently disabling itself on heavy-instrumental tracks
# where most lines fall back to Strategy 3.
_MULTILINE_MIN_ELIGIBLE_LINES = 3
_MULTILINE_SAMPLE_SIZE = 5
_MULTILINE_MATCHED_COUNT_THRESHOLD = 2
_MULTILINE_MIN_APPLY_SHIFT_S = 0.2

# Spread metric: median absolute deviation (MAD), not stdev. MAD is the
# robust analogue of stdev — `median(|x - median(x)|)`. Less sensitive
# than stdev to a single outlier in small samples (N=5), which matters
# here because Whisper's per-line jitter routinely produces one ~2x
# outlier line in an otherwise tight 5-line cluster.
#
# Empirically driven cap. Parcels - Overnight prod shifts:
# [0.43, 0.85, 0.27, 0.71, 0.47] — stdev 0.232 (too wide for 0.15 guard)
# but MAD 0.20. Metronomy - The Bay shifts:
# [0.58, 0.51, 0.82, 0.67, 0.61] — MAD 0.06 (well under either threshold).
# Clean tracks (well-aligned audio) have MAD ~0-0.05 because Whisper
# jitter on matched lines is sub-100ms. Cap at 0.22 to admit Overnight
# (MAD 0.20) with small headroom for cross-run Whisper non-determinism.
_MULTILINE_MAX_MAD_S = 0.22

# Inlier-consensus guard: after the MAD cap admits a sample, require N
# of the 5 sample shifts to fall within `_MULTILINE_INLIER_K * MAD` of
# the median. This rejects scenarios where the MAD is small only because
# 2-3 shifts happen to cluster while others scatter widely (the
# non-uniform-drift class). For Overnight (MAD 0.20, inlier band ±0.30):
# 4 of 5 shifts are inliers (0.85 is the outlier), refined median = 0.45.
# For Bay (MAD 0.06, inlier band ±0.09): 3 of 5 inliers, refined = 0.61.
# Clean tracks (MAD ~0.03, tight band) usually have 4-5 inliers; their
# refined median is near zero so the `_MIN_APPLY_SHIFT_S` gate skips.
#
# After filtering, the APPLIED shift is `median(inliers)` — using inlier
# consensus means a single high-jitter line doesn't pull the applied
# value away from the cluster.
_MULTILINE_INLIER_K = 1.5
_MULTILINE_MIN_INLIERS = 3

# Linear re-anchor — first path, above the uniform multi-line median.
# This catches tracks where the LRCLIB-indexed recording and the actual
# audio cut diverge progressively rather than by one constant offset. The
# model is deliberately small and robust: Theil-Sen slope + median
# intercept, gated by sample count, track-span coverage, minimum slope,
# residual MAD, and implausible endpoint shifts. Any failed gate falls back
# to the existing uniform paths byte-for-byte.
_LINEAR_MIN_ELIGIBLE_LINES = 6
_LINEAR_MIN_SPAN_FRAC = 0.30
_LINEAR_MIN_SLOPE = 0.01
_LINEAR_MAX_RESID_MAD_S = 0.15
_LINEAR_MIN_X_DELTA_S = 1e-6


def align_with_line_anchors(
    anchor_lines: Sequence[SyncedLine],
    whisper_words: Sequence[WhisperWord] | tuple[WhisperWord, ...],
    track_end_s: float | None = None,
) -> AlignmentResult:
    """Align canonical lines using LRC line timestamps as line bounds.

    Each `anchor_lines[i]` provides primary start time `t_i`; the line's
    window is `[t_i, t_{i+1})` (last line uses `track_end_s` or
    `whisper_words[-1].end_s + 0.5`). Per-word timing is distributed within
    each window using Whisper's words. If the canonical line prefix is
    strongly present just before `t_i`, the window start is moved back for
    that line only.

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
    # Parallel-list invariant: `matched_counts[i]` is the count of real
    # Whisper word matches that backed `aligned_lines[i]`. The linear and
    # multi-line re-anchor paths use this to exclude Strategy 3 (pure
    # interpolation, matched_count = 0) lines from shift estimates — see the
    # constant block above for why.
    matched_counts: list[int] = []
    # Parallel to aligned_lines. True means a line-specific prefix lookback
    # moved the window start earlier than its LRC anchor. Those local fixes
    # must not be used as evidence for whole-track re-anchoring.
    local_anchor_adjusted: list[bool] = []
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
        effective_window_start = window_start
        prefix_match: _PrefixLookbackMatch | None = None

        # If the line's own prefix is already visible inside the normal
        # anchor window, keep the LRCLIB anchor as-is. The lookback is only
        # for the bug class where a late anchor chopped off the line start.
        in_window_prefix_count, _, _ = _count_ordered_prefix_matches(expected, words_in_window)
        if in_window_prefix_count < _ANCHOR_PREFIX_LOOKBACK_MIN_MATCHES:
            lookback_start = max(
                anchor_lines[i - 1].start_s if i > 0 else 0.0,
                window_start - _ANCHOR_PREFIX_LOOKBACK_S,
            )
            lookback_words = [w for w in whisper_list if lookback_start <= w.start_s < window_start]
            prefix_match = _find_pre_anchor_prefix_match(
                expected,
                lookback_words,
                anchor_start_s=window_start,
            )
            if prefix_match is not None:
                effective_window_start = prefix_match.start_s
                words_in_window = [
                    w for w in whisper_list if effective_window_start <= w.start_s < window_end
                ]
                log.warning(
                    "lyrics_alignment_anchor_prefix_lookback_applied",
                    line=anchor.text.strip()[:80],
                    anchor_s=round(window_start, 3),
                    effective_start_s=round(effective_window_start, 3),
                    shift_s=round(effective_window_start - window_start, 3),
                    matched_prefix=list(prefix_match.matched_words),
                    matched_count=prefix_match.matched_count,
                )

        line, matched_in_line = _align_within_window(
            anchor.text,
            expected,
            words_in_window,
            effective_window_start,
            window_end,
        )
        if line is not None:
            aligned_lines.append(line)
            matched_counts.append(matched_in_line)
            local_anchor_adjusted.append(prefix_match is not None)
            matched_words += matched_in_line

    confidence = (matched_words / total_words) if total_words else 0.0

    # LRC-anchor re-anchor for cases where the audio cut does not match the
    # LRC-indexed cut (e.g. official-video cut vs album cut).
    #
    # Detection signal: the shift between the first aligned line's
    # `start_s` (Whisper's first reliable detection of the song's first
    # sung word) and the first LRC anchor. When |shift| > the threshold,
    # we conclude Whisper is detecting vocals at a different audio offset
    # than LRC was indexed for, AND Whisper's per-line `start_s` / `end_s`
    # in subsequent lines are unreliable — Whisper systematically stacks
    # tokens on instrumental moments and assigns them wrong line windows
    # (the Instant Crush #361/362 class). LRC anchors with a uniform shift
    # applied are far more reliable for line bounds than Whisper's
    # per-line detection in this case.
    #
    # When triggered: rewrite every line's `start_s` to
    # `LRC_anchor[i] + shift` and `end_s` to the next anchor + shift
    # (minus a small safety gap), so the line spans the full LRC-implied
    # vocal window aligned to the actual audio. The last line gets a
    # bounded extension from `track_end_s` since it has no "next anchor".
    # Per-word Whisper timings (`AlignedWord.start_s` / `end_s`) are NOT
    # rewritten — karaoke `\kf` and per-word-pop consume per-word values
    # and stay byte-identical regardless of the line-bound rewrite.
    aligned_lines = _maybe_reanchor_to_lrc(
        aligned_lines=aligned_lines,
        anchor_lines=anchor_lines,
        track_end_s=track_end_s,
        whisper_words=whisper_list,
        matched_counts=matched_counts,
        local_anchor_adjusted=local_anchor_adjusted,
    )

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


def _mad(values: list[float], median_value: float) -> float:
    """Median absolute deviation — robust spread metric.

    `mad = median(|x - median(x)|)`. Less sensitive than stdev to a single
    outlier in small samples (N=5), which matters here because Whisper's
    per-line jitter routinely produces one ~2x-outlier line in an
    otherwise tight 5-line cluster (see `_MULTILINE_MAX_MAD_S` docstring
    for the Parcels - Overnight empirical data driving this choice).
    """
    return statistics.median(abs(v - median_value) for v in values)


def _maybe_reanchor_to_lrc(
    *,
    aligned_lines: list[AlignedLine],
    anchor_lines: Sequence[SyncedLine],
    track_end_s: float | None,
    whisper_words: list[WhisperWord],
    matched_counts: list[int],
    local_anchor_adjusted: list[bool] | None = None,
) -> list[AlignedLine]:
    """Decide whether to rewrite line bounds to `LRC_anchor[i] + shift`.

    Three paths, evaluated in order:

      0. **Linear drift fit (progressive cut drift)**: when enough
         matched lines are spread across the track and their shifts fit a
         low-residual line, apply `intercept + slope * LRC_anchor_time`.
         Catches official-video cuts whose audio slowly diverges from the
         LRCLIB-indexed recording.

      1. **Multi-line median (sub-second uniform drift)**: when at least
         `_MULTILINE_MIN_ELIGIBLE_LINES` aligned lines have real Whisper
         matches AND their per-line shifts agree (low spread) AND the
         median shift is meaningfully non-zero, apply the median.
         Catches the Overnight + The Bay class: small consistent drift
         that single-L0 can't safely detect because L0 noise on clean
         tracks (~100-300ms) sits in the same range.

      2. **Single-L0 (large-cut drift)**: when the earlier paths don't
         qualify, fall back to the original L0-shift logic. Catches the Instant
         Crush class: large shift (>1s) where even noisy L0 detection
         is unambiguous, or short tracks where only 1-2 lines aligned
         cleanly.

      3. **No shift**: both paths skip, return unchanged.

    Per-word `AlignedWord` timings are NEVER modified — karaoke `\\kf` and
    per-word-pop consume per-word values and stay byte-identical
    regardless of which (if any) path fires.

    Args:
        matched_counts: parallel to `aligned_lines`; counts real Whisper
            word matches per line. Used to filter Strategy 3 (pure
            interpolation, shift=0 by construction) lines out of the
            linear and multi-line eligible sets.
        local_anchor_adjusted: parallel to `aligned_lines`; marks lines
            whose start was repaired with pre-anchor prefix lookback. These
            local corrections are excluded from global drift estimation.
            Defaults to all-False for legacy direct test calls.
    """
    if not aligned_lines or not anchor_lines:
        return aligned_lines

    # Parallel-list invariant — `align_with_line_anchors` is the only caller
    # and always builds these together. Hard assert so any future caller
    # that forgets the list gets a clear error rather than a silent
    # mis-alignment downstream.
    assert len(aligned_lines) == len(matched_counts), (
        f"matched_counts length ({len(matched_counts)}) must match "
        f"aligned_lines length ({len(aligned_lines)})"
    )
    if local_anchor_adjusted is None:
        local_anchor_adjusted = [False] * len(aligned_lines)
    assert len(aligned_lines) == len(local_anchor_adjusted), (
        f"local_anchor_adjusted length ({len(local_anchor_adjusted)}) must match "
        f"aligned_lines length ({len(aligned_lines)})"
    )

    # Map each aligned line back to its source anchor by ordinal position.
    # `align_with_line_anchors` iterates `anchor_lines` in order and appends
    # one `AlignedLine` per anchor it successfully resolved. Some anchors
    # may have been skipped (empty text, malformed window), so the i-th
    # mapping only holds when the lengths match. Bail rather than
    # mis-align.
    if len(aligned_lines) != len(anchor_lines):
        # Use the `_multiline_skipped` event-name prefix even though the
        # length-mismatch bail blocks BOTH paths (multi-line + single-L0),
        # so a telemetry filter on `*_multiline_*` catches all skip
        # reasons consistently. The single-L0 path's own implausible-shift
        # bail (line ~755) keeps the old `_reanchor_skipped` name for
        # backward compat with #363 dashboards.
        log.info(
            "lyrics_alignment_reanchor_multiline_skipped",
            reason="anchor_alignment_length_mismatch",
            anchor_count=len(anchor_lines),
            aligned_count=len(aligned_lines),
        )
        return aligned_lines

    track_dur = (
        track_end_s
        if track_end_s is not None
        else (whisper_words[-1].end_s if whisper_words else 0.0)
    )

    # ── Path 0: linear fit for progressively drifting cuts ─────────────
    eligible_indices = [
        i
        for i, mc in enumerate(matched_counts)
        if mc >= _MULTILINE_MATCHED_COUNT_THRESHOLD and not local_anchor_adjusted[i]
    ]
    linear_diag: dict[str, object] = {
        "aligned_count": len(aligned_lines),
        "eligible_count": len(eligible_indices),
        "eligible_threshold": _MULTILINE_MATCHED_COUNT_THRESHOLD,
        "matched_counts": list(matched_counts),
        "local_anchor_adjusted": list(local_anchor_adjusted),
        "enabled": settings.lyric_linear_reanchor_enabled,
    }
    if settings.lyric_linear_reanchor_enabled:
        linear_result = _fit_linear_reanchor(
            aligned_lines=aligned_lines,
            anchor_lines=anchor_lines,
            eligible_indices=eligible_indices,
            track_dur=track_dur,
        )
        linear_diag.update(linear_result.diag)
        if linear_result.applied:
            log.info(
                "lyrics_alignment_reanchor_linear_applied",
                path="linear",
                **linear_diag,
            )
            return _apply_shift(
                aligned_lines=aligned_lines,
                anchor_lines=anchor_lines,
                shift_at=linear_result.shift_at,
                track_end_s=track_end_s,
            )
        log.info(
            "lyrics_alignment_reanchor_linear_skipped",
            reason=linear_result.reason,
            **linear_diag,
        )
    else:
        log.info(
            "lyrics_alignment_reanchor_linear_skipped",
            reason="disabled_by_flag",
            **linear_diag,
        )

    # ── Path 1: multi-line median ──────────────────────────────────────
    multiline_diag: dict[str, object] = {
        "aligned_count": len(aligned_lines),
        "eligible_count": len(eligible_indices),
        "eligible_threshold": _MULTILINE_MATCHED_COUNT_THRESHOLD,
        "matched_counts": list(matched_counts),
        "local_anchor_adjusted": list(local_anchor_adjusted),
    }

    if len(eligible_indices) >= _MULTILINE_MIN_ELIGIBLE_LINES:
        sample_indices = eligible_indices[:_MULTILINE_SAMPLE_SIZE]
        shifts = [aligned_lines[i].start_s - anchor_lines[i].start_s for i in sample_indices]
        median_shift = statistics.median(shifts)
        spread_mad = _mad(shifts, median_shift)

        # Inlier filter — keep shifts within `_MULTILINE_INLIER_K * MAD`
        # of the median. Refined median is computed only over the
        # inlier set so a high-Whisper-jitter line doesn't pull the
        # applied shift away from the consensus cluster.
        #
        # Band-half floored at 1ms: when shifts are nominally identical
        # (e.g., perfectly aligned synthetic data, or a track whose
        # Whisper happens to produce bit-equal offsets), MAD computes
        # to 0 and IEEE 754 imprecision can put two "equal" shifts
        # ~1e-15 apart — without the floor, one falls outside the
        # zero-width band and gets dropped. 1ms is below any real
        # signal (Whisper word timings round to ms precision in the
        # cache) and safely above float noise.
        band_half = max(_MULTILINE_INLIER_K * spread_mad, 1e-3)
        inlier_shifts = [s for s in shifts if abs(s - median_shift) <= band_half]
        refined_median = statistics.median(inlier_shifts) if inlier_shifts else median_shift

        multiline_diag.update(
            {
                "sample_indices": list(sample_indices),
                "per_line_shifts": [round(s, 3) for s in shifts],
                "median_shift_s": round(median_shift, 3),
                "spread_mad_s": round(spread_mad, 3),
                "inlier_count": len(inlier_shifts),
                "inlier_shifts": [round(s, 3) for s in inlier_shifts],
                "refined_median_s": round(refined_median, 3),
                "min_apply_shift_s": _MULTILINE_MIN_APPLY_SHIFT_S,
                "max_mad_s": _MULTILINE_MAX_MAD_S,
                "inlier_k": _MULTILINE_INLIER_K,
                "min_inliers": _MULTILINE_MIN_INLIERS,
            }
        )

        # Implausible-shift guard: same defense as the single-L0 path so
        # an outlier in the median can't drive a garbage shift to every
        # line. Check against the refined median (post-inlier-filter) so
        # a single Whisper hallucination on L0 doesn't trigger the bail
        # when the cluster's true shift is small.
        if track_dur > 0 and abs(refined_median) > track_dur / 3.0:
            log.warning(
                "lyrics_alignment_reanchor_multiline_skipped",
                reason="implausible_shift",
                track_dur_s=round(track_dur, 3),
                **multiline_diag,
            )
            # Don't fall through to single-L0 — if the multi-line refined
            # median is implausible, the L0 shift will be too (L0 is a
            # subset of the multi-line sample).
            return aligned_lines

        # Three-gate qualification. ALL must pass to apply the refined
        # median; failing any one falls through to single-L0.
        #
        # Gate 1: MAD cap — rejects scattered samples (non-uniform drift
        #   class), where the spread itself disqualifies the "uniform
        #   shift" hypothesis.
        # Gate 2: inlier consensus — at least N of 5 sample shifts must
        #   cluster within ±k*MAD of the median. Rejects samples that
        #   pass MAD by accident (e.g., 2 tight pairs + 1 outlier sum
        #   to a small MAD but lack consensus).
        # Gate 3: refined median magnitude — only apply if the consensus
        #   cluster's median is meaningfully non-zero. Clean tracks
        #   produce small refined medians and skip here.
        mad_ok = spread_mad < _MULTILINE_MAX_MAD_S
        inliers_ok = len(inlier_shifts) >= _MULTILINE_MIN_INLIERS
        magnitude_ok = abs(refined_median) > _MULTILINE_MIN_APPLY_SHIFT_S

        if mad_ok and inliers_ok and magnitude_ok:
            log.info(
                "lyrics_alignment_reanchor_multiline_applied",
                path="multi_line",
                **multiline_diag,
            )
            return _apply_uniform_shift(
                aligned_lines=aligned_lines,
                anchor_lines=anchor_lines,
                shift_s=refined_median,
                track_end_s=track_end_s,
            )

        # Multi-line guard didn't qualify — record why and fall through to
        # single-L0. Precedence (most-specific first): magnitude > MAD >
        # inliers, so the reason field gives the operator the most
        # actionable diagnostic.
        if not magnitude_ok:
            multiline_skip_reason = "median_too_small"
        elif not mad_ok:
            multiline_skip_reason = "spread_too_wide"
        else:
            multiline_skip_reason = "insufficient_inlier_consensus"
        log.info(
            "lyrics_alignment_reanchor_multiline_skipped",
            reason=multiline_skip_reason,
            **multiline_diag,
        )
    else:
        log.info(
            "lyrics_alignment_reanchor_multiline_skipped",
            reason="insufficient_eligible_lines",
            **multiline_diag,
        )

    # ── Path 2: single-L0 (unchanged from PR #363) ─────────────────────
    first_aligned_start = aligned_lines[0].start_s
    first_anchor_start = anchor_lines[0].start_s
    audio_shift_s = first_aligned_start - first_anchor_start

    if local_anchor_adjusted[0]:
        log.info(
            "lyrics_alignment_reanchor_no_shift",
            path="none",
            reason="first_line_local_anchor_adjusted",
            audio_shift_s=round(audio_shift_s, 3),
            threshold_s=_AUDIO_SHIFT_THRESHOLD_S,
        )
        return aligned_lines

    if abs(audio_shift_s) <= _AUDIO_SHIFT_THRESHOLD_S:
        log.info(
            "lyrics_alignment_reanchor_no_shift",
            path="none",
            audio_shift_s=round(audio_shift_s, 3),
            threshold_s=_AUDIO_SHIFT_THRESHOLD_S,
        )
        return aligned_lines

    if track_dur > 0 and abs(audio_shift_s) > track_dur / 3.0:
        log.warning(
            "lyrics_alignment_reanchor_skipped",
            reason="implausible_shift",
            audio_shift_s=round(audio_shift_s, 3),
            track_dur_s=round(track_dur, 3),
        )
        return aligned_lines

    log.info(
        "lyrics_alignment_reanchor_single_l0_applied",
        path="single_l0",
        audio_shift_s=round(audio_shift_s, 3),
        anchor_count=len(anchor_lines),
        first_anchor_s=round(first_anchor_start, 3),
        first_aligned_s=round(first_aligned_start, 3),
    )
    return _apply_uniform_shift(
        aligned_lines=aligned_lines,
        anchor_lines=anchor_lines,
        shift_s=audio_shift_s,
        track_end_s=track_end_s,
    )


@dataclass(frozen=True, slots=True)
class _LinearReanchorResult:
    applied: bool
    reason: str
    diag: dict[str, object]
    shift_at: Callable[[float], float]


def _fit_linear_reanchor(
    *,
    aligned_lines: list[AlignedLine],
    anchor_lines: Sequence[SyncedLine],
    eligible_indices: list[int],
    track_dur: float,
) -> _LinearReanchorResult:
    """Fit a robust per-anchor shift curve and decide whether to apply it."""

    def _constant_zero(_t: float) -> float:
        return 0.0

    diag: dict[str, object] = {
        "min_eligible": _LINEAR_MIN_ELIGIBLE_LINES,
        "min_span_frac": _LINEAR_MIN_SPAN_FRAC,
        "min_slope": _LINEAR_MIN_SLOPE,
        "max_resid_mad_s": _LINEAR_MAX_RESID_MAD_S,
    }
    if len(eligible_indices) < _LINEAR_MIN_ELIGIBLE_LINES:
        return _LinearReanchorResult(False, "insufficient_eligible_lines", diag, _constant_zero)
    if track_dur <= 0:
        return _LinearReanchorResult(False, "missing_track_duration", diag, _constant_zero)

    points = [
        (anchor_lines[i].start_s, aligned_lines[i].start_s - anchor_lines[i].start_s)
        for i in eligible_indices
    ]
    xs = [p[0] for p in points]
    shifts = [p[1] for p in points]
    x_min = min(xs)
    x_max = max(xs)
    x_span = x_max - x_min
    x_span_frac = x_span / track_dur
    diag.update(
        {
            "eligible_indices": list(eligible_indices),
            "x_span_s": round(x_span, 3),
            "x_span_frac": round(x_span_frac, 4),
            "per_line_shifts": [round(s, 3) for s in shifts],
        }
    )
    if x_span_frac < _LINEAR_MIN_SPAN_FRAC:
        return _LinearReanchorResult(False, "span_too_small", diag, _constant_zero)

    slopes: list[float] = []
    for i, (x_i, shift_i) in enumerate(points):
        for x_j, shift_j in points[i + 1 :]:
            dx = x_j - x_i
            if abs(dx) <= _LINEAR_MIN_X_DELTA_S:
                continue
            slopes.append((shift_j - shift_i) / dx)
    if not slopes:
        return _LinearReanchorResult(False, "no_valid_slope_pairs", diag, _constant_zero)

    slope = statistics.median(slopes)
    intercept = statistics.median(shift - slope * x for x, shift in points)
    residuals = [shift - (intercept + slope * x) for x, shift in points]
    residual_median = statistics.median(residuals)
    resid_mad = _mad(residuals, residual_median)
    pred_min = intercept + slope * x_min
    pred_max = intercept + slope * x_max
    diag.update(
        {
            "slope": round(slope, 6),
            "intercept": round(intercept, 3),
            "resid_mad_s": round(resid_mad, 3),
            "pred_shift_min_s": round(pred_min, 3),
            "pred_shift_max_s": round(pred_max, 3),
        }
    )

    if abs(slope) <= _LINEAR_MIN_SLOPE:
        return _LinearReanchorResult(False, "slope_too_small", diag, _constant_zero)
    if resid_mad >= _LINEAR_MAX_RESID_MAD_S:
        return _LinearReanchorResult(False, "residual_spread_too_wide", diag, _constant_zero)
    if max(abs(pred_min), abs(pred_max)) > track_dur / 3.0:
        return _LinearReanchorResult(False, "implausible_endpoint_shift", diag, _constant_zero)

    def _shift_at(t: float) -> float:
        return intercept + slope * t

    return _LinearReanchorResult(True, "applied", diag, _shift_at)


def _apply_uniform_shift(
    *,
    aligned_lines: list[AlignedLine],
    anchor_lines: Sequence[SyncedLine],
    shift_s: float,
    track_end_s: float | None,
) -> list[AlignedLine]:
    """Rewrite every line's `start_s` / `end_s` to `LRC_anchor[i] + shift_s`.

    Uniform wrapper around `_apply_shift`, used by the multi-line median and
    single-L0 fallback paths.
    """

    return _apply_shift(
        aligned_lines=aligned_lines,
        anchor_lines=anchor_lines,
        shift_at=lambda _t: shift_s,
        track_end_s=track_end_s,
    )


def _apply_shift(
    *,
    aligned_lines: list[AlignedLine],
    anchor_lines: Sequence[SyncedLine],
    shift_at: Callable[[float], float],
    track_end_s: float | None,
) -> list[AlignedLine]:
    """Rewrite line bounds to `LRC_anchor[i] + shift_at(anchor_time)`.

    Per-word `AlignedWord` timings are NOT modified (the karaoke contract).
    Caller is responsible for ensuring `len(aligned_lines) == len(anchor_lines)`.
    """
    from dataclasses import replace  # noqa: PLC0415

    rebuilt: list[AlignedLine] = []
    for i, line in enumerate(aligned_lines):
        anchor = anchor_lines[i]
        new_start = anchor.start_s + shift_at(anchor.start_s)
        if i + 1 < len(anchor_lines):
            next_anchor = anchor_lines[i + 1]
            new_end = (
                next_anchor.start_s + shift_at(next_anchor.start_s) - _REANCHOR_NEXT_LINE_SAFETY_S
            )
        else:
            # Final line — no next anchor. Use whisper's last-word end (when
            # available) which is the most reliable signal we have for the
            # actual sung tail of the last line. Floor at
            # `_REANCHOR_LAST_LINE_MIN_DUR_S` past `new_start` so we render
            # the line for at least a readable minimum even when whisper
            # missed the tail. Cap at `track_end_s` so we never extend past
            # the audio file's actual duration.
            if line.words:
                whisper_last_end = max(w.end_s for w in line.words)
            else:
                whisper_last_end = new_start + _FALLBACK_TRAILING_WINDOW_S
            new_end = max(
                whisper_last_end + _LAST_WORD_TAIL_PAD_S,
                new_start + _REANCHOR_LAST_LINE_MIN_DUR_S,
            )
            if track_end_s is not None:
                new_end = min(new_end, track_end_s)

        # Guard: re-anchor never shrinks the line below its own audible
        # words — if Whisper found a real word past the new_end, extend to
        # cover it (otherwise per-word karaoke highlights would render past
        # the line's nominal end_s and the line-style window would clip).
        #
        # CAP the extension at `next_anchor + shift` (or track_end_s for
        # the last line) so a TRAILING-COLLAPSE interpolated word doesn't
        # push the line bound past the next line's actual vocal. The L2
        # case on Instant Crush demonstrates this: `_build_line` trailing
        # interpolation placed 6 unmatched canonical words past the LRC
        # window end (prev_end 39.64 with no usable cap), producing word
        # timings up to 42.04 that should NOT extend L2 past LRC's L3
        # anchor + shift.
        if line.words:
            words_max_end = max(w.end_s for w in line.words)
            if words_max_end > new_end:
                if i + 1 < len(anchor_lines):
                    hard_cap_end = (
                        anchor_lines[i + 1].start_s
                        + shift_at(anchor_lines[i + 1].start_s)
                        - _REANCHOR_NEXT_LINE_SAFETY_S
                    )
                elif track_end_s is not None:
                    hard_cap_end = track_end_s
                else:
                    hard_cap_end = words_max_end
                new_end = max(new_end, min(words_max_end, hard_cap_end))

        rebuilt.append(
            replace(
                line,
                start_s=round(new_start, 3),
                end_s=round(new_end, 3),
            )
        )
    return rebuilt


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
        words = _repair_collapsed_word_runs(
            tuple(
                AlignedWord(
                    text=expected_words[k],
                    start_s=round(whisper_words_in_window[k].start_s, 3),
                    end_s=round(whisper_words_in_window[k].end_s, 3),
                )
                for k in range(len(expected_words))
            ),
            line=anchor_text,
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
        matched_indices: list[int | None] = []
        cursor = 0
        matched_count = 0
        for canonical_word in expected_words:
            match_idx = _find_match(canonical_word, whisper_words_in_window, cursor)
            if match_idx is None:
                slots.append((canonical_word, None, None))
                matched_indices.append(None)
            else:
                ww = whisper_words_in_window[match_idx]
                slots.append((canonical_word, ww.start_s, ww.end_s))
                matched_indices.append(match_idx)
                matched_count += 1
                cursor = match_idx + 1
        repaired = _maybe_repair_low_confidence_whisper_tail(
            anchor_text,
            slots,
            matched_indices,
            whisper_words_in_window,
            window_end,
            matched_count,
        )
        if repaired is not None:
            return repaired
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


def _maybe_repair_low_confidence_whisper_tail(
    anchor_text: str,
    slots: list[tuple[str, float | None, float | None]],
    matched_indices: list[int | None],
    whisper_words_in_window: list[WhisperWord],
    window_end: float,
    matched_count: int,
) -> tuple[AlignedLine, int] | None:
    """Replace a bad canonical tail with unused Whisper words.

    This targets repeated-hook rows where LRCLIB's synced text diverges from
    the actual recording: the line prefix matches, then canonical tail words
    fail while Whisper has a coherent unused phrase in the same window.
    """
    expected_count = len(slots)
    if expected_count < 4 or matched_count <= 0:
        return None
    if matched_count / expected_count > _LOW_CONFIDENCE_WHISPER_TAIL_MAX_MATCH_RATIO:
        return None

    first_matched_slot_idx = next(
        (idx for idx, match_idx in enumerate(matched_indices) if match_idx is not None),
        None,
    )
    if first_matched_slot_idx is None:
        return None
    if first_matched_slot_idx > _LOW_CONFIDENCE_WHISPER_TAIL_MAX_PREFIX_MISSES:
        return None
    first_matched_whisper_idx = matched_indices[first_matched_slot_idx]
    if first_matched_whisper_idx is None:
        return None
    first_matched_word = slots[first_matched_slot_idx][0]
    first_matched_whisper_word = whisper_words_in_window[first_matched_whisper_idx].text
    if (
        _similarity(_normalize(first_matched_word), _normalize(first_matched_whisper_word))
        < _LOW_CONFIDENCE_WHISPER_TAIL_MIN_PREFIX_SIMILARITY
    ):
        return None

    later_matched = [
        idx
        for idx in matched_indices[first_matched_slot_idx + 1 :]
        if idx is not None and idx > first_matched_whisper_idx
    ]
    tail_stop_idx = min(later_matched) if later_matched else len(whisper_words_in_window)
    matched_set = {idx for idx in matched_indices if idx is not None}
    whisper_tail = [
        word
        for idx, word in enumerate(whisper_words_in_window)
        if first_matched_whisper_idx < idx < tail_stop_idx and idx not in matched_set
    ]
    if len(whisper_tail) < _LOW_CONFIDENCE_WHISPER_TAIL_MIN_UNUSED_WORDS:
        return None
    if window_end - whisper_tail[-1].end_s > _LOW_CONFIDENCE_WHISPER_TAIL_MAX_END_GAP_S:
        return None

    canonical_tail = [word for word, _, _ in slots[first_matched_slot_idx + 1 :]]
    if _word_sequence_similarity(canonical_tail, [word.text for word in whisper_tail]) >= (
        _LOW_CONFIDENCE_WHISPER_TAIL_MAX_SIMILARITY
    ):
        return None

    hybrid_slots = [
        *slots[: first_matched_slot_idx + 1],
        *((word.text, word.start_s, word.end_s) for word in whisper_tail),
    ]
    hybrid_text = " ".join(word for word, _, _ in hybrid_slots)
    line = _build_line(hybrid_text, hybrid_slots, tail_end_cap_s=window_end)
    if line is None:
        return None

    log.warning(
        "lyrics_alignment_low_confidence_whisper_tail_repaired",
        original_line=anchor_text.strip()[:80],
        repaired_line=hybrid_text[:80],
        matched_count=matched_count,
        expected_count=expected_count,
        whisper_tail=[word.text for word in whisper_tail],
    )
    hybrid_matched_count = sum(
        1 for _, start_s, end_s in hybrid_slots if start_s is not None and end_s is not None
    )
    return line, hybrid_matched_count


def _word_sequence_similarity(left: list[str], right: list[str]) -> float:
    left_norm = " ".join(_normalize(word) for word in left if _normalize(word))
    right_norm = " ".join(_normalize(word) for word in right if _normalize(word))
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()
