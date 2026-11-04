#!/usr/bin/env python3
"""Calibrate the ``audio_quality`` clip signal on a labeled set of synthetic clips.

    generate              write the committed fit set (30 AAC clips + labels.json)
    fit [--write]         fit the wind / crowd / clipping thresholds on the fit set
    check                 fail when the thresholds in audio_quality.py drifted from the fixtures
    report                precision/recall per class on the fit set AND a fresh held-out set,
                          written to .chip-eval/audio-calibration.md

The clips are synthesized with numpy (seeded, deterministic) and encoded to AAC with ffmpeg so the
measurement sees the same codec the analysis proxy uses. They prove the measurement separates the
failure modes we could think of; they do NOT prove real-phone performance. To add reality, put
real clips in a folder with ``labels.json`` (``{"file.m4a": "wind|crowd|clean|music|clipped|silence"}``)
and pass ``--real-dir``: ``report`` scores them separately and ``fit --include-real`` fits on them.

No network, no model.
"""

# ruff: noqa: E501  (report prose is long string literals)
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src/apps/api"
sys.path.insert(0, str(API))

from app.services.clip_signals import audio_quality as aq  # noqa: E402

FIXTURES = API / "tests/fixtures/clip_signals/audio"
REPORT = ROOT / ".chip-eval/audio-calibration.md"
MODULE = API / "app/services/clip_signals/audio_quality.py"
LABELS = ("clean", "wind", "crowd", "music", "clipped", "silence")
NOISY_LABELS = frozenset({"wind", "crowd"})
FIT_SEED_BASE = 1_000
HELDOUT_SEED_BASE = 90_000
MUTE_PRECISION_BAR = 0.9
SR = 44_100

# --------------------------------------------------------------------------------------------
# synthesis
# --------------------------------------------------------------------------------------------

_VOWELS = (
    (700, 1200, 2600),
    (300, 2200, 3000),
    (400, 800, 2500),
    (500, 1800, 2500),
    (300, 900, 2200),
)


def _fft_filter(x: np.ndarray, gain) -> np.ndarray:
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1 / SR)
    return np.fft.irfft(spec * gain(freqs), n=x.size)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2)) + 1e-12)


def _scale_to_db(x: np.ndarray, db: float) -> np.ndarray:
    return x * (10 ** (db / 20) / _rms(x))


