#!/usr/bin/env python3
"""Repeatable device check for KRI-1264: a Talking picture card at 2x zoom with a Pop entrance.

The chain is the real one, end to end: the server's editor projection of a plain card -> the iPhone
editor session (Zoom 2, Pop) -> the Save payload it sends -> the server's editor Save and Talking
compile -> the pinned recipe -> the device's recipe decoder and production exporter -> the MP4.

Run from the repo root (any worktree), after a `build-for-testing` of the Kria scheme:

  src/apps/api/.venv/bin/python scripts/ios/card-style-e2e.py all OUT --simulator UDID

or one step at a time: `prepare`, `app`, `save`, `render`, `score` (same OUT).

prepare: pins a Talking edit (4 s grey take, a green 640x640 photo with a red corner marker on
  1.0-3.0 s) and writes `editor.json` (what the app loads) and `cases/plain`.
app: the simulator test writes `save.json`, the `media_overlays` the app's Save sends.
save: runs `save.json` through the server's real editor Save and writes `cases/zoom2_pop`.
render: the simulator exports every case to `cases/<name>/phone.mp4`.
score: reads frames from each MP4 and checks the card against the requirement: the plain card is
  0.35 of the frame wide and still; the 2x card is twice as wide about the same centre once
  settled, smaller and fainter 0.1 s into its 0.4 s Pop, and gone outside its window. Writes
  `report.json` and `montage.png`; exits 1 on any failed check.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
IOS = REPO / "src/apps/ios"
TEST_CLASS = "KriaTests/DeviceTalkingCardStyleRenderE2ETests"
TEST_ENV = {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}
CANVAS_W, CANVAS_H = 1080, 1920
CARD_FRACTION = 0.35
SAMPLES = (0.5, 1.1, 2.0, 3.5)


def _pytest(out: Path, select: str) -> None:
    env = {**os.environ, **TEST_ENV, "KRIA_CARD_STYLE_E2E_OUT": str(out.resolve())}
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", select,
         "tests/routes/test_card_style_device_cases.py"],
        cwd=API, env=env, check=True,
    )  # fmt: skip


def _xcode(out: Path, test: str, simulator: str) -> None:
    env = {**os.environ, "TEST_RUNNER_KRIA_CARD_STYLE_E2E_DIR": str(out.resolve())}
    subprocess.run(
        ["xcodebuild", "-project", "Kria.xcodeproj", "-scheme", "Kria",
         "-skipPackagePluginValidation", "-derivedDataPath", ".derived-data",
         "CODE_SIGNING_ALLOWED=NO",
         "-destination", f"platform=iOS Simulator,id={simulator}",
         f"-only-testing:{TEST_CLASS}/{test}", "test-without-building"],
        cwd=IOS, env=env, check=True,
    )  # fmt: skip


def _frame(movie: Path, at: float):
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{at}", "-i", str(movie),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.uint8).reshape(CANVAS_H, CANVAS_W, 3).astype(int)


def _card_box(frame) -> dict | None:
    """The green photo plus its red marker: colour-dominant pixels, so captions and the grey take
    never count."""
    r, g, b = frame[..., 0], frame[..., 1], frame[..., 2]
    green = (g > 90) & (g > r + 50) & (g > b + 50)
    red = (r > 90) & (r > g + 50) & (r > b + 50)
    ys, xs = (green | red).nonzero()
    if len(xs) < 50:
        return None
    centre = frame[(ys.min() + ys.max()) // 2, (xs.min() + xs.max()) // 2]
    return {
        "min_x": int(xs.min()),
        "max_x": int(xs.max()),
        "min_y": int(ys.min()),
        "max_y": int(ys.max()),
        "width": int(xs.max() - xs.min() + 1),
        "centre_x": (int(xs.min()) + int(xs.max())) / 2,
        "centre_y": (int(ys.min()) + int(ys.max())) / 2,
        "centre_green": int(centre[1]),
    }


def score(out: Path) -> int:
    from PIL import Image, ImageDraw

    boxes: dict[str, dict[float, dict | None]] = {}
    frames = []
    for name in ("plain", "zoom2_pop"):
        movie = out / "cases" / name / "phone.mp4"
        boxes[name] = {}
        for at in SAMPLES:
            frame = _frame(movie, at)
            boxes[name][at] = _card_box(frame)
            frames.append((f"{name} t={at}s", frame))
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: object) -> None:
        checks.append((label, bool(ok), str(detail)))

    plain, styled = boxes["plain"], boxes["zoom2_pop"]
    expected = CARD_FRACTION * CANVAS_W
    for name, by_time in boxes.items():
        check(f"{name}: no card before its window (0.5 s)", by_time[0.5] is None, by_time[0.5])
        check(f"{name}: no card after its window (3.5 s)", by_time[3.5] is None, by_time[3.5])
    p11, p20, s11, s20 = plain[1.1], plain[2.0], styled[1.1], styled[2.0]
    if None in (p11, p20, s11, s20):
        check("every in-window frame shows the card", False, {"plain": plain, "zoom2_pop": styled})
    else:
        check("plain card is 0.35 of the frame wide", abs(p20["width"] - expected) <= 8, p20)
        check("plain card does not animate", abs(p11["width"] - p20["width"]) <= 2, (p11, p20))
        ratio = s20["width"] / p20["width"]
        check("2x card is twice as wide once settled", abs(ratio - 2) <= 0.06, round(ratio, 3))
        check(
            "2x card keeps the plain card's centre",
            abs(s20["centre_x"] - p20["centre_x"]) <= 4
            and abs(s20["centre_y"] - p20["centre_y"]) <= 4,
            (s20["centre_x"], s20["centre_y"], p20["centre_x"], p20["centre_y"]),
        )
        entering = s11["width"] / s20["width"]
        check("0.1 s into the Pop the card is smaller", 0.75 < entering < 0.95, round(entering, 3))
        check(
            "0.1 s into the Pop the card is fainter",
            s11["centre_green"] < s20["centre_green"] - 20,
            (s11["centre_green"], s20["centre_green"]),
        )
    report = {
        "cases": {
            name: {str(at): box for at, box in by_time.items()} for name, by_time in boxes.items()
        },
        "checks": [{"check": label, "pass": ok, "detail": detail} for label, ok, detail in checks],
        "pass": all(ok for _, ok, _ in checks),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))

    thumb_w, thumb_h = 270, 480
    sheet = Image.new("RGB", (thumb_w * len(SAMPLES), (thumb_h + 24) * 2), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, frame) in enumerate(frames):
        row, column = divmod(index, len(SAMPLES))
        image = Image.fromarray(frame.astype("uint8")).resize((thumb_w, thumb_h))
        sheet.paste(image, (column * thumb_w, row * (thumb_h + 24) + 24))
        draw.text((column * thumb_w + 6, row * (thumb_h + 24) + 6), label, fill="black")
    sheet.save(out / "montage.png")
    for label, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}")
    print(f"report: {out / 'report.json'}\nmontage: {out / 'montage.png'}")
    return 0 if report["pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("step", choices=["prepare", "app", "save", "render", "score", "all"])
    parser.add_argument("out", type=Path)
    parser.add_argument("--simulator", help="iOS simulator UDID (app, render, all)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.step in {"app", "render", "all"} and not args.simulator:
        parser.error("--simulator is required for app, render and all")
    steps = ["prepare", "app", "save", "render", "score"] if args.step == "all" else [args.step]
    for step in steps:
        print(f"== {step}", flush=True)
        if step == "prepare":
            _pytest(args.out, "prepare")
        elif step == "app":
            _xcode(args.out, "testWritesTheAppSaveForATwoTimesPoppedCard", args.simulator)
        elif step == "save":
            _pytest(args.out, "save")
        elif step == "render":
            _xcode(args.out, "testRendersEveryPreparedCardStyleCase", args.simulator)
        else:
            return score(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
