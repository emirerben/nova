"""Synthetic tests for the KRI-374 song aligner (numpy only, deterministic)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.config import settings
from app.pipeline import song_alignment as sa
from app.schemas.user_song import SongWord

SR = 16_000


# --------------------------------------------------------------------------- #
# Synthetic material
# --------------------------------------------------------------------------- #


def make_song(seconds: float, seed: int = 0) -> np.ndarray:
    """Noise + harmonic "music" whose spectrum changes every block.

    Deliberately non-periodic (random spectral envelope and phase per block,
    wandering pitch) so it does not correlate with itself at other lags.
    """
    rng = np.random.default_rng(seed)
    block, hop, n_env = 1024, 512, 24
    n = int(seconds * SR)
    n_blocks = n // hop + 2
    n_bins = block // 2 + 1
    xs = np.linspace(0, n_env - 1, n_bins)
    weights = np.stack([np.interp(xs, np.arange(n_env), np.eye(n_env)[k]) for k in range(n_env)])
    env = rng.normal(size=(n_blocks, n_env)) @ weights
    mag = np.exp(1.3 * env)
    phase = rng.uniform(0, 2 * np.pi, size=(n_blocks, n_bins))
    frames = np.fft.irfft(mag * np.exp(1j * phase), block, axis=1) * np.hanning(block)
    noise = np.zeros(n_blocks * hop + block)
    for k in range(n_blocks):
        noise[k * hop : k * hop + block] += frames[k]
    noise = noise[:n]
    noise /= np.abs(noise).max()

    # Harmonic part: f0 wanders every 0.3 s, 6 harmonics, amplitude pulses.
    seg = int(0.3 * SR)
    f0 = np.repeat(rng.uniform(110, 420, size=n // seg + 1), seg)[:n]
    ph = 2 * np.pi * np.cumsum(f0) / SR
    tone = sum(np.sin(h * ph + rng.uniform(0, 6.28)) / h for h in range(1, 7))
    amp = np.repeat(rng.uniform(0.2, 1.0, size=n // seg + 1), seg)[:n]
    tone = tone * amp
    tone /= np.abs(tone).max()
    song = 0.7 * noise + 0.3 * tone
    return (song / np.abs(song).max() * 0.5).astype(np.float32)


def slice_take(song: np.ndarray, start_s: float, length_s: float) -> np.ndarray:
    a = int(round(start_s * SR))
    return song[a : a + int(length_s * SR)].copy()


def phone_speaker(x: np.ndarray) -> np.ndarray:
    """Band-pass 300 Hz - 4 kHz plus a +12 dB resonance near 1.2 kHz."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(x.shape[0], 1 / SR)
    gain = 1.0 / (1.0 + (300.0 / np.maximum(f, 1.0)) ** 4)
    gain = gain / (1.0 + (f / 4000.0) ** 4)
    gain = gain * (1.0 + 3.0 * np.exp(-(((f - 1200.0) / 120.0) ** 2)))
    return np.fft.irfft(spec * gain, x.shape[0]).astype(np.float32)