def _voice(rng, n: int, f0_base: float, pause_scale: float, continuous: bool) -> np.ndarray:
    out = np.zeros(n)
    t = 0
    while t < n:
        syl = int(rng.uniform(0.10, 0.28) * SR)
        seg = min(syl, n - t)
        f0 = f0_base * (1 + rng.uniform(-0.12, 0.12))
        glide = rng.uniform(-0.15, 0.15)
        tt = np.arange(seg) / SR
        inst = f0 * (1 + glide * tt / (syl / SR))
        phase = 2 * np.pi * np.cumsum(inst) / SR
        formants = _VOWELS[int(rng.integers(len(_VOWELS)))]
        voiced = np.zeros(seg)
        for h in range(1, int(4000 // f0) + 1):
            fh = h * f0
            res = sum(math.exp(-0.5 * ((fh - f) / (0.18 * f + 60)) ** 2) for f in formants)
            voiced += (res / h**0.7) * np.sin(h * phase + rng.uniform(0, 6.28))
        fric = np.diff(rng.standard_normal(seg + 1)) * 0.12
        envelope = np.hanning(seg) if seg > 4 else np.ones(seg)
        out[t : t + seg] += (voiced + fric * np.hanning(seg) ** 4) * envelope
        gap = 0.0 if continuous else rng.uniform(0.05, 0.35) * pause_scale
        t += seg + int(gap * SR) + (int(rng.uniform(0, 0.03) * SR) if continuous else 0)
    return out


def speech(rng, n: int, f0: float, pause_scale: float = 1.0) -> np.ndarray:
    return _voice(rng, n, f0, pause_scale, continuous=False)


def wind(rng, n: int, fc: float, gust_sigma: float) -> np.ndarray:
    noise = rng.standard_normal(n)
    rumble = _fft_filter(noise, lambda f: 1 / (1 + (f / fc) ** 2))
    gust = _fft_filter(rng.standard_normal(n), lambda f: 1 / (1 + (f / rng.uniform(1.0, 2.5)) ** 4))
    gust = gust / (np.std(gust) + 1e-12)
    return rumble * np.exp(gust_sigma * gust)


def babble(rng, n: int, voices: int) -> np.ndarray:
    mix = sum(_voice(rng, n, rng.uniform(90, 260), 1.0, continuous=True) for _ in range(voices))
    mix = _fft_filter(mix, lambda f: np.where((f > 150) & (f < 5000), 1.0, 0.05))
    diffuse = _fft_filter(rng.standard_normal(n), lambda f: 1 / np.sqrt(np.maximum(f, 40.0)))
    return mix / _rms(mix) + 0.25 * diffuse / _rms(diffuse)


def steady_noise(rng, n: int, color: str) -> np.ndarray:
    noise = rng.standard_normal(n)
    if color == "traffic":
        return _fft_filter(noise, lambda f: 1 / (1 + (f / 900) ** 2) / np.sqrt(np.maximum(f, 30.0)))
    if color == "pink":
        return _fft_filter(noise, lambda f: 1 / np.sqrt(np.maximum(f, 30.0)))
    return noise  # hiss


def bass_music(rng, n: int, bpm: float, variant: str) -> np.ndarray:
    beat = 60.0 / bpm
    t = np.arange(n) / SR
    out = np.zeros(n)
    notes = (41.2, 55.0, 49.0, 62.0)
    n_beats = int(n / SR / beat) + 1
    for b in range(n_beats):
        start = int(b * beat * SR)
        if start >= n:
            break
        length = min(int(beat * SR), n - start)
        tt = t[:length]
        f = notes[b % len(notes)] * rng.choice([1.0, 1.0, 2.0])
        if variant in ("sub", "full", "full_speech", "hard_bass"):
            kick = np.sin(2 * np.pi * (45 * tt + 75 * (1 - np.exp(-tt * 30)) / 30)) * np.exp(
                -tt / 0.12
            )
            out[start : start + length] += 1.0 * kick
        if variant in ("bassline", "full", "full_speech", "sub", "hard_bass"):
            note = (
                np.sin(2 * np.pi * f * tt)
                + 0.3 * np.sin(4 * np.pi * f * tt)
                + 0.1 * np.sin(6 * np.pi * f * tt)
            )
            gate = np.minimum(1.0, (beat - tt) / 0.05)
            out[start : start + length] += 0.8 * note * np.clip(gate, 0, 1)
        if variant in ("full", "full_speech"):
            for half in (0, 1):
                s2 = start + int(half * beat * SR / 2)
                m = min(int(0.05 * SR), n - s2)
                if m > 0:
                    hat = np.diff(rng.standard_normal(m + 1), 2 if m > 3 else 1)
                    hat = np.pad(hat, (0, m - hat.size))
                    out[s2 : s2 + m] += 0.1 * hat * np.exp(-np.arange(m) / (0.015 * SR))
            if b % 2 == 1:
                m = min(int(0.15 * SR), n - start)
                snare = _fft_filter(
                    rng.standard_normal(m), lambda f: np.where((f > 1000) & (f < 5000), 1.0, 0.05)
                )
                out[start : start + m] += (
                    0.2 * snare / _rms(snare) * 0.3 * np.exp(-np.arange(m) / (0.05 * SR))
                )
    if variant == "hard_bass":
        # saturated 808 over a steady low-passed noise bed: noise-like low band, but it still pulses
        out = np.tanh(3.0 * out)
        bed = _fft_filter(rng.standard_normal(n), lambda f: 1 / (1 + (f / 160) ** 2))
        out = out + 0.4 * _rms(out) * bed / _rms(bed)
    if variant in ("full", "full_speech"):
        for freq in (220.0, 277.2, 329.6):
            out += 0.06 * np.sin(2 * np.pi * freq * t) + 0.02 * np.sin(4 * np.pi * freq * t)
    return out


# --------------------------------------------------------------------------------------------
# labeled clip specs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipSpec:
    name: str
    label: str
    seed: int
    params: dict[str, Any]

    def render(self) -> tuple[np.ndarray, int, dict[str, Any]]:
        """``(samples (n,), channels, encode)``; deterministic in ``seed`` and ``params``."""
        rng = np.random.default_rng(self.seed)
        p = self.params
        n = int(p["dur"] * SR)
        label = self.label
        if label == "silence":
            mode = p["mode"]
            x = np.zeros(n) if mode == "zeros" else _scale_to_db(rng.standard_normal(n), p["db"])
        elif label == "clean":
            x = _scale_to_db(speech(rng, n, p["f0"]), p["level_db"])
            x = x + _scale_to_db(steady_noise(rng, n, "pink"), p["room_db"])
        elif label == "wind":
            gusts = wind(rng, n, p["fc"], p["gust"])
            if p["pure"]:
                x = _scale_to_db(gusts, p["level_db"])
            else:
                x = _scale_to_db(speech(rng, n, p["f0"]), p["level_db"])
                x = x + _scale_to_db(gusts, p["level_db"] - p["snr_db"])
        elif label == "crowd":
            if p["kind"] == "babble":
                bed = babble(rng, n, p["voices"])
            else:
                bed = steady_noise(rng, n, p["kind"])
            if p["pure"]:
                x = _scale_to_db(bed, p["level_db"])
            else:
                x = _scale_to_db(speech(rng, n, p["f0"]), p["level_db"])
                x = x + _scale_to_db(bed, p["level_db"] - p["snr_db"])
        elif label == "music":
            x = _scale_to_db(bass_music(rng, n, p["bpm"], p["variant"]), p["level_db"])
            if p["variant"] == "full_speech":
                x = x + _scale_to_db(speech(rng, n, p["f0"]), p["level_db"] - 4)
        elif label == "clipped":
            base = speech(rng, n, p["f0"])
            x = np.clip(base / np.max(np.abs(base)) * p["drive"], -1.0, 1.0) * 0.99997
        else:
            raise ValueError(label)
        if label != "clipped":
            peak = np.max(np.abs(x))
            if peak > 0.89:
                x = x * (0.89 / peak)
        encode = {"bitrate": p["bitrate"], "sr": p["sr"], "channels": p["channels"]}
        return x, p["channels"], encode


def _draw(rng, wide: bool, label: str, idx: int) -> dict[str, Any]:
    stretch = 1.5 if wide else 1.0
    base = {
        "dur": float(rng.uniform(2.5, 6.0) if wide else 3.0),
        "channels": 2 if (idx % 3 == 2) else 1,
        "sr": int(rng.choice([44100, 48000]) if wide else 44100),
        "bitrate": int(rng.choice([32, 48, 64, 96]) if wide else 40) * 1000,
    }
    base["bitrate"] *= base["channels"]
    f0 = float(rng.choice([95, 110, 140, 180, 210, 240]) if wide else [95, 120, 180, 220][idx % 4])
    level = float(rng.uniform(-30, -14) if wide else rng.uniform(-26, -16))
    if label == "clean":
        return {**base, "f0": f0, "level_db": level, "room_db": float(rng.uniform(-66, -46))}
    if label == "wind":
        pure = idx % 6 >= 4
        return {
            **base,
            "f0": f0,
            "level_db": level,
            "pure": pure,
            "snr_db": float(rng.uniform(-6 * stretch, 10 * stretch)),
            "fc": float(rng.uniform(110, 240)),
            "gust": float(rng.uniform(0.8, 1.5)),
        }
    if label == "crowd":
        kinds = ["babble", "babble", "babble", "babble", "traffic", "hiss"]
        kind = kinds[idx % len(kinds)]
        return {
            **base,
            "f0": f0,
            "level_db": level,
            "kind": kind,
            "pure": idx % 6 == 3,
            "voices": int(rng.integers(6, 13)),
            "snr_db": float(rng.uniform(-1 * stretch, 9 * stretch)),
        }
    if label == "music":
        variants = ["sub", "bassline", "full", "hard_bass", "full_speech"]
        return {
            **base,
            "f0": f0,
            "level_db": float(rng.uniform(-26, -12)),
            "bpm": float(rng.uniform(85, 145)),
            "variant": variants[idx % len(variants)],
        }
    if label == "clipped":
        return {**base, "f0": f0, "drive": float(rng.uniform(3.0, 8.0 * stretch))}
    if label == "silence":
        modes = [("zeros", 0.0), ("noise", -90.0), ("noise", -70.0), ("noise", -58.0)]
        mode, db = modes[idx % len(modes)]
        return {**base, "mode": mode, "db": db + float(rng.uniform(-2, 2)) * (mode == "noise")}
    raise ValueError(label)


FIT_COUNTS = {"clean": 6, "wind": 6, "crowd": 6, "music": 5, "clipped": 3, "silence": 4}


def make_specs(counts: dict[str, int], seed_base: int, wide: bool) -> list[ClipSpec]:
    specs: list[ClipSpec] = []
    for label, count in counts.items():
        for idx in range(count):
            seed = seed_base + 100 * LABELS.index(label) + idx
            params = _draw(np.random.default_rng(seed + 7), wide, label, idx)
            specs.append(ClipSpec(f"{label}_{idx:02d}", label, seed, params))
    return specs


def encode(spec: ClipSpec, out: Path) -> None:
    x, channels, enc = spec.render()
    rng = np.random.default_rng(spec.seed + 3)
    if channels == 2:
        right = x + 0.002 * rng.standard_normal(x.size)
        pcm = np.stack([x, np.clip(right, -1, 1)], axis=1)
    else:
        pcm = x[:, None]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "f32le",
            "-ar",
            str(SR),
            "-ac",
            str(channels),
            "-i",
            "pipe:0",
            "-ar",
            str(enc["sr"]),
            "-c:a",
            "aac",
            "-b:a",
            str(enc["bitrate"]),
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            str(out),
        ],
        input=pcm.astype("<f4").tobytes(),
        check=True,
    )


