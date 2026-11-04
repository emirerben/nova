"""``audio_quality``: clipping, wind, steady/crowd noise and the noise floor of a clip.

A pure measurement over the clip's AAC/WAV analysis proxy: ffmpeg ``astats`` for peak and
clipping, one ffmpeg decode to 16 kHz mono PCM for everything spectral. No network, no model.

Stored record (``analysis["signals"]["audio_quality"]["data"]``)::

    {"wind_score": 0..1, "crowd_score": 0..1, "clipping": 0..1 (fraction of samples at the
     rails), "clipped": bool, "noise_floor_db": dBFS of the quietest tenth of the clip,
     "peak_db": dBFS, "noisy": bool (wind or crowd), "silent": bool, "evidence": [str]}

``noisy`` is the only field the mute-clip suggestion may act on. Clipping is reported but is not
"noisy": muting a distorted clip throws away the speech it was trying to save.

Failure modes this is built around (each is exercised by the calibration set, see
``scripts/clip_signal_calibration.py``):

* bass-heavy music read as wind: it has the same low-band energy, so wind also needs a noise-like
  (flat) low-band spectrum and an envelope with no beat periodicity. Sub-bass tones and kick
  drums fail one or the other.
* music or an HVAC hum read as crowd noise: the crowd head needs a noise-like mid band, no beat
  periodicity, a small gap between loud and quiet frames and loud frames that carry no pitch.
* dense clean speech (no pauses) read as a crowd: only the pitch feature separates a single voice
  from babble, so the calibration set draws dense speech and TTS voices on purpose.
* silent / near-silent clip (peak, loud level or audible frame count): no division by zero,
  ``noisy`` is False, ``silent`` is True.
* very short clip (< ``MIN_DURATION_S``): too few frames for a floor or a periodicity estimate,
  so it stores ``{"measured": False, "reason": "too_short"}`` rather than a guess.
* mono vs stereo: both are downmixed for the spectral features; clipping is read per file.
* missing audio stream or unreadable file: stores ``{"measured": False, "reason":
  "no_usable_audio"}``, so the backfill does not download the same clip on every run. ffmpeg
  missing or timing out is the tool failing, not the clip: the signal is absent (``compute``
  returns None) and the next run tries again. Never an exception.
* a quiet gust (inaudible rumble) is not wind: the low-band level, relative to the clip's speech
  level, has to be audible.
* recording loudness: the same content read as noisier the louder it was recorded, because an
  absolute noise floor was a feature. Every spectral feature is now measured after normalizing the
  clip's loud level (90th percentile frame RMS) to ``NORMALIZED_LEVEL_DB``, so a clean voice scores
  the same at any gain. Only the reported ``noise_floor_db`` stays absolute, and no head reads it.

Every number inside the ``FITTED`` markers comes from the labeled calibration set. Do not edit
that block by hand: ``scripts/clip_signal_calibration.py fit --write`` regenerates it, and
``check`` fails when it drifts from the fixtures. Bump ``SIGNAL_VERSION`` with any change to
the measurement or to the fitted block so stored entries are recomputed.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from app.pipeline.audio_pcm import decode_pcm_f32
from app.services.clip_signals.registry import SignalInputs, SignalSpec, register

logger = logging.getLogger(__name__)

SIGNAL_VERSION = 2

ANALYSIS_SR = 16_000
N_FFT = 1024
HOP = 512
MIN_DURATION_S = 1.0
MAX_ANALYZED_S = 300.0
FFMPEG_TIMEOUT_S = 60.0

# Spectral features are measured with the clip's loud level (90th percentile frame RMS) here.
NORMALIZED_LEVEL_DB = -20.0

# Pitch period search (80-400 Hz at 16 kHz) and the autocorrelation peak that counts as voiced.
PITCH_LAGS = (40, 200)
VOICED_ACF = 0.3
LOUD_REL_DB = 15.0

LOW_BAND_HZ = (30.0, 200.0)
MID_BAND_HZ = (300.0, 3400.0)
# Frames quieter than this carry no audible content; they count toward the floor, not the shape.
AUDIBLE_DB = -60.0
# Below this peak the clip is treated as silent whatever its spectrum says.
SILENT_PEAK_DB = -55.0
# Below this loud level (90th percentile frame RMS) nothing in the clip is audible either. Spectral
# features are loudness-normalized, so without this a -58 dBFS hiss would be amplified into a
# convincing "crowd".
SILENT_LEVEL_DB = -55.0
MIN_AUDIBLE_FRAMES = 8
FLOOR_CLAMP_DB = -90.0
# Beat/gust periodicity is looked for between 0.19 s and 1.0 s (frames are 32 ms).
ACF_LAG_FRAMES = (6, 31)
# "Samples at the maximum" only means clipping when the maximum is at the rail.
CLIP_PEAK_DB = -1.0

WIND_FEATURES = ("low_ratio", "low_flat", "low_acf", "low_cv", "low_rel_db")
CROWD_FEATURES = ("dyn_db", "mid_flat", "env_acf", "low_ratio", "unvoiced_frac")
# Direction each feature may push its score. The calibration fit may not use a feature against
# its physical meaning (e.g. "more bass-dominant means LESS wind"), because the synthetic
# clips it learns from can make that look true while real recordings will not.
WIND_SIGNS = (1, 1, -1, 1, 1)
CROWD_SIGNS = (-1, 1, -1, -1, 1)

# BEGIN FITTED (scripts/clip_signal_calibration.py fit --write)
FITTED: dict[str, Any] = {
    "wind": {
        "mean": [0.382725, 0.239247, 0.426486, 0.94101, -11.251249],
        "std": [0.286466, 0.215904, 0.295442, 0.460268, 7.219868],
        "weights": [0.890211, 1.730267, -0.431486, 0.464067, 0.738158],
        "bias": -2.781469,
        "threshold": 0.3865,
    },
    "crowd": {
        "mean": [24.573749, 0.120774, 0.647002, 0.303981, 0.081027],
        "std": [19.84924, 0.13911, 0.32843, 0.280296, 0.113218],
        "weights": [-0.916519, 0.472429, 0.0, -0.870273, 1.600847],
        "bias": -2.50521,
        "threshold": 0.4539,
    },
    "clipping_fraction": 0.000868,
}
# END FITTED


@dataclass(frozen=True)
class AudioFeatures:
    """Raw measurements: the calibration script fits on these, ``score`` makes the record."""

    duration_s: float
    silent: bool
    peak_db: float
    clip_fraction: float
    floor_db: float
    level_db: float
    dyn_db: float
    low_ratio: float
    low_flat: float
    low_acf: float
    low_cv: float
    low_rel_db: float
    mid_flat: float
    env_acf: float
    unvoiced_frac: float

    def head(self, names: Sequence[str]) -> list[float]:
        row = asdict(self)
        return [float(row[n]) for n in names]


def _db(power: float) -> float:
    return 10.0 * math.log10(max(power, 1e-12))


def _env_periodicity(env: np.ndarray, lags: tuple[int, int]) -> float:
    """How strongly an envelope repeats at a beat-like period, 0..1.

    The largest autocorrelation *peak* between ``lags``, measured above the trough before it. A
    gust is a slow smooth swell whose autocorrelation only decays (no peak), a kick pattern comes
    back. A steady envelope (no variance) does not repeat, so it scores 0.
    """
    lo, hi = lags
    hi = min(hi, env.size // 3)
    if hi < lo + 1:
        return 0.0
    centered = env - env.mean()
    energy = float(np.dot(centered, centered)) / env.size
    if energy <= 1e-18:
        return 0.0
    acf = np.array(
        [1.0]
        + [
            float(np.dot(centered[:-k], centered[k:])) / (env.size - k) / energy
            for k in range(1, hi + 2)
        ]
    )
    best = 0.0
    for k in range(lo, hi + 1):
        if acf[k] > acf[k - 1] and acf[k] >= acf[k + 1]:
            best = max(best, float(acf[k] - acf[1:k].min()))
    return min(max(best, 0.0), 1.0)


def _flatness(power: np.ndarray) -> np.ndarray:
    """Per-frame spectral flatness (geometric / arithmetic mean): ~0.56 for noise, ~0 for a tone."""
    p = power + 1e-14
    return np.exp(np.mean(np.log(p), axis=1)) / np.mean(p, axis=1)


def _unvoiced_fraction(power: np.ndarray, rms_db: np.ndarray, level_db: float) -> float:
    """Share of the clip's loud frames that carry no pitch, 0..1.

    "Loud" is within ``LOUD_REL_DB`` of the clip's loud level, so room tone between sentences is
    left out and only sound competing with the speaker counts. A voice has a strong autocorrelation
    peak at its pitch period in every voiced frame; babble, traffic and hiss do not. A clip with no
    loud frames has nothing to say about it, so 0.
    """
    loud = rms_db >= level_db - LOUD_REL_DB
    if not loud.any():
        return 0.0
    ac = np.fft.irfft(power[loud], axis=1)
    ac = ac / np.maximum(ac[:, :1], 1e-20)
    pitch = ac[:, PITCH_LAGS[0] : PITCH_LAGS[1]].max(axis=1)
    return float(np.mean(pitch < VOICED_ACF))


def _astats(path: str) -> tuple[float, float] | None:
    """``(peak_db, fraction_of_samples_at_the_peak)`` from ffmpeg astats, None when unreadable.

    The clip is converted to 16-bit first so decoder overshoot of a clipped AAC stream is clamped
    onto the rail and counted as the clipping it is.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-vn",
            "-af",
            "aformat=sample_fmts=s16,astats=measure_perchannel=none"
            ":measure_overall=Peak_level+Peak_count+Number_of_samples",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        return None
    overall = result.stderr.split("Overall", 1)[-1]
    fields: dict[str, float] = {}
    for key in ("Peak level dB", "Peak count", "Number of samples"):
        m = re.search(rf"{key}:\s*(-?inf|-?[0-9.]+)", overall)
        if m is None:
            return None
        fields[key] = float(m.group(1))
    samples = fields["Number of samples"]
    peak_db = fields["Peak level dB"]
    if samples <= 0 or math.isnan(peak_db):
        return None
    # digital silence reports -inf
    return max(peak_db, -120.0), fields["Peak count"] / samples


