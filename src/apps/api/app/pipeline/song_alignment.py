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
import structlog

from app.config import settings
from app.schemas.user_song import (
    AlignmentAlternate,
    PlacementCandidate,
    SongAlignment,
    TakeAlignment,
)

logger = logging.getLogger(__name__)
_slog = structlog.get_logger(__name__)

SAMPLE_RATE = 16_000

_BAND_HZ = (150.0, 5000.0)
_PHAT_EPS_REL = 1e-3  # regularizer, relative to the mean cross-spectrum magnitude
_MIN_TAKE_S = 1.0
_MIN_RMS = 1e-4
_PEAK_SUPPRESS_S = 0.25  # peaks closer than this are "the same" peak
_REFINE_S = 0.25  # text candidates are re-measured on audio within this window
_MERGE_S = 0.75  # an audio and a lyric candidate this close are one placement
_AUDIO_DELTA_MIN_Z = 6.0  # an audio peak must clear this to move a lyric delta
_DRIFT_FAIL_FACTOR = 0.4  # likelihood factor for a candidate the take does not support
_STRONG_AUDIO_FLOOR = 0.9
_TEXT_DISAGREE_FACTOR = 0.7
_LYRIC_OFF_AUDIO_FACTOR = 0.5  # lyric-only echo elsewhere when strong audio places the take
_DEDUP_S = 0.25
_N_AUDIO_PEAKS = 5
_MAX_ALTERNATES = 4  # legacy ``alternates`` cap
_MAD_TO_SIGMA = 1.4826
_RATIO_CAP = 50.0
_DEFAULT_MAX_TAKE_S = 120.0  # song spectrum is precomputed for takes up to this long

# Smith-Waterman scoring (integers so the row recurrence below is exact).
_SW_MATCH = 2
_SW_MISMATCH = -1
_SW_GAP = -1
_TEXT_MIN_MATCHED = 3
_TEXT_REL_BEST = 0.6  # KRI-471: weak lyric echoes are candidates too; likelihood ranks them
# A 10-minute rap song carries ~2400 words; the take side stays small (a take
# is <= 2 minutes). The Smith-Waterman rows are vectorized, so 4000 song words
# cost ~O(take_words) numpy passes (measured: a 10-take x 10-min run is ~seconds).
_TEXT_MAX_SONG_WORDS = 4000
_TEXT_MAX_TAKE_WORDS = 1500
_TEXT_MAX_CANDIDATES = 6
_TEXT_INLIER_TOL_S = 0.35  # a matched word is an inlier within this of the median offset
_TEXT_POOL_MIN_MATCHED = 8  # start+end offsets are pooled from this many matched words

# Drift check.
_DRIFT_SEARCH_S = 0.25
_DRIFT_MARGIN_S = 0.30
_DRIFT_INFORMATIVE_Z = 6.0
_WINDOW_TARGET_S = 3.0
_WINDOW_COVERAGE = 0.6

# Self-repeat similarity (ambiguity gate).
_REPEAT_LAG_S = 0.05  # re-performed repeats may sit a few ms apart
_REPEAT_MIN_OVERLAP_S = 0.5
_TRUNCATION_LOGGED: set[int] = set()


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


def _tokens_full(words: Any, limit: int) -> tuple[list[str], list[float], list[float], bool]:
    """Normalized tokens + start and end times, capped at ``limit``; the flag says it was capped."""
    toks: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for w in words or []:
        text, start, end = _word_fields(w)
        norm = _normalize_word(text)
        if norm:
            toks.append(norm)
            starts.append(start)
            ends.append(max(end, start))
    return toks[:limit], starts[:limit], ends[:limit], len(toks) > limit


def _tokens(words: Any, limit: int) -> tuple[list[str], list[float], bool]:
    """Normalized tokens + start times, capped at ``limit``; the flag says it was capped."""
    toks, starts, _ends, capped = _tokens_full(words, limit)
    return toks, starts, capped


