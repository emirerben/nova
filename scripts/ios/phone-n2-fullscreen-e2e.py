#!/usr/bin/env python3
# ruff: noqa: E402 -- executable harness configures sys.path and environment before app imports.
"""Build the authored N2 cabin replay for the native full-screen renderer.

This is an offline, deterministic replay of a decided sequence, not evidence
that a model understood the cabin footage. It compiles the real narrated phone
recipe, adds a river photo card, then applies the real sequence compiler so
the river video restarts from its opening frame after the photo.

Usage:
  src/apps/api/.venv/bin/python scripts/ios/phone-n2-fullscreen-e2e.py OUT_DIR
  TEST_RUNNER_KRIA_E2E_DIR=OUT_DIR xcodebuild ... \
    -only-testing:KriaTests/DeviceMontageRenderE2ETests/testN2FullscreenCabinReplay test
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = Path.home() / "Downloads/kria-cabin-test/upload/N2_cabin_review_narrated"
sys.path.insert(0, str(REPO / "src/apps/api"))
for name, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.config import settings
from app.pipeline.narrated_photo_sequence import compile_narrated_photo_sequences
from app.pipeline.phone_narrated_plan import (
    NarratedPhoneStep,
    compile_phone_narrated_plan,
    replace_editor_lanes,
)
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledOverlayCard
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

VOICEOVER_DURATION_S = 19.633991
CANVAS = (1080, 1920)


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def _duration(path: Path) -> float:
    return float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def _probe_video(path: Path) -> tuple[float, int, int, int]:
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )["streams"][0]
    rotation = next(
        (entry.get("rotation", 0) for entry in probe.get("side_data_list", []) if "rotation" in entry),
        0,
    )
    return _duration(path), int(probe["width"]), int(probe["height"]), int(rotation) % 360


def _binding(path: Path, media_id: str) -> PhoneSourceBinding:
    sha, size = _fingerprint(path)
    duration_s, width, height, orientation_degrees = _probe_video(path)
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"users/n2/analysis-proxy-{media_id}.mp4",
        generation="n2-replay",
        original=OriginalMediaDescriptor(
            sha256=sha,
            byte_count=size,
            duration_s=duration_s,
            width=width,
            height=height,
            orientation_degrees=orientation_degrees,
            has_audio=True,
        ),
    )


def _visual(path: Path, media_id: str) -> PhoneVisualBinding:
    sha, size = _fingerprint(path)
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/n2/visuals/{path.name}",
        generation="n2-replay",
        sha256=sha,
        byte_count=size,
    )


def _cover_rgb(path: Path, at_s: float, x: int, y: int) -> list[int]:
    """Reference RGB from ffmpeg's cover crop, away from title/caption bands."""
    image_args = ["-loop", "1"] if path.suffix.lower() in {".jpg", ".jpeg", ".png"} else []
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", *image_args, "-ss", str(at_s), "-i", str(path), "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    offset = (y * CANVAS[0] + x) * 3
    return list(raw[offset : offset + 3])


