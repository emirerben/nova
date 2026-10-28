"""Audible-speech veto for speech-cleanup cut plans (KRI-1017).

A cleanup cut is only as safe as the two signals it intersects: the transcript's word
gaps and the silence spans. On the Plaza de Mayo "maximo 2" take both were wrong at once.
The mixed-language transcript had no token for the Turkish stretches or for the guide's
soft English ("1810", "Yes", "whole"), and the take has no real silence, so the
ambient-energy floor (KRI-234: 10th percentile + 6 dB) sat above the guide's voice and
called it "silence". Every soundful island between two such fake silences then read as a
tokenless filler. The plan cut 13 spans; 6 of them held voice.

This module checks each cut against the audio itself, independent of any transcript: a
removal whose samples hold a sustained voiced run (periodic energy in the speech pitch
range, above the take's quiet floor) gives that audio back. A pause cut keeps its longer
voice-free edge, ``PAD_S`` clear of the voice, and is dropped when that edge is too short
to be a cut; a filler cut is dropped whole. Voicing, not loudness, is the test: the
guide's far-mic speech sat only 2-4 dB above the plaza noise, while traffic and wind are
aperiodic and stay below the periodicity bar. Spans the caller marks exempt (transcribed
fillers, consented retakes, creator-chosen spans) are not judged here; their own checks own
them. Pure numpy over decoded PCM; the caller decodes (``audio_pcm.decode_pcm_f32``).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from app.pipeline.audio_pcm import decode_pcm_f32
from app.pipeline.silence_cut import (
    MIN_CUT_S,
    PAD_S,
    REASON_FILLER_ACOUSTIC,
    REASON_FILLER_LEXICAL,
    REASON_SILENCE,
    CutPlan,
    Removal,
    is_filler_token,
)

SAMPLE_RATE = 16_000
FRAME_S = 0.04  # analysis window: two periods of a 50 Hz voice
HOP_S = 0.01
BAND_HZ = (80.0, 1000.0)  # where voiced harmonics carry the periodicity
PITCH_HZ = (60.0, 400.0)  # lag range searched for the period
MIN_PERIODICITY = 0.7  # normalised cross-correlation at the best lag
MIN_VOICED_RUN_S = 0.05  # 5 consecutive voiced hops; shorter runs are tonal blips
FLOOR_PERCENTILE = 5.0  # the take's quiet floor, from whole-take frame RMS
MIN_ABOVE_FLOOR_DB = 3.0  # a periodic hum sitting AT the floor is ambience, not a voice
CONTEXT_S = 0.1  # a run straddling a cut edge is measured whole
GUARDED_REASONS = frozenset({REASON_SILENCE, REASON_FILLER_ACOUSTIC, REASON_FILLER_LEXICAL})
_MAX_REPORTED_DROPS = 32
_EPS = 1e-9


def _merge(spans: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(spans):
        if hi - lo <= _EPS:
            continue
        if merged and lo <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _overlap(lo: float, hi: float, spans: Sequence[tuple[float, float]]) -> float:
    return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in spans)


def _subtract(
    lo: float, hi: float, spans: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    pieces: list[tuple[float, float]] = []
    cursor = lo
    for a, b in spans:
        if b <= cursor or a >= hi:
            continue
        if a > cursor:
            pieces.append((cursor, a))
        cursor = max(cursor, b)
    if hi > cursor:
        pieces.append((cursor, hi))
    return pieces


def _frames(x: np.ndarray, win: int, hop: int) -> np.ndarray:
    count = 1 + (len(x) - win) // hop
    index = np.arange(win)[None, :] + hop * np.arange(count)[:, None]
    return x[index]


def quiet_floor_db(samples: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    """The take's quiet floor: a low percentile of non-overlapping frame RMS levels."""

    win = int(sr * FRAME_S)
    usable = len(samples) - len(samples) % win
    if usable < win:
        return -120.0
    frames = samples[:usable].astype(np.float64).reshape(-1, win)
    levels = 10.0 * np.log10(np.mean(frames**2, axis=1) + 1e-12)
    return float(np.percentile(levels, FLOOR_PERCENTILE))