# --------------------------------------------------------------------------------------------
# measuring, fitting, scoring
# --------------------------------------------------------------------------------------------


@dataclass
class Measured:
    name: str
    label: str
    features: aq.AudioFeatures | None
    meta: dict[str, Any]


def measure_dir(directory: Path, labels: dict[str, str]) -> list[Measured]:
    return [
        Measured(name, label, aq.extract_features(str(directory / name)), {})
        for name, label in sorted(labels.items())
    ]


def load_labels(directory: Path) -> dict[str, str]:
    return {
        k: v["label"] if isinstance(v, dict) else v
        for k, v in json.loads((directory / "labels.json").read_text()).items()
    }


def _irls(
    x: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50
) -> tuple[np.ndarray, float]:
    n, d = x.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(iters):
        z = x @ w + b
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        s = p * (1 - p) + 1e-6
        grad_w = x.T @ (p - y) + l2 * w
        grad_b = float(np.sum(p - y))
        h = np.zeros((d + 1, d + 1))
        h[:d, :d] = (x * s[:, None]).T @ x + l2 * np.eye(d)
        h[:d, d] = h[d, :d] = (x * s[:, None]).sum(axis=0)
        h[d, d] = s.sum()
        step = np.linalg.solve(h + 1e-9 * np.eye(d + 1), np.append(grad_w, grad_b))
        w -= step[:d]
        b -= step[d]
        if np.max(np.abs(step)) < 1e-8:
            break
    return w, b


