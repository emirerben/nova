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


def test_unrelated_audio_never_places_confidently(spec120):
    # KRI-471: evidence is a likelihood, not a gate. Unrelated audio may leave a faint
    # noise peak, but it must stay far below the "ask the creator" bar.
    other = make_song(15.0, seed=999)
    res = _align(spec120, other)
    assert res.status != "confident"
    assert res.likelihood < settings.song_align_ask_likelihood
    assert all(c.method == "audio" and c.likelihood < 0.2 for c in res.candidates)
    assert res.confidence <= 0.3


def test_singing_without_song_audio_is_placed_by_lyrics(spec120):
    # KRI-466: sung over earbuds -> the take holds the singer, not the song.
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    other = make_song(15.0, seed=998)
    res = _align(
        spec120,
        other,
        words=take_words(words, 30.0, 15.0),
        song_words=words,
    )
    assert res.status == "confident"
    assert res.method == "lyrics"
    assert res.delta_s == pytest.approx(30.0, abs=0.1)
    assert res.text_score >= 3
    assert res.match_start_s is not None and res.match_end_s is not None
    assert 0.0 <= res.match_start_s < res.match_end_s <= 15.0


def test_few_lyric_words_give_lower_likelihood_not_unmatched(spec120):
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    few = take_words(words, 30.0, 15.0)[:4]
    res = _align(spec120, make_song(15.0, seed=998), words=few, song_words=words)
    full = _align(
        spec120, make_song(15.0, seed=998), words=take_words(words, 30.0, 15.0), song_words=words
    )
    # KRI-471: few words are weaker evidence (lower likelihood), not "unmatched".
    assert res.candidates and res.delta_s == pytest.approx(30.0, abs=0.2)
    assert full.likelihood > res.likelihood


def test_lyrics_with_inconsistent_word_offsets_are_not_confident(spec120):
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    jitter = np.random.default_rng(5)
    shaky = [
        SongWord(
            text=w.text,
            start_s=max(0.0, w.start_s + float(jitter.normal(0, 1.0))),
            end_s=max(0.3, w.start_s + 0.3),
        )
        for w in take_words(words, 30.0, 15.0)
    ]
    res = _align(spec120, make_song(15.0, seed=998), words=shaky, song_words=words)
    assert res.status != "confident"


def test_sparse_lyric_matches_inside_speech_are_not_confident(spec120):
    # Song words interleaved with lots of unrelated speech must not place a take.
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    sung = take_words(words, 30.0, 15.0)[:6]
    mixed: list[SongWord] = []
    for k, w in enumerate(sung):
        mixed.append(w)
        for j in range(3):
            t = w.start_s + 0.1 * (j + 1)
            mixed.append(SongWord(text=f"x{k}_{j}", start_s=t, end_s=t + 0.05))
    res = _align(spec120, make_song(15.0, seed=998), words=mixed, song_words=words)
    assert res.status != "confident"


def test_lyrics_over_a_repeated_chorus_are_ambiguous_with_every_offset():
    song = make_song(60.0, seed=6)
    rng = np.random.default_rng(4)
    words = make_words(rng, 60.0, repeat_at=(20.0, 40.0, 10.0))
    spec = sa.precompute_song(song)
    res = _align(
        spec,
        make_song(8.0, seed=997),
        words=take_words(words, 22.0, 6.0),
        song_words=words,
    )
    assert res.status == "ambiguous"
    assert res.method == "lyrics"
    deltas = sorted([res.delta_s, *[a.delta_s for a in res.alternates]])
    assert any(abs(d - 22.0) < 0.2 for d in deltas)
    assert any(abs(d - 42.0) < 0.2 for d in deltas)


def test_lyrics_path_respects_the_kill_switch(spec120):
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    cfg = settings.model_copy(update={"song_align_lyrics_enabled": False})
    res = _align(
        spec120,
        make_song(15.0, seed=998),
        words=take_words(words, 30.0, 15.0),
        song_words=words,
        settings_obj=cfg,
    )
    assert all(c.method == "audio" for c in res.candidates)  # no lyric candidate at all
    assert res.likelihood < settings.song_align_ask_likelihood