def voiced_runs(
    samples: np.ndarray,
    lo_s: float,
    hi_s: float,
    *,
    floor_db: float,
    sr: int = SAMPLE_RATE,
) -> list[tuple[float, float]]:
    """Sustained voiced runs inside ``[lo_s, hi_s)``, as frame-centre spans in seconds."""

    win = int(sr * FRAME_S)
    hop = int(sr * HOP_S)
    start = max(0, int(lo_s * sr))
    segment = samples[start : min(len(samples), int(hi_s * sr))].astype(np.float64)
    if len(segment) < win:
        return []

    size = 1 << int(np.ceil(np.log2(len(segment))))
    spectrum = np.fft.rfft(segment, size)
    freqs = np.fft.rfftfreq(size, 1.0 / sr)
    spectrum[(freqs < BAND_HZ[0]) | (freqs > BAND_HZ[1])] = 0.0
    banded = np.fft.irfft(spectrum, size)[: len(segment)]

    raw = _frames(segment, win, hop)
    level_db = 10.0 * np.log10(np.mean(raw**2, axis=1) + 1e-12)
    frames = _frames(banded, win, hop)
    frames = frames - frames.mean(axis=1, keepdims=True)
    power = np.fft.rfft(frames, 2 * win)
    autocorr = np.fft.irfft(power * np.conj(power), 2 * win)[:, :win]
    lags = np.arange(int(sr / PITCH_HZ[1]), int(sr / PITCH_HZ[0]) + 1)
    energy = np.concatenate([np.zeros((len(frames), 1)), np.cumsum(frames**2, axis=1)], axis=1)
    head = energy[:, win - lags]  # energy of frame[: win - lag]
    tail = energy[:, -1][:, None] - energy[:, lags]  # energy of frame[lag:]
    periodicity = (autocorr[:, lags] / np.sqrt(head * tail + 1e-18)).max(axis=1)

    voiced = (periodicity >= MIN_PERIODICITY) & (level_db >= floor_db + MIN_ABOVE_FLOOR_DB)
    centres = (start + hop * np.arange(len(frames)) + win / 2) / sr
    runs: list[tuple[float, float]] = []
    min_frames = int(round(MIN_VOICED_RUN_S / HOP_S))
    run_start: int | None = None
    for idx, flag in enumerate([*voiced.tolist(), False]):
        if flag and run_start is None:
            run_start = idx
        elif not flag and run_start is not None:
            if idx - run_start >= min_frames:
                runs.append(
                    (
                        float(centres[run_start] - HOP_S / 2),
                        float(centres[idx - 1] + HOP_S / 2),
                    )
                )
            run_start = None
    return runs


def guard_audible_speech(
    plan: CutPlan,
    samples: np.ndarray,
    *,
    exempt: Sequence[tuple[float, float]] = (),
    sr: int = SAMPLE_RATE,
) -> tuple[CutPlan, dict[str, Any]]:
    """``plan`` with every voiced silence/filler cut given back to the edit.

    Only ``GUARDED_REASONS`` are judged, and voicing inside ``exempt`` spans is ignored.
    A pause cut that holds voice keeps only its longer voice-free EDGE, ``PAD_S`` clear of
    the voice (the breathing room every kept word gets), when that edge is still a
    ``MIN_CUT_S`` cut; otherwise it is dropped. The stretch between two voiced runs is never
    kept as a cut: soft speech is detected in bursts, and its consonants sit between them.
    A filler cut that holds voice is dropped whole: a filler is one sound, half of it is not
    a cut. Either way the returned audio only grows the keep segments either side (at a clip
    edge it becomes the keep segment that holds the voice), so no other cut moves and the
    budget can only loosen. Audio that cannot be decoded drops every judged cut, so a decode
    fault never turns into cutting blind.
    """

    judged = [removal for removal in plan.removed if removal.reason in GUARDED_REASONS]
    report: dict[str, Any] = {
        "status": "ok",
        "checked": len(judged),
        "dropped": [],
        "trimmed": [],
        "dropped_total": 0,
        "trimmed_total": 0,
    }
    if not judged:
        return plan, report

    exempt_spans = _merge(exempt)
    # original removal -> (replacement or None when dropped, voiced seconds inside it)
    verdicts: list[tuple[Removal, Removal | None, float]] = []
    if samples is None or len(samples) < int(sr * FRAME_S):
        report["status"] = "audio_unavailable"
        verdicts = [(removal, None, 0.0) for removal in judged]
    else:
        floor_db = quiet_floor_db(samples, sr)
        for removal in judged:
            voiced = [
                piece
                for lo, hi in voiced_runs(
                    samples,
                    removal.start_s - CONTEXT_S,
                    removal.end_s + CONTEXT_S,
                    floor_db=floor_db,
                    sr=sr,
                )
                for piece in _subtract(lo, hi, exempt_spans)
                if min(piece[1], removal.end_s) - max(piece[0], removal.start_s) > _EPS
            ]
            if not voiced:
                continue
            voiced_s = sum(min(hi, removal.end_s) - max(lo, removal.start_s) for lo, hi in voiced)
            replacement = None
            if removal.reason == REASON_SILENCE:
                head = (removal.start_s, min(lo for lo, _ in voiced) - PAD_S)
                tail = (max(hi for _, hi in voiced) + PAD_S, removal.end_s)
                lo, hi = max(head, tail, key=lambda edge: edge[1] - edge[0])
                if hi - lo >= MIN_CUT_S - _EPS:
                    replacement = Removal(start_s=lo, end_s=hi, reason=removal.reason)
            verdicts.append((removal, replacement, voiced_s))

    def entry(removal: Removal, voiced_s: float) -> dict[str, Any]:
        return {
            "start_s": round(removal.start_s, 3),
            "end_s": round(removal.end_s, 3),
            "reason": removal.reason,
            "voiced_s": round(voiced_s, 3),
        }

    dropped = [entry(r, v) for r, new, v in verdicts if new is None]
    trimmed = [
        {
            **entry(r, v),
            "kept_cut_start_s": round(new.start_s, 3),
            "kept_cut_end_s": round(new.end_s, 3),
        }
        for r, new, v in verdicts
        if new is not None
    ]
    report["dropped"] = dropped[:_MAX_REPORTED_DROPS]
    report["trimmed"] = trimmed[:_MAX_REPORTED_DROPS]
    report["dropped_total"] = len(dropped)
    report["trimmed_total"] = len(trimmed)
    if not verdicts:
        return plan, report

    replaced = {id(removal): new for removal, new, _ in verdicts}
    removed: list[Removal] = []
    returned: list[tuple[float, float]] = []
    for removal in plan.removed:
        if id(removal) not in replaced:
            removed.append(removal)
            continue
        new = replaced[id(removal)]
        if new is None:
            returned.append((removal.start_s, removal.end_s))
        else:
            removed.append(new)
            returned.extend(_subtract(removal.start_s, removal.end_s, [(new.start_s, new.end_s)]))
    return (
        replace(
            plan,
            removed=removed,
            keep_segments=_merge([*plan.keep_segments, *returned]),
            time_saved_s=sum(r.end_s - r.start_s for r in removed),
        ),
        report,
    )


