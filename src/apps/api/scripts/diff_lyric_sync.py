#!/usr/bin/env python3
"""Diagnose lyric/audio sync drift for a Nova music job vs a YouTube reference.

The headline question this answers — "where are Nova's burned-in lyrics off vs
the YouTube reference, in milliseconds?" — needs no Nova credentials. Just
point the script at a local mp4 + a YouTube URL.

When a Nova `--job-id` is supplied AND we can reach the admin API (or hit
postgres directly), the script also pulls the job's `lyrics_cached` + recipe
and adds the per-pipeline-stage drift diagnostic columns.

Outputs an HTML report at `.dev/lyric-sync-diffs/<id>_<ts>.html` with:
  - Drift summary cards per stage (whichever stages we could measure)
  - Color-coded line-by-line table
  - Click-to-expand frame thumbnails (Nova + YouTube)

Standalone-runnable — does NOT need the api venv. Uses:
  - system Python 3.11+ with `numpy` and (optionally) `openai`
  - binaries on PATH: ffmpeg, ffprobe, yt-dlp, tesseract
  - optional `pytesseract` (Python wrapper for tesseract) — install via
    `pip install --user pytesseract` if not present.

Usage:
  python3 src/apps/api/scripts/diff_lyric_sync.py \\
    --nova-mp4 /path/to/output.mp4 \\
    --youtube-url 'https://youtube.com/watch?v=…' \\
    [--job-id UUID]              # for enriched diagnostic
    [--no-whisper]               # skip OpenAI Whisper
    [--cache-dir DIR]            # default .dev/lyric-sync-diffs/cache
    [--ocr-fps N]                # default 6 (~167ms resolution)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# .env loader — same stdlib-only parser as scripts/admin.py
# ---------------------------------------------------------------------------


def _load_env_file_into_environ() -> None:
    """Merge KEY=VALUE pairs from <repo>/.env into os.environ (does NOT overwrite
    keys already set in the shell). Walks up from this script until it finds
    a .env or reaches the filesystem root.
    """
    here = Path(__file__).resolve().parent
    for root in (here, *here.parents):
        candidate = root / ".env"
        if candidate.exists():
            break
    else:
        return
    for raw in candidate.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        os.environ.setdefault(k, v)


_load_env_file_into_environ()

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.services.yt_dlp_options import (  # noqa: E402
    YtDlpCookieConfigError,
    with_yt_dlp_cookiefile,
)

# ---------------------------------------------------------------------------
# Constants — mirrored from app.pipeline.lyric_injector (single source of truth
# is the prod module; these are kept in sync by the eyeball test of running
# this script against a known job and checking drift_inject ≈ 0).
# ---------------------------------------------------------------------------
_LINE_PRE_ROLL_S = 0.40  # default for `line` style (was 0.10 before the empirical fix)
_AUDIO_SR = 16000


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--nova-mp4", help="Local path to the Nova-rendered output mp4.")
    p.add_argument("--youtube-url", required=True, help="YouTube URL to use as reference.")
    p.add_argument("--job-id", help="Optional Nova job UUID — adds A/B/D diagnostic columns.")
    p.add_argument(
        "--admin-key",
        default=os.environ.get("ADMIN_PROD_API_KEY") or os.environ.get("ADMIN_API_KEY"),
        help="Admin API token; falls back to env ADMIN_PROD_API_KEY/ADMIN_API_KEY.",
    )
    p.add_argument(
        "--admin-base-url",
        default=os.environ.get("ADMIN_BASE_URL", "https://nova-video.fly.dev"),
        help="Base URL for the admin API. Defaults to Fly prod.",
    )
    p.add_argument(
        "--no-whisper", action="store_true", help="Skip OpenAI Whisper (faster, no API spend)."
    )
    p.add_argument(
        "--openai-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="OpenAI API key for Whisper; falls back to env OPENAI_API_KEY.",
    )
    p.add_argument("--cache-dir", default=".dev/lyric-sync-diffs/cache")
    p.add_argument("--out-dir", default=".dev/lyric-sync-diffs")
    p.add_argument(
        "--ocr-fps", type=float, default=6.0, help="OCR frame sampling fps. Default 6 (~167ms)."
    )
    p.add_argument(
        "--nova-band",
        default="0.72,1.0",
        help="Vertical band as y_start,y_end fractions of Nova frame to OCR. Default 0.72,1.0 (bottom 28%%, where Nova places lyrics). Pass 0,1 for full frame.",
    )
    p.add_argument(
        "--yt-band",
        default="0,1",
        help="Vertical band for YouTube frame OCR. Default 0,1 (full frame) — lyric videos place text anywhere. Narrow if you know where.",
    )
    p.add_argument(
        "--best-start-s",
        type=float,
        default=None,
        help="Override best_start_s (song time of section start). Auto-detected via audio cross-correlation when omitted.",
    )
    args = p.parse_args(argv)
    if not args.nova_mp4 and not args.job_id:
        p.error("Provide --nova-mp4 or --job-id (at least one).")
    return args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[diff_lyric_sync] {msg}", flush=True)


def _sha_short(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    quiet: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess:
    run_kwargs = {
        "check": False,
        "capture_output": quiet,
        "text": quiet,
    }
    if pass_fds:
        run_kwargs["pass_fds"] = pass_fds

    out = subprocess.run(cmd, **run_kwargs)
    if check and out.returncode != 0:
        err = (out.stderr or "") if quiet else ""
        raise RuntimeError(f"command failed ({cmd[0]} exit {out.returncode}):\n{err}")
    return out


# ---------------------------------------------------------------------------
# Admin API fetch (optional, for enriched diagnostic)
# ---------------------------------------------------------------------------


def _admin_api_get(path: str, token: str, base_url: str) -> dict | None:
    """GET /admin/<path> via stdlib urllib. Returns dict or None on failure."""
    import urllib.error
    import urllib.request

    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/admin/"):
        path = "/admin" + path
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"X-Admin-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            return json.loads(data)
    except urllib.error.HTTPError as e:
        _log(f"admin API HTTP {e.code} for {path}: {e.read()[:200]!r}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        _log(f"admin API error for {path}: {e!r}")
        return None


def _fetch_job_bundle_admin(args: argparse.Namespace) -> dict | None:
    """Best-effort fetch of job + track via admin API. Returns enrichment bundle or None."""
    if not args.job_id:
        return None
    if not args.admin_key:
        _log("no admin token in --admin-key / env; skipping enriched diagnostic")
        return None
    _log(f"fetching job {args.job_id} via admin API at {args.admin_base_url}…")
    job_resp = _admin_api_get(
        f"/admin/jobs/{args.job_id}/debug", args.admin_key, args.admin_base_url
    )
    if not job_resp:
        return None
    job = job_resp.get("job") or job_resp  # endpoint returns the job at top level or under .job
    assembly_plan = job.get("assembly_plan") or {}
    music_track_id = job.get("music_track_id")
    if not music_track_id:
        _log(f"job {args.job_id} has no music_track_id; cannot enrich")
        return None
    track_resp = _admin_api_get(
        f"/admin/music-tracks/{music_track_id}", args.admin_key, args.admin_base_url
    )
    if not track_resp:
        return None
    return {
        "job_id": str(job.get("id") or args.job_id),
        "job_status": job.get("status", "?"),
        "music_track_id": music_track_id,
        "track_title": track_resp.get("title") or "",
        "track_artist": track_resp.get("artist") or "",
        "lyrics_cached": track_resp.get("lyrics_cached") or {},
        "track_config": track_resp.get("track_config") or {},
        "duration_s": float(track_resp.get("duration_s") or 0.0),
        "lyrics_config_effective": assembly_plan.get("lyrics_config_effective"),
        "output_url": assembly_plan.get("output_url"),
    }


# ---------------------------------------------------------------------------
# Asset fetch
# ---------------------------------------------------------------------------


def _http_download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        _log(f"cache hit: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _log(f"downloading → {dest.name}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    _run(["curl", "-fsSL", "-o", str(tmp), url], quiet=False)
    tmp.rename(dest)


def _yt_dlp_download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        _log(f"cache hit: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _log(f"yt-dlp → {dest.name}")
    try:
        with with_yt_dlp_cookiefile(
            cookie_path=os.environ.get("YTDLP_COOKIES_PATH"),
            cookie_b64=os.environ.get("YTDLP_COOKIES_B64"),
            subprocess_safe=True,
            use_settings=False,
        ) as cookie_file:
            cmd = [
                "yt-dlp",
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "--no-playlist",
                "--quiet",
                "-o",
                str(dest),
            ]
            if cookie_file is not None:
                cmd.extend(["--cookies", str(cookie_file.path)])
            cmd.append(url)

            _run(cmd, quiet=False, pass_fds=cookie_file.pass_fds if cookie_file else ())
    except YtDlpCookieConfigError as exc:
        raise RuntimeError(f"yt-dlp cookie configuration is invalid: {exc}") from exc
    if not dest.exists():
        raise RuntimeError(f"yt-dlp produced no file at {dest}")


# ---------------------------------------------------------------------------
# Audio extract + FFT cross-correlation
# ---------------------------------------------------------------------------


def _ffmpeg_extract_wav(video: Path, wav: Path) -> None:
    if wav.exists() and wav.stat().st_size > 0:
        return
    wav.parent.mkdir(parents=True, exist_ok=True)
    _log(f"extracting audio → {wav.name}")
    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_AUDIO_SR),
            "-f",
            "wav",
            str(wav),
        ]
    )


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == _AUDIO_SR, f"expected {_AUDIO_SR} Hz wav"
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _align_audio(
    nova_pcm: np.ndarray,
    yt_pcm: np.ndarray,
    best_start_hint_s: float | None,
    best_end_hint_s: float | None,
) -> tuple[float, float, float]:
    """Find where the Nova mix appears in the YouTube reference.

    Returns (yt_t_at_nova_zero_s, confidence, search_window_start_s).

    `yt_t_at_nova_zero_s` is the YouTube absolute time that corresponds to
    Nova section time 0. So: nova_t = yt_t - yt_t_at_nova_zero_s.

    `confidence` is normalized cross-correlation in [0, 1]. <0.4 likely
    means the YouTube clip isn't the same recording.
    """
    sr = _AUDIO_SR
    # Restrict YouTube to a window around the hint to (a) speed up the FFT
    # and (b) avoid latching onto a repeated chorus elsewhere in the song.
    if (
        best_start_hint_s is not None
        and best_end_hint_s is not None
        and best_end_hint_s > best_start_hint_s
    ):
        slack = 10.0
        win_start = max(0.0, best_start_hint_s - slack)
        win_end = min(len(yt_pcm) / sr, best_end_hint_s + slack)
    else:
        win_start = 0.0
        win_end = len(yt_pcm) / sr
    yt_clip = yt_pcm[int(win_start * sr) : int(win_end * sr)]

    if len(nova_pcm) < sr or len(yt_clip) < sr:
        _log("audio too short for alignment; assuming nova zero aligns with yt zero")
        return 0.0, 0.0, win_start

    def _norm(x: np.ndarray) -> np.ndarray:
        x = x - x.mean()
        rms = math.sqrt(float(np.mean(x * x)))
        return x / rms if rms > 1e-9 else x

    a = _norm(nova_pcm.astype(np.float64))
    b = _norm(yt_clip.astype(np.float64))

    # FFT cross-correlation: R[k] = sum_t a[t] * b[t+k]. Peak at k* means
    # Nova (a) aligns with YouTube clip (b) starting at b[k*]. So Nova section
    # time 0 ↔ YouTube clip time k*/sr ↔ YouTube absolute (win_start + k*/sr).
    # `conj(A) * B` (not `A * conj(B)`) is the correct sign convention; the
    # earlier code had it backwards which clipped the search range to
    # max_lag = len(a) and missed alignments where Nova clips deep into the
    # YouTube source (chorus pickups etc).
    nfft = 1 << int(math.ceil(math.log2(len(a) + len(b))))
    A = np.fft.rfft(a, n=nfft)
    B = np.fft.rfft(b, n=nfft)
    corr = np.fft.irfft(np.conj(A) * B, n=nfft)
    # Valid lag range: Nova fits fully inside YouTube clip when
    # k ∈ [0, len(b) - len(a)]. (k beyond that has Nova running past the
    # end of b — circular wrap-around makes those peaks unreliable.)
    max_lag = max(1, len(b) - len(a) + 1)
    valid = corr[:max_lag]
    peak_idx = int(np.argmax(valid))
    peak_val = float(valid[peak_idx])
    # Peak prominence: ratio of peak to median |corr|. Same-recording
    # alignments score 50–500× the median; unrelated audio scores 5–15×.
    # Normalize to [0,1] via a soft cap at 50× → 1.0.
    median_abs = float(np.median(np.abs(valid))) or 1e-9
    prominence = peak_val / median_abs
    confidence = max(0.0, min(1.0, prominence / 50.0))
    yt_t_at_nova_zero = win_start + peak_idx / sr
    _log(
        f"audio-align: nova_zero ↔ yt_absolute {yt_t_at_nova_zero:.3f}s "
        f"(peak in search [{win_start:.2f}, {win_end:.2f}]) "
        f"prominence={prominence:.1f}× confidence={confidence:.3f}"
    )
    return yt_t_at_nova_zero, confidence, win_start


# ---------------------------------------------------------------------------
# OCR — ffmpeg frame extract → pytesseract → temporal grouping
# ---------------------------------------------------------------------------


def _extract_frames(
    video: Path, fps: float, out_dir: Path, crop_band: tuple[float, float] | None = None
) -> list[tuple[Path, float]]:
    """Extract one JPEG per (1/fps) into out_dir. Returns [(path, timestamp_s)].

    `crop_band=(y_start_frac, y_end_frac)` crops the vertical band where lyric
    overlays live. Tesseract is far more reliable on a tightly cropped band
    than on the full 1080x1920 frame (less noise from clip content).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # `image2` writes one file per frame; use a literal pattern.
    pattern = str(out_dir / "f_%06d.jpg")
    vf = [f"fps={fps}"]
    if crop_band is not None:
        y_start_frac, y_end_frac = crop_band
        h_frac = max(0.05, y_end_frac - y_start_frac)
        # Crop: width=in_w, height=in_h*h_frac, y_offset=in_h*y_start_frac.
        vf.append(f"crop=in_w:in_h*{h_frac:.4f}:0:in_h*{y_start_frac:.4f}")
        # 2x upscale with lanczos makes tesseract noticeably more accurate
        # on short-form caption text (no native HiDPI shipping fonts).
        vf.append("scale=iw*2:ih*2:flags=lanczos")
    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            ",".join(vf),
            "-q:v",
            "3",
            pattern,
        ]
    )
    frames = sorted(out_dir.glob("f_*.jpg"))
    return [(p, i / fps) for i, p in enumerate(frames)]


