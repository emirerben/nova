"""Procedural sound-effect synthesis for the creator library (KRI-173).

numpy only, 48 kHz, deterministic: every recipe draws randomness from an RNG
seeded by its slug, so a rebuild renders the same waveform. Recipes return raw,
un-levelled audio; ``master.master`` owns trimming, fades, loudness and format.

Melodies are original or generic idioms (chromatic "wah wah" descents, major
arpeggios); none reproduce a recognisable copyrighted jingle.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence

import numpy as np

SR = 48_000

Recipe = Callable[[np.random.Generator], np.ndarray]
RECIPES: dict[str, Recipe] = {}


def recipe(name: str) -> Callable[[Recipe], Recipe]:
    def register(fn: Recipe) -> Recipe:
        if name in RECIPES:
            raise ValueError(f"duplicate recipe {name!r}")
        RECIPES[name] = fn
        return fn

    return register


def rng_for(slug: str) -> np.random.Generator:
    seed = int.from_bytes(hashlib.sha256(slug.encode("utf-8")).digest()[:8], "big")
    return np.random.default_rng(seed)


def render(name: str, slug: str | None = None) -> np.ndarray:
    return np.asarray(RECIPES[name](rng_for(slug or name)), dtype=np.float64)


# ── Basics ──────────────────────────────────────────────────────────────────

_NOTE_INDEX = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note(name: str) -> float:
    """Equal-tempered frequency for names like ``"C5"``, ``"F#4"``, ``"Bb3"``."""
    letter, rest, accidental = name[0].upper(), name[1:], 0
    while rest and rest[0] in "#b":
        accidental += 1 if rest[0] == "#" else -1
        rest = rest[1:]
    midi = 12 * (int(rest) + 1) + _NOTE_INDEX[letter] + accidental
    return 440.0 * 2 ** ((midi - 69) / 12)


def samples(dur: float) -> int:
    return max(1, int(round(dur * SR)))


def times(dur: float) -> np.ndarray:
    return np.arange(samples(dur)) / SR


def _freqs(freq: float | np.ndarray, n: int) -> np.ndarray:
    return np.broadcast_to(np.asarray(freq, dtype=np.float64), (n,)).astype(np.float64)


def _cycles(freq: float | np.ndarray, n: int) -> np.ndarray:
    return np.cumsum(_freqs(freq, n)) / SR


def sine(freq: float | np.ndarray, dur: float, phase0: float = 0.0) -> np.ndarray:
    return np.sin(2 * np.pi * (_cycles(freq, samples(dur)) + phase0))


def _blep(t: np.ndarray, dt: np.ndarray) -> np.ndarray:
    out = np.zeros_like(t)
    rising = t < dt
    x = t[rising] / dt[rising]
    out[rising] = x + x - x * x - 1.0
    falling = t > 1.0 - dt
    x = (t[falling] - 1.0) / dt[falling]
    out[falling] = x * x + x + x + 1.0
    return out


def saw(freq: float | np.ndarray, dur: float) -> np.ndarray:
    n = samples(dur)
    dt = np.clip(_freqs(freq, n) / SR, 1e-9, 0.5)
    t = np.cumsum(dt) % 1.0
    return 2.0 * t - 1.0 - _blep(t, dt)


def square(freq: float | np.ndarray, dur: float, duty: float = 0.5) -> np.ndarray:
    n = samples(dur)
    dt = np.clip(_freqs(freq, n) / SR, 1e-9, 0.5)
    t = np.cumsum(dt) % 1.0
    y = np.where(t < duty, 1.0, -1.0)
    return y + _blep(t, dt) - _blep((t - duty) % 1.0, dt)


def triangle(freq: float | np.ndarray, dur: float) -> np.ndarray:
    t = _cycles(freq, samples(dur)) % 1.0
    return 4.0 * np.abs(t - 0.5) - 1.0


def noise(dur: float, rng: np.random.Generator, color: str = "white") -> np.ndarray:
    n = samples(dur)
    y = rng.standard_normal(n)
    if color != "white":
        spectrum = np.fft.rfft(y)
        freqs = np.fft.rfftfreq(n, 1 / SR)
        freqs[0] = 1.0
        spectrum /= freqs ** {"pink": 0.5, "brown": 1.0}[color]
        spectrum[0] = 0.0
        y = np.fft.irfft(spectrum, n)
    return y / (np.max(np.abs(y)) + 1e-12)


# ── Envelopes and layout ────────────────────────────────────────────────────


def env(dur: float, points: Sequence[tuple[float, float]]) -> np.ndarray:
    ts, values = zip(*points, strict=True)
    return np.interp(times(dur), ts, values)


def decay(dur: float, tau: float, attack: float = 0.002) -> np.ndarray:
    t = times(dur)
    shape = np.exp(-t / tau)
    if attack > 0:
        shape *= np.clip(t / attack, 0.0, 1.0)
    return shape


def gate(dur: float, attack: float = 0.005, release: float = 0.03) -> np.ndarray:
    return env(dur, [(0.0, 0.0), (attack, 1.0), (max(attack, dur - release), 1.0), (dur, 0.0)])


def stereo(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return x if x.ndim == 2 else np.stack([x, x], axis=1)


def pan(x: np.ndarray, position: float | np.ndarray) -> np.ndarray:
    """Equal-power pan of a mono signal; ``position`` in [-1, 1]."""
    angle = (np.clip(position, -1.0, 1.0) + 1.0) * np.pi / 4
    return np.stack([x * np.cos(angle), x * np.sin(angle)], axis=1) * math.sqrt(2)


def place(parts: Sequence[tuple[float, np.ndarray]], dur: float | None = None) -> np.ndarray:
    """Mix signals starting at the given offsets (seconds)."""
    is_stereo = any(np.asarray(sig).ndim == 2 for _, sig in parts)
    end = max(samples(at) + len(sig) for at, sig in parts)
    n = max(end, samples(dur)) if dur else end
    out = np.zeros((n, 2)) if is_stereo else np.zeros(n)
    for at, sig in parts:
        sig = stereo(sig) if is_stereo else np.asarray(sig, dtype=np.float64)
        start = samples(at) if at > 0 else 0
        out[start : start + len(sig)] += sig[: n - start]
    return out


def seq(
    notes: Sequence[tuple[float, float]], voice: Callable[[float, float], np.ndarray]
) -> np.ndarray:
    """Concatenate ``voice(freq, dur)`` for (freq, dur) steps; freq 0 = rest."""
    parts, at = [], 0.0
    for freq, dur in notes:
        if freq > 0:
            parts.append((at, voice(freq, dur)))
        at += dur
    return place(parts, at)


def normalize(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    return x * (peak / (np.max(np.abs(x)) + 1e-12))


def saturate(x: np.ndarray, drive: float) -> np.ndarray:
    return np.tanh(drive * x) / math.tanh(drive)


def crush(x: np.ndarray, bits: int, hold: int) -> np.ndarray:
    held = np.repeat(x[::hold], hold)[: len(x)]
    levels = 2 ** (bits - 1)
    return np.round(held * levels) / levels


# ── Filters (zero-phase FFT; time-varying via STFT overlap-add) ─────────────

_FFT_PAD = 8192


def _fft_apply(x: np.ndarray, gain: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 2:
        return np.stack([_fft_apply(x[:, ch], gain) for ch in range(x.shape[1])], axis=1)
    n = len(x)
    m = n + _FFT_PAD
    freqs = np.fft.rfftfreq(m, 1 / SR)
    return np.fft.irfft(np.fft.rfft(x, m) * gain(freqs), m)[:n]


def lowpass(x: np.ndarray, fc: float, order: int = 2) -> np.ndarray:
    return _fft_apply(x, lambda f: 1.0 / np.sqrt(1.0 + (f / fc) ** (2 * order)))


def highpass(x: np.ndarray, fc: float, order: int = 2) -> np.ndarray:
    return _fft_apply(x, lambda f: (f / fc) ** order / np.sqrt(1.0 + (f / fc) ** (2 * order)))


def _band_gain(freqs: np.ndarray, center: float | np.ndarray, octaves: float) -> np.ndarray:
    with np.errstate(divide="ignore"):
        distance = np.log2(np.maximum(freqs, 1.0) / center)
    return np.exp(-0.5 * (distance / octaves) ** 2)


def bandpass(x: np.ndarray, fc: float, octaves: float = 1.0) -> np.ndarray:
    return _fft_apply(x, lambda f: _band_gain(f, fc, octaves))


def peak_eq(x: np.ndarray, fc: float, gain_db: float, octaves: float = 1.0) -> np.ndarray:
    boost = 10 ** (gain_db / 20) - 1.0
    return _fft_apply(x, lambda f: 1.0 + boost * _band_gain(f, fc, octaves))


def tv_filter(
    x: np.ndarray,
    gain: Callable[[np.ndarray, np.ndarray], np.ndarray],
    frame: int = 1024,
) -> np.ndarray:
    """Time-varying filter: ``gain(t, freqs)`` is evaluated per STFT frame."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 2:
        return np.stack([tv_filter(x[:, ch], gain, frame) for ch in range(2)], axis=1)
    hop = frame // 4
    n = len(x)
    padded = np.concatenate([np.zeros(frame), x, np.zeros(2 * frame)])
    window = np.hanning(frame + 1)[:-1]
    n_frames = (len(padded) - frame) // hop + 1
    index = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    spectra = np.fft.rfft(padded[index] * window, axis=1)
    centers = (hop * np.arange(n_frames) + frame / 2 - frame) / SR
    freqs = np.fft.rfftfreq(frame, 1 / SR)
    filtered = np.fft.irfft(spectra * gain(centers[:, None], freqs[None, :]), frame, axis=1)
    out = np.bincount(index.ravel(), (filtered * window).ravel(), len(padded))
    norm = np.bincount(index.ravel(), np.tile(window**2, n_frames), len(padded))
    return (out / np.maximum(norm, 1e-9))[frame : frame + n]