def test_audio_placement_wins_over_disagreeing_lyrics(song120, spec120):
    rng = np.random.default_rng(8)
    words = make_words(rng, 120.0)
    take = slice_take(song120, 40.0, 16.0)
    res = _align(spec120, take, words=take_words(words, 70.0, 16.0), song_words=words)
    assert res.method == "audio"
    assert res.delta_s == pytest.approx(40.0, abs=0.01)


def test_audio_match_range_spans_only_agreeing_windows(song120, spec120):
    # 6 s of unrelated sound, then 12 s of the song: the match starts after the lead-in.
    lead = make_song(6.0, seed=321)
    take = np.concatenate([lead, slice_take(song120, 50.0, 12.0)])
    res = _align(spec120, take)
    assert res.method == "audio"
    assert res.status == "confident"
    assert res.delta_s == pytest.approx(44.0, abs=0.01)
    # Padded by one window so a quiet edge window never costs footage; never past the lead-in.
    assert res.match_start_s is not None and 0.0 <= res.match_start_s <= 6.0
    assert res.match_end_s == pytest.approx(18.0, abs=0.1)


def test_proxy_offset_does_not_move_the_match_range(song120, spec120):
    take = slice_take(song120, 12.5, 15.0)
    base = _align(spec120, take)
    cfg = settings.model_copy(update={"song_alignment_proxy_offset_s": 0.05})
    shifted = _align(spec120, take, settings_obj=cfg)
    assert shifted.match_start_s == base.match_start_s
    assert shifted.match_end_s == base.match_end_s


def test_time_stretched_take_is_not_confident(song120, spec120):
    take = slice_take(song120, 30.0, 40.0)
    t = np.arange(int(take.shape[0] / 1.01)) * 1.01
    stretched = np.interp(t, np.arange(take.shape[0]), take).astype(np.float32)
    res = _align(spec120, stretched)
    clean = _align(spec120, slice_take(song120, 30.0, 40.0))
    # The drift check no longer rejects: it cuts the likelihood (x0.4) and drops the range.
    assert res.candidates
    assert res.likelihood <= clean.likelihood * 0.5
    assert res.match_start_s is None


def test_drift_check_fails_when_sub_windows_disagree(song120):
    # Second half of the take is cut 0.1 s later in the song: 100 ms of jump.
    take = np.concatenate([slice_take(song120, 30.0, 8.0), slice_take(song120, 38.1, 6.0)])
    assert not sa._drift_ok(song120, take, 30.0, SR, 0.04)
    assert sa._drift_ok(song120, slice_take(song120, 30.0, 14.0), 30.0, SR, 0.04)
    res = _align(sa.precompute_song(song120), take)
    clean = _align(sa.precompute_song(song120), slice_take(song120, 30.0, 14.0))
    assert res.candidates and res.candidates[0].delta_s == pytest.approx(30.0, abs=0.01)
    assert res.likelihood <= clean.likelihood * 0.5


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


def test_text_agreement_merges_with_audio_and_disagreement_lowers_it(song120, spec120):
    # A weaker (repeat-free, mid-strength) audio peak: shorten + noise the take.
    rng = np.random.default_rng(8)
    words = make_words(rng, 120.0)
    take = add_noise(slice_take(song120, 40.0, 5.0), 0.0, seed=4)

    agree = _align(spec120, take, words=take_words(words, 40.0, 5.0), song_words=words)
    assert agree.text_score >= 3
    assert agree.candidates[0].method in ("both", "lyrics")

    no_text = _align(spec120, take)
    wrong = take_words(words, 47.0, 5.0, lead_s=0.0)
    disagree = _align(spec120, take, words=wrong, song_words=words)
    assert agree.likelihood >= no_text.likelihood
    assert disagree.confidence <= agree.confidence


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


# --------------------------------------------------------------------------- #
# KRI-374 review: repeats, unrelated takes, long lyrics
# --------------------------------------------------------------------------- #


def _loop_song(seconds: float = 60.0, loop_s: float = 4.0, seed: int = 31):
    """A song that is one ``loop_s`` loop repeated, lyrics repeated with it."""
    loop = make_song(loop_s, seed=seed)
    reps = int(round(seconds / loop_s))
    song = np.tile(loop, reps)
    rng = np.random.default_rng(seed)
    base = make_words(rng, loop_s)
    words = [
        SongWord(text=w.text, start_s=w.start_s + k * loop_s, end_s=w.end_s + k * loop_s)
        for k in range(reps)
        for w in base
    ]
    return song, words


