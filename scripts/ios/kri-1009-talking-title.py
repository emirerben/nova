#!/usr/bin/env python3
"""Repeatable KRI-1009 native-output proof: a Talking top-corner text becomes the title.

A creator who asks for 'Title "The Cabildo" at the top' on a Talking edit gets one
whole-video pinned text in a top corner. Talking has no corner-text lane, so that text
used to be dropped; `check_strategy_for_runtime_v2` now turns it into the opening title
(`strategy_policy._talking_top_text_as_title`). This script shows the phone engine really
draws that title, and draws nothing for the pre-fix strategy.

Run from the repo root (any worktree) with the API venv:

  src/apps/api/.venv/bin/python scripts/ios/kri-1009-talking-title.py prepare OUT
  KRIA_AUDIO_PARITY_DIR=OUT swift test --package-path src/apps/ios/Packages/KriaMediaEngine \
    --arch arm64 --scratch-path SWIFT_SCRATCH --filter AudioParityFixtureTests
  src/apps/api/.venv/bin/python scripts/ios/kri-1009-talking-title.py compare OUT

OUT is any directory; SWIFT_SCRATCH is a build directory outside the repo (about 400 MB,
delete it afterwards).

prepare: runs the REAL `check_strategy_for_runtime_v2` on a phone-bound Talking manifest
with the production verified-feature list, asserts the T1 strategy (one pinned text
"The Cabildo" + flag emoji in `top_center`, no `opening_title`) comes back as
`opening_title`, builds the title row with the worker's own `_phone_talking_title_rows`,
compiles the REAL phone Talking recipe and writes `cases/kri1009_title`. The no-title
twin `cases/kri1009_notitle` comes from the same strategy with the repair switched off
(`opening_title` None, the pre-fix result), so the only difference between the two
recipes is the title.

compare: samples frames of the two exported movies with ffmpeg and checks pixels. The
title band (10-20 percent of the frame height) must be bright inside the recipe's title
window (1.0 s among the samples) and plain footage after it (4.0 s); the twin must show
plain footage at every sample. It then runs the same checks with the twin's movie
standing in for the title movie and requires them to FAIL, so a dropped title cannot
pass. Writes `OUT/result.json` and `OUT/montage.png`; exits non-zero on any failure.

Limits: synthetic footage (a flat 1080x1920 portrait backdrop and a tone), not real
speech or a face. The movies come from `AVFoundationLocalExporter` built for macOS and run
on this Mac, the same code the iPhone runs, but not a device run. The face sampler is
stubbed to "frames decoded, no face": the local OpenCV has no CascadeClassifier, so the
real sampler errors and the worker then protects a conservative face box that moves the
title to mid-frame. With the stub the title takes the preset top-band spot, which is what
a clip without a face at the top gets. Face avoidance itself is covered by
tests/pipeline/test_phone_subtitled_title.py, not by this script. No provider calls.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
# A worktree venv is an editable install of another checkout: put this worktree's `app` first.
sys.path.insert(0, str(API))
# Same placeholders tests/conftest.py uses; Settings only needs them to exist.
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

TITLE = "The Cabildo \U0001f1e6\U0001f1f7"  # "The Cabildo" + Argentina flag
TITLE_CASE = "kri1009_title"
NOTITLE_CASE = "kri1009_notitle"
# The creator's media id as the manifest sees it; the recipe compiler gets its own binding.
MEDIA_ID = "analysis-proxy-ios-3E33F71E-FB1F-400A-865A-4D99D1694EDC.mp4"
FOOTAGE_SECONDS = 8.0
FOOTAGE_BACKDROP = "0x2c4a6b"  # flat mid-dark blue-grey, luma about 66: nothing hides the title
# The verified-feature list the KRI-1009 production repro used (`PHONE_RENDER_VERIFIED_FEATURES`).
PROD_VERIFIED_FEATURES = [
    "alphaOverlay", "animatedText", "audioDucking", "audioMix", "authoredText",
    "basicComposition", "behindSubject", "cameraEffects", "captions", "carouselEffects",
    "clipTransitions", "crossfade", "customEffects", "editorMedia", "freehandDrawing",
    "freezeFrame", "goldenHourLook", "hdr", "hevcDecode", "local1080Export", "mediaCards",
    "motionPresets", "motionScenes", "musicBed", "narrationAudio", "positionedText",
    "processedAudio", "semanticCamera", "singleWordText", "slidePosts", "soundEffects",
    "sourceCrop", "stillImages", "textBend", "variableSpeed", "visualBlocks", "visualVideos",
]  # fmt: skip

# Title band as fractions of the frame height; the preset top spot (y 0.15) sits inside it.
TITLE_BAND = (0.10, 0.20)
SAMPLE_SECONDS = (0.5, 1.0, 1.5, 2.0, 4.0, 6.0)
BRIGHT_LUMA = 200  # title glyphs are white; the backdrop is about 66
TITLE_MIN_BRIGHT_FRACTION = 0.02  # the title covers about 12 percent of the band
BACKGROUND_MAX_LUMA = 100
WINDOW_MARGIN_S = 0.25  # skip fade edges when classifying samples against the window


def ffmpeg(*args: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True
    ).stdout


@contextlib.contextmanager
def patched(target: object, name: str, value: object) -> Iterator[None]:
    """Set `target.name` to `value` for the block, then put the original back."""

    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------- prepare


def make_footage(path: Path) -> None:
    ffmpeg(
        "-f", "lavfi", "-i", f"color=c={FOOTAGE_BACKDROP}:s=1080x1920:r=30:d={FOOTAGE_SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:d={FOOTAGE_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-shortest", str(path),
    )  # fmt: skip


def footage_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out)


def phone_talking_manifest() -> Any:
    """A phone-bound Talking manifest with production flags, set straight on `settings`."""

    from app.services import creator_capabilities as capabilities

    settings = capabilities.settings
    settings.guided_edit_capability_enabled = True
    for name in capabilities._FEATURE_SETTINGS.values():
        setattr(settings, name, True)
    for name in (
        "subtitled_archetype_enabled",
        "edit_format_talking_head_enabled",
        "phone_subtitled_rendering_enabled",
    ):
        setattr(settings, name, True)
    settings.phone_render_verified_features = PROD_VERIFIED_FEATURES
    return capabilities.resolve_creator_manifest(
        item_id="item",
        edit_format="subtitled",
        media=[{"media_id": MEDIA_ID, "kind": "video", "duration_s": 35.416667}],
        phone_source_media_ids=[MEDIA_ID],
        phone_rendering_allowed=True,
    )


def t1_strategy() -> Any:
    """What the Main Creator wrote for 'Title "The Cabildo" at the top' (KRI-1008 T1)."""

    from app.agents._schemas.creator_policy import CreativeStrategy
    from app.schemas.edit_proposal import PinnedText

    return CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        selected_media_ids=[MEDIA_ID],
        caption_style="karaoke",
        opening_title=None,
        pacing="balanced",
        direction="native",
        media_scope="selected",
        target_duration_s=35,
        pinned_texts=[PinnedText(text=TITLE, corner="top_center")],
    )


def write_case(out: Path, name: str, recipe: Any, footage: Path) -> None:
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


def prepare(out: Path) -> None:
    import app
    from app.kria import strategy_policy
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
    from app.pipeline import phone_subtitled_title
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services.phone_rollout import validate_phone_pilot_recipe
    from app.services.phone_sources import PhoneSourceBinding
    from app.tasks import generative_build

    assert Path(app.__file__).resolve().is_relative_to(API), f"imported {app.__file__}"
    out.mkdir(parents=True, exist_ok=True)
    manifest = phone_talking_manifest()

    # 1. The real policy check: the fixed code, and the pre-fix result of the same function
    #    (the corner-text -> title rewrite switched off: the text is left out, no title).
    fixed = check_strategy_for_runtime_v2(manifest, t1_strategy(), simplify_unsupported=True)
    assert isinstance(fixed, CheckedStrategy), fixed
    assert fixed.strategy.opening_title == TITLE, fixed.strategy.opening_title
    assert not fixed.strategy.pinned_texts, fixed.strategy.pinned_texts
    with patched(strategy_policy, "_talking_top_text_as_title", lambda m, s: (s, None)):
        pre_fix = check_strategy_for_runtime_v2(manifest, t1_strategy(), simplify_unsupported=True)
    assert isinstance(pre_fix, CheckedStrategy), pre_fix
    assert pre_fix.strategy.opening_title is None, pre_fix.strategy.opening_title
    assert not pre_fix.strategy.pinned_texts  # the corner text was left out, as in the incident

    def summarize(checked: CheckedStrategy) -> dict[str, Any]:
        pins = checked.strategy.pinned_texts
        return {
            "opening_title": checked.strategy.opening_title,
            "pinned_texts": [p.model_dump() for p in pins] if pins else None,
            "notices": list(checked.notices),
            "omitted_fields": list(checked.omitted_fields),
        }

    (out / "strategy.json").write_text(
        json.dumps(
            {"fixed": summarize(fixed), "pre_fix": summarize(pre_fix)},
            indent=2,
            ensure_ascii=False,
        )
    )

    # 2. Synthetic source, bound the way the phone pipeline binds an original.
    (out / "inputs").mkdir(exist_ok=True)
    footage = out / "inputs" / "footage.mp4"
    make_footage(footage)
    duration = footage_duration(footage)
    binding = PhoneSourceBinding(
        media_id="clip-0",
        proxy_path="user/analysis-proxy-clip-0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=sha256(footage),
            byte_count=footage.stat().st_size,
            duration_s=duration,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )
    cues = [
        {"text": "Hello everyone", "start_s": 0.0, "end_s": 1.5},
        {"text": "welcome back", "start_s": 1.5, "end_s": 3.0},
    ]

    def build(strategy: Any) -> tuple[list[dict], dict | None, Any]:
        rows: list[dict] = []
        receipt = None
        if strategy.opening_title:
            rows, receipt = generative_build._phone_talking_title_rows(
                strategy.opening_title,
                duration_s=getattr(strategy, "opening_title_duration_s", None),
                cues=cues,
                binding=binding,
                clip_path=str(footage),
                keep_segments=[(0.0, duration)],
                landscape_fit="fill",
            )
        recipe = compile_phone_subtitled_plan((binding,), caption_cues=cues, text_elements=rows)
        validate_phone_pilot_recipe(recipe)
        return rows, receipt, recipe

    # 3. Both recipes under a face sampler that says "frames decoded, no face" (see the module
    #    docstring), so the title takes the preset top-band spot and the twin differs only by title.
    def no_face(path: Any, anchors: Any, **kwargs: Any) -> tuple[list, dict]:
        return [], {"attempted": len(anchors), "decoded": len(anchors)}

    with patched(phone_subtitled_title, "sample_face_regions", no_face):
        rows, receipt, recipe = build(fixed.strategy)
        twin_rows, _, twin_recipe = build(pre_fix.strategy)

    [row] = rows
    assert row["text"] == TITLE, row["text"]
    assert TITLE_BAND[0] <= row["y_frac"] <= TITLE_BAND[1], row["y_frac"]
    assert receipt and receipt["status"] == "preset", receipt
    title_layers = [layer for layer in recipe.text_layers if layer.id.startswith("text-")]
    assert len(title_layers) == 1, [layer.id for layer in recipe.text_layers]
    assert twin_rows == []
    assert not [layer for layer in twin_recipe.text_layers if layer.id.startswith("text-")]
    write_case(out, TITLE_CASE, recipe, footage)
    write_case(out, NOTITLE_CASE, twin_recipe, footage)
    (out / "title_row.json").write_text(
        json.dumps({"row": row, "placement_receipt": receipt}, indent=2, ensure_ascii=False)
    )
    print(f"Prepared {out}: title window {title_layers[0].start}-{title_layers[0].end} s")


# ---------------------------------------------------------------------------- compare


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
            )  # fmt: skip
        )
    ).convert("RGB")


def band_luma(image: Image.Image) -> dict[str, float]:
    luma = np.asarray(image.convert("L"), dtype=float)
    height = luma.shape[0]
    band = luma[int(height * TITLE_BAND[0]) : int(height * TITLE_BAND[1])]
    return {
        "mean_luma": round(float(band.mean()), 1),
        "max_luma": round(float(band.max()), 1),
        "bright_fraction": round(float((band >= BRIGHT_LUMA).mean()), 4),
    }


def classify(stats: dict[str, float]) -> str:
    if stats["bright_fraction"] >= TITLE_MIN_BRIGHT_FRACTION:
        return "title"
    if stats["bright_fraction"] == 0 and stats["max_luma"] <= BACKGROUND_MAX_LUMA:
        return "background"
    return "other"


def title_window(case: Path) -> tuple[float, float] | None:
    layers = json.loads((case / "recipe.json").read_text())["text_layers"]
    windows = [
        (layer["start"], layer["end"]) for layer in layers if layer["id"].startswith("text-")
    ]
    return windows[0] if windows else None


def probe(movie: Path) -> dict[str, Any]:
    streams = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(movie)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    return {
        "codecs": sorted(s["codec_name"] for s in streams),
        "width": video["width"],
        "height": video["height"],
    }


def sample_movie(movie: Path) -> tuple[list[dict[str, Any]], list[Image.Image]]:
    samples, images = [], []
    for seconds in SAMPLE_SECONDS:
        image = frame(movie, seconds)
        stats = band_luma(image)
        samples.append({"t": seconds, **stats, "looks_like": classify(stats)})
        images.append(image)
    return samples, images


def evaluate(
    title_samples: list[dict[str, Any]],
    twin_samples: list[dict[str, Any]],
    window: tuple[float, float],
) -> dict[str, bool]:
    """The pixel checks. `window` is the title's start/end in the title case's recipe."""

    start, end = window
    inside = [
        s for s in title_samples if start + WINDOW_MARGIN_S <= s["t"] <= end - WINDOW_MARGIN_S
    ]
    after = [s for s in title_samples if s["t"] >= end + 0.5]
    probe_times = {s["t"] for s in (*inside, *after)}
    return {
        # The checks below only mean something if 1.0 s is inside and 4.0 s outside the window.
        "samples_straddle_window": {1.0, 4.0} <= probe_times
        and 1.0 in {s["t"] for s in inside}
        and 4.0 in {s["t"] for s in after},
        "title_drawn_in_window": bool(inside) and all(s["looks_like"] == "title" for s in inside),
        "title_gone_after_window": bool(after)
        and all(s["looks_like"] == "background" for s in after),
        "twin_has_no_title_anywhere": all(s["looks_like"] == "background" for s in twin_samples),
    }


def compare(out: Path) -> None:
    title_dir = out / "cases" / TITLE_CASE
    twin_dir = out / "cases" / NOTITLE_CASE
    movies = {title_dir: title_dir / "phone.mp4", twin_dir: twin_dir / "phone.mp4"}
    for case, movie in movies.items():
        if not movie.exists():
            raise SystemExit(f"No export for {case.name}; run the native exporter first")
        if movie.stat().st_mtime_ns < (case / "recipe.json").stat().st_mtime_ns:
            raise SystemExit(f"Stale export: {movie}; rerun the native exporter")
    window = title_window(title_dir)
    if window is None:
        raise SystemExit("The title recipe has no title text layer")
    if title_window(twin_dir) is not None:
        raise SystemExit("The no-title twin recipe carries a title text layer")

    title_samples, title_images = sample_movie(movies[title_dir])
    twin_samples, twin_images = sample_movie(movies[twin_dir])
    checks = evaluate(title_samples, twin_samples, window)
    geometry = {case.name: probe(movie) for case, movie in movies.items()}
    checks["movies_are_1080x1920_h264_aac"] = all(
        g["width"] == 1080 and g["height"] == 1920 and g["codecs"] == ["aac", "h264"]
        for g in geometry.values()
    )
    # Negative control of this comparison: with the twin's movie standing in for the title
    # movie the same checks must fail, or a dropped title would pass unnoticed.
    swapped = evaluate(twin_samples, twin_samples, window)
    negative_control = {
        "swapped_checks": swapped,
        "detects_missing_title": not swapped["title_drawn_in_window"],
    }
    checks["negative_control_detects_missing_title"] = negative_control["detects_missing_title"]

    result = {
        "script": "scripts/ios/kri-1009-talking-title.py",
        "title": TITLE,
        "title_window_s": list(window),
        "band_height_fraction": list(TITLE_BAND),
        "cases": {
            TITLE_CASE: {
                "recipe_sha256": sha256(title_dir / "recipe.json"),
                "movie_sha256": sha256(movies[title_dir]),
                "samples": title_samples,
            },
            NOTITLE_CASE: {
                "recipe_sha256": sha256(twin_dir / "recipe.json"),
                "movie_sha256": sha256(movies[twin_dir]),
                "samples": twin_samples,
            },
        },
        "movies": geometry,
        "checks": checks,
        "negative_control": negative_control,
        "passed": all(checks.values()),
        "evidence": "Synthetic footage, real strategy policy, worker title row and phone "
        "Talking compiler, AVFoundationLocalExporter on macOS. Not a device run.",
    }
    (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    write_montage(out / "montage.png", title_images, twin_images)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["passed"]:
        failed = [name for name, ok in checks.items() if not ok]
        raise SystemExit(f"Native title proof failed: {', '.join(failed)}")


def write_montage(
    path: Path, title_images: list[Image.Image], twin_images: list[Image.Image]
) -> None:
    thumb_w, thumb_h, label_h = 270, 480, 24
    rows = (("with title (fixed)", title_images), ("no-title twin (pre-fix)", twin_images))
    montage = Image.new(
        "RGB", (thumb_w * len(SAMPLE_SECONDS), (thumb_h + label_h) * len(rows)), "#141414"
    )
    draw = ImageDraw.Draw(montage)
    for row, (label, images) in enumerate(rows):
        for col, (seconds, image) in enumerate(zip(SAMPLE_SECONDS, images, strict=True)):
            x, y = col * thumb_w, row * (thumb_h + label_h)
            draw.text((x + 6, y + 6), f"{label} @ {seconds:g}s", fill="white")
            montage.paste(image.resize((thumb_w, thumb_h)), (x, y + label_h))
    montage.save(path)


COMMANDS: dict[str, Callable[[Path], None]] = {"prepare": prepare, "compare": compare}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    COMMANDS[args.command](args.out.resolve())
