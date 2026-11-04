"""Tight-pause cuts: extra silence removals layered on a consented speech-cleanup plan.

Speech cleanup's pause rule only tightens word gaps of 0.6 s or more. A creator who talks
fast and asks Kria to "cut my pauses so it's tight" wants the 0.4-0.6 s gaps gone too. This
module adds those cuts at render time, ON TOP of an already-consented ``CutPlan``:

- it never moves, shrinks or removes an existing removal;
- it only cuts silence between two kept words that sit in the same keep segment, leaving
  ``PAD_S`` of breathing room on each word (the margin today's pause rule keeps);
- every new cut goes through the KRI-1017 voice guard, and a fault in silence detection or
  audio decoding means no new cuts at all (never cut without having heard the audio);
- a kept speech fragment shorter than ``MIN_KEEP_SPEECH_SEGMENT_S`` never touches a new cut.

Pure given decoded samples; ``apply_tight_pauses`` is the I/O wrapper.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from app.pipeline import audio_pcm
from app.pipeline.silence_cut import (
    MAX_REMOVALS,
    MIN_KEEP_SEGMENT_S,
    MIN_KEEP_SPEECH_SEGMENT_S,
    PAD_S,
    REASON_SILENCE,
    CutPlan,
    Removal,
)
from app.pipeline.speech_voice_guard import (
    FRAME_S,
    SAMPLE_RATE,
    _field,
    _merge,
    _subtract,
    guard_audible_speech,
    quiet_floor_db,
)

TIGHT_MIN_CUT_S = 0.12  # smallest extra cut worth making
TIGHT_PAUSE_TARGET_S = 0.3  # what a tight cut aims to leave (2 x PAD_S = 0.24 by word stamps)
TIGHT_PAUSE_LIMIT_S = 0.35  # a kept pause longer than this still counts as "long"
TIGHT_REPORT_VERSION = 1
SILENCE_MIN_S = 0.1  # same min_silence_s the preflight analysis uses
# Quiet check (KRI-969 safety): audio this far above the take's quiet floor is sound, not
# silence, whatever Kria's absolute -30 dB spans say. A soft word ending at -38 dBFS in a
# -58 dBFS room is 20 dB up and is protected.
TIGHT_QUIET_ABOVE_FLOOR_DB = 15.0
# On a noisy take "silence" is ambient noise and soft speech hides in it (KRI-1017): no cuts.
TIGHT_MAX_FLOOR_DBFS = -45.0
# Inside a word's own stamp the loud bar is lower than elsewhere: today's cleanup never cuts
# inside a stamp, so a soft unvoiced ending/onset (+12 dB) there stays protected. Room tone
# flickers up to about +8 dB in 20 ms frames, so the bar sits above that.
TIGHT_QUIET_IN_STAMP_ABOVE_FLOOR_DB = 10.0
# ... but only next to the word's own audible core: a stamp stretched far back into a pause
# is not a soft onset 0.7 s away from the sound.
TIGHT_SOFT_ONSET_S = 0.2
TIGHT_SOFT_ENDING_S = 0.3
# Each keep segment is one clip on the phone recipe's video track (max 100). Leave headroom
# for the named / opening / explicit cuts that come after this module.
TIGHT_MAX_KEEP_SEGMENTS = 85
_LOUD_WIN_S = 0.02
_LOUD_HOP_S = 0.01
_SLIVER_MIN_S = 0.01
# The acceptance tool's definition of a pause (not Kria's -30 dB spans).
_MEASURE_FILTER = "highpass=f=90,silencedetect=n=-38dB:d=0.12"

_EPS = 1e-9
_TOUCH_S = 1e-6
_JOIN_S = 1e-3
# silencedetect spans run a few ms into a word's fade; a 0.35 s gap measures 0.352 s
_MEASURE_TOL_S = 0.005


@dataclass(frozen=True)
class TightPauseReport:
    status: Literal["applied", "no_change", "unavailable"]
    added_cuts: int
    added_removed_s: float
    longest_pause_s: float | None
    longest_pause_before_s: float | None
    pauses_over_limit: int | None
    guard_dropped: int
    guard_trimmed: int
    capped: bool
    reason: str | None

    def to_variant(self) -> dict[str, Any]:
        def two(v: float | None) -> float | None:
            return None if v is None else round(float(v), 2)

        return {
            "version": TIGHT_REPORT_VERSION,
            "status": self.status,
            "target_s": TIGHT_PAUSE_TARGET_S,
            "limit_s": TIGHT_PAUSE_LIMIT_S,
            "added_cuts": int(self.added_cuts),
            "added_removed_ms": int(round(self.added_removed_s * 1000)),
            "longest_pause_s": two(self.longest_pause_s),
            "longest_pause_before_s": two(self.longest_pause_before_s),
            "pauses_over_limit": self.pauses_over_limit,
            "guard_dropped": int(self.guard_dropped),
            "guard_trimmed": int(self.guard_trimmed),
            "capped": bool(self.capped),
            "reason": self.reason,
        }


def _normalize_words(words: Sequence[Any]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for raw in words or ():
        text = _field(raw, "text", "word")
        if not str(text or "").strip():
            continue
        start = float(_field(raw, "start_s", "start") or 0.0)
        end = float(_field(raw, "end_s", "end") or 0.0)
        out.append((start, max(start, end)))
    out.sort()
    return out


def _complement(removed: Sequence[Removal], duration_s: float) -> list[tuple[float, float]]:
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for r in sorted(removed, key=lambda r: (r.start_s, r.end_s)):
        if r.start_s - cursor > _EPS:
            segments.append((cursor, r.start_s))
        cursor = max(cursor, r.end_s)
    if duration_s - cursor > _EPS:
        segments.append((cursor, duration_s))
    return segments


def _word_kept(word: tuple[float, float], keep: Sequence[tuple[float, float]]) -> bool:
    start, end = word
    if end - start <= _EPS:
        return any(lo - _EPS <= start < hi + _EPS for lo, hi in keep)
    inside = sum(max(0.0, min(end, hi) - max(start, lo)) for lo, hi in keep)
    return inside > (end - start) / 2


def _segment_index(t: float, keep: Sequence[tuple[float, float]]) -> int | None:
    for i, (lo, hi) in enumerate(keep):
        if lo - _TOUCH_S <= t <= hi + _TOUCH_S:
            return i
    return None


def _loud_runs(
    samples: np.ndarray, sr: int, floor_db: float, above_db: float = TIGHT_QUIET_ABOVE_FLOOR_DB
) -> list[tuple[float, float]]:
    """Spans where 20 ms RMS frames sit more than ``TIGHT_QUIET_ABOVE_FLOOR_DB`` over the floor."""

    win, hop = int(sr * _LOUD_WIN_S), int(sr * _LOUD_HOP_S)
    if len(samples) < win:
        return []
    x = samples.astype(np.float64)
    csum = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(0, len(x) - win + 1, hop)
    level = 10.0 * np.log10((csum[starts + win] - csum[starts]) / win + 1e-12)
    loud = level > floor_db + above_db
    runs: list[tuple[float, float]] = []
    first: int | None = None
    for i, flag in enumerate([*loud.tolist(), False]):
        if flag and first is None:
            first = i
        elif not flag and first is not None:
            runs.append((starts[first] / sr, (starts[i - 1] + win) / sr))
            first = None
    return _merge(runs)


def _soft_zones(
    stamps: list[tuple[float, float]], silences: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Strict-bar zones: a soft onset / ending beside each stamp's audible core."""

    zones: list[tuple[float, float]] = []
    for ws, we in stamps:
        core = [p for p in _subtract(ws, we, silences) if p[1] - p[0] > _SLIVER_MIN_S]
        if core:
            zones.append((max(ws, core[0][0] - TIGHT_SOFT_ONSET_S), core[0][0]))
            zones.append((core[-1][1], min(we, core[-1][1] + TIGHT_SOFT_ENDING_S)))
    return _merge(zones)


