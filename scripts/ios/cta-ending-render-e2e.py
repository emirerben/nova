#!/usr/bin/env python3
"""Item 14 (CTA ending) before/after on the iPhone simulator.

Prepares an input folder for the existing simulator render test
`DeviceMontageRenderE2ETests/testTalkingHeadCutawayRendersOnTheIPhone`
(`assertCase("talking_head")`), so the REAL iPhone engine renders a phone Talking edit
of a REAL talking take with its ending:

  --mode before  today's ending: one lime closing tag (the render worker with the new
                 switch off: `_phone_talking_closing_rows(..., cta=False)`).
  --mode after   item 14's ending: a small white handwritten "comment", the big cream
                 keyword and (with --arrow) a curly arrow drawing
                 (`_phone_talking_closing_rows(..., cta=True, arrow=...)`).

Everything is production code: the worker's own caption-cue builders, its own
`_phone_talking_closing_rows` (real face sampling on the take, OpenCV, local, free),
`compile_phone_subtitled_plan`, `with_drawings` and `validate_phone_pilot_recipe`.
No model calls. The take and the outputs are footage: keep `--out` outside git.

Usage (API venv, it imports `app`):
    src/apps/api/.venv/bin/python scripts/ios/cta-ending-render-e2e.py \\
        --take take.mp4 --words words.json --closing "comment READ" [--arrow] \\
        --mode before|after --out DIR

`--words` is whisper.cpp JSON (`-ml 1`): `transcription[].offsets.from/to` in ms + `text`.
Needs ffprobe on PATH. The script prints the xcodebuild command; the rendered MP4 lands
at DIR/frames/talking_head.mp4.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for name, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from app.agents.transcript import Word
from app.config import settings
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.captions import (
    PHONE_CUE_BUDGET,
    build_plain_cues,
    resplit_cues_into_sentences,
)
from app.pipeline.phone_recipe_shared import with_drawings
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding
from app.tasks.generative_build import _phone_talking_closing_rows

MEDIA_ID = "cta-take"
# The Talking default: karaoke captions are the "sentence" style compiled with the default look
# (the worker's `caption_style` is only "sentence" or "word").
CAPTION_STYLE = "sentence"


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = json.loads(out)
    return {
        "width": int(data["streams"][0]["width"]),
        "height": int(data["streams"][0]["height"]),
        "duration_s": float(data["format"]["duration"]),
    }


def _binding(path: Path, probe: dict) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=MEDIA_ID,
        proxy_path=f"users/owner/creation/analysis-proxy-{MEDIA_ID}.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            byte_count=path.stat().st_size,
            duration_s=probe["duration_s"],
            width=probe["width"],
            height=probe["height"],
            has_audio=True,
        ),
    )


def _words(path: Path) -> list[Word]:
    """whisper.cpp `-ml 1` tokens -> words (a token without a leading space continues the
    previous word; bracketed markers like [_BEG_] are dropped)."""
    words: list[Word] = []
    for token in json.loads(path.read_text())["transcription"]:
        text = token["text"]
        if not text.strip() or text.strip().startswith("[_"):
            continue
        start, end = token["offsets"]["from"] / 1000.0, token["offsets"]["to"] / 1000.0
        if words and not text.startswith(" "):
            prev = words[-1]
            words[-1] = Word(
                text=prev.text + text, start_s=prev.start_s, end_s=max(end, prev.end_s)
            )
        else:
            words.append(Word(text=text.strip(), start_s=start, end_s=max(end, start)))
    return words


def _drawings_bbox(drawings: list[dict]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for drawing in drawings:
        for stroke in drawing.get("strokes") or [drawing]:
            for point in stroke.get("points") or []:
                if isinstance(point, dict):
                    xs.append(float(point["x"]))
                    ys.append(float(point["y"]))
                else:
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--take", type=Path, required=True)
    parser.add_argument("--words", type=Path, required=True)
    parser.add_argument("--closing", required=True)
    parser.add_argument("--arrow", action="store_true")
    parser.add_argument("--mode", choices=("before", "after"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    take = out / "take.mp4"
    shutil.copyfile(args.take, take)
    probe = _probe(take)
    duration = probe["duration_s"]
    binding = _binding(take, probe)

    # Captions: the worker's path -- build_plain_cues(PHONE_CUE_BUDGET) then
    # resplit_cues_into_sentences. The LLM caption correction is skipped (a paid model call).
    cues = resplit_cues_into_sentences(
        build_plain_cues(_words(args.words), attach_words=True, budget=PHONE_CUE_BUDGET)
    )

    rows, receipt, drawings = _phone_talking_closing_rows(
        args.closing,
        photo_card=None,
        photo_aspect=None,
        binding=binding,
        clip_path=str(take),
        keep_segments=[(0.0, duration)],
        landscape_fit="fill",
        cta=args.mode == "after",
        arrow=args.mode == "after" and args.arrow,
        cues=cues,
        caption_style=CAPTION_STYLE,
    )
    if not rows:
        sys.exit("no closing rows were produced (empty text or no time for it)")

    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=cues,
        caption_style=CAPTION_STYLE,
        landscape_fit="fill",
        text_elements=rows,
        text_elements_user_edited=True,
    )
    if drawings:
        recipe = with_drawings(recipe, drawings)

    settings.phone_render_verified_features = sorted(recipe.required_capabilities)
    validate_phone_pilot_recipe(recipe)

    # Dropping this capability must route the recipe to the cloud (the Swift route check).
    required = set(recipe.required_capabilities)
    drop = next(
        c
        for c in ("freehandDrawing", "authoredText", "positionedText", "animatedText")
        if c in required
    )

    status = DeviceRenderStatus(
        phase="awaiting_device",
        request=make_device_request(
            job_id=uuid.uuid4(), variant_id="talking_head", revision=1, recipe=recipe
        ),
    ).model_dump(mode="json")
    (out / "subtitled-status.json").write_text(json.dumps(status, indent=2))
    e2e = {
        "verified_features": sorted(required),
        "cases": {
            "talking_head": {
                "status_file": "subtitled-status.json",
                "duration_s": recipe.duration,
                "required_capabilities": sorted(required),
                "drop_capability": drop,
                "clips": [{"media_id": MEDIA_ID, "file": "take.mp4"}],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": False,
                "samples": [],
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2))
    (out / "ending.json").write_text(
        json.dumps(
            {"rows": rows, "drawings": drawings, "receipt": receipt},
            indent=2,
            default=str,
        )
    )

    print(f"mode={args.mode} closing={args.closing!r} arrow={args.arrow} out={out}")
    print(
        f"take: {probe['width']}x{probe['height']} {duration:.2f}s, {len(cues)} caption cues"
    )
    for row in rows:
        print(
            f"  row {row.get('id')}: {row.get('text')!r} "
            f"{float(row['start_s']):.2f}-{float(row['end_s']):.2f}s font={row.get('font_family')} "
            f"color={row.get('color')} size={row.get('size_px')}px "
            f"x_frac={row.get('x_frac')} y_frac={row.get('y_frac')}"
        )
    print(f"  drawings: {len(drawings)} bbox={_drawings_bbox(drawings)}")
    print(f"  receipt: {json.dumps(receipt, default=str)[:600]}")
    print(
        f"  recipe duration={recipe.duration:.3f}s required_capabilities={sorted(required)}"
    )
    print(f"  drop_capability={drop}")
    print("Render it on a simulator from src/apps/ios:")
    print(
        f"  TEST_RUNNER_KRIA_E2E_DIR={out} xcodebuild -project Kria.xcodeproj -scheme Kria "
        "-skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO "
        "ONLY_ACTIVE_ARCH=YES ARCHS=arm64 COMPILER_INDEX_STORE_ENABLE=NO "
        "-collect-test-diagnostics never "
        '-destination "platform=iOS Simulator,name=iPhone 17 Pro" '
        "-only-testing:KriaTests/DeviceMontageRenderE2ETests/"
        "testTalkingHeadCutawayRendersOnTheIPhone test"
    )
    print(f"The rendered MP4 lands at {out / 'frames' / 'talking_head.mp4'}.")


if __name__ == "__main__":
    main()