def _ocr_one(image_path: Path) -> str:
    """Run tesseract on one image and return text."""
    import pytesseract  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    img = Image.open(str(image_path))
    # PSM 6 = "assume a single uniform block of text" — best for short-form
    # video captions where text sits on a single horizontal band.
    text = pytesseract.image_to_string(img, config="--psm 6")
    return text.strip()


def _ocr_phrases(
    video: Path, fps: float, cache_dir: Path, lyric_band: tuple[float, float] | None = (0.72, 1.0)
) -> list[dict]:
    """OCR the video frame-by-frame, return [{text, start_s, end_s}] phrases.

    `lyric_band=(y_start_frac, y_end_frac)` is the vertical strip where lyric
    overlays live (default: bottom 28% — matches Nova's `bottom` position
    and most YouTube lyric videos).

    A "phrase" is consecutive frames with the same normalized text.

    NOTE: tesseract on macOS (homebrew install) is sandboxed and CANNOT read
    files under /tmp/. We always extract frames under the cache dir which
    lives inside the project tree.
    """
    band_key = f"{int(lyric_band[0] * 100)}_{int(lyric_band[1] * 100)}" if lyric_band else "full"
    key = f"{_sha_short(video)}_fps{int(fps * 100)}_band{band_key}"
    cache_file = cache_dir / "ocr" / f"{key}.json"
    if cache_file.exists():
        _log(f"ocr cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text())

    try:
        import pytesseract  # noqa: F401,PLC0415
    except ImportError:
        _log(
            "pytesseract not installed; install with `pip install --user pytesseract` to enable OCR"
        )
        return []

    # Scratch dir under cache_dir (NOT /tmp/) — tesseract sandbox refuses /tmp.
    frames_dir = cache_dir / "frames" / f"{video.stem}_{key}"
    if frames_dir.exists():
        # Hygiene: wipe stale frames so a half-finished previous run doesn't
        # bleed into this one.
        for p in frames_dir.glob("f_*.jpg"):
            p.unlink()
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = _extract_frames(video, fps, frames_dir, crop_band=lyric_band)
    band_desc = (
        "full frame" if lyric_band is None else f"band y∈[{lyric_band[0]:.2f},{lyric_band[1]:.2f}]"
    )
    _log(f"OCR {video.name}: {len(frames)} frames at {fps} fps ({band_desc})…")
    results: list[tuple[float, str]] = []
    t0 = time.monotonic()
    for i, (p, t) in enumerate(frames):
        text = _normalize_ocr(_ocr_one(p))
        results.append((t, text))
        if i and i % 50 == 0:
            elapsed = time.monotonic() - t0
            _log(f"  {i}/{len(frames)} ({elapsed:.1f}s elapsed)")

    # Fuzzy-group adjacent frames into phrases.
    # Tesseract's per-frame output drifts by 1-2 chars from neighboring frames
    # even when the on-screen text is identical (anti-aliasing flicker,
    # compression artifacts). A strict `text == prev_text` check produces 1
    # phrase per frame and the noise filter drops everything. Instead, group
    # frames whose normalized-token-set overlap is ≥ 0.5 (Jaccard).
    # Within a phrase we keep the LONGEST OCR text seen (highest-information
    # variant) — saves us from emitting truncated mid-fade frames.
    @dataclass
    class _Group:
        texts: list[str]
        start_s: float
        end_s: float

        @property
        def best_text(self) -> str:
            # Longest by char count tends to be the most complete read.
            return max(self.texts, key=len) if self.texts else ""

        @property
        def token_set(self) -> set[str]:
            return set(_norm_words(self.best_text))

    groups: list[_Group] = []
    frame_dt = 1.0 / fps
    for t, text in results:
        if not text:
            continue
        toks = set(_norm_words(text))
        # Drop frames whose OCR is < 2 alnum tokens AND with no long token
        # (OCR noise on background pixels emits short fragments like "ee",
        # "a", "—_———" → noise floor we don't want polluting the timeline).
        has_word_token = any(len(t_) >= 3 for t_ in toks)
        if len(toks) < 2 and not has_word_token:
            continue
        if not toks:
            continue
        # If the most recent group has high overlap AND is recent enough, extend it.
        # Use a generous overlap threshold (0.3) because tesseract output drifts
        # heavily frame-to-frame on the same caption (~50% of tokens may shift).
        if groups:
            last = groups[-1]
            inter = len(toks & last.token_set)
            overlap = inter / max(len(toks), len(last.token_set))
            gap = t - last.end_s
            # Merge when EITHER strong-fraction match OR at least 2 shared
            # tokens (latter catches phrases with high OCR noise).
            if (overlap >= 0.3 or inter >= 2) and gap <= 4 * frame_dt:
                last.texts.append(text)
                last.end_s = t + frame_dt
                continue
        groups.append(_Group(texts=[text], start_s=t, end_s=t + frame_dt))

    # Drop groups whose BEST OCR text isn't a real caption. Heuristics:
    #  - Need at least 2 tokens (lyrics are multi-word phrases)
    #  - ≥ 40% of tokens must be ≥3 chars (noise OCR on backgrounds emits a
    #    lot of 1–2 char fragments like "ee", "a", "—", "wr")
    #  - Need at least one token of length ≥4 (a "real word")
    phrases = []
    min_dur = 1.5 / fps
    for g in groups:
        best = g.best_text
        if not best or (g.end_s - g.start_s) < min_dur:
            continue
        words = _norm_words(best)
        if len(words) < 2:
            continue
        long_frac = sum(1 for w in words if len(w) >= 3) / len(words)
        if long_frac < 0.4:
            continue
        if not any(len(w) >= 4 for w in words):
            continue
        phrases.append({"text": best, "start_s": g.start_s, "end_s": g.end_s})
    _log(f"OCR found {len(phrases)} phrases for {video.name}")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(phrases))
    return phrases