def sweep_band(
    x: np.ndarray,
    center: Callable[[np.ndarray], np.ndarray],
    octaves: float = 1.0,
    frame: int = 1024,
) -> np.ndarray:
    return tv_filter(x, lambda t, f: _band_gain(f, np.maximum(center(t), 20.0), octaves), frame)


def sweep_lowpass(
    x: np.ndarray, cutoff: Callable[[np.ndarray], np.ndarray], order: int = 2, frame: int = 1024
) -> np.ndarray:
    return tv_filter(
        x,
        lambda t, f: 1.0 / np.sqrt(1.0 + (f / np.maximum(cutoff(t), 20.0)) ** (2 * order)),
        frame,
    )


def convolve(x: np.ndarray, ir: np.ndarray) -> np.ndarray:
    n = len(x) + len(ir) - 1
    m = 1 << (n - 1).bit_length()
    return np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(ir, m), m)[:n]


def reverb(
    x: np.ndarray,
    rng: np.random.Generator,
    rt60: float = 1.0,
    wet: float = 0.25,
    predelay: float = 0.012,
    damp_hz: float = 6000.0,
) -> np.ndarray:
    """Stereo room from decaying, damped noise impulse responses."""
    dry = stereo(x)
    t = times(rt60)
    out = np.zeros((len(dry) + samples(predelay) + len(t) - 1, 2))
    out[: len(dry)] += dry
    lead = samples(predelay)
    for ch in range(2):
        ir = lowpass(rng.standard_normal(len(t)) * np.exp(-6.91 * t / rt60), damp_hz)
        ir /= np.sqrt(np.sum(ir**2)) + 1e-12
        tail = convolve(dry[:, ch], ir) * wet
        out[lead : lead + len(tail), ch] += tail
    return out


