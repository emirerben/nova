#!/usr/bin/env python3
"""KRI-1342 item 7 + KRI-1346 (corner label + closing card) before/after on the iPhone simulator.

Prepares an input folder for the existing simulator render test
`DeviceMontageRenderE2ETests/testTalkingHeadCutawayRendersOnTheIPhone`
(`assertCase("talking_head")`), so the REAL iPhone engine renders a phone Talking edit of a REAL
talking take:

  --mode before  today: the corner line becomes the opening title (KRI-1009,
                 `_phone_talking_title_rows`) and the colour ending is refused (no card).
  --mode after   the corner line stays at its corner for the whole video, off every face
                 (`_phone_talking_corner_rows`), and the video ends on the colour card after
                 the last word (`closing_card_from_requirements` -> `closing_card_text_row` ->
                 `compile_phone_subtitled_plan(closing_card=...)`).

Everything is production code: the worker's caption-cue builders, its corner-text placement
(real face sampling on the take, OpenCV, local, free), the compiler and
`validate_phone_pilot_recipe`. No model calls. The take and outputs are footage: keep `--out`
outside git.

Usage (API venv, it imports `app`):
    src/apps/api/.venv/bin/python scripts/ios/talking-label-endcard-render-e2e.py \\
        --take take.mp4 --words words.json --label Yorumla --corner top_left \\
        --closing "follow for more brand and marketing content" \\
        --background red --text-color yellow --mode before|after --out DIR

`--words` is whisper.cpp JSON (`-ml 1`). Needs ffprobe on PATH. The script prints the
xcodebuild command; the rendered MP4 lands at DIR/frames/talking_head.mp4, and `plan.json`
holds the rows, the card and the recipe's timing for the frame checks.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
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

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.pipeline.captions import (
    PHONE_CUE_BUDGET,
    build_plain_cues,
    resplit_cues_into_sentences,
)
from app.pipeline.phone_subtitled_plan import (
    _STORY_CANVAS,
    compile_phone_subtitled_plan,
)
from app.pipeline.phone_talking_closing_card import (
    align_rows_to_recipe,
    closing_card_from_requirements,
    closing_card_receipt,
    closing_card_text_row,
)
from app.schemas.edit_proposal import PinnedText
from app.services.phone_rollout import (
    PHONE_NARRATED_CAPTION_FEATURES,
    validate_phone_pilot_recipe,
)
from app.tasks.generative_build import (
    _phone_talking_corner_rows,
    _phone_talking_title_rows,
)

# Reuse the CTA script's probe / binding / whisper reader (same take conventions).
_spec = importlib.util.spec_from_file_location(
    "cta_e2e", Path(__file__).with_name("cta-ending-render-e2e.py")
)
_cta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cta)  # type: ignore[union-attr]

CAPTION_STYLE = "sentence"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--take", type=Path, required=True)
    parser.add_argument("--words", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--corner", default="top_left")
    parser.add_argument("--closing", required=True)
    parser.add_argument("--background", required=True)
    parser.add_argument("--text-color", default=None)
    parser.add_argument("--card-seconds", type=float, default=None)
    parser.add_argument("--mode", choices=("before", "after"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    take = out / "take.mp4"
    shutil.copyfile(args.take, take)
    probe = _cta._probe(take)
    duration = probe["duration_s"]
    binding = _cta._binding(take, probe)
    cues = resplit_cues_into_sentences(
        build_plain_cues(
            _cta._words(args.words), attach_words=True, budget=PHONE_CUE_BUDGET
        )
    )
    keep = [(0.0, duration)]
    common = {
        "binding": binding,
        "clip_path": str(take),
        "keep_segments": keep,
        "landscape_fit": "fill",
    }

    card_spec = None
    receipt: dict = {}
    if args.mode == "before":
        rows, title_receipt = _phone_talking_title_rows(
            args.label, duration_s=None, cues=cues, **common
        )
        receipt["title"] = title_receipt
        compile_kwargs: dict = {}
    else:
        pin = PinnedText(text=args.label, corner=args.corner)
        rows, receipt["corner_text"] = _phone_talking_corner_rows(
            [pin], talk_end_s=duration, cues=cues, caption_style=CAPTION_STYLE, **common
        )
        facts = {"closing": args.closing, "background_color": args.background}
        if args.text_color:
            facts["text_color"] = args.text_color
        if args.card_seconds:
            facts["duration_s"] = args.card_seconds
        card_spec = closing_card_from_requirements(
            [
                BriefRequirement(
                    id="r5",
                    kind="text",
                    scope="global",
                    description="end card",
                    literal=args.closing,
                    facts=facts,
                )
            ]
        )
        if card_spec is None:
            sys.exit("the closing card could not be read from the facts")
        card_row = closing_card_text_row(
            card_spec,
            start_s=duration,
            end_s=duration + card_spec.duration_s,
            canvas=_STORY_CANVAS,
        )
        if card_row is None:
            sys.exit("the closing card's words cannot be drawn")
        rows = [*rows, card_row]
        compile_kwargs = {
            "closing_card": {
                "duration_s": card_spec.duration_s,
                "background_color": card_spec.background_color,
            }
        }

    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=cues,
        caption_style=CAPTION_STYLE,
        landscape_fit="fill",
        text_elements=rows,
        text_elements_user_edited=True,
        **compile_kwargs,
    )
    rows = align_rows_to_recipe(rows, recipe)
    required = set(recipe.required_capabilities)
    settings.phone_render_verified_features = sorted(
        required
        | set(PHONE_NARRATED_CAPTION_FEATURES)
        | {"closingCard", "freezeFrame", "visualBlocks"}
    )
    settings.phone_talking_closing_card_enabled = args.mode == "after"
    validate_phone_pilot_recipe(recipe)
    if card_spec is not None:
        receipt["closing_card"] = closing_card_receipt(card_spec, recipe, rows)

    drop = next(
        c
        for c in ("freezeFrame", "visualBlocks", "positionedText", "animatedText")
        if c in required
    )
    samples = []
    card = receipt.get("closing_card") or {}
    if card.get("status") == "applied":
        mid = (card["start_s"] + card["end_s"]) / 2
        rgb = [int(card["background_color"][i : i + 2], 16) for i in (1, 3, 5)]
        samples = [
            {"name": "card-corner", "t": mid, "x": 40, "y": 1880, "rgb": rgb},
            {"name": "card-top", "t": mid, "x": 540, "y": 120, "rgb": rgb},
        ]
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
                "clips": [{"media_id": _cta.MEDIA_ID, "file": "take.mp4"}],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": False,
                "samples": samples,
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2))
    plan = {
        "mode": args.mode,
        "take_duration_s": duration,
        "recipe_duration_s": recipe.duration,
        "last_word_end_s": max(
            (float(w["end_s"]) for c in cues for w in (c.get("words") or [])),
            default=None,
        ),
        "rows": rows,
        "receipt": receipt,
        "fills": [f.model_dump(mode="json") for f in recipe.visual_fills],
        "required_capabilities": sorted(required),
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=2, default=str))

    print(f"mode={args.mode} out={out}")
    print(f"take {probe['width']}x{probe['height']} {duration:.2f}s, {len(cues)} cues")
    for row in rows:
        print(
            f"  row {row['id']}: {row['text']!r} {float(row['start_s']):.2f}-"
            f"{float(row['end_s']):.2f}s x={row.get('x_frac')} y={row.get('y_frac')} "
            f"size={row.get('size_px')} color={row.get('color')}"
        )
    print(f"  receipt: {json.dumps(receipt, default=str)[:900]}")
    print(
        f"  recipe duration={recipe.duration:.3f}s caps={sorted(required)} drop={drop}"
    )
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


if __name__ == "__main__":
    main()
