"""Locate raw takes inside a creator-uploaded song (KRI-374, lane A).

A creator films takes while the song plays on another phone. Each take must be
placed at the song time it was recorded against so a lip-sync montage can tile
them on the song's timeline.

Convention (never drift): ``song_time = take_time + delta_s``.

Pure numpy -- no scipy/librosa, no network, no DB, no storage. Algorithm:

1. Text anchors: Smith-Waterman local alignment of the take's transcript words
   against the song's words. Every local optimum within 85% of the best is a
   candidate offset (repeated choruses yield several). Text only ever *adds*
   evidence; it can never make a take confident on its own.
2. Audio: full-song GCC-PHAT, band-limited to 150-5000 Hz so phone-speaker
   coloring does not matter, with parabolic sub-sample peak refinement. The top
   audio peaks are unioned with the text candidates and each text candidate is
   re-measured on the audio within +/-250 ms.
3. Features: ``peak_z`` (robust sigma units over the whole lag range),
   ``peak_ratio`` (best peak / best peak outside +/-0.25 s of it), text
   agreement and a sub-window drift check.
4. Status from ``settings.song_align_*`` thresholds: confident / ambiguous /
   unmatched. Silent, tiny or empty takes are unmatched and never raise.

``settings.song_alignment_proxy_offset_s`` is added to every returned delta
(scores are untouched) to absorb a measured proxy-vs-original timing offset.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.config import settings
from app.schemas.user_song import (
    AlignmentAlternate,
    SongAlignment,
    TakeAlignment,
)

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

_BAND_HZ = (150.0, 5000.0)
_PHAT_EPS_REL = 1e-3  # regularizer, relative to the mean cross-spectrum magnitude
_MIN_TAKE_S = 1.0
_MIN_RMS = 1e-4
_PEAK_SUPPRESS_S = 0.25  # peaks closer than this are "the same" peak
_REFINE_S = 0.25  # text candidates are re-measured on audio within this window
_N_AUDIO_PEAKS = 5
_MAX_ALTERNATES = 4
_MAD_TO_SIGMA = 1.4826
_RATIO_CAP = 50.0
_DEFAULT_MAX_TAKE_S = 120.0  # song spectrum is precomputed for takes up to this long

# Smith-Waterman scoring (integers so the row recurrence below is exact).
_SW_MATCH = 2
_SW_MISMATCH = -1
_SW_GAP = -1
_TEXT_MIN_MATCHED = 3
_TEXT_REL_BEST = 0.85
_TEXT_MAX_WORDS = 1500
_TEXT_MAX_CANDIDATES = 6

# Drift check.
_DRIFT_SEARCH_S = 0.25
_DRIFT_MARGIN_S = 0.30
_DRIFT_INFORMATIVE_Z = 6.0
_WINDOW_TARGET_S = 3.0
_WINDOW_COVERAGE = 0.6


# --------------------------------------------------------------------------- #
# Spectrum helpers
# --------------------------------------------------------------------------- #


def _good_fft_size(n: int) -> int:
    """Smallest 2^a * 3^b * 5^c >= n (numpy's pocketfft is fast on these)."""
    best = 1 << max(0, (n - 1).bit_length())
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p = p35
            while p < n:
                p *= 2
            best = min(best, p)
            p35 *= 3
        p5 *= 5
    return best


@dataclass
class SongSpectrum:
    """The song's rFFT, computed once and reused across every take."""

    pcm: np.ndarray
    sr: int = SAMPLE_RATE
    _spectra: dict[int, np.ndarray] = field(default_factory=dict, repr=False)

    @property
    def n_samples(self) -> int:
        return int(self.pcm.shape[0])

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.sr

    def fft_size_for(self, take_samples: int) -> int:
        """Reuse an already-computed size whenever the take fits in it."""
        need = self.n_samples + take_samples
        fitting = [n for n in self._spectra if n >= need]
        if fitting:
            return min(fitting)
        return _good_fft_size(need)

    def spectrum(self, nfft: int) -> np.ndarray:
        cached = self._spectra.get(nfft)
        if cached is None:
            cached = np.fft.rfft(self.pcm, nfft).astype(np.complex64)
            self._spectra[nfft] = cached
        return cached


def _prepare_pcm(pcm: Any) -> np.ndarray:
    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return arr - float(arr.mean())


def precompute_song(
    song_pcm: Any,
    *,
    sr: int = SAMPLE_RATE,
    max_take_s: float = _DEFAULT_MAX_TAKE_S,
) -> SongSpectrum:
    """Compute the song's rFFT once for takes up to ``max_take_s`` long."""
    if isinstance(song_pcm, SongSpectrum):
        return song_pcm
    spec = SongSpectrum(pcm=_prepare_pcm(song_pcm), sr=sr)
    if spec.n_samples:
        spec.spectrum(_good_fft_size(spec.n_samples + int(max_take_s * sr)))
    return spec


def _phat_cross(
    song_spec: np.ndarray, take_pcm: np.ndarray, nfft: int, sr: int
) -> tuple[np.ndarray, int]:
    """Band-limited PHAT cross-correlation; returns the circular result.

    ``c[L] ~ sum_n song[n + L] * take[n]`` so a peak at lag ``L`` means
    ``song_time = take_time + L / sr``.
    """
    take_spec = np.fft.rfft(take_pcm, nfft).astype(np.complex64)
    k_lo = max(1, int(np.ceil(_BAND_HZ[0] * nfft / sr)))
    k_hi = min(nfft // 2, int(np.floor(_BAND_HZ[1] * nfft / sr)))
    cross = song_spec[k_lo : k_hi + 1] * np.conj(take_spec[k_lo : k_hi + 1])
    mag = np.abs(cross)
    eps = _PHAT_EPS_REL * float(mag.mean()) + 1e-20
    weighted = np.zeros(nfft // 2 + 1, dtype=np.complex64)
    weighted[k_lo : k_hi + 1] = cross / (mag + eps)
    return np.fft.irfft(weighted, nfft).astype(np.float32), nfft


def _lag_view(c: np.ndarray, nfft: int, lo: int, hi: int) -> np.ndarray:
    """Correlation values for lags ``lo..hi`` inclusive (circular indexing)."""
    if hi < 0:
        return c[nfft + lo : nfft + hi + 1]
    if lo >= 0:
        return c[lo : hi + 1]
    return np.concatenate((c[nfft + lo :], c[: hi + 1]))


def _robust_stats(corr: np.ndarray) -> tuple[float, float]:
    med = float(np.median(corr))
    sigma = float(np.median(np.abs(corr - med))) * _MAD_TO_SIGMA
    return med, max(sigma, 1e-12)


def _top_peaks(corr: np.ndarray, n: int, suppress: int) -> list[tuple[int, float]]:
    work = corr.copy()
    peaks: list[tuple[int, float]] = []
    for _ in range(n):
        i = int(np.argmax(work))
        v = float(work[i])
        if not np.isfinite(v) or v == -np.inf:
            break
        peaks.append((i, v))
        work[max(0, i - suppress) : i + suppress + 1] = -np.inf
    return peaks


def _subsample(corr: np.ndarray, i: int) -> float:
    """Parabolic peak interpolation; returns the fractional index offset."""
    if i <= 0 or i >= corr.shape[0] - 1:
        return 0.0
    y0, y1, y2 = float(corr[i - 1]), float(corr[i]), float(corr[i + 1])
    denom = y0 - 2.0 * y1 + y2
    if denom >= 0.0:
        return 0.0
    return float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))


# --------------------------------------------------------------------------- #
# Text anchors
# --------------------------------------------------------------------------- #


def _word_fields(word: Any) -> tuple[str, float, float]:
    if isinstance(word, dict):
        return (
            str(word.get("text", "")),
            float(word.get("start_s", 0.0)),
            float(word.get("end_s", 0.0)),
        )
    return str(word.text), float(word.start_s), float(word.end_s)


def _normalize_word(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).casefold()
    s = unicodedata.normalize("NFKD", s).replace("ı", "i")
    return "".join(ch for ch in s if ch.isalnum() and not unicodedata.combining(ch))


def _tokens(words: Any) -> tuple[list[str], list[float]]:
    toks: list[str] = []
    starts: list[float] = []
    for w in words or []:
        text, start, _end = _word_fields(w)
        norm = _normalize_word(text)
        if norm:
            toks.append(norm)
            starts.append(start)
    return toks[:_TEXT_MAX_WORDS], starts[:_TEXT_MAX_WORDS]


def text_candidates(take_words: Any, song_words: Any) -> list[tuple[float, float, int]]:
    """Smith-Waterman candidates as ``(delta_s, sw_score, matched_words)``.

    Every non-extendable local optimum scoring >= 85% of the best becomes a
    candidate (so a chorus sung twice gives two). Candidates need at least 3
    matched words. Sorted best-first, deduped within 0.25 s.
    """
    tw, t_start = _tokens(take_words)
    sw, s_start = _tokens(song_words)
    n, m = len(tw), len(sw)
    if n < _TEXT_MIN_MATCHED or m < _TEXT_MIN_MATCHED:
        return []

    vocab: dict[str, int] = {}
    t_ids = np.array([vocab.setdefault(t, len(vocab)) for t in tw], dtype=np.int32)
    s_ids = np.array([vocab.setdefault(t, len(vocab)) for t in sw], dtype=np.int32)
    match = t_ids[:, None] == s_ids[None, :]  # (n, m)
    sub = np.where(match, _SW_MATCH, _SW_MISMATCH).astype(np.int32)

    h = np.zeros((n + 1, m + 1), dtype=np.int32)
    idx = np.arange(m + 1, dtype=np.int32)
    for i in range(1, n + 1):
        prev = h[i - 1]
        base = np.zeros(m + 1, dtype=np.int32)
        base[1:] = np.maximum(prev[:-1] + sub[i - 1], prev[1:] + _SW_GAP)
        np.maximum(base, 0, out=base)
        # Horizontal gaps are linear, so the recurrence collapses to a cummax.
        h[i] = np.maximum.accumulate(base - _SW_GAP * idx) + _SW_GAP * idx

    best = int(h.max())
    if best <= 0:
        return []

    # End cells: a match reached on the diagonal that cannot extend by a match.
    ends: list[tuple[int, int, int]] = []
    ii, jj = np.nonzero(h >= _TEXT_REL_BEST * best)
    for i, j in zip(ii.tolist(), jj.tolist(), strict=True):
        if i == 0 or j == 0 or not match[i - 1, j - 1]:
            continue
        if h[i, j] != h[i - 1, j - 1] + _SW_MATCH:
            continue
        if i < n and j < m and match[i, j]:
            continue
        ends.append((int(h[i, j]), i, j))
    ends.sort(reverse=True)

    cands: list[tuple[float, float, int]] = []
    for score, i, j in ends[: _TEXT_MAX_CANDIDATES * 3]:
        diffs: list[float] = []
        while i > 0 and j > 0 and h[i, j] > 0:
            cur = h[i, j]
            if cur == h[i - 1, j - 1] + sub[i - 1, j - 1]:
                if match[i - 1, j - 1]:
                    diffs.append(s_start[j - 1] - t_start[i - 1])
                i, j = i - 1, j - 1
            elif cur == h[i - 1, j] + _SW_GAP:
                i -= 1
            else:
                j -= 1
        if len(diffs) < _TEXT_MIN_MATCHED:
            continue
        delta = float(np.median(diffs))
        if any(abs(delta - d) <= _PEAK_SUPPRESS_S for d, _s, _k in cands):
            continue
        cands.append((delta, float(score), len(diffs)))
        if len(cands) >= _TEXT_MAX_CANDIDATES:
            break
    return cands


# --------------------------------------------------------------------------- #
# Drift check
# --------------------------------------------------------------------------- #


def _window_delta(
    song: np.ndarray, take: np.ndarray, a: int, b: int, delta_s: float, sr: int
) -> tuple[float, float] | None:
    """Re-measure ``take[a:b]`` against the song near ``delta_s``.

    Returns ``(delta_s, local_peak_z)`` or ``None`` when the window cannot be
    placed inside the song.
    """
    margin = int(_DRIFT_MARGIN_S * sr)
    seg = take[a:b]
    s0 = int(round((a / sr + delta_s) * sr)) - margin
    s1 = int(round((b / sr + delta_s) * sr)) + margin
    cs0, cs1 = max(0, s0), min(song.shape[0], s1)
    if cs1 - cs0 < seg.shape[0]:
        return None
    song_seg = song[cs0:cs1]
    nfft = _good_fft_size(song_seg.shape[0] + seg.shape[0])
    c, _ = _phat_cross(np.fft.rfft(song_seg, nfft).astype(np.complex64), seg, nfft, sr)
    expected = int(round((a / sr + delta_s) * sr)) - cs0
    search = int(_DRIFT_SEARCH_S * sr)
    lo = max(-seg.shape[0], expected - search)
    hi = min(song_seg.shape[0] - 1, expected + search)
    if hi <= lo + 4:
        return None
    local = _lag_view(c, nfft, lo, hi)
    i = int(np.argmax(local))
    med, sigma = _robust_stats(local)
    z = (float(local[i]) - med) / sigma
    lag = lo + i + _subsample(local, i)
    return (cs0 + lag) / sr - a / sr, z


def _drift_ok(
    song: np.ndarray, take: np.ndarray, delta_s: float, sr: int, tolerance_s: float
) -> bool:
    """Window-consistency check: the whole take must support ``delta_s``.

    The take is split into ~3 s sub-windows (2-5 of them). Each is re-measured
    against the song near ``delta_s``. The check fails when

    * any window with a significant local peak disagrees with ``delta_s`` by
      more than ``tolerance_s`` (clock drift, a cut inside the take), or
    * fewer than ~60% of the audible windows land on ``delta_s`` at all. A
      coincidence (the same note sung elsewhere in the song) correlates only
      over a few words, never over the whole take.

    Audibly silent windows and windows that hang outside the song carry no
    information and are ignored.
    """
    n_samples = take.shape[0]
    n_windows = int(np.clip(round(n_samples / sr / _WINDOW_TARGET_S), 1, 5))
    edges = np.linspace(0, n_samples, n_windows + 1).astype(int)
    rms = [float(np.sqrt(np.mean(take[edges[k] : edges[k + 1]] ** 2))) for k in range(n_windows)]
    loud = max(rms)
    audible = 0
    agreeing = 0
    for k in range(n_windows):
        a, b = int(edges[k]), int(edges[k + 1])
        if rms[k] < max(_MIN_RMS, 0.1 * loud) or (b - a) / sr < 0.5:
            continue
        w = _window_delta(song, take, a, b, delta_s, sr)
        if w is None:
            continue
        audible += 1
        if w[1] < _DRIFT_INFORMATIVE_Z:
            continue
        if abs(w[0] - delta_s) > tolerance_s:
            return False
        agreeing += 1
    if audible == 0:
        return False
    return agreeing >= int(np.ceil(_WINDOW_COVERAGE * audible))


# --------------------------------------------------------------------------- #
# Public alignment API
# --------------------------------------------------------------------------- #


def _unmatched(
    media_id: str,
    proxy_generation: int | None,
    *,
    peak_z: float = 0.0,
    peak_ratio: float = 0.0,
    text_score: float = 0.0,
    confidence: float = 0.0,
) -> TakeAlignment:
    return TakeAlignment(
        media_id=media_id,
        proxy_generation=proxy_generation,
        status="unmatched",
        delta_s=None,
        confidence=confidence,
        text_score=text_score,
        peak_z=peak_z,
        peak_ratio=peak_ratio,
    )


def _confidence(z: float, ratio: float, cfg: Any) -> float:
    """Monotone in ``peak_z`` and ``peak_ratio``; 0 at noise level, 1 when strong."""
    z_full = max(cfg.song_align_strong_peak_z * 1.5, 6.0)
    r_full = max(cfg.song_align_strong_peak_ratio * 1.5, 1.5)
    cz = float(np.clip((z - 5.0) / (z_full - 5.0), 0.0, 1.0))
    cr = float(np.clip((ratio - 1.0) / (r_full - 1.0), 0.0, 1.0))
    return float(np.sqrt(cz * cr))


def align_take(
    song: Any,
    take_pcm: Any,
    song_words: Any,
    take_words: Any,
    *,
    media_id: str,
    proxy_generation: int | None = None,
    settings_obj: Any | None = None,
    sr: int = SAMPLE_RATE,
) -> TakeAlignment:
    """Place one take in the song. ``song`` is PCM or a ``SongSpectrum``."""
    try:
        return _align_take(
            song,
            take_pcm,
            song_words,
            take_words,
            media_id=media_id,
            proxy_generation=proxy_generation,
            cfg=settings_obj if settings_obj is not None else settings,
            sr=sr,
        )
    except Exception:  # noqa: BLE001 -- an aligner fault must degrade to "unmatched"
        logger.exception("song_alignment.align_take failed media_id=%s", media_id)
        return _unmatched(media_id, proxy_generation)


def _align_take(
    song: Any,
    take_pcm: Any,
    song_words: Any,
    take_words: Any,
    *,
    media_id: str,
    proxy_generation: int | None,
    cfg: Any,
    sr: int,
) -> TakeAlignment:
    spec = song if isinstance(song, SongSpectrum) else precompute_song(song, sr=sr)
    take = _prepare_pcm(take_pcm)
    sl, tl = spec.n_samples, take.shape[0]
    if sl == 0 or tl < _MIN_TAKE_S * sr or float(np.sqrt(np.mean(take**2))) < _MIN_RMS:
        return _unmatched(media_id, proxy_generation)

    offset = float(cfg.song_alignment_proxy_offset_s)

    # --- text anchors ---------------------------------------------------
    t_cands = text_candidates(take_words, song_words)
    text_score = float(t_cands[0][2]) if t_cands else 0.0

    # --- audio: full-song GCC-PHAT --------------------------------------
    nfft = spec.fft_size_for(tl)
    c, _ = _phat_cross(spec.spectrum(nfft), take, nfft, sr)
    lag0 = -tl
    corr = _lag_view(c, nfft, lag0, sl - 1)
    med, sigma = _robust_stats(corr)
    suppress = int(_PEAK_SUPPRESS_S * sr)
    peaks = _top_peaks(corr, _N_AUDIO_PEAKS, suppress)
    if not peaks:
        return _unmatched(media_id, proxy_generation, text_score=text_score)

    # Union audio peaks with audio-refined text candidates.
    pool: list[tuple[int, float]] = list(peaks)
    refine = int(_REFINE_S * sr)
    for delta, _score, _k in t_cands:
        centre = int(round(delta * sr)) - lag0
        lo, hi = max(0, centre - refine), min(corr.shape[0], centre + refine + 1)
        if hi > lo:
            j = lo + int(np.argmax(corr[lo:hi]))
            pool.append((j, float(corr[j])))
    pool.sort(key=lambda p: p[1], reverse=True)
    cands: list[tuple[int, float]] = []
    for i, v in pool:
        if all(abs(i - ci) > suppress for ci, _ in cands):
            cands.append((i, v))

    def delta_of(i: int) -> float:
        return (lag0 + i + _subsample(corr, i)) / sr

    def z_of(v: float) -> float:
        return (v - med) / sigma

    top_i, top_v = cands[0]

    def ratio_of(i: int, v: float) -> float:
        p2 = max((pv for pi, pv in peaks if abs(pi - i) > suppress), default=0.0)
        return _RATIO_CAP if p2 <= 0.0 else float(min(_RATIO_CAP, v / p2))

    # A candidate is only a real placement if it is a strong peak AND the whole
    # take supports it (window consistency / drift check).
    valid: list[tuple[int, float]] = []
    for i, v in cands:
        if z_of(v) < cfg.song_align_confident_peak_z:
            continue
        if _drift_ok(spec.pcm, take, delta_of(i), sr, cfg.song_align_drift_tolerance_s):
            valid.append((i, v))

    if not valid:
        z = z_of(top_v)
        ratio = ratio_of(top_i, top_v)
        return _unmatched(
            media_id,
            proxy_generation,
            peak_z=z,
            peak_ratio=ratio,
            text_score=text_score,
            confidence=min(_confidence(z, ratio, cfg), 0.3),
        )

    best_i, p1 = valid[0]
    best_delta = delta_of(best_i)
    peak_z = z_of(p1)
    peak_ratio = ratio_of(best_i, p1)

    text_agree = any(abs(d - best_delta) <= cfg.song_align_text_agree_s for d, _s, _k in t_cands)
    confidence = _confidence(peak_z, peak_ratio, cfg)
    if t_cands and not text_agree:
        confidence *= 0.7

    strong = (
        peak_z >= cfg.song_align_strong_peak_z and peak_ratio >= cfg.song_align_strong_peak_ratio
    )
    is_confident = peak_ratio >= cfg.song_align_confident_peak_ratio and (text_agree or strong)
    common = {
        "media_id": media_id,
        "proxy_generation": proxy_generation,
        "text_score": text_score,
        "peak_z": float(peak_z),
        "peak_ratio": float(peak_ratio),
    }
    if is_confident:
        return TakeAlignment(
            status="confident",
            delta_s=best_delta + offset,
            confidence=float(np.clip(confidence, 0.0, 1.0)),
            **common,
        )
    if len(valid) >= 2:
        alternates = [
            AlignmentAlternate(delta_s=delta_of(i) + offset, score=float(z_of(v)))
            for i, v in valid[:_MAX_ALTERNATES]
        ]
        return TakeAlignment(
            status="ambiguous",
            delta_s=best_delta + offset,
            confidence=float(np.clip(min(confidence, 0.6), 0.0, 1.0)),
            alternates=alternates,
            **common,
        )
    return TakeAlignment(
        status="unmatched",
        delta_s=None,
        confidence=float(np.clip(min(confidence, 0.3), 0.0, 1.0)),
        **common,
    )


def align_takes(
    song_pcm: Any,
    takes: dict[str, tuple[Any, Any, int | None]],
    song_words: Any,
    song_generation: int,
    *,
    settings_obj: Any | None = None,
    sr: int = SAMPLE_RATE,
) -> SongAlignment:
    """Align many takes against one song, computing the song's rFFT once.

    ``takes`` maps ``media_id -> (pcm, words, proxy_generation)``.
    """
    longest = max((np.asarray(p).shape[0] / sr for p, _w, _g in takes.values()), default=0.0)
    spec = (
        song_pcm
        if isinstance(song_pcm, SongSpectrum)
        else precompute_song(song_pcm, sr=sr, max_take_s=max(longest, 1.0))
    )
    out: dict[str, TakeAlignment] = {}
    for media_id, (pcm, words, generation) in takes.items():
        out[media_id] = align_take(
            spec,
            pcm,
            song_words,
            words,
            media_id=media_id,
            proxy_generation=generation,
            settings_obj=settings_obj,
            sr=sr,
        )
    return SongAlignment(song_generation=song_generation, takes=out)