def _normalize_ocr(s: str) -> str:
    """Clean OCR output: collapse whitespace, strip control chars."""
    s = re.sub(r"\s+", " ", s)
    s = "".join(ch for ch in s if ch.isprintable() or ch == " ")
    return s.strip()


# ---------------------------------------------------------------------------
# Whisper — direct openai SDK call
# ---------------------------------------------------------------------------


def _whisper_words(audio_path: Path, prompt: str, api_key: str, cache_dir: Path) -> list[dict]:
    key = f"{_sha_short(audio_path)}_p{len(prompt)}"
    cache_file = cache_dir / "whisper" / f"{key}.json"
    if cache_file.exists():
        _log(f"whisper cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text())

    upload_path = audio_path
    size = audio_path.stat().st_size
    if size > 24 * 1024 * 1024:
        upload_path = audio_path.with_suffix(".opus")
        if not upload_path.exists():
            _log(f"audio is {size // 1024 // 1024} MB → re-encoding to opus 16k")
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(audio_path),
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "16k",
                    "-ac",
                    "1",
                    str(upload_path),
                ]
            )

    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(api_key=api_key)
    _log(f"whisper transcribing {upload_path.name}…")
    with open(upload_path, "rb") as f:
        kwargs: dict = {
            "model": "whisper-1",
            "file": f,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word"],
        }
        if prompt:
            kwargs["prompt"] = prompt[:800]
        resp = client.audio.transcriptions.create(**kwargs)

    words = [
        {"text": (w.word or "").strip(), "start_s": float(w.start), "end_s": float(w.end)}
        for w in (resp.words or [])
        if (w.word or "").strip()
    ]
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(words))
    return words