def add_noise(x: np.ndarray, snr_db: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    power = float(np.mean(x**2))
    sigma = np.sqrt(power / (10 ** (snr_db / 10)))
    return (x + rng.normal(0, sigma, x.shape[0])).astype(np.float32)


def add_singer(x: np.ndarray, level: float = 0.25) -> np.ndarray:
    t = np.arange(x.shape[0]) / SR
    vib = 6 * np.sin(2 * np.pi * 5.5 * t)
    voice = np.sin(2 * np.pi * (220 * t + vib / (2 * np.pi * 5.5) * 0)) + 0.5 * np.sin(
        2 * np.pi * 440 * t + vib
    )
    rms_x = float(np.sqrt(np.mean(x**2)))
    voice = voice / float(np.sqrt(np.mean(voice**2))) * rms_x * level
    return (x + voice).astype(np.float32)


def make_words(
    rng: np.random.Generator, seconds: float, repeat_at: tuple[float, float, float] | None = None
) -> list[SongWord]:
    vocab = [f"w{k}" for k in range(250)]
    words: list[SongWord] = []
    t = 0.5
    while t < seconds - 0.5:
        words.append(SongWord(text=vocab[int(rng.integers(len(vocab)))], start_s=t, end_s=t + 0.3))
        t += 0.45
    if repeat_at is not None:
        src, dst, length = repeat_at
        block = [w for w in words if src <= w.start_s < src + length]
        words = [w for w in words if not dst <= w.start_s < dst + length]
        for w in block:
            words.append(
                SongWord(text=w.text, start_s=w.start_s - src + dst, end_s=w.end_s - src + dst)
            )
        words.sort(key=lambda w: w.start_s)
    return words


def take_words(
    words: list[SongWord], start_s: float, length_s: float, lead_s: float = 0.0, seed: int = 3
) -> list[SongWord]:
    """Transcript of the take: jittered timings, ~10% of words dropped."""
    rng = np.random.default_rng(seed)
    out = []
    for w in words:
        if start_s <= w.start_s and w.end_s <= start_s + length_s and rng.random() > 0.1:
            j = float(rng.normal(0, 0.03))
            s = max(0.0, w.start_s - start_s + lead_s + j)
            out.append(SongWord(text=w.text, start_s=s, end_s=s + 0.3))
    return out


@pytest.fixture(scope="module")
def song120() -> np.ndarray:
    return make_song(120.0, seed=11)


@pytest.fixture(scope="module")
def spec120(song120: np.ndarray) -> sa.SongSpectrum:
    return sa.precompute_song(song120)


def _align(spec, take, *, words=(), song_words=(), **kw):
    return sa.align_take(spec, take, list(song_words), list(words), media_id="m", **kw)


# --------------------------------------------------------------------------- #
# Audio alignment
# --------------------------------------------------------------------------- #


def test_clean_slice_recovered_within_2ms_and_sign_convention(song120, spec120):
    take = slice_take(song120, 12.5, 15.0)
    res = _align(spec120, take)
    assert res.status == "confident"
    assert res.delta_s == pytest.approx(12.5, abs=0.002)
    # song_time = take_time + delta_s: take sample at t=3 s is song sample at 15.5 s.
    assert take[3 * SR] == song120[int(15.5 * SR)]
    assert res.peak_z >= settings.song_align_strong_peak_z
    assert res.peak_ratio >= settings.song_align_strong_peak_ratio
    assert 0.0 <= res.confidence <= 1.0


@pytest.mark.parametrize(
    "name,perturb",
    [
        ("phone_speaker", lambda x: phone_speaker(x)),
        ("gain", lambda x: x * 0.08),
        ("noise_0db", lambda x: add_noise(x, 0.0)),
        ("noise_-5db", lambda x: add_noise(x, -5.0)),
        ("singer", lambda x: add_singer(x, 0.8)),
        ("everything", lambda x: add_noise(add_singer(phone_speaker(x) * 0.3, 0.5), -3.0)),
    ],
)
def test_perturbed_takes_still_confident(song120, spec120, name, perturb):
    take = perturb(slice_take(song120, 47.3, 18.0))
    res = _align(spec120, take)
    assert res.status == "confident", (name, res)
    assert res.delta_s == pytest.approx(47.3, abs=0.002), name


def test_leading_silence_shifts_delta(song120, spec120):
    # The phone starts recording 2.5 s before the song reaches 61.0 s.
    take = np.concatenate([np.zeros(int(2.5 * SR), np.float32), slice_take(song120, 61.0, 14.0)])
    res = _align(spec120, take)
    assert res.status == "confident"
    assert res.delta_s == pytest.approx(61.0 - 2.5, abs=0.002)


def test_take_starting_before_the_song_has_negative_delta(song120, spec120):
    take = np.concatenate([np.zeros(int(1.5 * SR), np.float32), slice_take(song120, 0.0, 10.0)])
    res = _align(spec120, take)
    assert res.status == "confident"
    assert res.delta_s == pytest.approx(-1.5, abs=0.002)


def test_repeated_section_is_ambiguous_with_both_offsets():
    song = make_song(90.0, seed=5)
    a, b = int(20 * SR), int(40 * SR)
    song[b : b + 12 * SR] = song[a : a + 12 * SR]
    spec = sa.precompute_song(song)

    inside = _align(spec, slice_take(song, 22.0, 6.0))
    assert inside.status == "ambiguous"
    deltas = [alt.delta_s for alt in inside.alternates]
    assert any(abs(d - 22.0) < 0.005 for d in deltas)
    assert any(abs(d - 42.0) < 0.005 for d in deltas)
    assert len(inside.alternates) <= 4
    scores = [alt.score for alt in inside.alternates]
    assert scores == sorted(scores, reverse=True)

    # Spanning into unique material disambiguates.
    spanning = _align(spec, slice_take(song, 28.0, 12.0))
    assert spanning.status == "confident"
    assert spanning.delta_s == pytest.approx(28.0, abs=0.002)


def test_unrelated_audio_is_unmatched(spec120):
    other = make_song(15.0, seed=999)
    res = _align(spec120, other)
    assert res.status == "unmatched"
    assert res.delta_s is None
    assert res.confidence <= 0.3


def test_unrelated_audio_with_matching_lyrics_is_still_not_confident(spec120):
    # Text alone must never make a take confident.
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    other = make_song(15.0, seed=998)
    res = _align(
        spec120,
        other,
        words=take_words(words, 30.0, 15.0),
        song_words=words,
    )
    assert res.status != "confident"
    assert res.text_score >= 3


def test_time_stretched_take_is_not_confident(song120, spec120):
    take = slice_take(song120, 30.0, 40.0)
    t = np.arange(int(take.shape[0] / 1.01)) * 1.01
    stretched = np.interp(t, np.arange(take.shape[0]), take).astype(np.float32)
    res = _align(spec120, stretched)
    assert res.status != "confident"


def test_drift_check_fails_when_sub_windows_disagree(song120):
    # Second half of the take is cut 0.1 s later in the song: 100 ms of jump.
    take = np.concatenate([slice_take(song120, 30.0, 8.0), slice_take(song120, 38.1, 6.0)])
    assert not sa._drift_ok(song120, take, 30.0, SR, 0.04)
    assert sa._drift_ok(song120, slice_take(song120, 30.0, 14.0), 30.0, SR, 0.04)
    res = _align(sa.precompute_song(song120), take)
    assert res.status != "confident"


def test_silent_short_and_empty_takes_are_unmatched(spec120):
    for take in (
        np.zeros(10 * SR, np.float32),
        np.zeros(0, np.float32),
        np.full(20, 0.3, np.float32),
        np.full(5 * SR, np.nan, np.float32),
    ):
        res = _align(spec120, take)
        assert res.status == "unmatched"
        assert res.delta_s is None


def test_empty_song_is_unmatched():
    res = sa.align_take(np.zeros(0, np.float32), np.ones(3 * SR, np.float32), [], [], media_id="m")
    assert res.status == "unmatched"


def test_proxy_offset_applies_to_delta_not_scores(song120, spec120):
    take = slice_take(song120, 12.5, 15.0)
    base = _align(spec120, take)
    shifted_cfg = settings.model_copy(update={"song_alignment_proxy_offset_s": 0.037})
    shifted = _align(spec120, take, settings_obj=shifted_cfg)
    assert shifted.delta_s == pytest.approx(base.delta_s + 0.037, abs=1e-9)
    assert shifted.peak_z == pytest.approx(base.peak_z)
    assert shifted.peak_ratio == pytest.approx(base.peak_ratio)
    assert shifted.confidence == pytest.approx(base.confidence)


def test_proxy_offset_applies_to_alternates():
    song = make_song(60.0, seed=6)
    song[int(40 * SR) : int(50 * SR)] = song[int(20 * SR) : int(30 * SR)]
    spec = sa.precompute_song(song)
    take = slice_take(song, 22.0, 5.0)
    cfg = settings.model_copy(update={"song_alignment_proxy_offset_s": 0.05})
    res = _align(spec, take, settings_obj=cfg)
    assert res.status == "ambiguous"
    deltas = sorted(a.delta_s for a in res.alternates)
    assert deltas[0] == pytest.approx(22.05, abs=0.005)
    assert deltas[1] == pytest.approx(42.05, abs=0.005)


def test_confidence_is_monotone_in_peak_z_and_ratio():
    c = sa._confidence
    zs = [c(z, 3.0, settings) for z in (5, 8, 12, 20, 40, 400)]
    rs = [c(30.0, r, settings) for r in (1.0, 1.2, 1.5, 2.0, 3.0, 50.0)]
    assert zs == sorted(zs)
    assert rs == sorted(rs)
    assert all(0.0 <= v <= 1.0 for v in zs + rs)


# --------------------------------------------------------------------------- #
# Text evidence
# --------------------------------------------------------------------------- #


def test_text_candidates_find_offset_and_repeated_choruses():
    rng = np.random.default_rng(4)
    words = make_words(rng, 120.0, repeat_at=(20.0, 60.0, 12.0))
    cands = sa.text_candidates(take_words(words, 22.0, 8.0), words)
    deltas = sorted(d for d, _s, _k in cands)
    assert any(abs(d - 22.0) < 0.15 for d in deltas)
    assert any(abs(d - 62.0) < 0.15 for d in deltas)
    assert all(k >= 3 for _d, _s, k in cands)


def test_text_candidates_normalize_case_accents_and_punctuation():
    song = [
        SongWord(text=t, start_s=10 + i, end_s=10.5 + i)
        for i, t in enumerate(["Aşk", "OLUR,", "bana", "Gel!", "şimdi"])
    ]
    take = [
        SongWord(text=t, start_s=i, end_s=0.5 + i)
        for i, t in enumerate(["ask", "olur", "BANA", "gel"])
    ]
    # "Aşk" vs "ask": NFKD strips the cedilla-like mark, so these must match.
    cands = sa.text_candidates(take, song)
    assert cands and cands[0][0] == pytest.approx(10.0, abs=0.01)


def test_text_candidates_need_three_matches():
    song = [SongWord(text=t, start_s=i, end_s=i + 0.4) for i, t in enumerate("a b c d e".split())]
    take = [SongWord(text=t, start_s=i, end_s=i + 0.4) for i, t in enumerate("a b x".split())]
    assert sa.text_candidates(take, song) == []


def test_text_agreement_gates_confidence_when_audio_is_not_strong(song120, spec120):
    # Make the "strong audio" escape hatch unreachable so only text can confirm.
    cfg = settings.model_copy(
        update={"song_align_strong_peak_z": 1e9, "song_align_strong_peak_ratio": 1e9}
    )
    rng = np.random.default_rng(8)
    words = make_words(rng, 120.0)
    take = slice_take(song120, 40.0, 16.0)

    agree = _align(
        spec120, take, words=take_words(words, 40.0, 16.0), song_words=words, settings_obj=cfg
    )
    assert agree.status == "confident"
    assert agree.text_score >= 3

    no_text = _align(spec120, take, settings_obj=cfg)
    assert no_text.status != "confident"

    # Lyrics that point 7 s elsewhere disagree with the audio peak.
    wrong = take_words(words, 47.0, 16.0, lead_s=0.0)
    disagree = _align(spec120, take, words=wrong, song_words=words, settings_obj=cfg)
    assert disagree.status != "confident"
    assert disagree.confidence < agree.confidence


def test_text_disagreement_lowers_confidence_even_with_strong_audio(song120, spec120):
    rng = np.random.default_rng(9)
    words = make_words(rng, 120.0)
    take = slice_take(song120, 40.0, 16.0)
    agree = _align(spec120, take, words=take_words(words, 40.0, 16.0), song_words=words)
    disagree = _align(spec120, take, words=take_words(words, 47.0, 16.0), song_words=words)
    assert agree.confidence > disagree.confidence


# --------------------------------------------------------------------------- #
# Batch API + performance
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(120)
def test_align_takes_ten_takes_against_180s_song_is_fast():
    song = make_song(180.0, seed=21)
    rng = np.random.default_rng(22)
    words = make_words(rng, 180.0)
    starts = [5.0, 20.5, 33.3, 48.0, 62.7, 80.1, 97.4, 111.9, 130.2, 150.6]
    takes = {}
    for k, start in enumerate(starts):
        length = 20.0 + 2.0 * k
        take = add_noise(phone_speaker(slice_take(song, start, length)), 0.0, seed=k)
        takes[f"m{k}"] = (take, take_words(words, start, length, seed=k), 100 + k)
    takes["silent"] = (np.zeros(8 * SR, np.float32), [], None)

    t0 = time.perf_counter()
    result = sa.align_takes(song, takes, words, song_generation=7)
    elapsed = time.perf_counter() - t0

    assert result.song_generation == 7
    for k, start in enumerate(starts):
        row = result.takes[f"m{k}"]
        assert row.status == "confident", (k, row)
        assert row.delta_s == pytest.approx(start, abs=0.002)
        assert row.proxy_generation == 100 + k
    assert result.takes["silent"].status == "unmatched"
    # Target is < 10 s on a laptop; the ceiling is generous so CI never flakes.
    assert elapsed < 20.0, f"10 takes took {elapsed:.1f}s"
