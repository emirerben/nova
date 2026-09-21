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

Three more cases exercise the OTHER two KRI-132 phone compilers -- neither
goes through `compile_phone_montage_plan` at all:
  - `subtitled_sentence` -- `app.pipeline.phone_subtitled_plan
                     .compile_phone_subtitled_plan`: one portrait clip,
                     sentence (`pop-in`) captions with a deliberate gap
                     between cues, the clip's own audio kept at full volume,
                     no music/narration (basicComposition/local1080Export/
                     positionedText/animatedText).
  - `subtitled_word` -- same compiler, `caption_style="word"` -- per-word
                     timings compile to the karaoke-line highlight sweep
                     instead of plain pop-in blocks.
  - `narrated`    -- `app.pipeline.phone_narrated_plan
                     .compile_phone_narrated_plan`: two clips tiled onto
                     narration step windows; the second clip is SHORTER than
                     its step, so its `TimelineClip.rate` is exercised < 1
                     (slow-down, never freeze-hold) -- plus captions and an
                     audible footage bed under the voice (basicComposition/
                     local1080Export/narrationAudio/audioMix/variableSpeed/
                     positionedText/animatedText).

All three new cases additionally write `caption_samples` into `e2e.json`
(`{name, t, region, expect_text}`): a region derived from the compiled
recipe's own `PortableTextLayer`/`PositionedTextRun` geometry (see
`_caption_region` below), sampled once while a cue is on screen and once in
a deliberate gap with no active cue, so `DeviceMontageRenderE2ETests` can
assert caption pixels appear and disappear without OCR.

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
from app.pipeline.phone_narrated_plan import NarratedPhoneStep, compile_phone_narrated_plan
from app.pipeline.phone_recipe_shared import PhoneMusicBed, PhoneNarrationBed
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
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


