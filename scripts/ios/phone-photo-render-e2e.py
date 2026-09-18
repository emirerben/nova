#!/usr/bin/env python3
"""Prepare the KRI-121 on-device photo render check.

Compiles a phone recipe (footage, crossfade, Visuals photo) with the server's
real `compile_phone_guided_plan`, writes it as the device receives it, and
prints the simulator test command that renders it through the production
device resolver and exporter (`DevicePhotoRenderE2ETests`).

Run with the API's Python environment (it imports `app`):
    src/apps/api/.venv/bin/python scripts/ios/phone-photo-render-e2e.py [OUT_DIR]
Needs ffmpeg on PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
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
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import (
    PhoneSourceBinding,
    PhoneVisualBinding,
)

VERIFIED = [
    "basicComposition",
    "positionedText",
    "audioMix",
    "local1080Export",
    "crossfade",
]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def main() -> None:
    out = Path(
        sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="kria-photo-e2e-")
    )
    out.mkdir(parents=True, exist_ok=True)
    video, photo = out / "video.mp4", out / "photo.jpg"
    # Solid blue footage with a tone; a landscape photo whose halves are red and
    # green, so cover-cropping onto the portrait canvas keeps both halves.
    _ffmpeg(
        *("-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=30:d=10"),
        *("-f", "lavfi", "-i", "sine=frequency=440:duration=10"),
        *(
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ),
    )
    _ffmpeg(
        *("-f", "lavfi", "-i", "color=c=red:s=2016x3024"),
        *("-f", "lavfi", "-i", "color=c=0x00ff00:s=2016x3024"),
        *("-filter_complex", "hstack", "-frames:v", "1", "-q:v", "2", str(photo)),
    )
    video_sha, video_bytes = _fingerprint(video)
    photo_sha, photo_bytes = _fingerprint(photo)
    photo_id = str(uuid.uuid4())
    source = PhoneSourceBinding(
        media_id="analysis-proxy-e2e.mp4",
        proxy_path="users/owner/creation/analysis-proxy-e2e.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=video_sha,
            byte_count=video_bytes,
            duration_s=10,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )
    visual = PhoneVisualBinding(
        media_id=photo_id,
        gcs_path=f"users/owner/plan/item/pool/{photo_id}.jpg",
        generation="202",
        sha256=photo_sha,
        byte_count=photo_bytes,
    )
    beat = {"beat_id": "beat", "topic": "Scene", "layout": "fullscreen"}
    plan = GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 6,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "guided_story",
            "goal": "Show the scene",
            "pace": "balanced",
            "approved_duration_s": 4.7,
            "resolved_duration_s": 4.7,
            "selected_media_ids": [source.media_id, photo_id],
            "editor_revision_number": 1,
            "montage_audio": {"preserve_source_audio": True},
            "editor_audio_level": 1.0,
            "story_timeline": [
                {
                    **beat,
                    "moment_id": "video",
                    "media_id": source.media_id,
                    "lane": "clip",
                    "kind": "video",
                    "gcs_path": source.proxy_path,
                    "generation": source.generation,
                    "source_start_s": 2,
                    "source_end_s": 5,
                    "output_start_s": 0,
                    "output_end_s": 3,
                    "duration_s": 3,
                    "transition_after": "crossfade",
                    "transition_duration_s": 0.3,
                },
                {
                    **beat,
                    "moment_id": "photo",
                    "media_id": photo_id,
                    "lane": "asset",
                    "kind": "image",
                    "gcs_path": visual.gcs_path,
                    "generation": visual.generation,
                    "source_start_s": 0,
                    "source_end_s": 2,
                    "output_start_s": 2.7,
                    "output_end_s": 4.7,
                    "duration_s": 2,
                    "transition_after": "cut",
                },
            ],
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": 4.7,
                    "resolved_duration_s": 4.7,
                    "start_s": 0,
                    "end_s": 4.7,
                }
            ],
            "text_elements": [],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
        }
    )
    settings.phone_render_verified_features = [*VERIFIED, "stillImages"]
    recipe = compile_phone_guided_plan(plan, (source,), (visual,))
    validate_phone_pilot_recipe(recipe)
    request = make_device_request(
        job_id=uuid.uuid4(), variant_id="guided_story", revision=1, recipe=recipe
    )
    status = DeviceRenderStatus(phase="awaiting_device", request=request)
    (out / "status.json").write_text(
        json.dumps(status.model_dump(mode="json"), indent=2)
    )
    (out / "e2e.json").write_text(
        json.dumps(
            {
                "video_media_id": source.media_id,
                "visual_asset_id": visual.render_asset().id,
                "verified_features": settings.phone_render_verified_features,
            }
        )
    )
    print(f"Wrote {out}. Render it on a simulator from src/apps/ios:")
    print(
        f"  TEST_RUNNER_KRIA_E2E_DIR={out} xcodebuild -project Kria.xcodeproj -scheme Kria "
        "-skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO "
        '-destination "platform=iOS Simulator,name=<iPhone>" '
        "-only-testing:KriaTests/DevicePhotoRenderE2ETests test"
    )
    print(f"Frames and the rendered MP4 land in {out / 'frames'}.")


if __name__ == "__main__":
    main()