def _active_source_time(recipe, media_id: str, timeline_s: float) -> float:
    """Use the compiled rate/source offset, never a guessed source time."""
    asset_id = next(asset.id for asset in recipe.assets if asset.id == media_id)
    for clip in next(track for track in recipe.tracks if track.id == "narrated").clips:
        end_s = clip.timeline_start + clip.source_duration / clip.rate
        if clip.source_asset_id == asset_id and clip.timeline_start <= timeline_s < end_s:
            return clip.source_start + (timeline_s - clip.timeline_start) * clip.rate
    raise ValueError(f"no active {media_id} clip at {timeline_s}")


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        raise SystemExit("usage: phone-n2-fullscreen-e2e.py OUT_DIR [SOURCE_KIT]")
    source = Path(sys.argv[2]).expanduser() if len(sys.argv) == 3 else DEFAULT_SOURCE
    if not source.is_dir():
        raise SystemExit(f"missing local N2 source kit: {source}")
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)

    loft = source / "N2_01_footage.mp4"
    river = source / "N2_05_footage.mp4"
    laptop = source / "N2_06_footage.mp4"
    bathtub_photo = source / "N2_08_visual.jpg"
    river_photo = source / "N2_09_visual.jpg"
    voiceover = source / "N2_10_voiceover.m4a"
    script = source / "N2_00_SCRIPT.txt"
    for path in (loft, river, laptop, bathtub_photo, river_photo, voiceover, script):
        if not path.is_file():
            raise SystemExit(f"missing N2 fixture media: {path.name}")
        if path != script:
            shutil.copy2(path, out / path.name)

    # Keep the private spoken copy local. These are deliberately authored,
    # estimated phrase windows for a deterministic renderer replay; they are
    # not claimed as transcription output or observed word timings.
    caption_lines = [line.strip() for line in script.read_text().splitlines() if line.strip()]
    caption_windows = [(0.2, 2.8), (3.0, 5.5), (5.7, 10.0), (10.3, 14.0), (14.2, 19.3)]
    if len(caption_lines) != len(caption_windows):
        raise SystemExit("N2 script no longer matches the authored replay timing schedule")
    caption_cues = [
        {"text": text, "start_s": start_s, "end_s": end_s}
        for text, (start_s, end_s) in zip(caption_lines, caption_windows, strict=True)
    ]

    bindings = (_binding(loft, "n2-loft"), _binding(river, "n2-river"), _binding(laptop, "n2-laptop"))
    narration_sha, narration_bytes = _fingerprint(voiceover)
    narration = PhoneNarrationBed(
        plan_item_id="n2-cabin-replay",
        generation="n2-replay",
        fingerprint=RenderFingerprint(sha256=narration_sha, byte_count=narration_bytes),
        duration_s=VOICEOVER_DURATION_S,
    )
    # Authored timing replay of the supplied transcript. The last source is
    # slower than its speech window, exercising the narrated compiler's retime.
    recipe = compile_phone_narrated_plan(
        [
            NarratedPhoneStep(step_id="loft", media_id="n2-loft", start_s=0, end_s=10.3),
            NarratedPhoneStep(step_id="river", media_id="n2-river", start_s=10.3, end_s=14.0),
            NarratedPhoneStep(step_id="laptop", media_id="n2-laptop", start_s=14.0, end_s=VOICEOVER_DURATION_S),
        ],
        bindings,
        narration,
        voiceover_duration_s=VOICEOVER_DURATION_S,
        caption_cues=caption_cues,
        opening_title="3 things we loved",
        opening_title_end_s=2.4,
    )
    bathtub = _visual(bathtub_photo, "n2-bathtub-photo")
    photo = _visual(river_photo, "n2-river-photo")
    recipe = replace_editor_lanes(
        recipe,
        lanes=PhoneSubtitledLanes(
            overlays=[
                SubtitledOverlayCard(
                    id="beat-bathtub-1",
                    media_id=bathtub.media_id,
                    gcs_path=bathtub.gcs_path,
                    generation=bathtub.generation,
                    start_s=6.2,
                    end_s=7.7,
                    display_mode="fullscreen",
                    fade=False,
                ),
                SubtitledOverlayCard(
                    id="beat-river-1",
                    media_id=photo.media_id,
                    gcs_path=photo.gcs_path,
                    generation=photo.generation,
                    start_s=10.3,
                    end_s=11.8,
                    display_mode="fullscreen",
                    fade=False,
                )
            ]
        ),
        visuals=(bathtub, photo),
    )
    recipe, evidence = compile_narrated_photo_sequences(
        recipe,
        beats=[
            {
                "beat_id": "river",
                "trigger": "the river",
                "visual_id": "asset-n2-river-photo",
                "display_mode": "fullscreen",
                "following_clip_id": "n2-river",
                "hold_s": 1.5,
            }
        ],
        bindings=bindings,
    )
    # This fixture deliberately exercises the native-admitted overlay shape;
    # use its compiled capabilities as the local device qualification receipt.
    settings.phone_render_verified_features = sorted(recipe.required_capabilities)
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)
    request = make_device_request(
        job_id=uuid.UUID("d83cb0f5-d145-43dc-8f80-a0fa7b0a08e2"), variant_id="n2-fullscreen", revision=1, recipe=recipe
    )
    (out / "status-n2-fullscreen.json").write_text(
        DeviceRenderStatus(phase="awaiting_device", request=request).model_dump_json(indent=2)
    )
    x, y = 540, 960
    river_opening_t = 12.5
    laptop_final_t = 16.0
    e2e = {
        "verified_features": sorted(recipe.required_capabilities),
        "cases": {
            "n2_fullscreen": {
                "status_file": "status-n2-fullscreen.json",
                "duration_s": recipe.duration,
                "required_capabilities": sorted(recipe.required_capabilities),
                "drop_capability": "visualBlocks",
                "clips": [
                    {"media_id": "n2-loft", "file": loft.name},
                    {"media_id": "n2-river", "file": river.name},
                    {"media_id": "n2-laptop", "file": laptop.name},
                ],
                "voiceover_asset_id": "voiceover-n2-cabin-replay",
                "voiceover_file": voiceover.name,
                "visual_files": {
                    "visual-n2-bathtub-photo": bathtub_photo.name,
                    "visual-n2-river-photo": river_photo.name,
                },
                "expects_narration_audio": True,
                "expected_copy": " ".join(["3 things we loved", *caption_lines]),
                "samples": [
                    {"name": "loft", "t": 2.8, "x": x, "y": y, "rgb": _cover_rgb(loft, _active_source_time(recipe, "n2-loft", 2.8), x, y)},
                    {"name": "bathtub_photo", "t": 6.8, "x": x, "y": y, "rgb": _cover_rgb(bathtub_photo, 0, x, y)},
                    {"name": "river_photo", "t": 10.8, "x": x, "y": y, "rgb": _cover_rgb(river_photo, 0, x, y)},
                    {"name": "river_opening", "t": river_opening_t, "x": x, "y": y, "rgb": _cover_rgb(river, _active_source_time(recipe, "n2-river", river_opening_t), x, y)},
                    {"name": "laptop_final", "t": laptop_final_t, "x": x, "y": y, "rgb": _cover_rgb(laptop, _active_source_time(recipe, "n2-laptop", laptop_final_t), x, y)},
                ],
                "caption_samples": [
                    {"name": "title_on", "t": 1.0, "region": [0, 80, 1080, 620], "expect_text": True},
                    {"name": "bath_caption_on", "t": 6.0, "region": [0, 1200, 1080, 1900], "expect_text": True},
                    {"name": "river_caption_on", "t": 12.5, "region": [0, 1200, 1080, 1900], "expect_text": True},
                ],
                "authored_replay": True,
                "sequence_evidence": evidence,
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2) + "\n")
    print(f"wrote authored N2 replay fixture to {out}")


if __name__ == "__main__":
    main()