def _binding(
    path: Path, media_id: str, *, duration_s: float = CLIP_DURATION_S
) -> PhoneSourceBinding:
    sha, size = _fingerprint(path)
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"users/owner/creation/analysis-proxy-{media_id}.mp4",
        generation="101",
        original=OriginalMediaDescriptor(
            sha256=sha,
            byte_count=size,
            duration_s=duration_s,
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


def _caption_region(
    layer, *, canvas_width: int, canvas_height: int, pad: float = 56.0
) -> list[int]:
    """Generous pixel bbox ([x0, y0, x1, y1], top-left origin) around every
    run in a compiled caption `layer`.

    Derived from the layer's own resolved geometry rather than hardcoded:
    `PositionedTextRun.x`/`baseline_y` are already fully-anchored ABSOLUTE
    canvas pixels (see `portable_text_layout.compile_text_overlay` --
    `x=cloud._anchored_left_x(...)`, `baseline_y=top + ascent + ...`), so only
    each run's width needs approximating (`compile_text_overlay` doesn't hand
    back a measured run width to its caller). A generous per-character
    estimate plus `pad` only needs to be a safe SUPERSET of the actual glyph
    pixels for a presence/absence pixel check, not an exact box.
    """
    lefts: list[float] = []
    rights: list[float] = []
    tops: list[float] = []
    bottoms: list[float] = []
    for run in layer.runs:
        est_width = len(run.text) * run.font_size * 0.66
        lefts.append(run.x)
        rights.append(run.x + est_width)
        tops.append(run.baseline_y - run.font_size * 1.1)
        bottoms.append(run.baseline_y + run.font_size * 0.4)
    x0 = max(0.0, min(lefts) - pad)
    x1 = min(float(canvas_width), max(rights) + pad)
    y0 = max(0.0, min(tops) - pad)
    y1 = min(float(canvas_height), max(bottoms) + pad)
    return [round(x0), round(y0), round(x1), round(y1)]


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
        "music-c1": ("white", 698),
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

    # --- case (e): "Talking to camera" subtitled, sentence captions --------
    # A deliberate 1.0s and 0.8s gap between cues so caption pixel-presence
    # can be checked both on and off.
    subtitled_sentence_cues = [
        {"text": "Hello there friend", "start_s": 0.3, "end_s": 1.3},
        {"text": "Welcome to the show", "start_s": 2.3, "end_s": 3.5},
        {"text": "Thanks for watching", "start_s": 4.3, "end_s": 5.6},
    ]
    _color_clip(out / "subtitled-sentence-c0.mp4", "blue", 340, duration=6.0)
    subtitled_sentence_binding = _binding(
        out / "subtitled-sentence-c0.mp4", "subtitled-sentence-c0", duration_s=6.0
    )
    try:
        recipes["subtitled_sentence"] = compile_phone_subtitled_plan(
            (subtitled_sentence_binding,), caption_cues=subtitled_sentence_cues
        )
    except (UnsupportedPhonePlan, ValueError) as exc:
        compile_errors["subtitled_sentence"] = f"{type(exc).__name__}: {exc}"

    # --- case (f): "Talking to camera" subtitled, word/karaoke captions ----
    subtitled_word_cues = [
        {
            "text": "Hello world",
            "start_s": 0.3,
            "end_s": 1.4,
            "words": [
                {"text": "Hello", "start_s": 0.3, "end_s": 0.8},
                {"text": "world", "start_s": 0.8, "end_s": 1.4},
            ],
        },
        {
            "text": "Testing captions",
            "start_s": 2.4,
            "end_s": 3.7,
            "words": [
                {"text": "Testing", "start_s": 2.4, "end_s": 3.0},
                {"text": "captions", "start_s": 3.0, "end_s": 3.7},
            ],
        },
    ]
    _color_clip(out / "subtitled-word-c0.mp4", "teal", 350, duration=6.0)
    subtitled_word_binding = _binding(
        out / "subtitled-word-c0.mp4", "subtitled-word-c0", duration_s=6.0
    )
    try:
        recipes["subtitled_word"] = compile_phone_subtitled_plan(
            (subtitled_word_binding,), caption_cues=subtitled_word_cues, caption_style="word"
        )
    except (UnsupportedPhonePlan, ValueError) as exc:
        compile_errors["subtitled_word"] = f"{type(exc).__name__}: {exc}"

    # --- case (g): narrated walkthrough -- second clip retimes (rate<1) ----
    # c0 has more footage (5s) than its 4s step -> trims, rate=1.0. c1 has
    # LESS footage (3s) than its 6s step -> slows down instead of freezing.
    _color_clip(out / "narrated-c0.mp4", "gold", 260, duration=5.0)
    _color_clip(out / "narrated-c1.mp4", "salmon", 410, duration=3.0)
    narrated_bindings = (
        _binding(out / "narrated-c0.mp4", "narrated-c0", duration_s=5.0),
        _binding(out / "narrated-c1.mp4", "narrated-c1", duration_s=3.0),
    )
    narrated_steps = [
        NarratedPhoneStep(step_id="s0", media_id="narrated-c0", start_s=0.0, end_s=4.0),
        NarratedPhoneStep(step_id="s1", media_id="narrated-c1", start_s=4.0, end_s=10.0),
    ]
    # Reuses the same 10s voice tone (`voice_path`/`voice_sha`/`voice_bytes`,
    # already computed above for the montage "narration" case) under a
    # distinct `plan_item_id` -- both are just fixture tone bytes, and a
    # second real ffmpeg render buys nothing here.
    narrated_narration = PhoneNarrationBed(
        plan_item_id="e2e-narrated-item",
        generation="9",
        fingerprint=RenderFingerprint(sha256=voice_sha, byte_count=voice_bytes),
        duration_s=10.0,
    )
    narrated_cues = [
        {"text": "Look at this view", "start_s": 0.5, "end_s": 2.0},
        {"text": "Now check this out", "start_s": 5.0, "end_s": 6.5},
    ]
    try:
        recipes["narrated"] = compile_phone_narrated_plan(
            narrated_steps,
            narrated_bindings,
            narrated_narration,
            voiceover_duration_s=10.0,
            # Voice stays dominant but footage_bed_gain = 1 - mix = 0.4 keeps
            # the clips' own audio audible under it (unlike "music", which
            # replaces source audio entirely).
            mix=0.6,
            caption_cues=narrated_cues,
        )
    except (UnsupportedPhonePlan, ValueError) as exc:
        compile_errors["narrated"] = f"{type(exc).__name__}: {exc}"

    if compile_errors:
        print("phone compiler REJECTED:")
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

    # Caption regions are derived from each case's OWN compiled text layers
    # (see `_caption_region`), not hardcoded -- one region per cue, indexed
    # in the same order the cues were passed (cues are already sorted and
    # none of ours are dropped, so cue index == `recipe.text_layers` index).
    subtitled_sentence_region0 = _caption_region(
        recipes["subtitled_sentence"].text_layers[0],
        canvas_width=CANVAS["width"],
        canvas_height=CANVAS["height"],
    )
    subtitled_word_region0 = _caption_region(
        recipes["subtitled_word"].text_layers[0],
        canvas_width=CANVAS["width"],
        canvas_height=CANVAS["height"],
    )
    narrated_region0 = _caption_region(
        recipes["narrated"].text_layers[0],
        canvas_width=CANVAS["width"],
        canvas_height=CANVAS["height"],
    )
    narrated_region1 = _caption_region(
        recipes["narrated"].text_layers[1],
        canvas_width=CANVAS["width"],
        canvas_height=CANVAS["height"],
    )

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
                    {"name": "c1", "t": 4.5, "x": 540, "y": 960, "rgb": [255, 255, 255]},
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
            "subtitled_sentence": {
                "status_file": "status-subtitled-sentence.json",
                "duration_s": recipes["subtitled_sentence"].duration,
                "required_capabilities": sorted(
                    recipes["subtitled_sentence"].required_capabilities
                ),
                "drop_capability": "positionedText",
                "clips": [
                    {"media_id": "subtitled-sentence-c0", "file": "subtitled-sentence-c0.mp4"},
                ],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": False,
                # Sampled near the top of frame, far from the caption safe
                # zone (which sits near the bottom -- cloud margin 384px on
                # a 1920px canvas), so this never overlaps a caption region.
                "samples": [
                    {"name": "c0", "t": 1.0, "x": 540, "y": 200, "rgb": [0, 0, 255]},
                ],
                "caption_samples": [
                    {
                        "name": "cue0_on",
                        "t": 0.8,
                        "region": subtitled_sentence_region0,
                        "expect_text": True,
                    },
                    {
                        "name": "cue0_gap",
                        "t": 1.8,
                        "region": subtitled_sentence_region0,
                        "expect_text": False,
                    },
                ],
            },
            "subtitled_word": {
                "status_file": "status-subtitled-word.json",
                "duration_s": recipes["subtitled_word"].duration,
                "required_capabilities": sorted(recipes["subtitled_word"].required_capabilities),
                "drop_capability": "animatedText",
                "clips": [
                    {"media_id": "subtitled-word-c0", "file": "subtitled-word-c0.mp4"},
                ],
                "music_asset_id": None,
                "music_file": None,
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": False,
                "samples": [
                    {"name": "c0", "t": 1.0, "x": 540, "y": 200, "rgb": [0, 128, 128]},
                ],
                "caption_samples": [
                    {
                        "name": "cue0_on",
                        "t": 0.6,
                        "region": subtitled_word_region0,
                        "expect_text": True,
                    },
                    {
                        "name": "cue0_gap",
                        "t": 2.0,
                        "region": subtitled_word_region0,
                        "expect_text": False,
                    },
                ],
            },
            "narrated": {
                "status_file": "status-narrated.json",
                "duration_s": recipes["narrated"].duration,
                "required_capabilities": sorted(recipes["narrated"].required_capabilities),
                "drop_capability": "variableSpeed",
                "clips": [
                    {"media_id": "narrated-c0", "file": "narrated-c0.mp4"},
                    {"media_id": "narrated-c1", "file": "narrated-c1.mp4"},
                ],
                "music_asset_id": None,
                "music_file": None,
                "voiceover_asset_id": f"voiceover-{narrated_narration.plan_item_id}",
                "voiceover_file": voice_path.name,
                # mix=0.6 keeps the voice dominant but footage_bed_gain
                # (1 - mix = 0.4) leaves the clips' own audio audible under
                # it -- unlike "music", which replaces source audio entirely.
                "expects_source_audio": True,
                "expects_music_audio": False,
                "expects_narration_audio": True,
                "samples": [
                    {"name": "c0", "t": 1.5, "x": 540, "y": 200, "rgb": [255, 215, 0]},
                    {"name": "c1", "t": 7.0, "x": 540, "y": 200, "rgb": [250, 128, 114]},
                ],
                "caption_samples": [
                    {"name": "cue0_on", "t": 0.9, "region": narrated_region0, "expect_text": True},
                    {
                        "name": "cue0_gap",
                        "t": 3.0,
                        "region": narrated_region0,
                        "expect_text": False,
                    },
                    {"name": "cue1_on", "t": 5.5, "region": narrated_region1, "expect_text": True},
                    {
                        "name": "cue1_gap",
                        "t": 8.0,
                        "region": narrated_region1,
                        "expect_text": False,
                    },
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
    for case_id, case in e2e["cases"].items():
        caption_samples = case.get("caption_samples")
        if not caption_samples:
            continue
        regions = {sample["name"]: sample["region"] for sample in caption_samples}
        print(f"  {case_id} caption regions: {regions}")
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
