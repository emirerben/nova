#!/usr/bin/env python3
"""Prepare the KRI-132 on-device montage-family render check.

Companion to `scripts/ios/phone-photo-render-e2e.py` (KRI-121, guided
compiler only). That script never touches `app.pipeline.phone_montage_plan
.compile_phone_montage_plan` -- the SECOND phone compiler, used for
`montage`/`day_vlog`/`single_hero` generative-edit variants (see
`docs/runbooks/phone-rendering.md`'s "Montage family on the phone" section).
This script closes that gap the same way: it builds the exact decision/
binding shapes `tests/pipeline/test_phone_montage_plan.py` uses (`_binding`/
`_step`/`fixture`, ~lines 15-101 there), calls the REAL
`compile_phone_montage_plan` followed by the REAL
`app.services.phone_rollout.validate_phone_pilot_recipe`, and writes what the
device receives so `DeviceMontageRenderE2ETests` can render it through the
production device resolver and exporter on the simulator.

Four cases exercise four distinct compiler branches:
  - `cuts_text`   -- plain cuts, preserved original audio, an agent-text intro
                     (basicComposition/positionedText/animatedText).
  - `crossfade`   -- a crossfade transition between two clips, no text/music
                     (basicComposition/crossfade).
  - `music`       -- a licensed music bed replaces the original audio, no
                     text/transition (basicComposition/audioMix/musicBed).
  - `narration`   -- a recorded voiceover (KRI-132), mixed under the clips'
                     own audio at mix=0.4, through the SAME per-asset grant
                     as the music case's `.library` asset (a `.voiceover`
                     asset now, see `app.kria.render_assets
                     .VoiceoverRenderAsset`), with a source tone longer than
                     the video timeline to prove the compiler's duration
                     clamp (basicComposition/audioMix/narrationAudio).

`day_vlog`/`single_hero` are deliberately NOT separate cases: read
`app/pipeline/phone_montage_plan.py` in full -- neither `resolved_archetype`
nor `edit_format` is referenced anywhere in `compile_phone_montage_plan`.
`app/pipeline/generative_decision.py`'s module docstring confirms this is by
design ("the montage-family renderer itself has no `edit_format` concept ...
present for forward compatibility with callers that do"). Archetype only
steers what the upstream matcher/decision phase selects as `assembly_steps`
BEFORE this compiler ever sees them; the compiler itself treats every
montage-family archetype identically. A format-specific phone case would
therefore just be a relabeled `cuts_text`/`crossfade` case, not a new branch.

Run with the API's Python environment (it imports `app`):
    src/apps/api/.venv/bin/python scripts/ios/phone-montage-render-e2e.py [OUT_DIR]
Needs ffmpeg on PATH.

Render it on a simulator from src/apps/ios (KRIA_E2E_DIR must be OUT_DIR):
    TEST_RUNNER_KRIA_E2E_DIR=OUT_DIR xcodebuild -project Kria.xcodeproj -scheme Kria \
      -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO \
      -destination "platform=iOS Simulator,name=<iPhone>" \
      -only-testing:KriaTests/DeviceMontageRenderE2ETests test
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
from app.kria.render_assets import RenderFingerprint
from app.pipeline.generative_decision import (
    GenerativeAssemblyStepDecision,
    GenerativeVariantDecision,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_montage_plan import compile_phone_montage_plan
from app.pipeline.phone_recipe_shared import PhoneMusicBed, PhoneNarrationBed
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding

CLIP_DURATION_S = 3.0
CANVAS = {"width": 1080, "height": 1920}


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def _color_clip(path: Path, color: str, freq: int, duration: float = CLIP_DURATION_S) -> None:
    """A short portrait clip: solid `color` video + a `freq`-Hz sine tone, so a
    rendered frame's dominant color and a decoded audio track's frequency
    content are both independently checkable on the device side."""
    _ffmpeg(
        *("-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:r=30:d={duration}"),
        *("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}"),
        *("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)),
    )


def _tone(path: Path, freq: int, duration: float) -> None:
    """A standalone AAC tone -- the licensed-music-bed stand-in. `RenderLibraryCache`
    stores installed library bytes at an extensionless `<sha256>-<byteCount>` path
    (see `KriaMediaEngine/RenderAssets.swift`'s `location(_:)`); AAC-in-M4A is a
    reasonable production proxy since the real catalog serves AAC audio."""
    _ffmpeg(
        *("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}"),
        *("-c:a", "aac", str(path)),
    )


def _binding(path: Path, media_id: str) -> PhoneSourceBinding:
    sha, size = _fingerprint(path)
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"users/owner/creation/analysis-proxy-{media_id}.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=sha,
            byte_count=size,
            duration_s=CLIP_DURATION_S,
            width=CANVAS["width"],
            height=CANVAS["height"],
            has_audio=True,
        ),
    )


def _step(
    clip_id: str,
    *,
    in_s: float = 0.0,
    duration: float = CLIP_DURATION_S,
    transition_in: str | None = None,
    transition_duration_s: float | None = None,
) -> GenerativeAssemblyStepDecision:
    return GenerativeAssemblyStepDecision(
        clip_id=clip_id,
        in_s=in_s,
        out_s=in_s + duration,
        target_duration_s=duration,
        transition_in=transition_in,
        transition_duration_s=transition_duration_s,
    )


def _decision(
    variant_id: str,
    steps: list[GenerativeAssemblyStepDecision],
    clip_id_to_media_id: dict[str, str],
    *,
    text_mode: str,
    intro_overlay_params: dict | None,
    music_track_id: str | None = None,
    music_start_s: float | None = None,
    mix: float | None = None,
    voiceover_gcs_path: str | None = None,
    voiceover_target_s: float | None = None,
) -> GenerativeVariantDecision:
    extras = {
        "base": {},
        "variant_t0": 0.0,
        "recipe": {"color_grade": "none"},
        "beats": [],
        "canvas": CANVAS,
        "masonry_requested": False,
        "lyrics_rendered": False,
        "assembly_landscape_fit": "fill",
        "clip_id_to_media_id": clip_id_to_media_id,
        "intro_overlay_params": intro_overlay_params,
    }
    if voiceover_gcs_path:
        extras["voiceover_gcs_path"] = voiceover_gcs_path
        extras["voiceover_target_s"] = voiceover_target_s
    return GenerativeVariantDecision(
        variant_id=variant_id,
        rank=1,
        text_mode=text_mode,
        resolved_archetype="montage",
        orientation="portrait",
        duration_s=sum(s.target_duration_s or 0 for s in steps),
        assembly_steps=steps,
        music_track_id=music_track_id,
        music_start_s=music_start_s,
        mix=mix,
        extras=extras,
    )


def _status(job_id: uuid.UUID, variant_id: str, recipe) -> dict:
    request = make_device_request(job_id=job_id, variant_id=variant_id, revision=1, recipe=recipe)
    return DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(mode="json")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="kria-montage-e2e-"))
    out.mkdir(parents=True, exist_ok=True)

    # --- media fixtures -----------------------------------------------
    clips = {
        "cuts-c0": ("red", 440),
        "cuts-c1": ("lime", 494),  # ffmpeg "green" is #008000, not pure green
        "crossfade-c0": ("blue", 523),
        "crossfade-c1": ("yellow", 587),
        "music-c0": ("cyan", 659),
        "music-c1": ("magenta", 698),
        "narration-c0": ("orange", 349),
        "narration-c1": ("purple", 392),
    }
    for name, (color, freq) in clips.items():
        _color_clip(out / f"{name}.mp4", color, freq)
    tone_path = out / "music-bed.m4a"
    _tone(tone_path, 880, duration=8.0)
    # Longer than the two narration clips' combined 6s timeline, to prove the
    # compiler clamps the narration track's `source_duration` to the video
    # timeline rather than the voiceover's own (longer) length.
    voice_path = out / "voiceover.m4a"
    _tone(voice_path, 1046, duration=10.0)

    bindings = {name: _binding(out / f"{name}.mp4", name) for name in clips}

    recipes: dict[str, object] = {}
    compile_errors: dict[str, str] = {}

    def _compile(case_id: str, decision, case_bindings, *, music=None, narration=None) -> None:
        try:
            recipes[case_id] = compile_phone_montage_plan(
                decision, case_bindings, music=music, narration=narration
            )
        except (UnsupportedPhonePlan, ValueError) as exc:
            compile_errors[case_id] = f"{type(exc).__name__}: {exc}"

    # --- case (a): plain cuts, original audio, agent-text intro -------
    cuts_decision = _decision(
        "cuts_text",
        [_step("cuts-c0"), _step("cuts-c1")],
        {"cuts-c0": "cuts-c0", "cuts-c1": "cuts-c1"},
        text_mode="agent_text",
        intro_overlay_params={
            "text": "hello world",
            "effect": "fade-in",
            "layout": "linear",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFD24A",
        },
    )
    _compile("cuts_text", cuts_decision, (bindings["cuts-c0"], bindings["cuts-c1"]))

    # --- case (b): licensed music bed replaces original audio ---------
    tone_sha, tone_bytes = _fingerprint(tone_path)
    music_bed = PhoneMusicBed(
        catalog_id="e2e-track",
        generation="7",
        fingerprint=RenderFingerprint(sha256=tone_sha, byte_count=tone_bytes),
        duration_s=8.0,
        start_s=0.0,
        volume=1.0,
    )
    music_decision = _decision(
        "music",
        [_step("music-c0"), _step("music-c1")],
        {"music-c0": "music-c0", "music-c1": "music-c1"},
        text_mode="none",
        intro_overlay_params=None,
        music_track_id="e2e-track",
        music_start_s=0.0,
    )
    _compile("music", music_decision, (bindings["music-c0"], bindings["music-c1"]), music=music_bed)

    # --- case (c): crossfade transition, no text/music -----------------
    crossfade_decision = _decision(
        "crossfade",
        [
            _step("crossfade-c0"),
            _step("crossfade-c1", transition_in="crossfade", transition_duration_s=0.3),
        ],
        {"crossfade-c0": "crossfade-c0", "crossfade-c1": "crossfade-c1"},
        text_mode="none",
        intro_overlay_params=None,
    )
    _compile("crossfade", crossfade_decision, (bindings["crossfade-c0"], bindings["crossfade-c1"]))

    # --- case (d): recorded voiceover mixed under the clips' own audio -
    voice_sha, voice_bytes = _fingerprint(voice_path)
    narration_bed = PhoneNarrationBed(
        plan_item_id="e2e-item",
        generation="9",
        fingerprint=RenderFingerprint(sha256=voice_sha, byte_count=voice_bytes),
        duration_s=10.0,
    )
    narration_decision = _decision(
        "narration",
        [_step("narration-c0"), _step("narration-c1")],
        {"narration-c0": "narration-c0", "narration-c1": "narration-c1"},
        text_mode="none",
        intro_overlay_params=None,
        mix=0.4,
        voiceover_gcs_path="voiceover-uploads/direct/u/e2e-item/voice.m4a",
        voiceover_target_s=None,  # falls back to narration_bed.duration_s (10s), then clamps to the 6s video timeline
    )
    _compile(
        "narration", narration_decision, (bindings["narration-c0"], bindings["narration-c1"]),
        narration=narration_bed,
    )

    if compile_errors:
        print("compile_phone_montage_plan REJECTED:")
        for case_id, reason in compile_errors.items():
            print(f"  {case_id}: {reason}")
        sys.exit(1)

    # --- verify + validate ----------------------------------------------
    all_capabilities: set[str] = set()
    for recipe in recipes.values():
        all_capabilities |= recipe.required_capabilities
    settings.phone_render_verified_features = sorted(all_capabilities)

    rejections: dict[str, str] = {}
    for case_id, recipe in recipes.items():
        try:
            validate_phone_pilot_recipe(recipe)
        except ValueError as exc:
            rejections[case_id] = str(exc)

    if rejections:
        print("validate_phone_pilot_recipe REJECTED:")
        for case_id, reason in rejections.items():
            print(f"  {case_id}: {reason}")
        print(f"verified_features used: {sorted(all_capabilities)}")
        sys.exit(1)

    # --- write device fixtures -------------------------------------------
    job_ids = {case_id: uuid.uuid4() for case_id in recipes}
    for case_id, recipe in recipes.items():
        status = _status(job_ids[case_id], case_id, recipe)
        (out / f"status-{case_id.replace('_', '-')}.json").write_text(json.dumps(status, indent=2))

    e2e = {
        "verified_features": sorted(all_capabilities),
        "cases": {
            "cuts_text": {
                "status_file": "status-cuts-text.json",
                "duration_s": recipes["cuts_text"].duration,
                "required_capabilities": sorted(recipes["cuts_text"].required_capabilities),
                "drop_capability": "positionedText",
                "clips": [
                    {"media_id": "cuts-c0", "file": "cuts-c0.mp4"},
                    {"media_id": "cuts-c1", "file": "cuts-c1.mp4"},
                ],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "samples": [
                    {"name": "c0", "t": 1.5, "x": 60, "y": 100, "rgb": [255, 0, 0]},
                    {"name": "c1", "t": 4.5, "x": 60, "y": 100, "rgb": [0, 255, 0]},
                ],
            },
            "music": {
                "status_file": "status-music.json",
                "duration_s": recipes["music"].duration,
                "required_capabilities": sorted(recipes["music"].required_capabilities),
                "drop_capability": "musicBed",
                "clips": [
                    {"media_id": "music-c0", "file": "music-c0.mp4"},
                    {"media_id": "music-c1", "file": "music-c1.mp4"},
                ],
                "music_asset_id": f"music-{music_bed.catalog_id}",
                "music_file": tone_path.name,
                "expects_source_audio": False,
                "expects_music_audio": True,
                "samples": [
                    {"name": "c0", "t": 1.5, "x": 540, "y": 960, "rgb": [0, 255, 255]},
                    {"name": "c1", "t": 4.5, "x": 540, "y": 960, "rgb": [255, 0, 255]},
                ],
            },
            "crossfade": {
                "status_file": "status-crossfade.json",
                "duration_s": recipes["crossfade"].duration,
                "required_capabilities": sorted(recipes["crossfade"].required_capabilities),
                "drop_capability": "crossfade",
                "clips": [
                    {"media_id": "crossfade-c0", "file": "crossfade-c0.mp4"},
                    {"media_id": "crossfade-c1", "file": "crossfade-c1.mp4"},
                ],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "samples": [
                    {"name": "c0", "t": 1.0, "x": 540, "y": 960, "rgb": [0, 0, 255]},
                    {"name": "c1", "t": 5.0, "x": 540, "y": 960, "rgb": [255, 255, 0]},
                ],
                "blend_sample": {"t": 2.85, "x": 540, "y": 960},
            },
            "narration": {
                "status_file": "status-narration.json",
                "duration_s": recipes["narration"].duration,
                "required_capabilities": sorted(recipes["narration"].required_capabilities),
                "drop_capability": "narrationAudio",
                "clips": [
                    {"media_id": "narration-c0", "file": "narration-c0.mp4"},
                    {"media_id": "narration-c1", "file": "narration-c1.mp4"},
                ],
                "music_asset_id": None,
                "music_file": None,
                "voiceover_asset_id": f"voiceover-{narration_bed.plan_item_id}",
                "voiceover_file": voice_path.name,
                # mix=0.4 mixes the clips' own audio in under the voice (never silences
                # it) -- unlike the "music" case, which replaces source audio entirely.
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": True,
                "samples": [
                    {"name": "c0", "t": 1.5, "x": 540, "y": 960, "rgb": [255, 165, 0]},
                    {"name": "c1", "t": 4.5, "x": 540, "y": 960, "rgb": [128, 0, 128]},
                ],
            },
        },
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2))

    print(f"Wrote {out}. Recipes compiled and validated for: {', '.join(recipes)}")
    for case_id, recipe in recipes.items():
        print(
            f"  {case_id}: duration={recipe.duration:.3f}s "
            f"required_capabilities={sorted(recipe.required_capabilities)}"
        )
    print(
        "verified_features (union, written to settings for validation only): "
        f"{sorted(all_capabilities)}"
    )
    print("Render it on a simulator from src/apps/ios:")
    print(
        f"  TEST_RUNNER_KRIA_E2E_DIR={out} xcodebuild -project Kria.xcodeproj -scheme Kria "
        "-skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO "
        '-destination "platform=iOS Simulator,name=<iPhone>" '
        "-only-testing:KriaTests/DeviceMontageRenderE2ETests test"
    )
    print(f"Frames and the rendered MP4s land in {out / 'frames'}.")


if __name__ == "__main__":
    main()