def _field(record: Any, *names: str) -> Any:
    for name in names:
        value = record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)
        if value is not None:
            return value
    return None


def voice_guard_exempt_spans(
    words: Sequence[Any],
    retake_spans: Sequence[tuple[int, int]] = (),
    forced: Sequence[Any] = (),
) -> list[tuple[float, float]]:
    """Spans whose voicing the guard must not judge: their own checks own them.

    - every transcribed filler word, padded by ``PAD_S`` like the rule-1 cut that removes it
      (a real "uh" is voiced by nature; only a TOKENLESS island is the guard's business);
    - every word of a consented retake span, padded the same way;
    - every creator-chosen forced removal, exactly as given.

    ``words`` and ``forced`` may be dicts or objects (``text``/``word``, ``start_s``/``start``,
    ``end_s``/``end``); ``retake_spans`` are inclusive index pairs into ``words``.
    """

    timed: list[tuple[str, float, float]] = []
    for raw in words:
        text = _field(raw, "text", "word")
        start = _field(raw, "start_s", "start")
        end = _field(raw, "end_s", "end")
        timed.append((str(text or ""), float(start or 0.0), float(end or 0.0)))

    spans: list[tuple[float, float]] = [
        (max(0.0, start - PAD_S), end + PAD_S)
        for text, start, end in timed
        if text and is_filler_token(text)
    ]
    for span in retake_spans:
        try:
            first, last = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= first <= last < len(timed):
            spans.append((max(0.0, timed[first][1] - PAD_S), timed[last][2] + PAD_S))
    for record in forced:
        start, end = _field(record, "start_s", "start"), _field(record, "end_s", "end")
        if start is not None and end is not None:
            spans.append((float(start), float(end)))
    return _merge(spans)


def apply_voice_guard(
    plan: CutPlan,
    media_path: str,
    *,
    words: Sequence[Any],
    retake_spans: Sequence[tuple[int, int]] = (),
    forced: Sequence[Any] = (),
    pcm_loader: Callable[[str], np.ndarray] | None = None,
) -> tuple[CutPlan, dict[str, Any]]:
    """``guard_audible_speech`` for the media the plan was built from.

    Decodes ``media_path`` with ``pcm_loader`` (default ``decode_pcm_f32``). A decode fault
    (hung or failing ffmpeg) is treated as undecodable audio, which drops every judged cut:
    the plan then keeps speech rather than cutting without having heard it.
    """

    if not any(removal.reason in GUARDED_REASONS for removal in plan.removed):
        return plan, {
            "status": "ok",
            "checked": 0,
            "dropped": [],
            "trimmed": [],
            "dropped_total": 0,
            "trimmed_total": 0,
        }
    try:
        samples = (pcm_loader or decode_pcm_f32)(media_path)
    except (subprocess.SubprocessError, OSError):
        samples = np.zeros(0, dtype=np.float32)
    return guard_audible_speech(
        plan, samples, exempt=voice_guard_exempt_spans(words, retake_spans, forced)
    )