def song_text_coverage_s(song_words: Any) -> float | None:
    """Song time up to which the lyrics reach the text aligner, ``None`` if uncapped.

    Past this point the song's words are dropped (``_TEXT_MAX_SONG_WORDS``), so a
    take placed there by audio must not be penalized for "disagreeing" with text
    that never saw its section.
    """
    toks, starts, capped = _tokens(song_words, _TEXT_MAX_SONG_WORDS)
    if not capped:
        return None
    key = len(toks)
    if key not in _TRUNCATION_LOGGED and len(_TRUNCATION_LOGGED) < 64:
        _TRUNCATION_LOGGED.add(key)
        _slog.warning(
            "song_alignment_lyrics_truncated",
            kept_words=len(toks),
            covered_until_s=round(starts[-1], 2),
        )
    return float(starts[-1])


@dataclass
class _TextMatch:
    """One lyric placement of a take: where it sits and how tightly the words agree."""

    delta_s: float
    score: float
    matched: int
    spread_s: float  # median absolute deviation of the per-word offsets
    take_start_s: float  # first matched take word start
    take_end_s: float  # last matched take word end
    density: float  # matched words / take words inside the matched span
    song_density: float = 1.0  # matched words / song words inside the matched span
    inlier_frac: float = 1.0  # share of matched words whose offset is within tolerance
    long_matched: int = 0  # matched words of 3+ characters (stopword/CJK-char guard)


def text_candidates(take_words: Any, song_words: Any) -> list[tuple[float, float, int]]:
    """Smith-Waterman candidates as ``(delta_s, sw_score, matched_words)``.

    Every non-extendable local optimum scoring >= 85% of the best becomes a
    candidate (so a chorus sung twice gives two). Candidates need at least 3
    matched words. Sorted best-first, deduped within 0.25 s.
    """
    return [(m.delta_s, m.score, m.matched) for m in _text_matches(take_words, song_words)]


def _text_matches(take_words: Any, song_words: Any) -> list[_TextMatch]:
    """``text_candidates`` with the evidence the lyrics-only path needs."""
    tw, t_start, t_end, _ = _tokens_full(take_words, _TEXT_MAX_TAKE_WORDS)
    sw, s_start, s_end, _ = _tokens_full(song_words, _TEXT_MAX_SONG_WORDS)
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

    cands: list[_TextMatch] = []
    for score, i, j in ends[: _TEXT_MAX_CANDIDATES * 3]:
        diffs: list[float] = []
        end_diffs: list[float] = []
        took: list[int] = []
        song_took: list[int] = []
        while i > 0 and j > 0 and h[i, j] > 0:
            cur = h[i, j]
            if cur == h[i - 1, j - 1] + sub[i - 1, j - 1]:
                if match[i - 1, j - 1]:
                    diffs.append(s_start[j - 1] - t_start[i - 1])
                    end_diffs.append(s_end[j - 1] - t_end[i - 1])
                    took.append(i - 1)
                    song_took.append(j - 1)
                i, j = i - 1, j - 1
            elif cur == h[i - 1, j] + _SW_GAP:
                i -= 1
            else:
                j -= 1
        if len(diffs) < _TEXT_MIN_MATCHED:
            continue
        # Sung onsets smear; with enough words the word ends steady the estimate.
        pooled = diffs + end_diffs if len(diffs) >= _TEXT_POOL_MIN_MATCHED else diffs
        delta = float(np.median(pooled))
        if any(abs(delta - c.delta_s) <= _PEAK_SUPPRESS_S for c in cands):
            continue
        spread = float(np.median(np.abs(np.asarray(diffs) - np.median(diffs))))
        first, last = min(took), max(took)
        med_diff = float(np.median(diffs))
        inliers = sum(1 for d in diffs if abs(d - med_diff) <= _TEXT_INLIER_TOL_S)
        song_span = max(song_took) - min(song_took) + 1
        cands.append(
            _TextMatch(
                delta_s=delta,
                score=float(score),
                matched=len(diffs),
                spread_s=spread,
                take_start_s=float(t_start[first]),
                take_end_s=float(t_end[last]),
                density=len(diffs) / float(last - first + 1),
                song_density=len(diffs) / float(song_span),
                inlier_frac=inliers / float(len(diffs)),
                long_matched=sum(1 for k in took if len(tw[k]) >= 3),
            )
        )
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
    """Bool form of ``_drift_check``."""
    return _drift_check(song, take, delta_s, sr, tolerance_s)[0]


