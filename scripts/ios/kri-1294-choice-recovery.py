#!/usr/bin/env python3
"""Compare prepared KRI-1294 recovery and missing-text native exports.

Usage: src/apps/api/.venv/bin/python scripts/ios/kri-1294-choice-recovery.py OUT
Preparation and native export commands: docs/reviews/kri-1294-choice-request.md.
Synthetic footage and authored provider responses only; never a production claim.
"""

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
import numpy as np
from PIL import Image

root = Path(sys.argv[1]).resolve()
movie = root / "cases/approved/phone.mp4"
twin = root / "cases/missing_text/phone.mp4"
for path in (movie, twin):
    if path.stat().st_mtime_ns < path.with_name("recipe.json").stat().st_mtime_ns:
        raise SystemExit(f"Stale native export: {path}; rerun AudioParityFixtureTests")


def frame(path, t):
    return np.array(
        Image.open(
            io.BytesIO(
                subprocess.check_output(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-ss",
                        str(t),
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-f",
                        "image2pipe",
                        "-c:v",
                        "png",
                        "-",
                    ]
                )
            )
        ).convert("RGB")
    )


def probe(path):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        )
    )


p = probe(movie)
duration = float(p["format"]["duration"])
records = []
for t in (0.1, duration / 2, duration - 0.1):
    a, b = frame(movie, t), frame(twin, t)
    mask = a.min(axis=2) > 210
    ys, xs = np.where(mask)
    records.append(
        {
            "t": round(t, 3),
            "mean_frame_diff": float(abs(a.astype(float) - b).mean()),
            "white_pixels": int(mask.sum()),
            "center_y": float((ys.min() + ys.max()) / 2 / a.shape[0]),
        }
    )
Image.fromarray(frame(movie, duration / 2)).save(root / "frame.png")
samples = np.frombuffer(
    subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
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
        ]
    ),
    dtype="<f4",
)
rms = float(np.sqrt(np.mean(samples**2)))
checks = {
    "duration_matches_approved_4_5s": abs(duration - 4.5) < 0.04,
    "portrait_1080x1920": any(
        s.get("width") == 1080 and s.get("height") == 1920 for s in p["streams"]
    ),
    "silent_source_stays_silent": rms < 0.00001,
    "text_visible_throughout": all(
        r["white_pixels"] > 500 and r["mean_frame_diff"] > 0.05 for r in records
    ),
    "middle_placement": all(abs(r["center_y"] - 0.5) < 0.05 for r in records),
    "missing_text_negative_control_detected": all(
        r["mean_frame_diff"] > 0.05 for r in records
    ),
}
report = {
    "checks": checks,
    "audio_rms": rms,
    "duration_s": duration,
    "samples": records,
    "recipe_sha256": hashlib.sha256(
        movie.with_name("recipe.json").read_bytes()
    ).hexdigest(),
    "mp4_sha256": hashlib.sha256(movie.read_bytes()).hexdigest(),
    "evidence": "Synthetic original bytes; authored provider responses; real approval/source bindings, text composer, compiler, macOS AVFoundation exporter. Not original-footage/live-model/production proof.",
}
(root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
assert all(checks.values())