def _signed_irls(x: np.ndarray, y: np.ndarray, signs: tuple[int, ...]) -> tuple[np.ndarray, float]:
    """Logistic fit where a feature that lands on the wrong side of its sign prior is dropped."""
    active = np.ones(x.shape[1], dtype=bool)
    while True:
        w = np.zeros(x.shape[1])
        w[active], b = _irls(x[:, active], y)
        wrong = active & (w * np.array(signs) < 0)
        if not wrong.any():
            return w, b
        active &= ~wrong


def _fit_head(
    rows: list[Measured], names: tuple[str, ...], signs: tuple[int, ...], positive: str, drop: str
) -> dict[str, Any]:
    use = [m for m in rows if m.features and not m.features.silent and m.label != drop]
    x = np.array([m.features.head(names) for m in use])
    y = np.array([1.0 if m.label == positive else 0.0 for m in use])
    mean, std = x.mean(axis=0), x.std(axis=0)
    std = np.where(std < 1e-9, 1.0, std)
    w, b = _signed_irls((x - mean) / std, y, signs)
    scores = 1 / (1 + np.exp(-np.clip(((x - mean) / std) @ w + b, -30, 30)))
    neg_max = float(scores[y == 0].max())
    pos_min = float(scores[y == 1].min())
    if pos_min > neg_max:
        threshold = (neg_max + pos_min) / 2
    else:  # overlap: the lowest threshold that keeps every negative out
        above = np.sort(scores[y == 1][scores[y == 1] > neg_max])
        threshold = (neg_max + (above[0] if above.size else 1.0)) / 2
    r = lambda v: [round(float(a), 6) for a in v]  # noqa: E731
    return {
        "mean": r(mean),
        "std": r(std),
        "weights": r(w),
        "bias": round(float(b), 6),
        "threshold": round(threshold, 4),
    }