# ---------------------------------------------------------------------------
# Line-set source: lyrics_cached if available, else Whisper-derived
# ---------------------------------------------------------------------------


def _norm_words(s: str) -> list[str]:
    return [
        "".join(ch for ch in tok.lower() if ch.isalnum())
        for tok in s.split()
        if any(ch.isalnum() for ch in tok)
    ]


def _build_lines_from_whisper(
    whisper_words: list[dict], gap_threshold_s: float = 1.0
) -> list[dict]:
    """Heuristically group Whisper words into lines using inter-word silence."""
    lines: list[dict] = []
    if not whisper_words:
        return lines
    cur: list[dict] = [whisper_words[0]]
    for w in whisper_words[1:]:
        prev = cur[-1]
        if float(w["start_s"]) - float(prev["end_s"]) > gap_threshold_s:
            lines.append(_words_to_line(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(_words_to_line(cur))
    return lines


def _words_to_line(words: list[dict]) -> dict:
    text = " ".join(w["text"] for w in words)
    return {
        "text": text,
        "start_s": float(words[0]["start_s"]),
        "end_s": float(words[-1]["end_s"]),
        "words": words,
    }


# ---------------------------------------------------------------------------
# Match line ↔ OCR phrase
# ---------------------------------------------------------------------------


def _match_score(line_words: list[str], ocr_words: list[str]) -> float:
    """Fuzzy similarity between two word lists.

    Tesseract on stylized caption fonts often drops spaces, producing one
    long blob like 'theihighestinithe' that's not literally equal to any
    word in a clean reference like 'HIGHEST IN THE'. Strict set-intersection
    misses these matches.

    We use char-bigram Jaccard on the joined lowercased-alphanumeric forms.
    A bigram set hits every word boundary plus internal spelling — so
    'highestinithe' and 'highest in the' share most of their bigrams.
    """
    if not line_words or not ocr_words:
        return 0.0

    def _bigrams(words: list[str]) -> set[str]:
        joined = "".join(words)
        return {joined[i : i + 2] for i in range(len(joined) - 1)} if len(joined) >= 2 else set()

    a = _bigrams(line_words)
    b = _bigrams(ocr_words)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _nearest_ocr(
    line_words: list[str],
    ocr_phrases: list[dict],
    expected_t_s: float | None,
    radius_s: float = 2.0,
    min_score: float = 0.35,
) -> dict | None:
    best: tuple[float, dict] | None = None
    for ph in ocr_phrases:
        sc = _match_score(line_words, _norm_words(ph["text"]))
        if sc < min_score:
            continue
        if expected_t_s is not None:
            dt = abs(float(ph["start_s"]) - expected_t_s)
            if dt > radius_s:
                continue
            combined = sc - 0.15 * (dt / radius_s)
        else:
            combined = sc
        if best is None or combined > best[0]:
            best = (combined, ph)
    return best[1] if best else None


def _first_whisper_match(
    first_word: str, whisper_words: list[dict], expected_t_s: float | None, radius_s: float = 2.5
) -> dict | None:
    if not first_word or not whisper_words:
        return None
    cand = [
        w
        for w in whisper_words
        if "".join(c for c in w["text"].lower() if c.isalnum()) == first_word
    ]
    if not cand:
        return None
    if expected_t_s is None:
        return cand[0]
    cand_in_radius = [w for w in cand if abs(float(w["start_s"]) - expected_t_s) <= radius_s]
    pool = cand_in_radius or cand
    return min(pool, key=lambda w: abs(float(w["start_s"]) - expected_t_s))


# ---------------------------------------------------------------------------
# Per-line diff
# ---------------------------------------------------------------------------


@dataclass
class LineDiff:
    line_idx: int
    text: str
    line_start_track_s: float
    A_expected_section_s: float | None
    C_nova_ocr_section_s: float | None
    D_nova_whisper_section_s: float | None
    E_yt_ocr_section_s: float | None
    F_yt_whisper_section_s: float | None
    matched_nova_ocr_text: str = ""
    matched_yt_ocr_text: str = ""
    _best_start_s: float | None = None

    @property
    def drift_render_ms(self) -> int | None:
        if self.A_expected_section_s is None or self.C_nova_ocr_section_s is None:
            return None
        return int(round((self.C_nova_ocr_section_s - self.A_expected_section_s) * 1000))

    @property
    def drift_audio_mix_ms(self) -> int | None:
        if self.D_nova_whisper_section_s is None or self._best_start_s is None:
            return None
        expected = self.line_start_track_s - self._best_start_s
        return int(round((self.D_nova_whisper_section_s - expected) * 1000))

    @property
    def drift_vs_youtube_ms(self) -> int | None:
        if self.C_nova_ocr_section_s is None or self.E_yt_ocr_section_s is None:
            return None
        return int(round((self.C_nova_ocr_section_s - self.E_yt_ocr_section_s) * 1000))

    @property
    def drift_whisper_vs_youtube_ms(self) -> int | None:
        if self.D_nova_whisper_section_s is None or self.F_yt_whisper_section_s is None:
            return None
        return int(round((self.D_nova_whisper_section_s - self.F_yt_whisper_section_s) * 1000))

    @property
    def drift_overlay_vs_audio_ms(self) -> int | None:
        """Headline answer: does Nova's overlay land on Nova's vocal? C − D."""
        if self.C_nova_ocr_section_s is None or self.D_nova_whisper_section_s is None:
            return None
        return int(round((self.C_nova_ocr_section_s - self.D_nova_whisper_section_s) * 1000))


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def _drift_cls(ms: int | None) -> str:
    if ms is None:
        return "drift-na"
    a = abs(ms)
    if a <= 50:
        return "drift-ok"
    if a <= 100:
        return "drift-warn"
    return "drift-bad"


def _stats(vals: list[int]) -> tuple[int | None, int | None]:
    if not vals:
        return None, None
    av = sorted(abs(v) for v in vals)
    p50 = av[len(av) // 2]
    p95 = av[min(len(av) - 1, int(len(av) * 0.95))]
    return p50, p95


def _ffmpeg_thumb_b64(video: Path, t: float, cache_dir: Path) -> str | None:
    if t is None or t < 0:
        return None
    out = cache_dir / "thumbs" / f"{video.stem}_{int(round(t * 1000))}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, t):.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=360:-1",
                "-q:v",
                "5",
                str(out),
            ],
            check=False,
            capture_output=True,
        )
        if r.returncode != 0 or not out.exists():
            return None
    b64 = base64.b64encode(out.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    return _esc_html(s).replace('"', "&quot;")


def _fmt_s(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def _fmt_drift(ms: int | None) -> str:
    if ms is None:
        return "—"
    sign = "+" if ms > 0 else ""
    return f"{sign}{ms} ms"


def _build_diagnosis(diffs: list[LineDiff], confidence: float, has_bundle: bool) -> str:
    notes: list[str] = []
    if not diffs:
        return "No matching lyric lines were detected — both OCR and Whisper were empty for the cached section."

    dr = [d.drift_render_ms for d in diffs if d.drift_render_ms is not None]
    da = [d.drift_audio_mix_ms for d in diffs if d.drift_audio_mix_ms is not None]
    dy = [d.drift_vs_youtube_ms for d in diffs if d.drift_vs_youtube_ms is not None]
    dyw = [
        d.drift_whisper_vs_youtube_ms for d in diffs if d.drift_whisper_vs_youtube_ms is not None
    ]
    doa = [d.drift_overlay_vs_audio_ms for d in diffs if d.drift_overlay_vs_audio_ms is not None]

    # Headline: overlay-vs-audio drift (does Nova's overlay land on Nova's
    # own vocal?). This is the question the user is most likely asking.
    if doa:
        med = int(statistics.median(doa))
        p50 = statistics.median(abs(v) for v in doa)
        direction = "lag behind" if med > 0 else "lead"
        notes.append(
            f"<b>📍 Headline — Overlay vs Nova audio (C − D)</b>: p50 = {p50:.0f} ms "
            f"(signed median {med:+d} ms — Nova's overlays {direction} the vocal). "
            f"This isolates ALL overlay-timing issues into one number, independent of "
            f"YouTube. If this is large, the overlay isn't landing on the word being sung — "
            f"regardless of how YouTube renders the same song."
        )

    if dr and statistics.median(abs(v) for v in dr) > 50 and has_bundle:
        med = int(statistics.median(dr))
        notes.append(
            f"<b>Renderer drift</b> p50 = {statistics.median(abs(v) for v in dr):.0f} ms "
            f"(signed median {med:+d} ms). "
            f"Nova's burned-in overlays appear "
            f"{'later' if med > 0 else 'earlier'} than the recipe formula expects "
            f"(line.start_s − best_start_s − _LINE_PRE_ROLL_S). "
            "Suspect inject_lyric_overlays or text_overlay_skia.py."
        )

    if da and statistics.median(abs(v) for v in da) > 50 and has_bundle:
        med = int(statistics.median(da))
        notes.append(
            f"<b>Audio-mix drift</b> p50 = {statistics.median(abs(v) for v in da):.0f} ms "
            f"(signed median {med:+d} ms). "
            "Nova's audio is sung "
            f"{med:+d} ms relative to where best_start_s would predict it — "
            "check _mix_template_audio's `audio_start_offset_s` in music_orchestrate.py:670."
        )

    if dy and statistics.median(abs(v) for v in dy) > 50:
        med = int(statistics.median(dy))
        std = statistics.stdev(dy) if len(dy) > 1 else 0.0
        if std < 80:
            direction = "lag behind" if med > 0 else "lead"
            notes.append(
                f"<b>Nova vs YouTube</b> shows a near-constant {med:+d} ms {direction} "
                f"(σ={std:.0f} ms). Try bumping `_LINE_PRE_ROLL_S` in lyric_injector.py "
                f"by {-med / 1000:+.2f} s — that absorbs the systemic offset in one knob."
            )
        else:
            notes.append(
                f"<b>Nova vs YouTube</b> drift is variable: p50 = "
                f"{statistics.median(abs(v) for v in dy):.0f} ms, σ = {std:.0f} ms. "
                "Not a simple constant offset — multiple drift sources stacking."
            )

    if dyw and statistics.median(abs(v) for v in dyw) > 50:
        med = int(statistics.median(dyw))
        notes.append(
            f"<b>Whisper sync (Nova vs YouTube)</b> p50 = "
            f"{statistics.median(abs(v) for v in dyw):.0f} ms (signed {med:+d} ms). "
            f"This isolates audio-mix offset from overlay rendering — if it's near zero "
            f"while OCR drift is large, the audio is fine and the overlays are misplaced."
        )

    if confidence < 0.4:
        notes.append(
            f"⚠ Audio cross-correlation confidence is only {confidence:.2f} — the YouTube clip "
            "may be a different recording, mix, or cover. Treat YouTube-comparison columns with caution."
        )

    if not notes:
        notes.append(
            "All measured drift values are within 50 ms — pipeline timing is healthy. "
            "Any perceived sync issue is below typical perceptual thresholds."
        )

    if not has_bundle:
        notes.append(
            "<small>Note: --job-id was not provided (or admin API was unreachable). The "
            "<i>recipe-expected (A)</i>, <i>drift_render</i>, and <i>drift_audio_mix</i> columns "
            "require Nova's cached lyric timings and were skipped.</small>"
        )

    return "<br><br>".join(notes)


def _render_html(
    *,
    bundle: dict | None,
    diffs: list[LineDiff],
    confidence: float,
    youtube_url: str,
    nova_video: Path,
    yt_video: Path,
    yt_t_at_nova_zero_s: float,
    cache_dir: Path,
    diagnosis: str,
    ocr_fps: float,
) -> str:
    has_bundle = bool(bundle and bundle.get("lyrics_cached"))
    title = (
        f"{bundle['track_title']} — {bundle['track_artist']}"
        if has_bundle
        else f"Nova vs YouTube — {nova_video.name}"
    )

    best_start = float((bundle or {}).get("track_config", {}).get("best_start_s", 0.0))
    best_end = float((bundle or {}).get("track_config", {}).get("best_end_s", 0.0))
    style = ((bundle or {}).get("lyrics_config_effective") or {}).get("style", "?")
    job_id = (bundle or {}).get("job_id", "—")
    track_id = (bundle or {}).get("music_track_id", "—")
    job_status = (bundle or {}).get("job_status", "—")

    dr = [d.drift_render_ms for d in diffs if d.drift_render_ms is not None]
    da = [d.drift_audio_mix_ms for d in diffs if d.drift_audio_mix_ms is not None]
    dy = [d.drift_vs_youtube_ms for d in diffs if d.drift_vs_youtube_ms is not None]
    dyw = [
        d.drift_whisper_vs_youtube_ms for d in diffs if d.drift_whisper_vs_youtube_ms is not None
    ]
    doa = [d.drift_overlay_vs_audio_ms for d in diffs if d.drift_overlay_vs_audio_ms is not None]

    p50_r, p95_r = _stats(dr)
    p50_a, p95_a = _stats(da)
    p50_y, p95_y = _stats(dy)
    p50_yw, p95_yw = _stats(dyw)
    p50_oa, p95_oa = _stats(doa)

    # Identify dominant stage for highlight.
    stage_to_p50 = {
        "render": p50_r or 0,
        "audio_mix": p50_a or 0,
        "vs_yt": p50_y or 0,
        "wh_vs_yt": p50_yw or 0,
        "ov_audio": p50_oa or 0,
    }
    dominant = max(stage_to_p50, key=lambda k: stage_to_p50[k])

    def hl(name: str) -> str:
        return " highlight" if name == dominant and stage_to_p50[dominant] > 50 else ""

    if confidence < 0.3:
        banner = (
            f'<div class="banner bad">⚠ Audio cross-correlation confidence is very low '
            f"({confidence:.2f}). The YouTube reference probably isn't the same recording as Nova's mix. "
            f"The YouTube columns may be meaningless.</div>"
        )
    elif confidence < 0.5:
        banner = f'<div class="banner warn">⚠ Audio match confidence: {confidence:.2f} (borderline).</div>'
    else:
        banner = f'<div class="banner ok">✓ Audio match confidence: {confidence:.2f}</div>'

    # Build table rows.
    rows: list[str] = []
    for d in diffs:
        nova_thumb = (
            _ffmpeg_thumb_b64(nova_video, d.C_nova_ocr_section_s, cache_dir)
            if d.C_nova_ocr_section_s is not None
            else None
        )
        yt_thumb = None
        if d.E_yt_ocr_section_s is not None:
            # nova_section_t → yt absolute = nova_t + yt_t_at_nova_zero
            yt_abs = d.E_yt_ocr_section_s + yt_t_at_nova_zero_s
            yt_thumb = _ffmpeg_thumb_b64(yt_video, yt_abs, cache_dir)

        thumb_id = f"line-{d.line_idx}"
        thumbs_html = ""
        if nova_thumb or yt_thumb:
            thumbs_html = (
                f'<tr class="row-thumbs" data-thumb-id="{thumb_id}"><td colspan="10"><div class="thumbs">'
                f"<figure><figcaption>Nova @ {_fmt_s(d.C_nova_ocr_section_s)}s — OCR: "
                f'"{_esc_html(d.matched_nova_ocr_text[:80])}"</figcaption>'
                + (
                    f'<img src="{nova_thumb}" alt="">'
                    if nova_thumb
                    else '<span class="sub">no frame</span>'
                )
                + "</figure>"
                f'<figure><figcaption>YouTube — OCR: "{_esc_html(d.matched_yt_ocr_text[:80])}"</figcaption>'
                + (
                    f'<img src="{yt_thumb}" alt="">'
                    if yt_thumb
                    else '<span class="sub">no frame</span>'
                )
                + "</figure></div></td></tr>"
            )

        rows.append(
            f'<tr class="row-line" data-thumb-id="{thumb_id}">'
            f'<td class="numeric">{d.line_idx}</td>'
            f'<td class="text-cell" title="{_esc_attr(d.text)}">{_esc_html(d.text[:70])}</td>'
            f'<td class="numeric">{_fmt_s(d.A_expected_section_s)}</td>'
            f'<td class="numeric">{_fmt_s(d.C_nova_ocr_section_s)}</td>'
            f'<td class="numeric">{_fmt_s(d.D_nova_whisper_section_s)}</td>'
            f'<td class="numeric">{_fmt_s(d.E_yt_ocr_section_s)}</td>'
            f'<td class="numeric">{_fmt_s(d.F_yt_whisper_section_s)}</td>'
            f'<td class="drift {_drift_cls(d.drift_render_ms)}">{_fmt_drift(d.drift_render_ms)}</td>'
            f'<td class="drift {_drift_cls(d.drift_audio_mix_ms)}">{_fmt_drift(d.drift_audio_mix_ms)}</td>'
            f'<td class="drift {_drift_cls(d.drift_vs_youtube_ms)}">{_fmt_drift(d.drift_vs_youtube_ms)}</td>'
            "</tr>"
        )
        if thumbs_html:
            rows.append(thumbs_html)

    raw = json.dumps(
        [
            {
                "line_idx": d.line_idx,
                "text": d.text,
                "line_start_track_s": d.line_start_track_s,
                "A_expected_section_s": d.A_expected_section_s,
                "C_nova_ocr_section_s": d.C_nova_ocr_section_s,
                "D_nova_whisper_section_s": d.D_nova_whisper_section_s,
                "E_yt_ocr_section_s": d.E_yt_ocr_section_s,
                "F_yt_whisper_section_s": d.F_yt_whisper_section_s,
                "drift_render_ms": d.drift_render_ms,
                "drift_audio_mix_ms": d.drift_audio_mix_ms,
                "drift_vs_youtube_ms": d.drift_vs_youtube_ms,
                "drift_whisper_vs_youtube_ms": d.drift_whisper_vs_youtube_ms,
                "drift_overlay_vs_audio_ms": d.drift_overlay_vs_audio_ms,
                "matched_nova_ocr": d.matched_nova_ocr_text,
                "matched_yt_ocr": d.matched_yt_ocr_text,
            }
            for d in diffs
        ],
        indent=2,
    )

    return _HTML_TEMPLATE.format(
        title=_esc_html(title),
        job_id=_esc_html(str(job_id)),
        music_track_id=_esc_html(str(track_id)),
        job_status=_esc_html(str(job_status)),
        best_start_s=f"{best_start:.3f}",
        best_end_s=f"{best_end:.3f}",
        style=_esc_html(str(style)),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        youtube_url=_esc_attr(youtube_url),
        ocr_fps=ocr_fps,
        banner_html=banner,
        cls_render=hl("render"),
        cls_audio=hl("audio_mix"),
        cls_vs_yt=hl("vs_yt"),
        cls_wh_vs_yt=hl("wh_vs_yt"),
        p50_render="—" if p50_r is None else p50_r,
        p95_render="—" if p95_r is None else p95_r,
        n_render=len(dr),
        p50_audio="—" if p50_a is None else p50_a,
        p95_audio="—" if p95_a is None else p95_a,
        n_audio=len(da),
        p50_vs_yt="—" if p50_y is None else p50_y,
        p95_vs_yt="—" if p95_y is None else p95_y,
        n_vs_yt=len(dy),
        p50_wh_vs_yt="—" if p50_yw is None else p50_yw,
        p95_wh_vs_yt="—" if p95_yw is None else p95_yw,
        n_wh_vs_yt=len(dyw),
        diagnosis_html=diagnosis,
        n_lines=len(diffs),
        table_rows="\n".join(rows),
        raw_json=_esc_html(raw),
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Lyric sync diff — {title}</title>
<style>
:root {{
  --bg: #0e1116; --panel: #151b23; --border: #232a35; --text: #e6edf3;
  --muted: #8b949e; --accent: #58a6ff; --ok: #3fb950; --warn: #d29922; --bad: #f85149;
}}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 24px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 13px; margin: 24px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
a {{ color: var(--accent); }}
.meta {{ color: var(--muted); margin-bottom: 12px; font-size: 13px; }}
code {{ background: var(--panel); padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
.cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 12px 0; }}
.card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }}
.card h3 {{ margin: 0 0 6px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
.card .big {{ font-size: 22px; font-weight: 600; font-feature-settings: "tnum"; }}
.card .sub {{ color: var(--muted); font-size: 12px; }}
.card.highlight {{ border-color: var(--bad); box-shadow: 0 0 0 1px var(--bad) inset; }}
.banner {{ padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-weight: 600; }}
.banner.ok {{ background: rgba(63,185,80,0.12); color: var(--ok); border: 1px solid rgba(63,185,80,0.3); }}
.banner.warn {{ background: rgba(210,153,34,0.12); color: var(--warn); border: 1px solid rgba(210,153,34,0.3); }}
.banner.bad {{ background: rgba(248,81,73,0.12); color: var(--bad); border: 1px solid rgba(248,81,73,0.3); }}
table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; font-size: 13px; margin-top: 8px; }}
th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ background: #1c232c; color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; }}
tr.row-line {{ cursor: pointer; }}
tr.row-line:hover {{ background: rgba(88, 166, 255, 0.06); }}
tr.row-thumbs {{ display: none; background: #11161d; }}
tr.row-thumbs.open {{ display: table-row; }}
tr.row-thumbs td {{ padding: 12px; }}
.thumbs {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.thumbs figure {{ margin: 0; }}
.thumbs figcaption {{ color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
.thumbs img {{ max-width: 100%; border-radius: 6px; border: 1px solid var(--border); }}
.numeric {{ font-feature-settings: "tnum"; text-align: right; }}
.drift {{ font-weight: 600; font-feature-settings: "tnum"; text-align: right; }}
.drift-ok {{ color: var(--ok); }} .drift-warn {{ color: var(--warn); }} .drift-bad {{ color: var(--bad); }} .drift-na {{ color: var(--muted); }}
.text-cell {{ max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
details {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 24px; }}
details pre {{ overflow-x: auto; font-size: 12px; }}
.legend {{ color: var(--muted); font-size: 12px; margin: 6px 0 12px; }}
</style>
</head>
<body>

<h1>Lyric sync diff — {title}</h1>
<p class="meta">
  Job <code>{job_id}</code> · Track <code>{music_track_id}</code> · Status <code>{job_status}</code><br>
  Best section <code>[{best_start_s}s, {best_end_s}s]</code> · Style <code>{style}</code> · OCR @ {ocr_fps} fps<br>
  Generated <code>{generated_at}</code> · YouTube reference: <a href="{youtube_url}">{youtube_url}</a>
</p>

{banner_html}

<h2>Drift summary (median absolute, milliseconds)</h2>
<div class="cards">
  <div class="card{cls_render}"><h3>Renderer (C − A)</h3><div class="big">{p50_render}<span class="sub"> ms p50</span></div><div class="sub">p95 {p95_render} ms · n={n_render}</div></div>
  <div class="card{cls_audio}"><h3>Audio mix (D − expected)</h3><div class="big">{p50_audio}<span class="sub"> ms p50</span></div><div class="sub">p95 {p95_audio} ms · n={n_audio}</div></div>
  <div class="card{cls_vs_yt}"><h3>vs YouTube OCR (C − E)</h3><div class="big">{p50_vs_yt}<span class="sub"> ms p50</span></div><div class="sub">p95 {p95_vs_yt} ms · n={n_vs_yt}</div></div>
  <div class="card{cls_wh_vs_yt}"><h3>vs YouTube Whisper (D − F)</h3><div class="big">{p50_wh_vs_yt}<span class="sub"> ms p50</span></div><div class="sub">p95 {p95_wh_vs_yt} ms · n={n_wh_vs_yt}</div></div>
</div>
<p class="legend">
  <b>A</b> = recipe-expected start (line.start − best_start − pre_roll). <b>C</b> = Nova OCR-detected start.
  <b>D</b> = Nova Whisper sung-at, in Nova-section coords. <b>E</b> = YouTube OCR mapped to Nova-section coords.
  <b>F</b> = YouTube Whisper mapped to Nova-section coords.<br>
  Color: <span class="drift drift-ok">≤50 ms</span> · <span class="drift drift-warn">≤100 ms</span> · <span class="drift drift-bad">&gt;100 ms</span>.
</p>

<h2>Diagnosis</h2>
<p class="legend">{diagnosis_html}</p>

<h2>Per-line breakdown ({n_lines} lines)</h2>
<table>
<thead><tr>
  <th>#</th><th>Line</th>
  <th class="numeric">A</th>
  <th class="numeric">C nova OCR</th><th class="numeric">D nova Whisper</th>
  <th class="numeric">E yt OCR</th><th class="numeric">F yt Whisper</th>
  <th class="drift">drift_render</th>
  <th class="drift">drift_audio_mix</th>
  <th class="drift">drift_vs_youtube</th>
</tr></thead>
<tbody>
{table_rows}
</tbody>
</table>

<details>
<summary>Raw JSON</summary>
<pre>{raw_json}</pre>
</details>

<script>
document.querySelectorAll('tr.row-line').forEach(row => {{
  row.addEventListener('click', () => {{
    const id = row.dataset.thumbId;
    const thumbs = document.querySelector('tr.row-thumbs[data-thumb-id="' + id + '"]');
    if (thumbs) thumbs.classList.toggle('open');
  }});
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    cache_dir = Path(args.cache_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── [1] Try to enrich from Nova admin API
    bundle = _fetch_job_bundle_admin(args)

    # ── [2] Resolve Nova mp4
    if args.nova_mp4:
        nova_video = Path(args.nova_mp4).resolve()
        if not nova_video.exists():
            raise SystemExit(f"--nova-mp4 not found: {nova_video}")
    elif bundle and bundle.get("output_url"):
        nova_video = cache_dir / f"nova_{bundle['job_id']}.mp4"
        _http_download(bundle["output_url"], nova_video)
    else:
        raise SystemExit(
            "No Nova video source: pass --nova-mp4 PATH or set up admin API access for --job-id."
        )

    yt_id = hashlib.sha1(args.youtube_url.encode()).hexdigest()[:12]
    yt_video = cache_dir / f"yt_{yt_id}.mp4"
    _yt_dlp_download(args.youtube_url, yt_video)

    # ── [3] Audio extract + cross-correlation
    nova_wav = cache_dir / f"{nova_video.stem}_{_sha_short(nova_video)}.wav"
    yt_wav = cache_dir / f"yt_{yt_id}.wav"
    _ffmpeg_extract_wav(nova_video, nova_wav)
    _ffmpeg_extract_wav(yt_video, yt_wav)

    nova_pcm = _read_wav(nova_wav)
    yt_pcm = _read_wav(yt_wav)

    best_start = (
        args.best_start_s
        if args.best_start_s is not None
        else float((bundle or {}).get("track_config", {}).get("best_start_s", 0.0))
    )
    best_end = float((bundle or {}).get("track_config", {}).get("best_end_s", 0.0))

    yt_t_at_nova_zero, confidence, _win_start = _align_audio(
        nova_pcm, yt_pcm, best_start or None, best_end or None
    )

    # Define mapping helper: yt_absolute → nova_section
    def yt_to_nova(yt_abs_s: float) -> float:
        return yt_abs_s - yt_t_at_nova_zero

    # And: track_absolute (in lyrics_cached) → yt_absolute approximation:
    # If bundle exists, the cached lyric `line.start_s` is in the YouTube source
    # track timeline. So track_abs == yt_abs (modulo the recording variant).
    # Then nova_section = yt_to_nova(line.start_s) = line.start_s - yt_t_at_nova_zero.
    # The yt_t_at_nova_zero ALSO encodes best_start_s — so when bundle exists,
    # yt_t_at_nova_zero ≈ best_start_s if the YouTube reference matches.

    # ── [4] OCR both videos
    def _parse_band(s: str) -> tuple[float, float] | None:
        try:
            a, b = (float(x) for x in s.split(","))
        except ValueError as exc:
            raise SystemExit(
                f"--*-band must be 'y_start,y_end' (e.g. '0,1' or '0.72,1.0'); got {s!r}"
            ) from exc
        if a < 0 or b > 1 or b <= a:
            raise SystemExit(f"band {a},{b} must satisfy 0 ≤ y_start < y_end ≤ 1")
        # Full frame → skip the crop+upscale entirely (saves a copy + speeds things up).
        return None if (a <= 0.0 and b >= 1.0) else (a, b)

    nova_band = _parse_band(args.nova_band)
    yt_band = _parse_band(args.yt_band)
    nova_ocr = _ocr_phrases(nova_video, fps=args.ocr_fps, cache_dir=cache_dir, lyric_band=nova_band)
    yt_ocr = _ocr_phrases(yt_video, fps=args.ocr_fps, cache_dir=cache_dir, lyric_band=yt_band)
    # YouTube-has-no-lyrics signal: if YT OCR finds drastically fewer phrases
    # than Nova (or near-zero absolute), warn loudly. Likely an official music
    # video instead of a lyric video.
    yt_has_lyrics = len(yt_ocr) >= max(3, len(nova_ocr) * 2)
    if not yt_has_lyrics:
        _log(
            f"⚠ YouTube OCR found only {len(yt_ocr)} phrases vs Nova's {len(nova_ocr)} — "
            "the YouTube URL may be a music video without burned-in lyrics. "
            "For audio-based comparison, enable Whisper (--openai-key or env OPENAI_API_KEY)."
        )

    # ── [5] Whisper both (optional)
    nova_whisper: list[dict] = []
    yt_whisper: list[dict] = []
    if not args.no_whisper:
        if not args.openai_key:
            _log("no --openai-key / OPENAI_API_KEY env; skipping Whisper")
        else:
            prompt = ""
            if bundle and bundle.get("lyrics_cached"):
                prompt = bundle["lyrics_cached"].get("full_text") or ""
                if not prompt:
                    prompt = "\n".join(
                        ln.get("text", "")
                        for ln in (bundle["lyrics_cached"].get("lines", []) or [])
                    )
            try:
                nova_whisper = _whisper_words(nova_wav, prompt, args.openai_key, cache_dir)
                yt_whisper = _whisper_words(yt_wav, prompt, args.openai_key, cache_dir)
            except Exception as exc:  # noqa: BLE001
                _log(f"WARN: Whisper failed ({exc!r}); continuing without")

    # ── [6] Build the line set
    # Preference order:
    #   1. lyrics_cached (most authoritative — requires --job-id + admin)
    #   2. nova_ocr — what's ACTUALLY displayed on screen (the user's product).
    #      Whisper transcripts cover everything sung; OCR covers what was rendered.
    #      The user's question is "does the rendered overlay land on the vocal?"
    #      so iterate over rendered overlays. Whisper still fills D/F columns.
    #   3. nova_whisper — only when no overlay rendered (e.g. lyrics_config disabled).
    if bundle and bundle.get("lyrics_cached", {}).get("lines"):
        lines = bundle["lyrics_cached"]["lines"]
        line_source = "lyrics_cached"
    elif nova_ocr:
        # Each Nova OCR phrase becomes a "line". Shift to pseudo-absolute.
        track_anchor = best_start if best_start else yt_t_at_nova_zero
        lines = [
            {
                "text": p["text"],
                "start_s": float(p["start_s"]) + track_anchor,
                "end_s": float(p["end_s"]) + track_anchor,
            }
            for p in nova_ocr
        ]
        line_source = "nova-ocr"
    elif nova_whisper:
        lines = _build_lines_from_whisper(nova_whisper)
        track_anchor = best_start if best_start else yt_t_at_nova_zero
        for ln in lines:
            ln["start_s"] = float(ln["start_s"]) + track_anchor
            ln["end_s"] = float(ln["end_s"]) + track_anchor
        line_source = "whisper-derived"
    else:
        lines = []
        line_source = "(none)"

    _log(f"line source: {line_source} — {len(lines)} lines")
    if not lines:
        raise SystemExit(
            "No lyric source available. Provide one of:\n"
            "  --job-id <UUID> + --admin-key (or env ADMIN_PROD_API_KEY) — uses MusicTrack.lyrics_cached\n"
            "  --openai-key (or env OPENAI_API_KEY) — Whisper-transcribes the audio\n"
            "  Or check that pytesseract is installed (`pip install --user pytesseract`) so the Nova-OCR fallback works."
        )

    # ── [7] Diff per line
    diffs: list[LineDiff] = []
    nova_duration_s = len(nova_pcm) / _AUDIO_SR
    for idx, ln in enumerate(lines):
        text = ln.get("text", "")
        if not text:
            continue
        start_track_s = float(ln.get("start_s", 0.0))
        words = _norm_words(text)
        if not words:
            continue
        first_word = words[0]

        # A: recipe-expected start in Nova-section time. Meaningful only when
        # the line source carries true track-absolute timings (lyrics_cached).
        # In nova-ocr or whisper-derived modes the "line.start_s" was synthesized
        # from Nova's own timeline — computing A from it would just recover
        # (C ± constant) and the drift_render column would be a math artifact.
        if bundle and best_end > best_start and line_source == "lyrics_cached":
            in_section = best_start <= start_track_s <= best_end
            A = (start_track_s - best_start - _LINE_PRE_ROLL_S) if in_section else None
        else:
            A = None
            anchor = best_start if best_start else yt_t_at_nova_zero
            in_section = 0 <= (start_track_s - anchor) <= nova_duration_s

        if not in_section:
            continue

        # C: Nova OCR — search at A when available, else at line's section time.
        expected_c = (
            A
            if A is not None
            else (start_track_s - (best_start if best_start else yt_t_at_nova_zero))
        )
        nova_ph = _nearest_ocr(words, nova_ocr, expected_t_s=expected_c)
        C = float(nova_ph["start_s"]) if nova_ph else None

        # D: Nova Whisper first word (Nova-section coords)
        nova_w = _first_whisper_match(first_word, nova_whisper, expected_t_s=expected_c)
        D = float(nova_w["start_s"]) if nova_w else None

        # E: YouTube OCR (mapped from YouTube absolute → Nova section)
        # Expected YouTube absolute = start_track_s (lyrics_cached is in track time
        # which == YouTube absolute time for the source).
        yt_ph = _nearest_ocr(words, yt_ocr, expected_t_s=start_track_s, radius_s=3.0)
        E_yt_abs = float(yt_ph["start_s"]) if yt_ph else None
        E = yt_to_nova(E_yt_abs) if E_yt_abs is not None else None

        # F: YouTube Whisper first word
        yt_w = _first_whisper_match(first_word, yt_whisper, expected_t_s=start_track_s)
        F_yt_abs = float(yt_w["start_s"]) if yt_w else None
        F = yt_to_nova(F_yt_abs) if F_yt_abs is not None else None

        d = LineDiff(
            line_idx=idx,
            text=text,
            line_start_track_s=start_track_s,
            A_expected_section_s=A,
            C_nova_ocr_section_s=C,
            D_nova_whisper_section_s=D,
            E_yt_ocr_section_s=E,
            F_yt_whisper_section_s=F,
            matched_nova_ocr_text=(nova_ph["text"][:80] if nova_ph else ""),
            matched_yt_ocr_text=(yt_ph["text"][:80] if yt_ph else ""),
        )
        d._best_start_s = best_start if best_start else None
        diffs.append(d)

    _log(f"matched {len(diffs)} lines for comparison")

    diagnosis = _build_diagnosis(diffs, confidence, has_bundle=bool(bundle))
    if not yt_has_lyrics:
        diagnosis = (
            f"<b>⚠ YouTube has no detected on-screen lyrics</b> — found only "
            f"{len(yt_ocr)} phrase(s) vs Nova's {len(nova_ocr)}. The provided URL "
            f"is most likely an official music video, not a lyric video. The OCR-vs-OCR "
            f"comparison columns (E, F, drift_vs_youtube) will be empty.<br><br>"
            f"To compare Nova's overlay sync against the YouTube audio (when each "
            f"word is SUNG), enable Whisper: re-run with "
            f"<code>--openai-key &lt;OPENAI_API_KEY&gt;</code> "
            f"(or <code>export OPENAI_API_KEY=…</code>). That populates the D + F "
            f"columns and gives you a real audio-vs-overlay drift answer.<br><br>"
        ) + diagnosis
    html = _render_html(
        bundle=bundle,
        diffs=diffs,
        confidence=confidence,
        youtube_url=args.youtube_url,
        nova_video=nova_video,
        yt_video=yt_video,
        yt_t_at_nova_zero_s=yt_t_at_nova_zero,
        cache_dir=cache_dir,
        diagnosis=diagnosis,
        ocr_fps=args.ocr_fps,
    )

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = (bundle or {}).get("job_id", nova_video.stem)[:24]
    out_path = out_dir / f"{tag}_{ts}.html"
    out_path.write_text(html)
    _log(f"✓ wrote {out_path}")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
