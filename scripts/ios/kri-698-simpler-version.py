#!/usr/bin/env python3
"""Repeatable KRI-698 approval-to-export proof with synthetic, identifiable media.

Not an original-footage replay or live-model test. Prepare runs the real-Postgres
approval journey, phone compiler, immutable device pin and publication guard.
Export that exact pinned recipe with the existing native package harness:

  src/apps/api/.venv/bin/python scripts/ios/kri-698-simpler-version.py prepare OUT [--automatic]
  KRIA_AUDIO_PARITY_DIR=OUT swift test --package-path src/apps/ios/Packages/KriaMediaEngine \
    --filter AudioParityFixtureTests
  src/apps/api/.venv/bin/python scripts/ios/kri-698-simpler-version.py compare OUT

The movie must keep the title in its approved window, all three clips in order,
and the narration throughout. Missing unwaived rules and a missing retained title
must still be refused by the real verifier. A no-title twin export proves the
frame comparison can detect a dropped title. No timeline behavior is changed:
duration belongs to narration; ripple, scrub, resize and Undo are unaffected.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
COLORS = ("803040", "306080", "408030")


def ffmpeg(*args: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True
    ).stdout


def prepare(out: Path, *, automatic: bool = False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # A failed fresh export must never leave a previous run's movie as evidence.
    for name in ("approved", "missing_title"):
        (out / "cases" / name / "phone.mp4").unlink(missing_ok=True)
    for index, color in enumerate(COLORS, 1):
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x{color}:s=320x568:r=30:d=3",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            str(out / f"clip-{index}.mp4"),
        )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=650:sample_rate=48000:duration=6",
        "-c:a",
        "pcm_s16le",
        str(out / "voice.wav"),
    )
    subprocess.run(
        [
            "bash",
            "scripts/check-api.sh",
            "test",
            "tests/kria/test_runtime_automatic_simplification.py::"
            "test_first_turn_automatic_omission_reaches_native_phone_recipe"
            if automatic
            else "tests/kria/test_runtime_simpler_version_loop.py::"
            "test_simpler_approval_compiles_and_pins_while_full_request_still_refuses",
            "-q",
        ],
        cwd=REPO,
        env={**os.environ, "KRIA_SIMPLER_EXPORT_DIR": str(out)},
        check=True,
    )


def frame(movie: Path, seconds: float) -> Image.Image:
    return Image.open(
        io.BytesIO(
            ffmpeg(
                "-ss",
                str(seconds),
                "-i",
                str(movie),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "png",
                "-",
            )
        )
    ).convert("RGB")


def compare(out: Path) -> None:
    movie = out / "cases/approved/phone.mp4"
    twin = out / "cases/missing_title/phone.mp4"
    for path in (movie, twin):
        if path.stat().st_mtime_ns < path.with_name("recipe.json").stat().st_mtime_ns:
            raise SystemExit(f"Stale export: {path}; rerun the native exporter")
    frames = [frame(movie, t) for t in (0.8, 2.8, 4.8)]
    differences = [
        float(
            np.abs(
                np.asarray(image, dtype=float) - np.asarray(frame(twin, t), dtype=float)
            ).mean()
        )
        for image, t in zip(frames, (0.8, 2.8, 4.8), strict=True)
    ]
    observed = [np.median(np.asarray(image).reshape(-1, 3), axis=0) for image in frames]
    expected = [
        np.array([int(color[i : i + 2], 16) for i in (0, 2, 4)]) for color in COLORS
    ]
    errors = [
        float(np.abs(a - b).max()) for a, b in zip(observed, expected, strict=True)
    ]
    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(movie),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    samples = np.frombuffer(
        ffmpeg(
            "-i",
            str(movie),
            "-map",
            "0:a",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ),
        dtype="<f4",
    )
    peaks, rms = [], []
    for t in (0.8, 2.8, 4.8):
        window = samples[int(t * 48000) : int((t + 0.3) * 48000)]
        spectrum = abs(np.fft.rfft(window))
        rms.append(float(np.sqrt(np.mean(window**2))))
        peaks.append(float(np.fft.rfftfreq(len(window), 1 / 48000)[spectrum.argmax()]))
    checks = {
        "six_second_narration_length": abs(float(probe["format"]["duration"]) - 6)
        <= 0.1,
        "clip_order_and_all_three_clips": max(errors) < 15,
        "title_visible_in_approved_window": differences[0] > 0.1,
        "title_ends_when_approved": max(differences[1:]) < 0.1,
        "missing_title_negative_control_detected": differences[0] > 0.1,
        "narration_throughout": all(abs(peak - 650) < 10 for peak in peaks)
        and min(rms) > 0.02,
        "h264_aac": {s["codec_name"] for s in probe["streams"]} >= {"h264", "aac"},
    }
    report = {
        "pinned_recipe_sha256": hashlib.sha256(
            movie.with_name("recipe.json").read_bytes()
        ).hexdigest(),
        "export_sha256": hashlib.sha256(movie.read_bytes()).hexdigest(),
        "checks": checks,
        "title_frame_difference": differences,
        "clip_color_errors": errors,
        "narration_peak_hz": peaks,
        "narration_rms": rms,
        "evidence": "Synthetic media; real persisted approval, compiler and macOS native exporter. "
        "No live model, production deployment or physical iPhone claim.",
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    montage = Image.new("RGB", (360 * 3, 680), "#141414")
    draw = ImageDraw.Draw(montage)
    for i, image in enumerate(frames):
        image.thumbnail((360, 640))
        montage.paste(image, (i * 360, 30))
        draw.text(
            (i * 360 + 10, 10),
            ("Title + clip 1", "Clip 2; title ended", "Clip 3; narration continues")[i],
            fill="white",
        )
    montage.save(out / "montage.png")
    print(json.dumps(report, indent=2))
    if not all(checks.values()):
        raise SystemExit("Export verification failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "compare"))
    parser.add_argument("out", type=Path)
    parser.add_argument(
        "--automatic",
        action="store_true",
        help="exercise first-turn automatic omission instead of the explicit simpler answer",
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.out.resolve(), automatic=args.automatic)
    else:
        compare(args.out.resolve())