def fit(rows: list[Measured]) -> dict[str, Any]:
    clipped = [m.features.clip_fraction for m in rows if m.features and m.label == "clipped"]
    other = [m.features.clip_fraction for m in rows if m.features and m.label != "clipped"]
    hi = max(min(clipped), 1e-4)
    lo = max(max(other), 1e-5)
    clip_threshold = math.sqrt(lo * hi) if hi > lo else hi
    return {
        "wind": _fit_head(rows, aq.WIND_FEATURES, aq.WIND_SIGNS, "wind", "crowd"),
        "crowd": _fit_head(rows, aq.CROWD_FEATURES, aq.CROWD_SIGNS, "crowd", "wind"),
        "clipping_fraction": float(f"{clip_threshold:.3g}"),
    }


def predict(
    features: aq.AudioFeatures | None, params: dict[str, Any] | None = None
) -> dict[str, bool] | None:
    """Per-flag decisions under ``params`` (default: the committed thresholds)."""
    if features is None:
        return None
    saved = aq.FITTED
    if params is not None:
        aq.FITTED = params
    try:
        rec = aq.score(features)
    finally:
        aq.FITTED = saved
    return {
        "noisy": rec["noisy"],
        "wind": rec["wind_score"] >= (params or aq.FITTED)["wind"]["threshold"],
        "crowd": rec["crowd_score"] >= (params or aq.FITTED)["crowd"]["threshold"],
        "clipped": rec["clipped"],
        "silent": rec["silent"],
    }


def pr(tp: int, fp: int, fn: int) -> tuple[float | None, float | None]:
    return (tp / (tp + fp) if tp + fp else None, tp / (tp + fn) if tp + fn else None)


def evaluate(rows: list[Measured], params: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "absent": [m.name for m in rows if m.features is None],
        "classes": {},
        "confusion": {},
    }
    preds = [(m, predict(m.features, params)) for m in rows if m.features is not None]
    flags = {
        "wind": "wind",
        "crowd": "crowd",
        "clipped": "clipped",
        "silence": "silent",
        "mute (wind|crowd)": "noisy",
    }
    for name, flag in flags.items():
        positive = (
            (lambda lab, name=name: lab in NOISY_LABELS)
            if name.startswith("mute")
            else (lambda lab, name=name: lab == name)
        )
        tp = sum(1 for m, p in preds if positive(m.label) and p[flag])
        fp = sum(1 for m, p in preds if not positive(m.label) and p[flag])
        fn = sum(1 for m, p in preds if positive(m.label) and not p[flag])
        precision, recall = pr(tp, fp, fn)
        out["classes"][name] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
        }
    fired: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m, p in preds:
        fired[m.label]["n"] += 1
        for flag in ("noisy", "wind", "crowd", "clipped", "silent"):
            fired[m.label][flag] += int(p[flag])
    out["confusion"] = {k: dict(v) for k, v in fired.items()}
    out["false_positives"] = [
        m.name for m, p in preds if p["noisy"] and m.label not in NOISY_LABELS
    ]
    out["misses"] = [m.name for m, p in preds if not p["noisy"] and m.label in NOISY_LABELS]
    return out


# --------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------


def cmd_generate(_args) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for old in FIXTURES.glob("*.m4a"):
        old.unlink()
    labels = {}
    specs = make_specs(FIT_COUNTS, FIT_SEED_BASE, wide=False)
    for spec in specs:
        out = FIXTURES / f"{spec.name}.m4a"
        encode(spec, out)
        labels[out.name] = {"label": spec.label, "seed": spec.seed, "params": spec.params}
    (FIXTURES / "labels.json").write_text(json.dumps(labels, indent=1, sort_keys=True) + "\n")
    size = sum(f.stat().st_size for f in FIXTURES.glob("*.m4a"))
    print(f"wrote {len(specs)} clips, {size / 1024:.0f} KiB")
    return 0


def fit_rows(args) -> list[Measured]:
    rows = measure_dir(FIXTURES, load_labels(FIXTURES))
    if getattr(args, "include_real", False) and args.real_dir:
        rows += measure_dir(args.real_dir, load_labels(args.real_dir))
    return rows


def render_block(params: dict[str, Any]) -> str:
    return "FITTED: dict[str, Any] = " + json.dumps(params, indent=4) + "\n"