# ── Instruments ─────────────────────────────────────────────────────────────

_BELL = (
    (1.0, 1.0, 1.0),
    (2.0, 0.32, 0.55),
    (3.01, 0.16, 0.36),
    (4.17, 0.11, 0.24),
    (5.43, 0.06, 0.16),
)
_MARIMBA = ((1.0, 1.0, 1.0), (3.93, 0.28, 0.18), (9.8, 0.08, 0.07))


def bell(
    freq: float, dur: float, partials: Sequence[tuple[float, float, float]] = _BELL
) -> np.ndarray:
    t = times(dur)
    y = sum(
        amp * np.sin(2 * np.pi * freq * ratio * t) * np.exp(-t / max(0.02, dur * life / 3.2))
        for ratio, amp, life in partials
        if freq * ratio < SR * 0.45
    )
    return y * np.clip(t / 0.0015, 0.0, 1.0)


def marimba(freq: float, dur: float) -> np.ndarray:
    return bell(freq, dur, _MARIMBA)


def brass(
    freq: float,
    dur: float,
    bright: float = 6.0,
    vibrato: float = 0.0,
    scoop: float = 0.0,
    detune_cents: float = 7.0,
) -> np.ndarray:
    """Detuned saws through an opening low-pass: a synth-brass stab."""
    t = times(dur)
    pitch = np.ones_like(t)
    if scoop:
        pitch *= 2 ** (-scoop / 1200 * np.exp(-t / 0.045))
    if vibrato:
        pitch *= 1 + vibrato * np.clip((t - 0.18) / 0.25, 0, 1) * np.sin(2 * np.pi * 5.6 * t)
    ratio = 2 ** (detune_cents / 1200)
    voices = sum(saw(freq * pitch * r, dur) for r in (1 / ratio, 1.0, ratio)) / 3
    cutoff = freq * (1.4 + bright * (1 - np.exp(-t / 0.05)) * (0.65 + 0.35 * np.exp(-t / 0.4)))
    shaped = sweep_lowpass(voices, lambda tt: np.interp(tt, t, cutoff), order=2)
    return shaped * gate(dur, 0.018, min(0.08, dur / 3))


def chip(freq: float, dur: float, duty: float = 0.5, tau: float | None = None) -> np.ndarray:
    tone = square(freq, dur, duty) * gate(dur, 0.002, min(0.012, dur / 4))
    return tone * (decay(dur, tau, 0.0) if tau else 1.0)


def kick(dur: float = 0.45, hi: float = 130.0, lo: float = 45.0, tau: float = 0.22) -> np.ndarray:
    t = times(dur)
    body = np.sin(2 * np.pi * np.cumsum(lo + (hi - lo) * np.exp(-t / 0.035)) / SR)
    return body * decay(dur, tau, 0.001)


def snare(rng: np.random.Generator, dur: float = 0.3) -> np.ndarray:
    rattle = bandpass(noise(dur, rng), 3800, 1.2) * decay(dur, 0.09, 0.001)
    body = sine(190.0, dur) * decay(dur, 0.05, 0.001)
    return normalize(rattle) * 0.8 + body * 0.6


def tom(freq: float, dur: float = 0.4) -> np.ndarray:
    return kick(dur, freq * 1.3, freq, 0.16)


def cymbal(rng: np.random.Generator, dur: float = 1.6, tau: float = 0.5) -> np.ndarray:
    metal = sum(square(f, dur) for f in (205.3, 304.4, 369.6, 522.7, 540.0, 800.0)) / 6
    wash = highpass(metal * 0.6 + noise(dur, rng) * 0.5, 6500, 3)
    return normalize(wash) * decay(dur, tau, 0.002)


def timpani(freq: float, rng: np.random.Generator, dur: float = 1.2) -> np.ndarray:
    t = times(dur)
    head = np.sin(2 * np.pi * np.cumsum(freq * (1 + 0.06 * np.exp(-t / 0.06))) / SR)
    thump = lowpass(noise(dur, rng), 400) * decay(dur, 0.03)
    return head * decay(dur, 0.45, 0.003) + 0.5 * normalize(thump)


def whistle_tone(
    freq: float | np.ndarray,
    dur: float,
    rng: np.random.Generator,
    breath: float = 0.12,
) -> np.ndarray:
    tone = sine(freq, dur) + 0.06 * sine(np.asarray(freq) * 2, dur)
    center = np.broadcast_to(np.asarray(freq, dtype=np.float64), (samples(dur),))
    air = sweep_band(noise(dur, rng), lambda tt: np.interp(tt, times(dur), center), 0.12)
    return tone + breath * normalize(air)


def whoosh(
    rng: np.random.Generator,
    dur: float,
    lo: float,
    hi: float,
    peak_at: float = 0.45,
    octaves: float = 1.0,
    color: str = "pink",
    width: float = 0.24,
    travel: float = 0.6,
) -> np.ndarray:
    """Band-swept noise with a bell envelope and a left→right pass-by."""
    t = times(dur)
    shape = np.exp(-((((t / dur) - peak_at) / width) ** 2))
    swept = sweep_band(
        noise(dur, rng, color),
        lambda tt: lo * (hi / lo) ** np.interp(tt, t, shape),
        octaves,
        2048 if lo < 250 else 1024,
    )
    return pan(normalize(swept) * shape, np.linspace(-travel, travel, len(t)))


# ── Rejection / fail ────────────────────────────────────────────────────────


def _buzz(dur: float, freq: float = 146.8) -> np.ndarray:
    tone = 0.5 * saw(freq, dur) + 0.5 * saw(freq * 1.06, dur) + 0.35 * square(freq / 2, dur)
    tone = peak_eq(lowpass(tone, 3200), 900, 6.0, 1.2)
    return saturate(normalize(tone), 2.0) * gate(dur, 0.004, 0.03)


