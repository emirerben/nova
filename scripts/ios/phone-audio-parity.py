#!/usr/bin/env python3
"""Cloud-vs-phone audio mix parity check (KRI-139).

Builds one set of real inputs (spoken voiceover from macOS `say`, a synthetic
music bed, portrait footage with its own ambient audio), then produces the SAME
edit two ways:

- phone: the real phone compilers (`compile_phone_voiceover_montage_plan`,
  `compile_phone_narrated_plan`) write the recipe the device receives; the
  `AudioParityFixtureTests` package test renders it through `KriaMediaEngine`'s
  production exporter (`phone.mp4`).
- cloud: the real cloud mixer (`template_orchestrate._mix_user_voiceover`, the
  function the cloud renders these edits with) mixes the same inputs
  (`cloud.mp4`).

`compare` then measures both with ffmpeg: integrated loudness + true peak
(`ebur128`), the level at both ends, and the short-term (400 ms) loudness curve.

    src/apps/api/.venv/bin/python scripts/ios/phone-audio-parity.py prepare OUT
    (cd src/apps/ios/Packages/KriaMediaEngine && \
      KRIA_AUDIO_PARITY_DIR=OUT swift test --filter AudioParityFixtureTests)
    src/apps/api/.venv/bin/python scripts/ios/phone-audio-parity.py compare OUT

On a simulator, run the package scheme instead of `swift test`:
    TEST_RUNNER_KRIA_AUDIO_PARITY_DIR=OUT xcodebuild test -scheme KriaMediaEngine \
      -destination "platform=iOS Simulator,name=<iPhone>" \
      -only-testing:KriaMediaEngineTests/AudioParityFixtureTests
(the simulator's file system is the host's, so OUT is readable as is).

Needs ffmpeg and macOS `say` on PATH.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for name, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from app.config import settings  # noqa: E402
from app.kria.media_sources import OriginalMediaDescriptor  # noqa: E402
from app.kria.render_assets import RenderFingerprint  # noqa: E402
from app.pipeline.generative_decision import (  # noqa: E402
    GenerativeAssemblyStepDecision,
    GenerativeVariantDecision,
)
from app.pipeline.phone_narrated_plan import (  # noqa: E402
    NarratedPhoneStep,
    compile_phone_narrated_plan,
)
from app.pipeline.phone_recipe_shared import PhoneMusicBed, PhoneNarrationBed  # noqa: E402
from app.pipeline.phone_voiceover_montage_plan import (  # noqa: E402
    compile_phone_voiceover_montage_plan,
)
from app.services.phone_sources import PhoneSourceBinding  # noqa: E402

SCRIPT = (
    "Okay, so this is the little bakery I keep telling everyone about. "
    "Every morning they pull these out of the oven. "
    "And honestly? The cardamom bun is the reason I get up early."
)
MUSIC_START_S = 4.0
# Cloud default voiceover_bed_level (generative_build: `else 0.25`).
NARRATED_BED_LEVEL = 0.25


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def _fingerprint(path: Path) -> RenderFingerprint:
    return RenderFingerprint(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), byte_count=path.stat().st_size
    )


def _inputs(out: Path) -> dict[str, Path]:
    aiff = out / "voice.aiff"
    subprocess.run(["say", "-v", "Samantha", "-r", "175", "-o", str(aiff), SCRIPT], check=True)
    voice = out / "voice.m4a"
    # A beat of silence either side, like a real recording.
    _ffmpeg(
        "-i",
        str(aiff),
        "-af",
        "adelay=400|400,apad=pad_dur=0.6,aresample=48000",
        "-ac",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(voice),
    )
    duration = _duration(voice)
    music = out / "music.m4a"
    # Two-chord pad + a kick on every beat (120 BPM), 40 s long.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "aevalsrc='0.18*sin(2*PI*220*t)+0.12*sin(2*PI*277.2*t)+0.12*sin(2*PI*329.6*t)"
        "+0.5*sin(2*PI*55*t)*exp(-12*mod(t,0.5))':s=48000:d=40",
        "-ac",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(music),
    )
    footage = out / "footage.mp4"
    # Portrait footage whose own audio is café ambience: pink noise + a hum.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x4a6b8a:s=1080x1920:r=30:d={duration + 2:.3f}",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=color=pink:amplitude=0.08:d={duration + 2:.3f}:r=48000",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=120:sample_rate=48000:d={duration + 2:.3f}",
        "-filter_complex",
        "[1:a][2:a]amix=inputs=2:normalize=0,volume=1.0[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-shortest",
        str(footage),
    )
    return {"voice": voice, "music": music, "footage": footage}


def _binding(footage: Path) -> PhoneSourceBinding:
    fp = _fingerprint(footage)
    return PhoneSourceBinding(
        media_id="footage",
        proxy_path="user/analysis-proxy-footage.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=fp.sha256,
            byte_count=fp.byte_count,
            duration_s=_duration(footage),
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def _narration(voice: Path) -> PhoneNarrationBed:
    return PhoneNarrationBed(
        plan_item_id="item",
        generation="1",
        fingerprint=_fingerprint(voice),
        duration_s=_duration(voice),
    )


def _montage_decision(duration: float, *, mix: float, music: bool) -> GenerativeVariantDecision:
    return GenerativeVariantDecision(
        variant_id="voiceover_music" if music else "voiceover_only",
        rank=1,
        text_mode="none",
        orientation="portrait",
        duration_s=duration,
        assembly_steps=[
            GenerativeAssemblyStepDecision(
                clip_id="c0", in_s=0.0, out_s=duration, target_duration_s=duration
            )
        ],
        music_track_id="track" if music else None,
        music_start_s=MUSIC_START_S if music else None,
        mix=mix,
        extras={
            "base": {},
            "canvas": {"width": 1080, "height": 1920},
            "recipe": {"color_grade": "none"},
            "assembly_landscape_fit": "fill",
            "clip_id_to_media_id": {"c0": "footage"},
            "voiceover_gcs_path": "voiceover-uploads/direct/u/i/voice.m4a",
            "voiceover_target_s": duration,
        },
    )


def _write_case(out: Path, case: str, recipe, files: dict[str, Path]) -> None:
    directory = out / "cases" / case
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    assets = {}
    for asset in recipe.asset_manifest.assets:
        kind = asset.kind
        assets[asset.id] = str(
            files["footage" if kind == "original" else "music" if kind == "library" else "voice"]
        )
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))


def _cloud(out: Path, case: str, files: dict[str, Path], duration: float, **kwargs) -> None:
    from app.tasks import template_orchestrate

    directory = out / "cases" / case
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "assembled.mp4"
        # The cloud's assembled montage: the footage cut to the voiceover window.
        _ffmpeg("-i", str(files["footage"]), "-t", f"{duration:.3f}", "-c", "copy", str(video))
        if kwargs.pop("footage_bed", False):
            bed = Path(tmp) / "bed.m4a"
            _ffmpeg("-i", str(video), "-vn", "-c:a", "copy", str(bed))
            kwargs["footage_bed_path"] = str(bed)
        with mock.patch.object(
            template_orchestrate,
            "download_to_file",
            lambda _path, local: shutil.copyfile(files["music"], local),
        ):
            template_orchestrate._mix_user_voiceover(
                str(video),
                str(files["voice"]),
                str(directory / "cloud.mp4"),
                tmp,
                target_duration_s=duration,
                **kwargs,
            )


def prepare(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    files = _inputs(out)
    duration = _duration(files["voice"])
    binding = _binding(files["footage"])
    narration = _narration(files["voice"])
    lufs = settings.output_target_lufs

    # 1. Voiceover over a matched music bed (voiceover_music, mix 0.7 -> bed 0.3).
    music = PhoneMusicBed(
        catalog_id="track",
        generation="1",
        fingerprint=_fingerprint(files["music"]),
        duration_s=_duration(files["music"]),
        start_s=MUSIC_START_S,
    )
    recipe = compile_phone_voiceover_montage_plan(
        _montage_decision(duration, mix=0.7, music=True),
        (binding,),
        music=music,
        narration=narration,
        target_lufs=lufs,
    )
    _write_case(out, "voiceover_music", recipe, files)
    _cloud(
        out,
        "voiceover_music",
        files,
        duration,
        mix=0.7,
        music_gcs_path="music",
        music_start_offset_s=MUSIC_START_S,
    )

    # 2. Voiceover over the footage's own audio (voiceover_only, mix 0.4).
    recipe = compile_phone_voiceover_montage_plan(
        _montage_decision(duration, mix=0.4, music=False),
        (binding,),
        narration=narration,
        target_lufs=lufs,
    )
    _write_case(out, "voiceover_footage", recipe, files)
    _cloud(out, "voiceover_footage", files, duration, mix=0.4)

    # 3. Narrated: footage bed side-chain ducked under the voice.
    recipe = compile_phone_narrated_plan(
        [NarratedPhoneStep(step_id="s0", media_id="footage", start_s=0.0, end_s=duration)],
        (binding,),
        narration,
        voiceover_duration_s=duration,
        mix=1 - NARRATED_BED_LEVEL,
        target_lufs=lufs,
        duck_footage_bed=True,
    )
    _write_case(out, "narrated_ducked", recipe, files)
    _cloud(out, "narrated_ducked", files, duration, footage_bed=True, bed_level=NARRATED_BED_LEVEL)
    print(f"prepared {out} (voiceover {duration:.2f}s)")


def _ebur128(path: Path) -> tuple[float, float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = result.stderr[result.stderr.rindex("Summary:") :]
    integrated = float(summary.split("I:")[1].split("LUFS")[0])
    peak = float(summary.split("Peak:")[1].split("dBFS")[0])
    return integrated, peak


def _pcm(path: Path) -> list[float]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    import array

    samples = array.array("f")
    samples.frombytes(raw)
    return list(samples)


def _db(samples: list[float], start: float, end: float) -> float:
    window = samples[int(start * 48000) : int(end * 48000)]
    if not window:
        return -120.0
    power = sum(v * v for v in window) / len(window)
    return 10 * math.log10(power) if power > 0 else -120.0


def compare(out: Path) -> int:
    failures = 0
    rows = []
    for directory in sorted((out / "cases").iterdir()):
        cloud, phone = directory / "cloud.mp4", directory / "phone.mp4"
        if not phone.exists():
            print(f"{directory.name}: phone.mp4 missing -- run AudioParityFixtureTests first")
            failures += 1
            continue
        (ci, cp), (pi, pp) = _ebur128(cloud), _ebur128(phone)
        c, p = _pcm(cloud), _pcm(phone)
        # The phone export carries no brand outro here; compare the edit's own span.
        length = min(len(c), len(p)) / 48000
        # 400 ms short-term curve, only where the cloud is audible.
        curve = [
            abs(_db(c, t, t + 0.4) - _db(p, t, t + 0.4))
            for t in [i * 0.1 for i in range(int((length - 0.4) * 10))]
            if _db(c, t, t + 0.4) > -50
        ]
        ranked = sorted(curve)
        row = {
            "case": directory.name,
            "cloud_lufs": ci,
            "phone_lufs": pi,
            "delta_lu": round(pi - ci, 2),
            "cloud_tp": cp,
            "phone_tp": pp,
            "phone_first_20ms_db": round(_db(p, 0, 0.02), 1),
            "phone_last_20ms_db": round(_db(p, length - 0.02, length), 1),
            "cloud_last_20ms_db": round(_db(c, length - 0.02, length), 1),
            "short_term_median_abs_db": round(ranked[len(ranked) // 2], 2) if ranked else None,
            "short_term_p90_abs_db": round(ranked[int(len(ranked) * 0.9)], 2) if ranked else None,
        }
        rows.append(row)
        if abs(row["delta_lu"]) > 1.0 or pp > -1.0:
            failures += 1
    print(json.dumps(rows, indent=2))
    (out / "report.json").write_text(json.dumps(rows, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"prepare", "compare"}:
        raise SystemExit(__doc__)
    target = Path(sys.argv[2]).resolve()
    if sys.argv[1] == "prepare":
        prepare(target)
    else:
        raise SystemExit(compare(target))