def cmd_fit(args) -> int:
    params = fit(fit_rows(args))
    block = render_block(params)
    if not args.write:
        print(block)
        return 0
    text = MODULE.read_text()
    head, rest = text.split("# BEGIN FITTED", 1)
    marker_line, rest = rest.split("\n", 1)
    _, tail = rest.split("# END FITTED", 1)
    MODULE.write_text(f"{head}# BEGIN FITTED{marker_line}\n{block}# END FITTED{tail}")
    subprocess.run(["ruff", "format", str(MODULE)], check=False, cwd=API)
    print("wrote thresholds to", MODULE.relative_to(ROOT))
    return 0


def _close(a: Any, b: Any, tol: float = 2e-2) -> bool:
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k], tol) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b, strict=True))
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def cmd_check(args) -> int:
    fresh = fit(fit_rows(args))
    if _close(fresh, aq.FITTED):
        print("audio_quality thresholds match the fixtures")
        return 0
    print(
        "audio_quality FITTED drifted from the fixtures; run `fit --write` and bump SIGNAL_VERSION"
    )
    print(render_block(fresh))
    return 1


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _table(result: dict[str, Any]) -> list[str]:
    lines = [
        "| class (flag) | TP | FP | FN | precision | recall |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, c in result["classes"].items():
        lines.append(
            f"| {name} | {c['tp']} | {c['fp']} | {c['fn']} | {_fmt(c['precision'])} | {_fmt(c['recall'])} |"
        )
    return lines


def _confusion(result: dict[str, Any]) -> list[str]:
    lines = [
        "| true label | n | noisy | wind | crowd | clipped | silent |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label in LABELS:
        c = result["confusion"].get(label)
        if c:
            lines.append(
                f"| {label} | {c['n']} | {c['noisy']} | {c['wind']} | {c['crowd']} | {c['clipped']} | {c['silent']} |"
            )
    return lines


def snr_sweep(tmp: Path) -> list[str]:
    lines = ["| SNR dB (speech minus noise) | wind recall | crowd recall |", "| --- | --- | --- |"]
    for snr in (12, 8, 4, 0, -4, -8):
        cells = []
        for label in ("wind", "crowd"):
            hits = 0
            total = 6
            for i in range(total):
                params = _draw(
                    np.random.default_rng(HELDOUT_SEED_BASE + snr + i), True, label, i % 4
                )
                params.update(
                    {
                        "snr_db": float(snr),
                        "pure": False,
                        "kind": "babble",
                        "dur": 3.0,
                        "f0": 140.0,
                        "level_db": -20.0,
                    }
                )
                spec = ClipSpec(
                    f"sweep_{label}_{snr}_{i}", label, HELDOUT_SEED_BASE + 500 + i, params
                )
                path = tmp / f"{spec.name}.m4a"
                encode(spec, path)
                pred = predict(aq.extract_features(str(path)))
                hits += int(bool(pred and pred["noisy"]))
            cells.append(f"{hits}/{total}")
        lines.append(f"| {snr:+d} | {cells[0]} | {cells[1]} |")
    return lines


def cmd_report(args) -> int:
    fit_result = evaluate(measure_dir(FIXTURES, load_labels(FIXTURES)))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        counts = {"clean": 12, "wind": 12, "crowd": 12, "music": 10, "clipped": 6, "silence": 6}
        specs = make_specs(counts, HELDOUT_SEED_BASE, wide=True)
        labels = {}
        for spec in specs:
            encode(spec, tmp / f"{spec.name}.m4a")
            labels[f"{spec.name}.m4a"] = spec.label
        held = evaluate(measure_dir(tmp, labels))
        sweep = snr_sweep(tmp)
    real = None
    if args.real_dir:
        real = evaluate(measure_dir(args.real_dir, load_labels(args.real_dir)))

    mute = held["classes"]["mute (wind|crowd)"]
    mute_fit = fit_result["classes"]["mute (wind|crowd)"]
    precision = mute["precision"]
    gate = precision is not None and precision >= MUTE_PRECISION_BAR
    music = held["confusion"].get("music", {})
    lines = [
        "# audio_quality calibration",
        "",
        f"signal version {aq.SIGNAL_VERSION}; thresholds from `app/services/clip_signals/audio_quality.py` (FITTED).",
        "",
        "## Honest scope",
        "",
        "Every clip here is synthetic (numpy synthesis, encoded to AAC with ffmpeg): harmonic voiced speech, "
        "gusty low-passed noise for wind, summed continuous voices plus diffuse noise for crowd, kick/sub-bass/hat "
        "loops for bass music, hard-clipped speech, and silence. The fit set (30 committed clips) and the held-out set "
        "(60 clips, different seeds, wider parameter ranges, 2.5-6 s, 44.1/48 kHz, 32-96 kbps) come from the SAME generator "
        "family, so a good number here means the measurement separates the failure modes we thought of, "
        "not that it works on real phone audio. Real wind hits a phone mic differently (mic-specific noise "
        "reduction, AGC pumping), and real crowds and real music are far more varied. "
        "Treat the mute-clip gate below as necessary, not sufficient: re-run with `--real-dir` on labeled real clips "
        "before enabling `kria_suggestion_mute_clip_enabled` for creators.",
        "",
        f"## Mute-clip gate (precision >= {MUTE_PRECISION_BAR})",
        "",
        f"- held-out precision: **{_fmt(precision)}** (recall {_fmt(mute['recall'])}) -> "
        f"**{'PASS' if gate else 'FAIL'}**",
        f"- fit-set precision: {_fmt(mute_fit['precision'])} (recall {_fmt(mute_fit['recall'])}); the fit set was seen during fitting",
        f"- bass-heavy music flagged noisy on held-out: {music.get('noisy', 0)}/{music.get('n', 0)}",
        f"- held-out false positives: {held['false_positives'] or 'none'}",
        f"- held-out misses: {held['misses'] or 'none'}",
        f"- clips with no signal (absent): {held['absent'] or 'none'}",
        "",
        "## How to read this",
        "",
        "- Development used the fit set only. The held-out set was scored twice: once, then again after the "
        "periodicity measure was changed to autocorrelation peaks and a saturated-808 music variant was added; "
        "nothing was tuned on it beyond that.",
        "- `noisy` (wind or crowd) is what the mute card may act on; the separate wind / crowd rows only say which "
        "evidence line a card would show. A traffic-like rumble can read as wind and is still a correct mute.",
        "- `noisy` does not know whether the clip's speech matters. Muting a noisy clip also mutes its voice; "
        "the mute_clip kind has to decide that from understanding (speech present, voiceover or caption plans).",
        "- The `silence` row counts the `silent` flag only. The -58 dBFS hiss clip is audible room tone by "
        "definition (peak above -55 dBFS), so it reads as not silent and not noisy.",
        "- Sign priors (`WIND_SIGNS`, `CROWD_SIGNS`) stop the fit from using a feature against its physical meaning. "
        "A feature the data wants backwards gets weight 0 (see the FITTED block).",
        "",
        "## Held-out set (60 clips)",
        "",
        *_table(held),
        "",
        *_confusion(held),
        "",
        "## Fit set (30 committed clips; optimistic by construction)",
        "",
        *_table(fit_result),
        "",
        *_confusion(fit_result),
        "",
        "## Detection by SNR (speech minus noise, held-out generator, 6 clips each)",
        "",
        "Where the mute card stops firing as the noise gets quieter than the voice. A quiet gust under loud speech "
        "is deliberately not flagged.",
        "",
        *sweep,
    ]
    if real is not None:
        lines += ["", "## Real labeled clips", "", *_table(real), "", *_confusion(real)]
    else:
        lines += [
            "",
            "## Real labeled clips",
            "",
            'Not run. Put clips and a `labels.json` (`{"file.m4a": "wind|crowd|clean|music|clipped|silence"}`) in a '
            "folder and run `clip_signal_calibration.py report --real-dir DIR`; `fit --include-real --real-dir DIR` "
            "refits on them.",
        ]
    text = "\n".join(lines) + "\n"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text)
    (FIXTURES / "CALIBRATION.md").write_text(text)
    print(text)
    return 0 if gate else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    p_fit = sub.add_parser("fit")
    p_fit.add_argument("--write", action="store_true")
    for p in (p_fit, sub.add_parser("check"), sub.add_parser("report")):
        p.add_argument("--real-dir", type=Path, default=None)
    p_fit.add_argument("--include-real", action="store_true")
    args = parser.parse_args()
    return {"generate": cmd_generate, "fit": cmd_fit, "check": cmd_check, "report": cmd_report}[
        args.cmd
    ](args)


if __name__ == "__main__":
    sys.exit(main())