@recipe("wrong_buzzer_short")
def wrong_buzzer_short(rng: np.random.Generator) -> np.ndarray:
    return _buzz(0.42)


@recipe("wrong_buzzer_long")
def wrong_buzzer_long(rng: np.random.Generator) -> np.ndarray:
    return _buzz(1.1)


@recipe("wrong_buzzer_double")
def wrong_buzzer_double(rng: np.random.Generator) -> np.ndarray:
    return place([(0.0, _buzz(0.2)), (0.28, _buzz(0.36))])


@recipe("error_beep")
def error_beep(rng: np.random.Generator) -> np.ndarray:
    beep = lambda f, d: lowpass(square(f, d, 0.3), 5000) * gate(d, 0.003, 0.015)  # noqa: E731
    return seq([(note("A5"), 0.11), (0, 0.05), (note("E5"), 0.2)], beep)


@recipe("nope_boing")
def nope_boing(rng: np.random.Generator) -> np.ndarray:
    dur = 0.75
    t = times(dur)
    freq = 330 * np.exp(-t * 1.4) * (1 + 0.16 * np.exp(-t / 0.3) * np.sin(2 * np.pi * 11 * t))
    return (sine(freq, dur) + 0.3 * sine(freq * 2, dur)) * decay(dur, 0.24, 0.003)


@recipe("sad_trombone")
def sad_trombone(rng: np.random.Generator) -> np.ndarray:
    def wah(freq: float, dur: float) -> np.ndarray:
        t = times(dur)
        tone = brass(freq, dur, bright=2.5, scoop=40, vibrato=0.012 if dur > 0.6 else 0.0)
        mute = 500 + 1600 * np.sin(np.pi * np.clip(t / (dur * 0.9), 0, 1)) ** 0.7
        return sweep_lowpass(tone, lambda tt: np.interp(tt, t, mute), order=2)

    steps = [("D4", 0.36), ("C#4", 0.36), ("C4", 0.36), ("B3", 1.15)]
    return reverb(seq([(note(n), d) for n, d in steps], wah), rng, rt60=0.8, wet=0.12)


@recipe("fail_horn")
def fail_horn(rng: np.random.Generator) -> np.ndarray:
    dur = 1.3
    t = times(dur)
    bend = 1 - 0.16 * (np.clip(t - 0.75, 0.0, None) / 0.55) ** 1.5
    chord = sum(brass(f, dur, bright=3.5) for f in (98.0, 146.8, 196.0))
    bent = sum(saw(f * bend, dur) for f in (98.0, 146.8, 196.0)) / 3
    body = 0.7 * chord / 3 + 0.3 * lowpass(bent, 900)
    return saturate(normalize(body), 1.6) * gate(dur, 0.03, 0.12)


@recipe("game_over_jingle")
def game_over_jingle(rng: np.random.Generator) -> np.ndarray:
    lead = seq(
        [
            (note(n), d)
            for n, d in (("E5", 0.13), ("C5", 0.13), ("A4", 0.13), ("F4", 0.26), ("E4", 0.7))
        ],
        lambda f, d: chip(f, d, 0.5, 0.35 if d > 0.5 else None) * 0.6,
    )
    bass = seq(
        [(note(n), d) for n, d in (("A2", 0.39), ("D3", 0.26), ("E2", 0.7))],
        lambda f, d: triangle(f, d) * gate(d, 0.003, 0.02),
    )
    return place([(0.0, lead), (0.0, bass * 0.5)])


@recipe("cartoon_slip")
def cartoon_slip(rng: np.random.Generator) -> np.ndarray:
    zip_up = whistle_tone(600 * 3 ** (times(0.14) / 0.14), 0.14, rng) * gate(0.14, 0.005, 0.01)
    fall_t = times(0.45)
    fall = whistle_tone(1800 * (350 / 1800) ** (fall_t / 0.45), 0.45, rng) * gate(0.45, 0.005, 0.05)
    thud = kick(0.35, 95, 50, 0.1) + 0.3 * lowpass(noise(0.35, rng), 900) * decay(0.35, 0.04)
    return place([(0.0, zip_up * 0.5), (0.14, fall * 0.5), (0.62, thud)])


# ── Approval / success ──────────────────────────────────────────────────────


@recipe("correct_ding")
def correct_ding(rng: np.random.Generator) -> np.ndarray:
    return reverb(bell(note("E6"), 1.1), rng, rt60=0.7, wet=0.1)


@recipe("correct_ding_bright")
def correct_ding_bright(rng: np.random.Generator) -> np.ndarray:
    return reverb(bell(note("A6"), 0.8), rng, rt60=0.6, wet=0.1)


@recipe("double_ding")
def double_ding(rng: np.random.Generator) -> np.ndarray:
    dings = place([(0.0, bell(note("C6"), 0.5) * 0.8), (0.13, bell(note("E6"), 1.0))])
    return reverb(dings, rng, rt60=0.7, wet=0.1)


@recipe("success_chime")
def success_chime(rng: np.random.Generator) -> np.ndarray:
    notes = ("C5", "E5", "G5", "C6")
    chime = place([(0.08 * i, bell(note(n), 1.0) * (0.75 + 0.08 * i)) for i, n in enumerate(notes)])
    return reverb(chime, rng, rt60=0.9, wet=0.18)


@recipe("level_up")
def level_up(rng: np.random.Generator) -> np.ndarray:
    run = [(note(n), 0.045) for n in ("C5", "E5", "G5", "C6", "E6", "G6")]
    arpeggio = seq(run, lambda f, d: chip(f, d, 0.25))
    t = times(0.34)
    top = square(note("C7") * (1 + 0.01 * np.sin(2 * np.pi * 9 * t)), 0.34, 0.25) * decay(
        0.34, 0.14
    )
    return place([(0.0, arpeggio * 0.5), (0.27, top * 0.5)])