def extract_features(path: str) -> AudioFeatures | None:
    """Measure one clip. None when the audio is missing, unreadable or too short to judge."""
    found = _extract(path)
    return found if isinstance(found, AudioFeatures) else None


def _extract(path: str) -> AudioFeatures | str | None:
    """Features, or why there are none: a reason string when the clip itself has nothing to
    measure (final: re-measuring the same bytes gives the same answer), None when the tool failed
    (ffmpeg missing or timed out), which may pass."""
    try:
        stats = _astats(path)
        pcm = decode_pcm_f32(path, ANALYSIS_SR, timeout_s=FFMPEG_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("audio_quality_ffmpeg_unavailable", extra={"error": str(exc)[:200]})
        return None
    if stats is None or pcm.size == 0:
        return "no_usable_audio"
    pcm = pcm[: int(MAX_ANALYZED_S * ANALYSIS_SR)]
    if not np.isfinite(pcm).all():
        pcm = np.nan_to_num(pcm, nan=0.0, posinf=0.0, neginf=0.0)
    duration_s = pcm.size / ANALYSIS_SR
    if duration_s < MIN_DURATION_S:
        return "too_short"
    peak_db, at_peak = stats
    clip_fraction = at_peak if peak_db >= CLIP_PEAK_DB else 0.0

    windows = np.lib.stride_tricks.sliding_window_view(pcm.astype(np.float64), N_FFT)[::HOP]
    frame_rms = np.sqrt(np.mean(windows**2, axis=1))
    rms_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-6))
    floor_db = max(float(np.percentile(rms_db, 10)), FLOOR_CLAMP_DB)
    level_db = max(float(np.percentile(rms_db, 90)), FLOOR_CLAMP_DB)
    audible = rms_db >= AUDIBLE_DB
    silent = (
        peak_db < SILENT_PEAK_DB
        or level_db < SILENT_LEVEL_DB
        or int(audible.sum()) < MIN_AUDIBLE_FRAMES
    )
    if silent:
        return AudioFeatures(
            duration_s=duration_s,
            silent=True,
            peak_db=peak_db,
            clip_fraction=clip_fraction,
            floor_db=floor_db,
            level_db=level_db,
            dyn_db=max(level_db - floor_db, 0.0),
            low_ratio=0.0,
            low_flat=0.0,
            low_acf=0.0,
            low_cv=0.0,
            low_rel_db=FLOOR_CLAMP_DB,
            mid_flat=0.0,
            env_acf=0.0,
            unvoiced_frac=0.0,
        )

    gain = 10.0 ** ((NORMALIZED_LEVEL_DB - level_db) / 20.0)
    window = np.hanning(N_FFT)
    power = np.abs(np.fft.rfft(windows * (gain * window), axis=1)) ** 2
    hz_per_bin = ANALYSIS_SR / N_FFT
    lo = slice(math.ceil(LOW_BAND_HZ[0] / hz_per_bin), int(LOW_BAND_HZ[1] // hz_per_bin) + 1)
    mid = slice(math.ceil(MID_BAND_HZ[0] / hz_per_bin), int(MID_BAND_HZ[1] // hz_per_bin) + 1)
    p_low = power[:, lo]
    e_low = p_low.sum(axis=1)
    e_tot = power[:, lo.start :].sum(axis=1) + 1e-12

    low_env = np.sqrt(e_low)
    # one-sided power of a Hann-windowed frame back to mean-square amplitude
    low_ms = 2.0 * e_low / (N_FFT * float(np.sum(window**2)))
    return AudioFeatures(
        duration_s=duration_s,
        silent=False,
        peak_db=peak_db,
        clip_fraction=clip_fraction,
        floor_db=floor_db,
        level_db=level_db,
        dyn_db=max(level_db - floor_db, 0.0),
        low_ratio=float(np.mean((e_low / e_tot)[audible])),
        low_flat=float(np.median(_flatness(p_low)[audible])),
        low_acf=_env_periodicity(low_env, ACF_LAG_FRAMES),
        low_cv=float(np.std(low_env) / (np.mean(low_env) + 1e-12)),
        low_rel_db=_db(float(np.mean(low_ms[audible]))) - NORMALIZED_LEVEL_DB,
        mid_flat=float(np.median(_flatness(power[:, mid])[audible])),
        env_acf=_env_periodicity(frame_rms, ACF_LAG_FRAMES),
        unvoiced_frac=_unvoiced_fraction(power, rms_db, level_db),
    )


def _logistic(head: Mapping[str, Any], row: Sequence[float]) -> float:
    z = float(head["bias"])
    for x, mu, sd, w in zip(row, head["mean"], head["std"], head["weights"], strict=True):
        z += float(w) * (x - float(mu)) / (float(sd) or 1.0)
    return 1.0 / (1.0 + math.exp(-max(min(z, 40.0), -40.0)))


def head_scores(features: AudioFeatures) -> tuple[float, float]:
    if features.silent:
        return 0.0, 0.0
    return (
        _logistic(FITTED["wind"], features.head(WIND_FEATURES)),
        _logistic(FITTED["crowd"], features.head(CROWD_FEATURES)),
    )


def score(features: AudioFeatures) -> dict[str, Any]:
    wind, crowd = head_scores(features)
    wind_hit = wind >= FITTED["wind"]["threshold"]
    crowd_hit = crowd >= FITTED["crowd"]["threshold"]
    clipped = features.clip_fraction >= FITTED["clipping_fraction"]
    evidence: list[str] = []
    if features.silent:
        evidence.append("silent clip")
    if wind_hit:
        evidence.append(f"low-frequency rumble (wind), score {wind:.2f}")
    if crowd_hit:
        evidence.append(f"steady background noise, floor {features.floor_db:.0f} dBFS")
    if clipped:
        evidence.append(f"clipping on {features.clip_fraction * 100:.1f}% of samples")
    return {
        "wind_score": round(wind, 3),
        "crowd_score": round(crowd, 3),
        "clipping": round(features.clip_fraction, 5),
        "clipped": clipped,
        "noise_floor_db": round(features.floor_db, 1),
        "peak_db": round(features.peak_db, 1),
        "noisy": wind_hit or crowd_hit,
        "silent": features.silent,
        "evidence": evidence,
    }


def compute(inputs: SignalInputs) -> Mapping[str, Any] | None:
    if inputs.kind != "video" or not inputs.local_path:
        return None
    found = _extract(inputs.local_path)
    if isinstance(found, AudioFeatures):
        return score(found)
    # A clip with nothing to measure is stored as such, so the backfill does not download it again
    # on every run. No consumer reads a score from it: ``noisy`` and ``silent`` are absent.
    return None if found is None else {"measured": False, "reason": found}


SPEC = register(
    SignalSpec(
        name="audio_quality",
        version=SIGNAL_VERSION,
        kinds=frozenset({"video"}),
        compute=compute,
    )
)