def _drift_check(
    song: np.ndarray, take: np.ndarray, delta_s: float, sr: int, tolerance_s: float
) -> tuple[bool, tuple[float, float] | None]:
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

    Returns ``(ok, span)``. ``span`` is the take-time stretch from the first to the
    last window that landed on ``delta_s`` (the part of the take that matches the
    song), ``None`` when the check fails or nothing agreed.
    """
    n_samples = take.shape[0]
    n_windows = int(np.clip(round(n_samples / sr / _WINDOW_TARGET_S), 1, 5))
    edges = np.linspace(0, n_samples, n_windows + 1).astype(int)
    rms = [float(np.sqrt(np.mean(take[edges[k] : edges[k + 1]] ** 2))) for k in range(n_windows)]
    loud = max(rms)
    audible = 0
    agreeing = 0
    agree_first: int | None = None
    agree_last: int | None = None
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
            return False, None
        agreeing += 1
        if agree_first is None:
            agree_first = int(edges[k])
        agree_last = int(edges[k + 1])
    if audible == 0:
        return False, None
    if agreeing < int(np.ceil(_WINDOW_COVERAGE * audible)):
        return False, None
    if agree_first is None or agree_last is None:
        return True, None
    # Quiet or uninformative edge windows are skipped above, so pad by one window
    # rather than trimming footage that never disagreed with ``delta_s``.
    pad = _WINDOW_TARGET_S
    return True, (max(0.0, agree_first / sr - pad), min(n_samples / sr, agree_last / sr + pad))


# --------------------------------------------------------------------------- #
# Self-repeat check
# --------------------------------------------------------------------------- #


def _repeat_similarity(
    song: np.ndarray, delta_a: float, delta_b: float, take_len: int, sr: int
) -> float:
    """How alike the song sounds at two take placements, 0 (unrelated) .. 1 (identical).

    Both placements cover ``take_len`` samples of the song; only the stretch that
    sits inside the song at *both* is compared. The value is the band-limited PHAT
    coherence peak within +/- ``_REPEAT_LAG_S`` (1.0 for an exact repeat, ~0 for
    unrelated material), so a re-edited or 85%-shared chorus still scores high.
    """
    a0 = int(round(delta_a * sr))
    b0 = int(round(delta_b * sr))
    lo = max(0, -a0, -b0)
    hi = min(take_len, song.shape[0] - a0, song.shape[0] - b0)
    if hi - lo < int(_REPEAT_MIN_OVERLAP_S * sr):
        return 0.0
    seg_a = song[a0 + lo : a0 + hi]
    seg_b = song[b0 + lo : b0 + hi]
    nfft = _good_fft_size(2 * seg_a.shape[0])
    c, _ = _phat_cross(np.fft.rfft(seg_a, nfft).astype(np.complex64), seg_b, nfft, sr)
    k_lo = max(1, int(np.ceil(_BAND_HZ[0] * nfft / sr)))
    k_hi = min(nfft // 2, int(np.floor(_BAND_HZ[1] * nfft / sr)))
    full = 2.0 * (k_hi - k_lo + 1) / nfft  # the peak a perfect repeat would reach
    lag = int(_REPEAT_LAG_S * sr)
    near = np.concatenate((c[: lag + 1], c[nfft - lag :]))
    return float(np.clip(np.max(near) / full, 0.0, 1.0)) if full > 0 else 0.0


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


def _lyric_likelihood(m: _TextMatch, cfg: Any) -> float:
    """0..1 evidence that a take's words sit at ``m.delta_s`` (KRI-471).

    Long matched words (stopword / CJK-char guard) saturate; density on both the
    take and the song side, the share of consistent offsets and a tight spread
    each scale the score. Nothing here is a gate: a weak chain scores low and
    simply ranks below real singing.
    """
    words = 1.0 - float(np.exp(-m.long_matched / float(cfg.song_align_lyrics_words_scale)))
    dens = float(np.sqrt(max(m.density, 0.0) * max(m.song_density, 0.0)))
    tight = 1.0 / (1.0 + (m.spread_s / float(cfg.song_align_lyrics_spread_scale_s)) ** 2)
    return float(np.clip(words * dens * m.inlier_frac * tight, 0.0, 1.0))


@dataclass
class _Cand:
    """A placement hypothesis while the audio and lyric evidence is being merged."""

    delta: float  # before the proxy offset
    likelihood: float
    method: str  # audio | lyrics | both
    matched: int = 0
    peak_z: float = 0.0
    start: float | None = None
    end: float | None = None
    drift_ok: bool = False  # the whole take supports this audio peak


def _noisy_or(a: float, b: float) -> float:
    return 1.0 - (1.0 - a) * (1.0 - b)


def _finalize(
    media_id: str,
    proxy_generation: int | None,
    cands: list[_Cand],
    *,
    offset: float,
    text_score: float,
    peak_z: float,
    peak_ratio: float,
    cfg: Any,
) -> TakeAlignment:
    """Rank candidates and derive the legacy ``status`` / ``delta_s`` / ``alternates``."""
    floor = float(cfg.song_align_candidate_floor)
    kept = sorted((c for c in cands if c.likelihood >= floor), key=lambda c: -c.likelihood)
    ranked: list[_Cand] = []
    for c in kept:
        if all(abs(c.delta - r.delta) > _DEDUP_S for r in ranked):
            ranked.append(c)
    ranked = ranked[: int(cfg.song_align_candidates_max)]
    if not ranked:
        top = max((c.likelihood for c in cands), default=0.0)
        return _unmatched(
            media_id,
            proxy_generation,
            peak_z=peak_z,
            peak_ratio=peak_ratio,
            text_score=text_score,
            confidence=min(top, 0.3),
        )
    best = ranked[0]
    margin = (
        1.0
        if len(ranked) == 1
        else float((best.likelihood - ranked[1].likelihood) / best.likelihood)
    )
    margin = float(np.clip(margin, 0.0, 1.0))
    uncertain = margin < float(cfg.song_align_ask_margin) or best.likelihood < float(
        cfg.song_align_ask_likelihood
    )
    out = [
        PlacementCandidate(
            delta_s=c.delta + offset,
            likelihood=float(np.clip(c.likelihood, 0.0, 1.0)),
            method=c.method,  # type: ignore[arg-type]
            matched_words=c.matched,
            peak_z=float(c.peak_z),
            match_start_s=c.start,
            match_end_s=c.end,
        )
        for c in ranked
    ]
    extra: dict[str, Any] = {}
    if best.start is not None and best.end is not None and best.end > best.start:
        extra = {"match_start_s": best.start, "match_end_s": best.end}
    return TakeAlignment(
        media_id=media_id,
        proxy_generation=proxy_generation,
        status="ambiguous" if uncertain else "confident",
        delta_s=best.delta + offset,
        confidence=float(np.clip(best.likelihood, 0.0, 1.0)),
        text_score=text_score,
        peak_z=float(best.peak_z if best.method != "lyrics" else peak_z),
        peak_ratio=float(peak_ratio),
        alternates=(
            [
                AlignmentAlternate(delta_s=c.delta_s, score=float(c.likelihood))
                for c in out[:_MAX_ALTERNATES]
            ]
            if uncertain
            else []
        ),
        method="lyrics" if best.method == "lyrics" else "audio",
        likelihood=float(np.clip(best.likelihood, 0.0, 1.0)),
        margin=margin,
        candidates=out,
        **extra,
    )


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
    take_len_s = tl / sr
    pad = float(cfg.song_align_match_pad_s)

    # --- lyric candidates -------------------------------------------------
    t_matches = _text_matches(take_words, song_words)
    text_score = float(t_matches[0].matched) if t_matches else 0.0
    lyric: list[_Cand] = []
    if getattr(cfg, "song_align_lyrics_enabled", True):
        for m in t_matches:
            if m.matched < cfg.song_align_lyrics_min_words:
                continue
            start = max(0.0, m.take_start_s - pad)
            end = min(take_len_s, m.take_end_s + pad)
            lyric.append(
                _Cand(
                    delta=m.delta_s,
                    likelihood=_lyric_likelihood(m, cfg),
                    method="lyrics",
                    matched=m.matched,
                    start=start if end > start else None,
                    end=end if end > start else None,
                )
            )

    # --- audio: full-song GCC-PHAT ----------------------------------------
    nfft = spec.fft_size_for(tl)
    c, _ = _phat_cross(spec.spectrum(nfft), take, nfft, sr)
    lag0 = -tl
    corr = _lag_view(c, nfft, lag0, sl - 1)
    med, sigma = _robust_stats(corr)
    suppress = int(_PEAK_SUPPRESS_S * sr)
    peaks = _top_peaks(corr, _N_AUDIO_PEAKS, suppress)
    if not peaks:
        return _finalize(
            media_id,
            proxy_generation,
            lyric,
            offset=offset,
            text_score=text_score,
            peak_z=0.0,
            peak_ratio=0.0,
            cfg=cfg,
        )

    # Union the top audio peaks with audio-refined lyric candidates.
    pool: list[tuple[int, float]] = list(peaks)
    refine = int(_REFINE_S * sr)
    for m in t_matches:
        centre = int(round(m.delta_s * sr)) - lag0
        lo, hi = max(0, centre - refine), min(corr.shape[0], centre + refine + 1)
        if hi > lo:
            j = lo + int(np.argmax(corr[lo:hi]))
            pool.append((j, float(corr[j])))
    pool.sort(key=lambda p: p[1], reverse=True)
    audio_peaks: list[tuple[int, float]] = []
    for i, v in pool:
        if all(abs(i - ci) > suppress for ci, _ in audio_peaks):
            audio_peaks.append((i, v))

    def delta_of(i: int) -> float:
        return (lag0 + i + _subsample(corr, i)) / sr

    def z_of(v: float) -> float:
        return (v - med) / sigma

    def ratio_of(i: int, v: float) -> float:
        p2 = max((pv for pi, pv in peaks if abs(pi - i) > suppress), default=0.0)
        return _RATIO_CAP if p2 <= 0.0 else float(min(_RATIO_CAP, v / p2))

    top_i, top_v = audio_peaks[0]
    top_z, top_ratio = z_of(top_v), ratio_of(top_i, top_v)

    # Lyrics beyond the aligner's word cap were never seen by the text pass, so an
    # audio peak there cannot "disagree" with them.
    text_reach_s = song_text_coverage_s(song_words)
    floor = float(cfg.song_align_candidate_floor)

    def agrees_with_text(d: float) -> bool:
        blind = text_reach_s is not None and d >= text_reach_s
        return (
            not t_matches
            or blind
            or any(abs(m.delta_s - d) <= cfg.song_align_text_agree_s for m in t_matches)
        )

    # Peaks worth a drift check: above the noise floor, or strong enough that an exact
    # song repeat could be hiding their ratio (two equal peaks have ratio ~1).
    info: list[tuple[int, float, float, float, float]] = []  # (i, v, z, ratio, delta)
    drift: dict[int, tuple[bool, tuple[float, float] | None]] = {}
    for i, v in audio_peaks:
        z, ratio, d = z_of(v), ratio_of(i, v), delta_of(i)
        info.append((i, v, z, ratio, d))
        if _confidence(z, ratio, cfg) >= floor or z >= cfg.song_align_confident_peak_z:
            drift[i] = _drift_check(spec.pcm, take, d, sr, cfg.song_align_drift_tolerance_s)

    # Genuine song self-repeats (the song sounds alike at both placements) form one
    # group with the best drift-passing peak: they are tied, and the group's ratio is
    # measured against peaks *outside* it. Coincidental peaks keep their own ratio.
    group: dict[int, float] = {}  # peak index -> similarity to the lead
    leads = [
        r
        for r in info
        if r[0] in drift and drift[r[0]][0] and r[2] >= cfg.song_align_confident_peak_z
    ]
    lead = max(leads, key=lambda r: r[1], default=None)
    lead_ratio = 0.0
    if lead is not None:
        group[lead[0]] = 1.0
        for r in leads:
            if r[0] == lead[0]:
                continue
            sim = _repeat_similarity(spec.pcm, lead[4], r[4], tl, sr)
            if sim >= cfg.song_align_repeat_similarity_min:
                group[r[0]] = sim
        outside = max(
            (pv for pi, pv in peaks if all(abs(pi - gi) > suppress for gi in group)), default=0.0
        )
        lead_ratio = _RATIO_CAP if outside <= 0.0 else float(min(_RATIO_CAP, lead[1] / outside))

    audio: list[_Cand] = []
    for i, v, z, ratio, d in info:
        ok, span = drift.get(i, (False, None))
        if i in group and lead is not None:
            lead_like = _confidence(lead[2], lead_ratio, cfg)
            if not agrees_with_text(lead[4]):
                lead_like *= _TEXT_DISAGREE_FACTOR
            if (
                lead[2] >= cfg.song_align_strong_peak_z
                and lead_ratio >= cfg.song_align_strong_peak_ratio
            ):
                lead_like = max(lead_like, _STRONG_AUDIO_FLOOR)
            like = lead_like * group[i]
        else:
            like = _confidence(z, ratio, cfg)
            if like < floor:
                continue  # noise level: not evidence
            if ok:
                if not agrees_with_text(d):
                    like *= _TEXT_DISAGREE_FACTOR
                if z >= cfg.song_align_strong_peak_z and ratio >= cfg.song_align_strong_peak_ratio:
                    like = max(like, _STRONG_AUDIO_FLOOR)
            else:
                like *= _DRIFT_FAIL_FACTOR
                span = None
        audio.append(
            _Cand(
                delta=d,
                likelihood=like,
                method="audio",
                peak_z=z,
                drift_ok=ok,
                start=span[0] if span and span[1] > span[0] else None,
                end=span[1] if span and span[1] > span[0] else None,
            )
        )

    # --- merge lyric + audio within _MERGE_S -------------------------------
    merged: list[_Cand] = []
    used: set[int] = set()
    for ly in lyric:
        near = [
            (k, a)
            for k, a in enumerate(audio)
            if k not in used and abs(a.delta - ly.delta) <= _MERGE_S
        ]
        if not near:
            merged.append(ly)
            continue
        k, a = max(near, key=lambda ka: ka[1].likelihood)
        used.add(k)
        delta = ly.delta
        if a.peak_z >= _AUDIO_DELTA_MIN_Z and abs(a.delta - ly.delta) <= _REFINE_S and a.drift_ok:
            delta = a.delta
        merged.append(
            _Cand(
                delta=delta,
                likelihood=_noisy_or(a.likelihood, ly.likelihood),
                method="both",
                matched=ly.matched,
                peak_z=a.peak_z,
                start=a.start if a.start is not None else ly.start,
                end=a.end if a.end is not None else ly.end,
            )
        )
    merged.extend(a for k, a in enumerate(audio) if k not in used)
    # A take that carries the song's own audio (strong, drift-passing peak) is not an
    # earbud take, so a lyric-only echo elsewhere (a repeated chorus) is weak rivalry.
    if any(a.drift_ok and a.likelihood >= _STRONG_AUDIO_FLOOR for a in audio):
        for cand in merged:
            if cand.method == "lyrics":
                cand.likelihood *= _LYRIC_OFF_AUDIO_FACTOR
    return _finalize(
        media_id,
        proxy_generation,
        merged,
        offset=offset,
        text_score=text_score,
        peak_z=float(top_z),
        peak_ratio=float(top_ratio),
        cfg=cfg,
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