@recipe("tada_fanfare")
def tada_fanfare(rng: np.random.Generator) -> np.ndarray:
    ta = sum(brass(note(n), 0.14, bright=5.0) for n in ("G3", "B3", "D4", "G4")) / 4
    daaa = sum(brass(note(n), 1.1, bright=6.0, vibrato=0.01) for n in ("C4", "E4", "G4", "C5")) / 4
    daaa *= np.clip(0.75 + times(1.1) / 0.6, 0, 1.0)
    return reverb(place([(0.0, ta), (0.18, daaa), (0.18, cymbal(rng, 1.2) * 0.12)]), rng, 1.1, 0.16)


@recipe("checkmark_pop")
def checkmark_pop(rng: np.random.Generator) -> np.ndarray:
    return place([(0.0, _bubble(0.1, 420) * 0.8), (0.05, marimba(note("G6"), 0.25) * 0.6)])


@recipe("arcade_yes_blip")
def arcade_yes_blip(rng: np.random.Generator) -> np.ndarray:
    return seq([(note("C6"), 0.06), (note("G6"), 0.16)], lambda f, d: chip(f, d, 0.5, 0.12) * 0.6)


# ── Suspense / reveal ───────────────────────────────────────────────────────


def _heartbeat(bpm: float, beats: int, dub_at: float) -> np.ndarray:
    def thump(amp: float) -> np.ndarray:
        return lowpass(kick(0.3, 70, 45, 0.07), 180) * amp

    period = 60.0 / bpm
    parts = []
    for i in range(beats):
        parts += [(i * period, thump(1.0)), (i * period + dub_at, thump(0.7))]
    return place(parts)


@recipe("heartbeat")
def heartbeat(rng: np.random.Generator) -> np.ndarray:
    return _heartbeat(72, 4, 0.28)


@recipe("heartbeat_fast")
def heartbeat_fast(rng: np.random.Generator) -> np.ndarray:
    return _heartbeat(120, 6, 0.2)


@recipe("clock_ticking")
def clock_ticking(rng: np.random.Generator) -> np.ndarray:
    def tick(freq: float) -> np.ndarray:
        click = bandpass(noise(0.05, rng), freq * 1.5, 0.8) * decay(0.05, 0.004, 0.0003)
        ping = sine(freq, 0.05) * decay(0.05, 0.008, 0.0003)
        return normalize(click) * 0.7 + ping * 0.4

    parts = [(0.5 * i, tick(3200 if i % 2 == 0 else 2400)) for i in range(12)]
    return reverb(place(parts, 6.0), rng, rt60=0.35, wet=0.12)


@recipe("dramatic_sting")
def dramatic_sting(rng: np.random.Generator) -> np.ndarray:
    def hit(freqs: Sequence[float], dur: float) -> np.ndarray:
        horns = sum(brass(f, dur, bright=4.0) for f in freqs) / len(freqs)
        return horns + 0.6 * timpani(freqs[0], rng, max(dur, 0.8))[: len(horns)]

    t = times(1.5)
    tremolo = 0.75 + 0.25 * np.sin(2 * np.pi * 9 * t)
    strings = sum(saw(f, 1.5) for f in (note("F#3"), note("C4"), note("F#4"))) / 3
    held = hit([note("F#2"), note("C3"), note("F#3")], 1.5) + lowpass(strings, 2500) * tremolo * 0.4
    held *= gate(1.5, 0.01, 0.5)
    parts = [
        (0.0, hit([note("C2"), note("C3")], 0.22)),
        (0.32, hit([note("C2"), note("C3")], 0.22)),
    ]
    return reverb(place([*parts, (0.64, held)]), rng, rt60=1.6, wet=0.28)


def _riser(rng: np.random.Generator, dur: float) -> np.ndarray:
    t = times(dur)
    progress = t / dur
    air = sweep_band(noise(dur, rng, "pink"), lambda tt: 300 * 30 ** (tt / dur), 0.9)
    tone = lowpass(sum(saw(110 * 4**progress * r, dur) for r in (1.0, 1.5, 2.0)) / 3, 4000)
    body = normalize(air) * 0.7 + tone * 0.35
    return pan(body * progress**2, np.sin(2 * np.pi * 0.5 * t) * 0.3)


@recipe("riser_short")
def riser_short(rng: np.random.Generator) -> np.ndarray:
    return _riser(rng, 1.5)


@recipe("riser_long")
def riser_long(rng: np.random.Generator) -> np.ndarray:
    return _riser(rng, 3.0)


@recipe("reveal_boom")
def reveal_boom(rng: np.random.Generator) -> np.ndarray:
    dur = 2.2
    boom = kick(dur, 90, 32, 0.55)
    blast = sweep_lowpass(noise(dur, rng), lambda tt: 200 + 4800 * np.exp(-tt / 0.18)) * decay(
        dur, 0.25
    )
    return reverb(saturate(boom + 0.5 * normalize(blast), 1.8), rng, rt60=2.0, wet=0.22)


def _sparkle(rng: np.random.Generator, count: int, span: float) -> np.ndarray:
    scale = [note(n) for n in ("C7", "D7", "E7", "G7", "A7", "C8")]
    parts = []
    for i in range(count):
        at = span * (i / count) ** 1.2 + rng.uniform(0, 0.02)
        freq = scale[min(len(scale) - 1, int(i / count * len(scale) + rng.integers(0, 2)))]
        ping = sine(freq, 0.3) * decay(0.3, rng.uniform(0.05, 0.12), 0.001) * rng.uniform(0.5, 1.0)
        parts.append((at, pan(ping, rng.uniform(-0.8, 0.8))))
    shimmer = bandpass(noise(span + 0.3, rng), 9000, 0.6) * env(
        span + 0.3, [(0, 0), (span, 1), (span + 0.3, 0)]
    )
    parts.append((0.0, stereo(normalize(shimmer) * 0.12)))
    return reverb(place(parts), rng, rt60=1.0, wet=0.3)


