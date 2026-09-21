#!/usr/bin/env python3
"""Prepare the KRI-121 on-device Visuals render check.

Compiles two phone recipes with the server's real `compile_phone_guided_plan`
(footage crossfading into a Visuals photo, the same photo as a supporting card,
a Visuals video and a transparent cutout; and a project made only of Visuals),
writes them as the device receives them, and
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


def _moment(moment_id: str, start: float, end: float, **fields: object) -> dict:
    return {
        "beat_id": "beat",
        "topic": "Scene",
        "layout": "fullscreen",
        "moment_id": moment_id,
        "output_start_s": start,
        "output_end_s": end,
        "duration_s": round(end - start, 3),
        "transition_after": "cut",
        **fields,
    }


def _plan(
    moments: list[dict], media_ids: list[str], *, source_audio: bool
) -> GuidedStoryExecutionPlan:
    duration = moments[-1]["output_end_s"]
    return GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 6,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "guided_story",
            "goal": "Show the scene",
            "pace": "balanced",
            "approved_duration_s": duration,
            "resolved_duration_s": duration,
            "selected_media_ids": media_ids,
            "editor_revision_number": 1,
            **(
                {
                    "montage_audio": {"preserve_source_audio": True},
                    "editor_audio_level": 1.0,
                }
                if source_audio
                else {}
            ),
            "story_timeline": moments,
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": duration,
                    "resolved_duration_s": duration,
                    "start_s": 0,
                    "end_s": duration,
                }
            ],
            "text_elements": [],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
        }
    )


def _status(plan, sources, visuals) -> dict:
    recipe = compile_phone_guided_plan(plan, sources, visuals)
    validate_phone_pilot_recipe(recipe)
    request = make_device_request(
        job_id=uuid.uuid4(), variant_id="guided_story", revision=1, recipe=recipe
    )
    return DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(
        mode="json"
    )


def main() -> None:
    out = Path(
        sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="kria-photo-e2e-")
    )
    out.mkdir(parents=True, exist_ok=True)
    video, photo = out / "video.mp4", out / "photo.jpg"
    pool_video, cutout = out / "pool-video.mp4", out / "cutout.png"
    # Blue footage and a yellow Visuals video, both with a tone. A landscape
    # photo whose halves are red and green, so cover-cropping onto the portrait
    # canvas keeps both halves and the card shows the whole photo. A cutout
    # whose transparent center hides white under magenta edges.
    for path, color, size in (
        (video, "blue", "1920x1080"),
        (pool_video, "yellow", "1080x1920"),
    ):
        _ffmpeg(
            *("-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d=10"),
            *("-f", "lavfi", "-i", "sine=frequency=440:duration=10"),
            *(
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ),
        )
    _ffmpeg(
        *("-f", "lavfi", "-i", "color=c=red:s=2016x3024"),
        *("-f", "lavfi", "-i", "color=c=0x00ff00:s=2016x3024"),
        *("-filter_complex", "hstack", "-frames:v", "1", "-q:v", "2", str(photo)),
    )
    from PIL import Image

    art = Image.new("RGBA", (1080, 1920), (255, 0, 255, 255))
    art.paste((255, 255, 255, 0), (270, 480, 810, 1440))
    art.save(cutout)

    def visual(path: Path, **probe: object) -> PhoneVisualBinding:
        sha, size = _fingerprint(path)
        media_id = str(uuid.uuid4())
        return PhoneVisualBinding(
            media_id=media_id,
            gcs_path=f"users/owner/plan/item/pool/{media_id}{path.suffix}",
            generation="202",
            sha256=sha,
            byte_count=size,
            **probe,
        )

    video_sha, video_bytes = _fingerprint(video)
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
    still, art_still = visual(photo), visual(cutout)
    clip = visual(pool_video, kind="video", duration_s=10, width=1080, height=1920)

    def pooled(
        binding: PhoneVisualBinding, moment_id: str, start: float, end: float, **fields
    ):
        return _moment(
            moment_id,
            start,
            end,
            media_id=binding.media_id,
            lane="asset",
            kind=binding.kind,
            gcs_path=binding.gcs_path,
            generation=binding.generation,
            source_start_s=1 if binding.kind == "video" else 0,
            source_end_s=round(end - start + (1 if binding.kind == "video" else 0), 3),
            **fields,
        )

    settings.phone_render_verified_features = [*VERIFIED, "stillImages", "visualVideos"]
    footage = _moment(
        "video",
        0,
        3,
        media_id=source.media_id,
        lane="clip",
        kind="video",
        gcs_path=source.proxy_path,
        generation=source.generation,
        source_start_s=2,
        source_end_s=5,
        transition_after="crossfade",
        transition_duration_s=0.3,
    )
    mixed = _plan(
        [
            footage,
            pooled(still, "photo", 2.7, 4.7),
            pooled(still, "card", 4.7, 6.7, layout="supporting_card"),
            pooled(clip, "pool-video", 6.7, 8.7),
            pooled(art_still, "cutout", 8.7, 10.7),
        ],
        [source.media_id, still.media_id, clip.media_id, art_still.media_id],
        source_audio=True,
    )
    # A project made only of Visuals renders on the iPhone with no originals.
    visuals_only = _plan(
        [pooled(still, "photo", 0, 2), pooled(clip, "pool-video", 2, 4)],
        [still.media_id, clip.media_id],
        source_audio=False,
    )
    (out / "status.json").write_text(
        json.dumps(_status(mixed, (source,), (still, clip, art_still)), indent=2)
    )
    (out / "status-visuals-only.json").write_text(
        json.dumps(_status(visuals_only, (), (still, clip)), indent=2)
    )
    (out / "e2e.json").write_text(
        json.dumps(
            {
                "video_media_id": source.media_id,
                "verified_features": settings.phone_render_verified_features,
                "visual_files": {
                    still.render_asset().id: photo.name,
                    clip.render_asset().id: pool_video.name,
                    art_still.render_asset().id: cutout.name,
                },
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
    print(f"Frames and the rendered MP4s land in {out / 'frames'}.")


if __name__ == "__main__":
    main()
