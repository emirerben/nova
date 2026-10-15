#!/usr/bin/env python3
"""Prepare and inspect native gameday typography exports without provider calls.

  src/apps/api/.venv/bin/python scripts/ios/kri-669-gameday-text.py prepare .dev/gameday-text
  (cd src/apps/ios/Packages/KriaMediaEngine && KRIA_AUDIO_PARITY_DIR="$ROOT/.dev/gameday-text" swift test --filter AudioParityFixtureTests)
  src/apps/api/.venv/bin/python scripts/ios/kri-669-gameday-text.py compare .dev/gameday-text

The existing production AVFoundation exporter consumes the real server recipe.
Synthetic sources replace private footage; this is native output, not a live
model call or a production re-send. Original equal-size rows remain a control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for key, value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "test-key-not-used",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}.items():
    os.environ.setdefault(key, value)


def prepare(out: Path) -> None:
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.pipeline.guided_story import (
        GuidedStoryExecutionPlan,
        compile_execution_plan,
    )
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan
    from app.pipeline.unified_montage import plan_unified_montage
    from app.services.phone_sources import PhoneSourceBinding
    from tests.pipeline.test_unified_montage import clip

    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, color in enumerate(("0x18302b", "0x243349", "0x493428", "0x322b49")):
        path = out / f"source-{index}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=1920x1080:r=30:d=5",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
        paths.append(path)
    bindings = tuple(
        PhoneSourceBinding(
            media_id=f"c{i}",
            proxy_path=f"users/u/analysis-proxy-c{i}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                byte_count=path.stat().st_size,
                duration_s=5,
                width=1920,
                height=1080,
                has_audio=False,
            ),
        )
        for i, path in enumerate(paths)
    )
    cases = {
        "original": ["Gameday", "Aston Villa v Fenerbahçe", "6 am"],
        "hierarchy": ["Gameday", "Aston Villa v Fenerbahçe", "6 am"],
        "wrapped": [
            "Gameday",
            "Aston Villa v Fenerbahçe — a morning before the train",
            "6 am",
        ],
    }
    cases["styled"] = cases["hierarchy"]
    cases["styled-wrapped"] = cases["wrapped"]
    for name, copy in cases.items():
        pins = [{"text": text, "corner": "top_left"} for text in copy]
        if name != "original":
            for level, pin in enumerate(pins, 1):
                pin.update(heading_level=level, stack_id="gameday")
        plan = plan_unified_montage(
            [clip(i, minutes=i) for i in range(4)],
            strategy={"pinned_texts": pins},
            output_orientation="portrait",
        )
        compiled = compile_execution_plan(plan.guided_edit(), track=None)
        if name.startswith("styled"):
            from app.pipeline.phone_text_style import resolve_phone_text_style

            compiled["text_elements"] = resolve_phone_text_style(
                compiled["text_elements"],
                preferences={"all_text": {"font_family": "Inter"}},
            )
        recipe = compile_phone_guided_plan(
            GuidedStoryExecutionPlan.model_validate(compiled), bindings
        )
        directory = out / "cases" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "recipe.json").write_text(recipe.model_dump_json(indent=2))
        assets = {f"c{i}": str(path.resolve()) for i, path in enumerate(paths)}
        for asset in recipe.assets:
            if asset.id.startswith("font-"):
                font = (
                    REPO / "src/apps/api/assets/fonts" / asset.id.removeprefix("font-")
                )
                assert font.is_file(), font
                assets[asset.id] = str(font)
        (directory / "assets.json").write_text(json.dumps(assets, indent=2))
        (directory / "rows.json").write_text(
            json.dumps(compiled["text_elements"], indent=2)
        )
    print(f"Prepared {len(cases)} real server recipes: {out}")


def compare(out: Path) -> None:
    from PIL import Image
    from app.pipeline.text_stack_layout import measure_text_block

    panels = []
    checks = {}
    for directory in sorted((out / "cases").iterdir()):
        rows = json.loads((directory / "rows.json").read_text())
        rows = [row for row in rows if row["id"].startswith("guided-pinned-")]
        frame = directory / "frame.png"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-ss",
                "0.5",
                "-i",
                str(directory / "phone.mp4"),
                "-frames:v",
                "1",
                str(frame),
            ],
            check=True,
        )
        pixels = Image.open(frame).convert("RGB")
        sizes = [row["size_px"] for row in rows]
        bounds = []
        ink_boxes = []
        for row in rows:
            block = measure_text_block(row)
            center = row["y_frac"] * 1920
            top, bottom = center - block["height"] / 2, center + block["height"] / 2
            bounds.append((top, bottom))
            crop = pixels.crop(
                (70, max(0, int(top) - 3), 1020, min(1920, int(bottom) + 4))
            )
            mask = crop.convert("L").point(lambda value: 255 if value > 180 else 0)
            box = mask.getbbox()
            if box is None:
                raise AssertionError(
                    f"{directory.name}: no native text pixels for {row['text']}"
                )
            ink_boxes.append(list(box))
        gaps = [bounds[i + 1][0] - bounds[i][1] for i in range(2)]
        if directory.name != "original":
            assert sizes[0] > sizes[1] > sizes[2], sizes
            assert gaps[0] > gaps[1] > 0, gaps
            assert all(0 < top < bottom < 1920 for top, bottom in bounds), bounds
        checks[directory.name] = {
            "sizes": sizes,
            "gaps_px": gaps,
            "native_ink_boxes": ink_boxes,
        }
        panels.append(pixels.resize((360, 640)))
    montage = Image.new("RGB", (360 * len(panels), 640))
    for index, panel in enumerate(panels):
        montage.paste(panel, (360 * index, 0))
    montage.save(out / "montage.png")
    (out / "report.json").write_text(
        json.dumps(
            {"evidence": "native_AVFoundation_export", "cases": checks}, indent=2
        )
    )
    print(json.dumps(checks, indent=2))
    print(f"Native frames and report: {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "compare"))
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    {"prepare": prepare, "compare": compare}[args.action](args.out.resolve())