@recipe("sparkle_magic")
def sparkle_magic(rng: np.random.Generator) -> np.ndarray:
    return _sparkle(rng, 24, 1.1)


@recipe("sparkle_short")
def sparkle_short(rng: np.random.Generator) -> np.ndarray:
    return _sparkle(rng, 10, 0.35)


@recipe("countdown_beeps")
def countdown_beeps(rng: np.random.Generator) -> np.ndarray:
    beep = lambda f, d: sine(f, d) * gate(d, 0.003, 0.02)  # noqa: E731
    parts = [(0.5 * i, beep(note("A5"), 0.1)) for i in range(3)]
    return place([*parts, (1.5, beep(note("A6"), 0.45))])


# ── Transitions ─────────────────────────────────────────────────────────────


@recipe("whoosh_fast")
def whoosh_fast(rng: np.random.Generator) -> np.ndarray:
    return whoosh(rng, 0.35, 500, 3500)


@recipe("whoosh_slow")
def whoosh_slow(rng: np.random.Generator) -> np.ndarray:
    return whoosh(rng, 1.0, 300, 2500, octaves=1.2)


@recipe("whoosh_heavy")
def whoosh_heavy(rng: np.random.Generator) -> np.ndarray:
    air = whoosh(rng, 0.8, 120, 900, color="brown", octaves=1.3)
    t = times(0.8)
    sub = sine(60 - 20 * t / 0.8, 0.8) * np.exp(-((((t / 0.8) - 0.45) / 0.25) ** 2))
    return air + stereo(sub * 0.5)


@recipe("swoosh")
def swoosh(rng: np.random.Generator) -> np.ndarray:
    return whoosh(rng, 0.45, 1200, 6000, octaves=0.8)


@recipe("swipe")
def swipe(rng: np.random.Generator) -> np.ndarray:
    return whoosh(rng, 0.2, 2500, 9000, peak_at=0.35, octaves=0.8, width=0.2, color="white")


@recipe("whip_pan")
def whip_pan(rng: np.random.Generator) -> np.ndarray:
    return whoosh(rng, 0.28, 800, 5000, peak_at=0.4, octaves=0.6, width=0.18, travel=0.95)


@recipe("zoom_whoosh")
def zoom_whoosh(rng: np.random.Generator) -> np.ndarray:
    t = times(0.6)
    rise = sine(300 * 5 ** (t / 0.6), 0.6) * np.exp(-((((t / 0.6) - 0.6) / 0.25) ** 2))
    return whoosh(rng, 0.6, 400, 4000, peak_at=0.55) + stereo(rise * 0.35)


def _glitch(rng: np.random.Generator, pieces: int) -> np.ndarray:
    parts, at, last = [], 0.0, None
    for _ in range(pieces):
        dur = float(rng.uniform(0.02, 0.06))
        kind = rng.integers(0, 4)
        if kind == 0 or last is None:
            piece = crush(
                square(float(rng.uniform(200, 2000)), dur, 0.3), 4, int(rng.integers(4, 16))
            )
        elif kind == 1:
            piece = bandpass(noise(dur, rng), float(rng.uniform(800, 6000)), 0.5)
        elif kind == 2:
            piece = last  # stutter
        else:
            piece = np.zeros(samples(dur * 0.5))
        piece = normalize(piece) * 0.8 if np.any(piece) else piece
        parts.append((at, pan(piece, float(rng.uniform(-0.7, 0.7)))))
        at += len(piece) / SR
        last = piece
    return place(parts)


@recipe("glitch")
def glitch(rng: np.random.Generator) -> np.ndarray:
    return _glitch(rng, 10)


@recipe("glitch_short")
def glitch_short(rng: np.random.Generator) -> np.ndarray:
    return _glitch(rng, 7)


@recipe("tape_stop")
def tape_stop(rng: np.random.Generator) -> np.ndarray:
    dur = 0.9
    t = times(dur)
    speed = np.clip(1 - t / dur, 0, 1) ** 1.6
    chord = sum(saw(f * speed, dur) for f in (note("C3"), note("E3"), note("G3"), note("C4"))) / 4
    wobble = sweep_lowpass(chord, lambda tt: 300 + 3000 * np.interp(tt, t, speed))
    return wobble * np.clip(speed * 1.5, 0, 1) * gate(dur, 0.002, 0.05)


# ── Impacts ─────────────────────────────────────────────────────────────────


@recipe("vine_boom")
def vine_boom(rng: np.random.Generator) -> np.ndarray:
    dur = 1.6
    click = lowpass(noise(dur, rng), 3000) * decay(dur, 0.004, 0.0)
    body = kick(dur, 160, 45, 0.45)
    return reverb(saturate(body + 0.4 * click, 3.0), rng, rt60=1.4, wet=0.18)


@recipe("sub_drop")
def sub_drop(rng: np.random.Generator) -> np.ndarray:
    dur = 1.8
    t = times(dur)
    freq = 30 + 60 * np.exp(-t / 0.55)
    return (sine(freq, dur) + 0.15 * sine(freq * 2, dur)) * decay(dur, 0.7, 0.01)