def _protected_spans(
    all_words: list[tuple[float, float]],
    silences: list[tuple[float, float]],
    loud: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for ws, we in all_words:
        core = [p for p in _subtract(ws, we, silences) if p[1] - p[0] > _SLIVER_MIN_S]
        for lo, hi in core or [(ws, we)]:
            spans.append((lo - PAD_S, hi + PAD_S))
    spans.extend((lo - PAD_S, hi + PAD_S) for lo, hi in loud)
    return _merge(spans)


def _candidates(
    kept: list[tuple[float, float]],
    keep: Sequence[tuple[float, float]],
    consented: Sequence[Removal],
    silences: list[tuple[float, float]],
    all_words: list[tuple[float, float]],
    loud: list[tuple[float, float]],
) -> list[Removal]:
    """Cuts inside Kria silence spans that lie within the talk, clear of audible sound."""

    if not kept:
        return []
    talk_lo, talk_hi = kept[0][0], max(w[1] for w in kept)
    protected = _protected_spans(all_words, silences, loud)
    edges = [r.start_s for r in consented] + [r.end_s for r in consented]
    out: list[Removal] = []
    for s0, s1 in silences:
        if not (s0 > talk_lo and s1 < talk_hi):
            continue
        lo, hi = s0 + PAD_S, s1 - PAD_S
        if hi - lo <= _SLIVER_MIN_S:
            continue
        for k0, k1 in keep:
            seg = (max(lo, k0), min(hi, k1))
            if seg[1] - seg[0] <= _SLIVER_MIN_S:
                continue
            pieces = [
                p
                for p in _subtract(seg[0], seg[1], protected)
                if p[1] - p[0] >= TIGHT_MIN_CUT_S - _EPS
                or (
                    p[1] - p[0] > _SLIVER_MIN_S
                    and any(abs(p[0] - e) <= _TOUCH_S or abs(p[1] - e) <= _TOUCH_S for e in edges)
                )
            ]
            changed = True
            while changed and len(pieces) > 1:
                changed = False
                for i in range(len(pieces) - 1):
                    if pieces[i + 1][0] - pieces[i][1] < MIN_KEEP_SEGMENT_S - _EPS:
                        drop = (
                            i + 1
                            if pieces[i][1] - pieces[i][0] >= pieces[i + 1][1] - pieces[i + 1][0]
                            else i
                        )
                        del pieces[drop]
                        changed = True
                        break
            out.extend(Removal(start_s=p0, end_s=p1, reason=REASON_SILENCE) for p0, p1 in pieces)
    return out


_SILENCE_RE = re.compile(r"silence_(start|end): (-?[0-9.]+)")


def measure_silence_spans(
    media_path: str, *, duration_s: float | None = None
) -> list[tuple[float, float]] | None:
    """Pauses by the acceptance tool's definition, or None when ffmpeg fails."""

    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", media_path, "-vn", "-af"]
            + [_MEASURE_FILTER, "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    spans: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value in _SILENCE_RE.findall(proc.stderr):
        if kind == "start":
            start = max(0.0, float(value))
        elif start is not None:
            spans.append((start, float(value)))
            start = None
    if start is not None and duration_s is not None and duration_s > start:
        spans.append((start, duration_s))
    return spans


def _enforce_fragments(
    consented: list[Removal],
    new: list[Removal],
    kept: list[tuple[float, float]],
    all_words: list[tuple[float, float]],
    duration_s: float,
) -> list[Removal]:
    new = list(new)
    while new:
        keep = _complement([*consented, *new], duration_s)
        victim: Removal | None = None
        for lo, hi in keep:
            if hi - lo >= MIN_KEEP_SPEECH_SEGMENT_S - _EPS:
                continue
            adjacent = [
                r for r in new if abs(r.end_s - lo) <= _TOUCH_S or abs(r.start_s - hi) <= _TOUCH_S
            ]
            if adjacent:
                victim = min(adjacent, key=lambda r: (r.end_s - r.start_s, -r.start_s))
                break
        if victim is None:
            break
        new.remove(victim)
    return new


def kept_pause_stats(
    plan: CutPlan,
    words: Sequence[Any],
    silences: Sequence[tuple[float, float]],
    duration_s: float,
) -> tuple[float, int]:
    """(longest kept pause in seconds, count of kept pauses over ``TIGHT_PAUSE_LIMIT_S``)."""

    normalized = _normalize_words(words)
    keep = [tuple(s) for s in plan.keep_segments]
    kept = [w for w in normalized if _word_kept(w, keep)]
    if len(kept) < 2:
        return 0.0, 0
    removed = _merge([(r.start_s, r.end_s) for r in plan.removed])

    def out_t(t: float) -> float:
        return t - sum(max(0.0, min(t, hi) - lo) for lo, hi in removed)

    talk_lo, talk_hi = out_t(kept[0][0]), out_t(max(w[1] for w in kept))
    pieces: list[tuple[float, float]] = []
    for s0, s1 in _merge(silences):
        cursor = s0
        for r0, r1 in [*removed, (s1 + 1.0, s1 + 2.0)]:
            if r1 <= cursor:
                continue
            if r0 >= s1:
                if s1 > cursor:
                    pieces.append((out_t(cursor), out_t(s1)))
                break
            if r0 > cursor:
                pieces.append((out_t(cursor), out_t(r0)))
            cursor = max(cursor, r1)
    clipped = [
        (max(lo, talk_lo), min(hi, talk_hi))
        for lo, hi in sorted(pieces)
        if min(hi, talk_hi) - max(lo, talk_lo) > _EPS
    ]
    merged: list[tuple[float, float]] = []
    for lo, hi in clipped:
        if merged and lo <= merged[-1][1] + _JOIN_S:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    lengths = [hi - lo for lo, hi in merged]
    return (
        max(lengths, default=0.0),
        sum(1 for x in lengths if x > TIGHT_PAUSE_LIMIT_S + _MEASURE_TOL_S),
    )


def tighten_pauses(
    plan: CutPlan,
    words: Sequence[Any],
    silences: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    samples: np.ndarray | None,
    sr: int = SAMPLE_RATE,
    measure_silences: Sequence[tuple[float, float]] | None = None,
    measurable: bool = True,
) -> tuple[CutPlan, TightPauseReport]:
    normalized = _normalize_words(words)
    spans = _merge([(float(a), float(b)) for a, b in silences])
    measure = spans if measure_silences is None else _merge(list(measure_silences))
    keep = [tuple(s) for s in plan.keep_segments]
    kept = [w for w in normalized if _word_kept(w, keep)]

    audio_ok = samples is not None and len(samples) >= int(sr * FRAME_S)
    if audio_ok:
        floor_db = quiet_floor_db(samples, sr)
        if floor_db > TIGHT_MAX_FLOOR_DBFS:
            return plan, TightPauseReport(
                "no_change", 0, 0.0, None, None, None, 0, 0, False, "noisy_take"
            )
        loud = _loud_runs(samples, sr, floor_db)
        stamps = _soft_zones(normalized, spans)
        in_stamp = _loud_runs(samples, sr, floor_db, TIGHT_QUIET_IN_STAMP_ABOVE_FLOOR_DB)
        loud = _merge(
            [
                *loud,
                *(
                    (max(lo, ws), min(hi, we))
                    for lo, hi in in_stamp
                    for ws, we in stamps
                    if min(hi, we) - max(lo, ws) > _EPS
                ),
            ]
        )
        candidates = _candidates(kept, keep, plan.removed, spans, normalized, loud)
    else:
        candidates = []
    before_longest = kept_pause_stats(plan, words, measure, duration_s)[0] if measurable else None

    guard_dropped = guard_trimmed = 0
    new: list[Removal] = []
    if candidates:
        probe = CutPlan(
            keep_segments=_complement(candidates, duration_s),
            removed=list(candidates),
            time_saved_s=sum(r.end_s - r.start_s for r in candidates),
        )
        guarded, g_report = guard_audible_speech(
            probe, samples, exempt=(), sr=sr, min_cut_s=TIGHT_MIN_CUT_S
        )
        guard_dropped, guard_trimmed = g_report["dropped_total"], g_report["trimmed_total"]
        new = sorted(guarded.removed, key=lambda r: r.start_s)

    consented = list(plan.removed)
    new = _enforce_fragments(consented, new, kept, normalized, duration_s)
    capped = False

    def over_budget() -> bool:
        return (
            len(consented) + len(new) > MAX_REMOVALS
            or len(_complement([*consented, *new], duration_s)) > TIGHT_MAX_KEEP_SEGMENTS
        )

    while new and over_budget():
        capped = True
        new.remove(min(new, key=lambda r: (r.end_s - r.start_s, -r.start_s)))
    if capped:
        new = _enforce_fragments(consented, new, kept, normalized, duration_s)

    reason = None if audio_ok else "audio_unavailable"
    if reason is None and not measurable:
        reason = "unmeasured"
    if not new:
        status: Literal["applied", "no_change", "unavailable"] = (
            "no_change" if audio_ok else "unavailable"
        )
        longest, over = (
            kept_pause_stats(plan, words, measure, duration_s) if measurable else (None, None)
        )
        return plan, TightPauseReport(
            status, 0, 0.0, longest, before_longest, over,
            guard_dropped, guard_trimmed, capped, reason,
        )  # fmt: skip

    removed = sorted([*consented, *new], key=lambda r: (r.start_s, r.end_s))
    out_plan = dataclasses.replace(
        plan,
        removed=removed,
        keep_segments=_complement(removed, duration_s),
        time_saved_s=sum(r.end_s - r.start_s for r in removed),
    )
    longest, over = (
        kept_pause_stats(out_plan, words, measure, duration_s) if measurable else (None, None)
    )
    return out_plan, TightPauseReport(
        "applied", len(new), sum(r.end_s - r.start_s for r in new), longest, before_longest,
        over, guard_dropped, guard_trimmed, capped, reason,
    )  # fmt: skip


def _unavailable(plan: CutPlan, reason: str) -> tuple[CutPlan, TightPauseReport]:
    return plan, TightPauseReport("unavailable", 0, 0.0, None, None, None, 0, 0, False, reason)


def apply_tight_pauses(
    plan: CutPlan,
    media_path: str,
    words: Sequence[Any],
    duration_s: float,
    *,
    silence_detect: Callable[..., Any] | None = None,
    pcm_loader: Callable[[str], np.ndarray] | None = None,
) -> tuple[CutPlan, TightPauseReport]:
    """``tighten_pauses`` for the media the plan was built from (detects + decodes itself)."""

    if silence_detect is None:
        from app.services.clip_speech import detect_silences_with_status

        silence_detect = detect_silences_with_status
    result = silence_detect(media_path, min_silence_s=SILENCE_MIN_S, ambient_adaptive=True)
    if getattr(result, "status", None) != "ok":
        return _unavailable(plan, "silence_detect_failed")
    try:
        samples = (pcm_loader or audio_pcm.decode_pcm_f32)(media_path)
    except (subprocess.SubprocessError, OSError):
        return _unavailable(plan, "audio_unavailable")
    if samples is None or len(samples) < int(SAMPLE_RATE * FRAME_S):
        return _unavailable(plan, "audio_unavailable")
    kria_spans = [tuple(s) for s in result.spans]
    measured = measure_silence_spans(media_path, duration_s=duration_s)
    return tighten_pauses(
        plan,
        words,
        kria_spans,
        duration_s,
        samples=samples,
        measure_silences=measured,
        measurable=measured is not None,
    )