def test_a_looped_song_with_repeating_lyrics_is_never_confident_at_one_repetition():
    """Probe: text agreed with *some* candidate, so noise let 2 of 12 takes through at a
    random repetition. With repeats present the strong audio rule must decide."""
    song, words = _loop_song()
    spec = sa.precompute_song(song)
    wrong = []
    for k in range(12):
        start = 3.0 + 4.0 * (k % 10) + 0.37 * k
        take = add_noise(slice_take(song, start, 5.0), 20.0, seed=100 + k)
        res = _align(spec, take, words=take_words(words, start, 5.0, seed=k), song_words=words)
        if res.status == "confident":
            wrong.append((k, start, res.delta_s))
    assert wrong == []


_SCALE = [220 * 2 ** (k / 12) for k in (0, 2, 4, 7, 9, 12, 14, 16)]


def make_tonal(seconds: float, seed: int, note_s: float = 0.25) -> np.ndarray:
    """Pure-tone "music": random scale notes with harmonics (heavy self-similarity)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    seg = int(note_s * SR)
    out = np.zeros(n)
    tt = np.arange(seg) / SR
    for k in range(n // seg + 1):
        f = _SCALE[int(rng.integers(len(_SCALE)))]
        tone = sum(np.sin(2 * np.pi * f * h * tt) / h for h in range(1, 5)) * np.hanning(seg) ** 0.3
        a, b = k * seg, min(n, k * seg + seg)
        out[a:b] += tone[: b - a]
    return (out / np.abs(out).max() * 0.5).astype(np.float32)


def test_an_unrelated_take_against_tonal_music_has_no_strong_candidate():
    """Probe: strong peaks from coincidental note matches were offered as 'ambiguous'
    placements. They may survive as faint candidates now, never as evidence to trust."""
    song = make_tonal(90.0, seed=1)
    spec = sa.precompute_song(song)
    take = make_tonal(12.0, seed=104)  # a different tune on the same scale
    res = _align(spec, take)
    assert res.status != "confident"
    assert res.likelihood < settings.song_align_ask_likelihood


def test_a_chorus_sharing_85_percent_stays_ambiguous_with_every_offset():
    song = make_song(90.0, seed=6)
    a, b = int(20 * SR), int(50 * SR)
    shared = int(0.85 * 12 * SR)
    song[b : b + shared] = song[a : a + shared]  # the last 15% is new material
    spec = sa.precompute_song(song)
    res = _align(spec, slice_take(song, 21.0, 6.0))
    assert res.status == "ambiguous"
    deltas = sorted(alt.delta_s for alt in res.alternates)
    assert any(abs(d - 21.0) < 0.01 for d in deltas)
    assert any(abs(d - 51.0) < 0.01 for d in deltas)


def test_repeat_similarity_threshold_is_a_setting():
    assert settings.song_align_repeat_similarity_min == 0.5


def _rap_words(count: int, seconds: float, seed: int = 22) -> list[SongWord]:
    rng = np.random.default_rng(seed)
    step = (seconds - 1.0) / count
    return [
        SongWord(text=f"w{int(rng.integers(250))}", start_s=0.5 + i * step, end_s=0.7 + i * step)
        for i in range(count)
    ]


@pytest.mark.timeout(120)
def test_a_ten_minute_song_keeps_its_late_lyrics_and_stays_fast():
    """~2400 words used to be cut at 1500, silently dropping the last 40% of the song."""
    song = make_song(600.0, seed=21)
    words = _rap_words(2400, 600.0)
    starts = [5.0, 60.5, 120.3, 180.0, 240.7, 300.1, 360.4, 420.9, 500.2, 560.6]
    takes = {}
    for k, start in enumerate(starts):
        length = 40.0 + 2.0 * k
        take = add_noise(phone_speaker(slice_take(song, start, length)), 0.0, seed=k)
        takes[f"m{k}"] = (take, take_words(words, start, length, seed=k), k)

    t0 = time.perf_counter()
    result = sa.align_takes(song, takes, words, song_generation=1)
    elapsed = time.perf_counter() - t0

    for k, start in enumerate(starts):
        row = result.takes[f"m{k}"]
        assert row.status == "confident", (k, row)
        assert row.delta_s == pytest.approx(start, abs=0.002)
        assert row.text_score >= 3, (k, "late lyrics must reach the text aligner")
    assert elapsed < 30.0, f"10 takes x 10 min took {elapsed:.1f}s"


def test_a_take_beyond_the_truncated_lyrics_is_not_penalised_for_disagreeing(
    song120, spec120, monkeypatch
):
    rng = np.random.default_rng(31)
    words = make_words(rng, 120.0, repeat_at=(10.0, 90.0, 10.0))  # chorus at 10 s and 90 s
    take = slice_take(song120, 90.0, 10.0)
    chorus = take_words(words, 90.0, 10.0, seed=2)

    full = _align(spec120, take, words=chorus, song_words=words)
    assert full.status == "confident"

    # Cap the lyrics at the first 60 s: the 90 s chorus is no longer visible to text,
    # so the only text candidate points at 10 s and "disagrees" with the audio.
    kept = sum(1 for w in words if w.start_s < 60.0)
    monkeypatch.setattr(sa, "_TEXT_MAX_SONG_WORDS", kept)
    sa._TRUNCATION_LOGGED.clear()
    assert sa.song_text_coverage_s(words) is not None
    capped = _align(spec120, take, words=chorus, song_words=words)
    assert capped.status == "confident"
    assert capped.confidence == pytest.approx(full.confidence)


def test_song_text_coverage_is_none_when_nothing_is_dropped():
    assert sa.song_text_coverage_s(_rap_words(2400, 600.0)) is None
    assert sa.song_text_coverage_s(_rap_words(4100, 600.0)) is not None


def test_chatter_of_common_words_stays_weak_evidence(spec120):
    # KRI-466 review: chance chains of common words must not read as singing.
    rng = np.random.default_rng(11)
    vocab = [f"c{k}" for k in range(12)]  # a tiny, repetitive "lyric" vocabulary
    song_words = [
        SongWord(text=vocab[int(rng.integers(12))], start_s=0.5 + 0.45 * k, end_s=0.8 + 0.45 * k)
        for k in range(250)
    ]
    placed = 0
    for seed in range(40):
        r = np.random.default_rng(100 + seed)
        chatter = [
            SongWord(text=vocab[int(r.integers(12))], start_s=0.4 * k, end_s=0.4 * k + 0.2)
            for k in range(14)
        ]
        res = _align(spec120, make_song(6.0, seed=900 + seed), words=chatter, song_words=song_words)
        placed += res.status == "confident"
        # Chance chains of a 12-word vocabulary can look like singing; they must stay
        # weak evidence (the creator is asked), never a near-certain placement.
        assert res.likelihood < 0.6
    assert placed <= 6


def test_short_tokens_never_place_a_take_by_lyrics(spec120):
    # Single-character tokens (CJK per-character output) carry no placement evidence.
    song_words = [
        SongWord(text=ch, start_s=0.5 + 0.4 * k, end_s=0.8 + 0.4 * k)
        for k, ch in enumerate("天地玄黄宇宙洪荒日月盈昃辰宿列张" * 6)
    ]
    take = [
        SongWord(text=w.text, start_s=w.start_s - 2.0, end_s=w.end_s - 2.0)
        for w in song_words[5:21]
    ]
    res = _align(spec120, make_song(7.0, seed=996), words=take, song_words=song_words)
    assert res.status != "confident"


# --------------------------------------------------------------------------- #
# KRI-471: likelihood placement
# --------------------------------------------------------------------------- #


def _tm(matched, spread, density, song_density, inlier, long):
    return sa._TextMatch(
        delta_s=0.0,
        score=2.0 * matched,
        matched=matched,
        spread_s=spread,
        take_start_s=0.0,
        take_end_s=5.0,
        density=density,
        song_density=song_density,
        inlier_frac=inlier,
        long_matched=long,
    )


def test_likelihood_ranks_real_matches_above_weak_ones_on_the_prod_stats():
    # (matched, spread, density, song density, inlier frac, long words) from job aed98bf6.
    real = {
        "t1": _tm(5, 0.14, 1.0, 1.0, 1.0, 3),
        "t3": _tm(7, 0.08, 1.0, 1.0, 0.71, 5),
        "t4": _tm(9, 0.06, 0.75, 0.69, 0.89, 7),
        "t8": _tm(8, 0.07, 0.89, 0.89, 0.75, 6),
    }
    weak = {
        "t5": _tm(3, 0.0, 0.75, 0.75, 1.0, 1),
        "t7_a": _tm(4, 0.16, 1.0, 1.0, 0.75, 2),
        "t7_wide": _tm(4, 1.68, 1.0, 1.0, 0.0, 2),
    }
    for name, m in real.items():
        assert sa._lyric_likelihood(m, settings) > 0.4, name
    for name, m in weak.items():
        assert sa._lyric_likelihood(m, settings) < 0.35, name
    assert sa._lyric_likelihood(weak["t7_wide"], settings) < settings.song_align_candidate_floor


def test_no_words_and_noise_audio_leaves_no_candidates(spec120):
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 0.1, 10 * SR).astype(np.float32)
    res = _align(spec120, noise)
    assert res.status == "unmatched"
    assert res.candidates == []
    assert res.likelihood == 0.0 and res.margin is None
    assert "candidates" not in res.model_dump(mode="json")


def test_exact_chorus_repeats_tie_with_near_zero_margin():
    song = make_song(60.0, seed=6)
    rng = np.random.default_rng(4)
    words = make_words(rng, 60.0, repeat_at=(20.0, 40.0, 10.0))
    res = _align(
        sa.precompute_song(song),
        make_song(8.0, seed=997),
        words=take_words(words, 22.0, 6.0),
        song_words=words,
    )
    top = res.candidates[:2]
    assert {round(c.delta_s) for c in top} == {22, 42}
    assert res.margin is not None and res.margin < settings.song_align_ask_margin
    assert res.status == "ambiguous"


def test_candidates_are_ranked_and_margin_is_relative_to_the_best(song120, spec120):
    rng = np.random.default_rng(8)
    words = make_words(rng, 120.0)
    take = slice_take(song120, 40.0, 16.0)
    res = _align(spec120, take, words=take_words(words, 70.0, 16.0), song_words=words)
    likes = [c.likelihood for c in res.candidates]
    assert likes == sorted(likes, reverse=True)
    assert res.likelihood == likes[0]
    assert res.margin == pytest.approx((likes[0] - likes[1]) / likes[0])
    assert len(res.candidates) <= settings.song_align_candidates_max


def test_random_chatter_does_not_outrank_real_match_for_same_take(spec120):
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    real = take_words(words, 30.0, 4.0)[:7]
    alone = _align(spec120, make_song(15.0, seed=998), words=real, song_words=words)
    # A 4-word chain of the same take's words elsewhere in the song, with loose timing.
    other = [w for w in words if 80.0 <= w.start_s < 84.0][:4]
    chatter = [
        SongWord(
            text=w.text,
            start_s=real[-1].end_s + 0.5 + 0.9 * k,
            end_s=real[-1].end_s + 0.8 + 0.9 * k,
        )
        for k, w in enumerate(other)
    ]
    both = _align(spec120, make_song(15.0, seed=998), words=real + chatter, song_words=words)
    assert both.delta_s == pytest.approx(30.0, abs=0.2)
    assert both.margin is not None and both.margin >= 0.3
    real_like = next(c.likelihood for c in both.candidates if abs(c.delta_s - 30.0) < 0.3)
    alone_like = next(c.likelihood for c in alone.candidates if abs(c.delta_s - 30.0) < 0.3)
    assert real_like >= alone_like * 0.8  # the extra chatter words only dilute density a little


def test_a_candidate_below_the_floor_is_dropped_and_none_means_unmatched(spec120):
    cfg = settings.model_copy(update={"song_align_candidate_floor": 0.99})
    rng = np.random.default_rng(2)
    words = make_words(rng, 120.0)
    res = _align(
        spec120,
        make_song(15.0, seed=998),
        words=take_words(words, 30.0, 15.0)[:4],
        song_words=words,
        settings_obj=cfg,
    )
    assert res.status == "unmatched" and res.candidates == [] and res.delta_s is None


def test_proxy_offset_shifts_every_candidate():
    song = make_song(60.0, seed=6)
    song[int(40 * SR) : int(50 * SR)] = song[int(20 * SR) : int(30 * SR)]
    spec = sa.precompute_song(song)
    cfg = settings.model_copy(update={"song_alignment_proxy_offset_s": 0.05})
    res = _align(spec, slice_take(song, 22.0, 5.0), settings_obj=cfg)
    assert sorted(round(c.delta_s, 2) for c in res.candidates[:2]) == [22.05, 42.05]