@recipe("small_explosion")
def small_explosion(rng: np.random.Generator) -> np.ndarray:
    dur = 2.0
    blast = sweep_lowpass(noise(dur, rng), lambda tt: 250 + 7000 * np.exp(-tt / 0.15)) * decay(
        dur, 0.35, 0.003
    )
    crackle = np.zeros(samples(dur))
    for at in np.sort(rng.uniform(0.05, 0.9, 40)):
        start = samples(float(at))
        crackle[start : start + 40] += (
            rng.uniform(0.2, 1.0) * np.exp(-np.arange(40) / 8) * np.exp(-at / 0.4)
        )
    body = normalize(blast) + kick(dur, 80, 35, 0.5) * 0.8 + highpass(crackle, 1500) * 0.6
    return reverb(saturate(body, 1.5), rng, rt60=1.5, wet=0.22)


@recipe("cinematic_hit")
def cinematic_hit(rng: np.random.Generator) -> np.ndarray:
    dur = 2.4
    slam = lowpass(noise(dur, rng), 2500) * decay(dur, 0.06, 0.001)
    body = kick(dur, 110, 38, 0.6) + 0.5 * normalize(slam) + 0.25 * cymbal(rng, dur, 0.4)
    return reverb(saturate(body, 1.4), rng, rt60=2.2, wet=0.3)


# ── Comedy / meme ───────────────────────────────────────────────────────────


def _boing(base: float, dur: float) -> np.ndarray:
    t = times(dur)
    freq = (
        base
        * (1 + 0.5 * (1 - np.exp(-t / 0.05)))
        * (1 + 0.12 * np.exp(-t / 0.35) * np.sin(2 * np.pi * 14 * t))
    )
    twang = sweep_band(saw(freq, dur), lambda tt: 2000 * np.exp(-tt / 0.3) + 500, 1.0)
    return (sine(freq, dur) * 0.7 + normalize(twang) * 0.4) * decay(dur, 0.32, 0.002)


@recipe("boing")
def boing(rng: np.random.Generator) -> np.ndarray:
    return _boing(150, 0.9)


@recipe("boing_high")
def boing_high(rng: np.random.Generator) -> np.ndarray:
    return _boing(300, 0.55)


def _slide_whistle(rng: np.random.Generator, start: float, end: float, dur: float) -> np.ndarray:
    t = times(dur)
    freq = start * (end / start) ** (t / dur) * (1 + 0.012 * np.sin(2 * np.pi * 6 * t))
    return whistle_tone(freq, dur, rng, 0.15) * gate(dur, 0.03, 0.06)


@recipe("slide_whistle_up")
def slide_whistle_up(rng: np.random.Generator) -> np.ndarray:
    return _slide_whistle(rng, 500, 2000, 0.8)


@recipe("slide_whistle_down")
def slide_whistle_down(rng: np.random.Generator) -> np.ndarray:
    return _slide_whistle(rng, 2000, 450, 0.8)


@recipe("rimshot")
def rimshot(rng: np.random.Generator) -> np.ndarray:
    parts = [
        (0.0, snare(rng) * 0.8),
        (0.2, tom(110.0) * 0.9),
        (0.42, kick() * 0.9),
        (0.42, cymbal(rng, 1.4, 0.45) * 0.5),
    ]
    return reverb(place(parts), rng, rt60=0.6, wet=0.12)


@recipe("crickets")
def crickets(rng: np.random.Generator) -> np.ndarray:
    dur = 6.0

    def chirp(freq: float, pulses: int) -> np.ndarray:
        pulse = sine(freq, 0.012) * np.hanning(samples(0.012))
        return place([(0.022 * i, pulse) for i in range(pulses)])

    parts: list[tuple[float, np.ndarray]] = []
    for freq, pulses, period, offset, position in (
        (4600, 3, 0.55, 0.0, -0.4),
        (4200, 4, 0.8, 0.3, 0.5),
    ):
        at = offset
        while at < dur - 0.2:
            parts.append((at, pan(chirp(freq, pulses), position)))
            at += period + float(rng.uniform(-0.04, 0.04))
    night = stereo(lowpass(noise(dur, rng, "pink"), 1500) * 0.02)
    return reverb(place([*parts, (0.0, night)], dur), rng, rt60=0.6, wet=0.15)


@recipe("bonk")
def bonk(rng: np.random.Generator) -> np.ndarray:
    dur = 0.35
    t = times(dur)
    knock = sine(260 + 160 * np.exp(-t / 0.015), dur) * decay(dur, 0.06, 0.0005)
    hollow = bandpass(noise(dur, rng), 900, 0.3) * decay(dur, 0.03, 0.0005)
    return knock + 0.5 * normalize(hollow)


@recipe("clown_horn")
def clown_horn(rng: np.random.Generator) -> np.ndarray:
    def honk(freq: float) -> np.ndarray:
        dur = 0.22
        t = times(dur)
        pitch = freq * (1 - 0.12 * np.exp(-t / 0.02))
        reed = saw(pitch, dur) + saw(pitch * 1.006, dur)
        reed = peak_eq(peak_eq(lowpass(reed, 5000), 1100, 12, 0.5), 2600, 6, 0.5)
        return saturate(normalize(reed), 1.5) * gate(dur, 0.01, 0.03)

    return place([(0.0, honk(340)), (0.32, honk(360))])


@recipe("squeaky_toy")
def squeaky_toy(rng: np.random.Generator) -> np.ndarray:
    def squeak(base: float) -> np.ndarray:
        dur = 0.18
        t = times(dur)
        freq = (base + 700 * np.sin(np.pi * t / dur)) * (1 + 0.03 * np.sin(2 * np.pi * 25 * t))
        tone = sine(freq, dur) + 0.2 * lowpass(saw(freq, dur), 6000)
        return tone * np.sin(np.pi * t / dur) ** 0.5

    return place([(0.0, squeak(1900)), (0.24, squeak(2200))])


@recipe("air_horn")
def air_horn(rng: np.random.Generator) -> np.ndarray:
    def blast(dur: float) -> np.ndarray:
        horn = saw(370, dur) + saw(373, dur) + 0.3 * square(185, dur)
        horn = peak_eq(highpass(horn, 200), 1500, 8, 1.0)
        return saturate(normalize(horn), 3.0) * gate(dur, 0.008, 0.03)

    return place([(0.0, blast(0.18)), (0.25, blast(0.18)), (0.5, blast(0.7))])


