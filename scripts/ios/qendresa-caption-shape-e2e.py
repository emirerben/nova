#!/usr/bin/env python3
"""Repeatable before/after render of Qendresa item 12 (caption shape) on the phone engine.

Qendresa (@qbuilder) shows 1-2 caption words at a time. Q1 (talking head): white, the
spoken word bright and the other dimmed, at about 0.62 of the height. Q2 (screen
walkthrough): black words in a light-grey rounded box at about 0.79. Behind
`settings.phone_caption_shape_enabled` (plus `captionShape` + `authoredText` verified and
`creator_client_protocol >= 36`) the Talking writer draws that; flag off draws today's
8-word lime sweep.

Run from the repo root (any worktree) with the API venv:

  src/apps/api/.venv/bin/python scripts/ios/qendresa-caption-shape-e2e.py \
    prepare OUT --take TAKE.mp4 --words WORDS.json
  KRIA_E2E_DIR=OUT xcodebuild test ... \
    -only-testing:KriaTests/DeviceCaptionShapeRenderE2ETests/testRendersEveryPreparedCaptionShapeCase
  src/apps/api/.venv/bin/python scripts/ios/qendresa-caption-shape-e2e.py score OUT --kit KIT_DIR

prepare: TAKE is a 1080x1920 talking video with speech; WORDS is whisper.cpp JSON
(`whisper-cli -ml 1 -sow -oj`). Writes `cases/{q1,q2}_{before,after}` (recipe.json,
assets.json, captions.json). *_before has `phone_caption_shape_enabled` off, *_after on;
nothing else differs. Each case runs the REAL `stamp_phone_text_style` on a stand-in job
(brief row + request as in tests/tasks/test_qendresa_caption_shape_worker.py), then the
worker's caption chain (`build_plain_cues` -> `resplit_cues_into_sentences` ->
`generation_caption_cues` -> `compile_phone_subtitled_plan`) inside
`phone_text_policy_scope`, and `validate_phone_pilot_recipe`.

score: runs the kit's `tools/check_result.sh MP4 Q1|Q2 --ignore-tail-s 1.6` on each
`cases/<name>/phone.mp4`, writes `result.json`, and asserts the item's done check:
`captions_short` and `captions_height` FAIL before and PASS after for Q1 and Q2, and no
rule that passed before fails after. The kit and footage live outside the repo and are
only read.

Limits: no provider calls. The font model call is skipped (`selection_calls = 2`). Face
sampling is stubbed to "frames decoded, no face" and the proxy download in
`_ensure_generation_environment` records no faces, so caption placement here does not
avoid a face (face avoidance is covered by unit tests). The worker's cut plan, title,
lanes and caption correction are not run: the take is captioned whole.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
# A worktree venv is an editable install of another checkout: put this worktree's `app` first.
sys.path.insert(0, str(API))
for key, value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "test-key-not-used",
    "WHISPER_BACKEND": "local",
    "WAITLIST_ADMIN_SECRET": "test-admin-secret",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
    "INTERNAL_API_KEY": "test-internal-key",
}.items():
    os.environ.setdefault(key, value)

CANVAS_H = 1920.0
CLIENT_PROTOCOL = 36
CASES = ("q1_before", "q1_after", "q2_before", "q2_after")
RULES = ("captions_on", "captions_short", "captions_height")
MUST_FLIP = ("captions_short", "captions_height")
IGNORE_TAIL_S = "1.6"

# Same prompts and brief rows as tests/tasks/test_qendresa_caption_shape_worker.py.
Q1_PROMPT = (
    "Cut out all my pauses and mistakes so it's tight. Captions word by word in the middle of the "
    'screen, white, two words at a time. Start with a big title "lets build an agent that reads '
    'an APP before I trust it!!" for the first 3 seconds. When I talk about something, show the '
    "matching screenshot or screen recording in the top or bottom part of the screen, not over "
    'my face. When I list the four things, show each one as big text, like "1. which company it '
    'sends data to?". End with "comment READ" and an arrow for the last 3 seconds. Only my '
    "voice, no music."
)
Q2_PROMPT = (
    "Start with me full screen and the title \"5 PROJECTS / the AI builder community / can't stop "
    'talking about!!!!" with the GitHub logo above it. After that, put my screen recordings full '
    "screen and me cut out (no background) small in the bottom right corner while I talk. Zoom "
    "into the recordings so the text is readable, and show the right one when I talk about each "
    "repo. "
    'Add a label when each one starts: "1. Spec-kit", "2. Impeccable", "3. TradingAgents", '
    '"4. Agents", "5. Claude code game studio". Captions word by word in a light grey box near the '
    'bottom. Cut all my pauses. End with "comment REPO" and an arrow. Only my voice.'
)
Q1_CAPTIONS: dict[str, Any] = {
    "id": "q1cap",
    "kind": "style",
    "scope": "global",
    "description": "Captions word by word in the middle of the screen, white, two words at a time",
    "facts": {
        "captions": "word_by_word",
        "caption_position": "middle",
        "caption_color": "#FFFFFF",
        "caption_words": 2,
    },
}
Q2_CAPTIONS: dict[str, Any] = {
    "id": "q2cap",
    "kind": "style",
    "scope": "global",
    "description": "Captions word by word in a light grey box near the bottom",
    "facts": {
        "captions": "word_by_word",
        "caption_box": "#E5E5E5",
        "caption_position": "bottom",
    },
}
QUESTIONS = {
    "q1": (Q1_CAPTIONS, Q1_PROMPT, "Q1"),
    "q2": (Q2_CAPTIONS, Q2_PROMPT, "Q2"),
}

# The production-like verified-feature list (same as scripts/ios/kri-1009-talking-title.py).
PROD_VERIFIED_FEATURES = [
    "alphaOverlay", "animatedText", "audioDucking", "audioMix", "authoredText",
    "basicComposition", "behindSubject", "cameraEffects", "captions", "carouselEffects",
    "clipTransitions", "crossfade", "customEffects", "editorMedia", "freehandDrawing",
    "freezeFrame", "goldenHourLook", "hdr", "hevcDecode", "local1080Export", "mediaCards",
    "motionPresets", "motionScenes", "musicBed", "narrationAudio", "positionedText",
    "processedAudio", "semanticCamera", "singleWordText", "slidePosts", "soundEffects",
    "sourceCrop", "stillImages", "textBend", "variableSpeed", "visualBlocks", "visualVideos",
    "captionShape",
]  # fmt: skip


@contextlib.contextmanager
def patched(target: object, name: str, value: object) -> Iterator[None]:
    """Set `target.name` to `value` for the block, then put the original back."""

    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


# ---------------------------------------------------------------------------- prepare


def load_words(path: Path) -> list[Any]:
    """whisper.cpp `-ml 1 -sow` JSON -> `Word`s: no-space tokens continue the previous word."""

    from app.pipeline.transcribe import Word

    tokens = json.loads(path.read_text())["transcription"]
    merged: list[list[Any]] = []  # [text, start_s, end_s]
    for token in tokens:
        raw = token["text"]
        text = raw.strip()
        if not text or (text.startswith("[") and text.endswith("]")):
            continue
        start, end = token["offsets"]["from"] / 1000, token["offsets"]["to"] / 1000
        if merged and not raw.startswith(" "):
            merged[-1][0] += text
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([text, start, end])
    return [
        Word(text, start, max(end, start + 0.01), 1.0) for text, start, end in merged
    ]


def probe_video(path: Path) -> dict[str, Any]:
    out = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    video = next(s for s in out["streams"] if s["codec_type"] == "video")
    return {
        "duration": float(out["format"]["duration"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s["codec_type"] == "audio" for s in out["streams"]),
    }


def write_case(out: Path, name: str, recipe: Any, footage: Path) -> None:
    import shutil

    directory = out / "cases" / name
    # A previous run's movie or export state must never pass as this run's evidence.
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    assets: dict[str, str] = {}
    for asset in recipe.asset_manifest.assets:
        if asset.kind == "original":
            assets[asset.id] = str(footage)
    for asset in [*recipe.assets, *recipe.asset_manifest.assets]:
        if asset.id.startswith("font-"):
            font = API / "assets/fonts" / asset.id.removeprefix("font-")
            assert font.exists(), font
            assets[asset.id] = str(font)
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))


def caption_summary(recipe: Any) -> list[dict[str, Any]]:
    rows = []
    for layer in recipe.text_layers:
        if not layer.id.startswith("caption-"):
            continue
        words = " ".join(run.text for run in layer.runs).split()
        rows.append(
            {
                "id": layer.id,
                "start": round(layer.start, 3),
                "end": round(layer.end, 3),
                "words": words,
                "anchor_y": round(layer.anchor_y / CANVAS_H, 4),
                "background": layer.background is not None,
            }
        )
    return rows


def build_case(name: str, words: list[Any], binding: Any) -> Any:
    from app.config import settings
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.brief_binding import BriefBinding
    from app.pipeline.captions import (
        PHONE_CUE_BUDGET,
        build_plain_cues,
        resplit_cues_into_sentences,
    )
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services import phone_text_styling as styling
    from app.services.phone_rollout import validate_phone_pilot_recipe
    from app.services.phone_text_styling import (
        PHONE_TEXT_STYLE_FIELD,
        generation_caption_cues,
        phone_text_policy_scope,
        stamp_phone_text_style,
    )

    question, kind = name.split("_")
    requirement, prompt, _ = QUESTIONS[question]
    after = kind == "after"

    def no_environment(canvas: Any) -> None:  # no proxy download: no faces sampled
        styling._POLICY.get()["_face_boxes"] = []

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(settings, "phone_text_styling_enabled", True))
        stack.enter_context(patched(settings, "phone_caption_shape_enabled", after))
        stack.enter_context(
            patched(
                settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES)
            )
        )
        stack.enter_context(
            patched(styling, "_ensure_generation_environment", no_environment)
        )
        job = SimpleNamespace(
            id=uuid.uuid4(),
            assembly_plan={
                "creator_brief_binding": BriefBinding.create(
                    uuid.uuid4(),
                    CreativeBrief(
                        version=1, requirements=[BriefRequirement(**requirement)]
                    ),
                ).model_dump(mode="json")
            },
            all_candidates={
                "creator_request": prompt,
                "creator_client_protocol": CLIENT_PROTOCOL,
                "voiceover_caption_style": "word",
            },
        )
        stamp_phone_text_style(job)
        policy = job.assembly_plan[PHONE_TEXT_STYLE_FIELD]
        policy["selection_calls"] = 2  # no font model call
        with phone_text_policy_scope(policy):
            cues = build_plain_cues(words, attach_words=True, budget=PHONE_CUE_BUDGET)
            cues = resplit_cues_into_sentences(cues)
            cues = generation_caption_cues(cues, language="en")
            recipe = compile_phone_subtitled_plan(
                (binding,), caption_cues=cues, caption_style="word"
            )
        validate_phone_pilot_recipe(recipe)
    return recipe


def prepare(out: Path, take: Path, words_path: Path) -> None:
    import app
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.services.phone_sources import PhoneSourceBinding

    assert Path(app.__file__).resolve().is_relative_to(API), f"imported {app.__file__}"
    info = probe_video(take)
    assert (info["width"], info["height"]) == (1080, 1920), info
    words = load_words(words_path)
    assert words, "no words in the whisper JSON"
    out.mkdir(parents=True, exist_ok=True)
    binding = PhoneSourceBinding(
        media_id="clip-0",
        proxy_path="user/analysis-proxy-clip-0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=hashlib.sha256(take.read_bytes()).hexdigest(),
            byte_count=take.stat().st_size,
            duration_s=info["duration"],
            width=info["width"],
            height=info["height"],
            has_audio=info["has_audio"],
        ),
    )

    def no_face(path: Any, anchors: Any, **kwargs: Any) -> tuple[list, dict]:
        return [], {"attempted": len(anchors), "decoded": len(anchors)}

    from app.pipeline import phone_subtitled_title

    with patched(phone_subtitled_title, "sample_face_regions", no_face):
        for name in CASES:
            recipe = build_case(name, words, binding)
            write_case(out, name, recipe, take.resolve())
            captions = caption_summary(recipe)
            (out / "cases" / name / "captions.json").write_text(
                json.dumps(captions, indent=2, ensure_ascii=False)
            )
            most = max((len(c["words"]) for c in captions), default=0)
            mean_y = (
                sum(c["anchor_y"] for c in captions) / len(captions)
                if captions
                else 0.0
            )
            boxes = sum(c["background"] for c in captions)
            print(
                f"{name}: {len(captions)} caption layers, max words {most}, "
                f"mean anchor_y {mean_y:.3f}, boxed {boxes}"
            )
    print(f"Prepared {out}")


# ---------------------------------------------------------------------------- score


def run_check(kit: Path, movie: Path, question: str) -> dict[str, Any]:
    subprocess.run(
        [
            str(kit / "tools/check_result.sh"),
            str(movie),
            question,
            "--ignore-tail-s",
            IGNORE_TAIL_S,
        ],
        check=False,
    )
    report = movie.with_suffix(".check") / "report.json"
    return json.loads(report.read_text())


def score(out: Path, kit: Path) -> int:
    results: dict[str, Any] = {}
    for name in CASES:
        movie = out / "cases" / name / "phone.mp4"
        if not movie.exists():
            print(f"{name}: no phone.mp4 (render it first)")
            continue
        report = run_check(kit, movie, QUESTIONS[name.split("_")[0]][2])
        rules = {r["id"]: r for r in report["rules"]}
        results[name] = {
            "passed": sum(bool(r["pass"]) for r in report["rules"]),
            "total": len(report["rules"]),
            "rules": {
                rid: {
                    "pass": bool(rules[rid]["pass"]),
                    "measured": rules[rid].get("measured"),
                }
                for rid in RULES
                if rid in rules
            },
            "all_pass": {r["id"]: bool(r["pass"]) for r in report["rules"]},
        }
    failures: list[str] = []
    for question in QUESTIONS:
        before, after = (
            results.get(f"{question}_before"),
            results.get(f"{question}_after"),
        )
        if before is None or after is None:
            failures.append(f"{question}: missing before or after movie")
            continue
        for rid in MUST_FLIP:
            if before["rules"].get(rid, {}).get("pass", True):
                failures.append(f"{question}: {rid} should FAIL before")
            if not after["rules"].get(rid, {}).get("pass", False):
                failures.append(f"{question}: {rid} should PASS after")
        for rid, ok in before["all_pass"].items():
            if ok and not after["all_pass"].get(rid, False):
                failures.append(f"{question}: {rid} passed before and fails after")
    (out / "result.json").write_text(
        json.dumps({"cases": results, "failures": failures}, indent=2, default=str)
    )
    print(f"{'case':10} {'rule':16} {'pass':5} measured")
    for name, data in results.items():
        for rid, rule in data["rules"].items():
            print(f"{name:10} {rid:16} {rule['pass']!s:5} {rule['measured']}")
        print(f"{name:10} {'TOTAL':16} {data['passed']}/{data['total']}")
    for failure in failures:
        print("FAIL:", failure)
    print("OK" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("out", type=Path)
    prep.add_argument("--take", type=Path, required=True)
    prep.add_argument("--words", type=Path, required=True)
    sc = sub.add_parser("score")
    sc.add_argument("out", type=Path)
    sc.add_argument("--kit", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.out, args.take, args.words)
        return 0
    return score(args.out, args.kit)


if __name__ == "__main__":
    sys.exit(main())