# ── UI / text ───────────────────────────────────────────────────────────────


def _pop(dur: float, lo: float, hi: float, tau: float, bright: float = 0.0) -> np.ndarray:
    t = times(dur)
    freq = lo + hi * np.exp(-t / 0.011)
    tone = sine(freq, dur) + bright * sine(freq * 2, dur)
    return tone * decay(dur, tau, 0.0005)


def _bubble(dur: float, base: float) -> np.ndarray:
    t = times(dur)
    freq = base * (1 + 2.2 * (1 - np.exp(-t / 0.02)))
    return sine(freq, dur) * decay(dur, 0.03, 0.0008)


@recipe("pop_soft")
def pop_soft(rng: np.random.Generator) -> np.ndarray:
    return _pop(0.14, 350, 700, 0.035)


@recipe("pop_accent")
def pop_accent(rng: np.random.Generator) -> np.ndarray:
    return _pop(0.18, 500, 1100, 0.05, bright=0.25)


@recipe("bubble_pop")
def bubble_pop(rng: np.random.Generator) -> np.ndarray:
    return _bubble(0.12, 500)


@recipe("bubble_pops")
def bubble_pops(rng: np.random.Generator) -> np.ndarray:
    return place(
        [(0.0, _bubble(0.12, 460)), (0.09, _bubble(0.12, 620) * 0.8), (0.2, _bubble(0.12, 540))]
    )


@recipe("notification")
def notification(rng: np.random.Generator) -> np.ndarray:
    return place([(0.0, marimba(note("G5"), 0.4)), (0.1, marimba(note("C6"), 0.6))])


@recipe("notification_chime")
def notification_chime(rng: np.random.Generator) -> np.ndarray:
    chime = place([(0.0, bell(note("E6"), 0.6) * 0.7), (0.09, bell(note("B6"), 0.8) * 0.6)])
    return reverb(chime, rng, rt60=0.6, wet=0.12)


@recipe("message_sent")
def message_sent(rng: np.random.Generator) -> np.ndarray:
    t = times(0.25)
    blip = sine(800 * 2 ** np.clip(t / 0.08, 0, 1), 0.25) * decay(0.25, 0.05, 0.001)
    air = whoosh(rng, 0.18, 1500, 7000, peak_at=0.5, travel=0.3)
    return place([(0.0, air * 0.5), (0.02, stereo(blip))])


@recipe("message_received")
def message_received(rng: np.random.Generator) -> np.ndarray:
    return place([(0.0, marimba(note("E5"), 0.4)), (0.08, marimba(note("B5"), 0.5))])


# ── Sports ──────────────────────────────────────────────────────────────────


def _pea_whistle(rng: np.random.Generator, dur: float, freq: float = 2850.0) -> np.ndarray:
    t = times(dur)
    rate = 38 + np.cumsum(rng.standard_normal(len(t))) / SR * 40
    trill = np.sin(2 * np.pi * np.cumsum(rate) / SR)
    tone = whistle_tone(freq * (1 + 0.02 * trill), dur, rng, 0.25)
    return tone * (0.7 + 0.3 * trill) * gate(dur, 0.02, 0.06)


@recipe("referee_whistle_long")
def referee_whistle_long(rng: np.random.Generator) -> np.ndarray:
    return reverb(_pea_whistle(rng, 1.4), rng, rt60=0.9, wet=0.15)


@recipe("goal_horn")
def goal_horn(rng: np.random.Generator) -> np.ndarray:
    dur = 2.7
    t = times(dur)
    chord = sum(saw(f, dur) + saw(f * 1.004, dur) for f in (116.5, 146.8, 174.6)) / 6
    horn = saturate(normalize(lowpass(chord, 1600)), 1.8) * (1 + 0.05 * np.sin(2 * np.pi * 6 * t))
    return reverb(horn * gate(dur, 0.08, 0.3), rng, rt60=1.8, wet=0.3)


# ── Money / wins ────────────────────────────────────────────────────────────


@recipe("arcade_coin")
def arcade_coin(rng: np.random.Generator) -> np.ndarray:
    return seq([(note("A5"), 0.07), (note("E6"), 0.4)], lambda f, d: chip(f, d, 0.5, 0.12) * 0.6)


@recipe("slot_machine_win")
def slot_machine_win(rng: np.random.Generator) -> np.ndarray:
    pattern = [note(n) for n in ("C6", "E6", "G6", "C7")]
    dings = [(0.06 * i, bell(pattern[i % 4], 0.25) * 0.6) for i in range(30)]
    pings = [
        (float(rng.uniform(0, 1.8)), bell(float(rng.uniform(3000, 5000)), 0.12) * 0.25)
        for _ in range(20)
    ]
    return reverb(place([*dings, *pings]), rng, rt60=0.8, wet=0.15)


@recipe("jackpot")
def jackpot(rng: np.random.Generator) -> np.ndarray:
    run = [(note(n), 0.05) for n in ("C5", "E5", "G5", "C6", "E6", "G6") * 2]
    arpeggio = seq(run, lambda f, d: chip(f, d, 0.25) * 0.5)
    t = times(1.2)
    held = sum(
        square(note(n) * (1 + 0.008 * np.sin(2 * np.pi * 7 * t)), 1.2, 0.25)
        for n in ("C6", "E6", "G6")
    )
    held = held / 3 * decay(1.2, 0.6, 0.005) * 0.6
    return reverb(
        place([(0.0, arpeggio), (0.6, held), (0.6, _sparkle(rng, 16, 1.0) * 0.5)]), rng, 0.8, 0.12
    )
