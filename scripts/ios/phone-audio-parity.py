#!/usr/bin/env python3
"""Real-export proof for phone audio (KRI-139 cloud parity, KRI-470 PR-G user song).

Two kinds of case share one harness (`prepare` writes recipes + inputs, the
`AudioParityFixtureTests` package test renders every prepared case through
`KriaMediaEngine`'s production exporter, `compare` measures the exported MP4s).

1. Cloud parity (KRI-139). One set of real inputs (spoken voiceover from macOS
   `say`, a synthetic music bed, portrait footage with its own ambient audio),
   produced the SAME edit two ways: the real phone compilers
   (`compile_phone_voiceover_montage_plan`, `compile_phone_narrated_plan`) and the
   real cloud mixer (`template_orchestrate._mix_user_voiceover`). `compare`
   measures both with ffmpeg: integrated loudness + true peak (`ebur128`), the level
   at both ends, and the short-term (400 ms) loudness curve.

2. Creator song, played once (KRI-470 PR-G / KRI-481). The recipe comes from the REAL
   server compiler (`plan_unified_montage` -> `compile_phone_guided_plan` song lane,
   `window_start_s > 0`). Inputs are tone/colour tagged so the EXPORT, not the recipe,
   is what gets judged:

   - the song is a staircase: source second k is one pure tone F[k], so the song's
     first seconds (tone A) differ from the chosen window (tone B..). A second copy
     of the song from source time 0 (the KRI-481 failure) shows up as tones F[0..],
     at any phase; a doubled window shows up as extra energy at the expected tone;
   - each camera clip is a unique solid colour carrying its own tone (the camera
     audio must be silent, the recipe mutes it);
   - the opening text is proven by frame difference against a no-text twin export.

   `compare` writes `report.json` (every measurement, per case) and `montage.png`
   (frames at each cut, the text frame vs its twin) and exits non-zero on a failed
   check. The `user_song_doubled_bed` case is a NEGATIVE CONTROL: the same recipe with
   the music bed switched back on (`audio.music_volume = 1.0`, i.e. #1441 reverted),
   scratch only; `compare` requires its check to FAIL, so a harness that cannot
   see the double play fails loudly.

Repeatable commands (macOS, ffmpeg on PATH; the parity cases also need macOS `say`):

    src/apps/api/.venv/bin/python scripts/ios/phone-audio-parity.py prepare OUT
    (cd src/apps/ios/Packages/KriaMediaEngine && \
      KRIA_AUDIO_PARITY_DIR=OUT swift test --filter AudioParityFixtureTests)
    src/apps/api/.venv/bin/python scripts/ios/phone-audio-parity.py compare OUT

`prepare OUT --cases user_song` builds only the song proof (no `say` needed);
`--cases parity` only the cloud-parity cases. `compare` judges whatever is in OUT.

On a simulator, run the package scheme instead of `swift test`:
    TEST_RUNNER_KRIA_AUDIO_PARITY_DIR=OUT xcodebuild test -scheme KriaMediaEngine \
      -destination "platform=iOS Simulator,name=<iPhone>" \
      -only-testing:KriaMediaEngineTests/AudioParityFixtureTests
(the simulator's file system is the host's, so OUT is readable as is).

Needs ffmpeg (and macOS `say` for the parity cases) on PATH.
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
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def _fingerprint(path: Path) -> RenderFingerprint:
    return RenderFingerprint(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_count=path.stat().st_size,
    )


def _inputs(out: Path) -> dict[str, Path]:
    aiff = out / "voice.aiff"
    subprocess.run(
        ["say", "-v", "Samantha", "-r", "175", "-o", str(aiff), SCRIPT], check=True
    )
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


def _montage_decision(
    duration: float, *, mix: float, music: bool
) -> GenerativeVariantDecision:
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
            files[
                "footage"
                if kind == "original"
                else "music"
                if kind == "library"
                else "voice"
            ]
        )
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))


def _cloud(
    out: Path, case: str, files: dict[str, Path], duration: float, **kwargs
) -> None:
    from app.tasks import template_orchestrate

    directory = out / "cases" / case
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "assembled.mp4"
        # The cloud's assembled montage: the footage cut to the voiceover window.
        _ffmpeg(
            "-i",
            str(files["footage"]),
            "-t",
            f"{duration:.3f}",
            "-c",
            "copy",
            str(video),
        )
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


def prepare_parity(out: Path) -> None:
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
        [
            NarratedPhoneStep(
                step_id="s0", media_id="footage", start_s=0.0, end_s=duration
            )
        ],
        (binding,),
        narration,
        voiceover_duration_s=duration,
        mix=1 - NARRATED_BED_LEVEL,
        target_lufs=lufs,
        duck_footage_bed=True,
    )
    _write_case(out, "narrated_ducked", recipe, files)
    _cloud(
        out,
        "narrated_ducked",
        files,
        duration,
        footage_bed=True,
        bed_level=NARRATED_BED_LEVEL,
    )
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


def _compare_parity(directory: Path) -> tuple[dict | None, bool]:
    cloud, phone = directory / "cloud.mp4", directory / "phone.mp4"
    if not phone.exists():
        print(
            f"{directory.name}: phone.mp4 missing -- run AudioParityFixtureTests first"
        )
        return None, True
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
        "kind": "cloud_parity",
        "cloud_lufs": ci,
        "phone_lufs": pi,
        "delta_lu": round(pi - ci, 2),
        "cloud_tp": cp,
        "phone_tp": pp,
        "phone_first_20ms_db": round(_db(p, 0, 0.02), 1),
        "phone_last_20ms_db": round(_db(p, length - 0.02, length), 1),
        "cloud_last_20ms_db": round(_db(c, length - 0.02, length), 1),
        "short_term_median_abs_db": round(ranked[len(ranked) // 2], 2)
        if ranked
        else None,
        "short_term_p90_abs_db": round(ranked[int(len(ranked) * 0.9)], 2)
        if ranked
        else None,
    }
    return row, abs(row["delta_lu"]) > 1.0 or pp > -1.0


def compare(out: Path) -> int:
    failures = 0
    rows = []
    for directory in sorted((out / "cases").iterdir()):
        proof_file = directory / "proof.json"
        if proof_file.exists():
            proof = json.loads(proof_file.read_text())
            if proof["kind"] == "text_twin":
                continue  # only a reference frame source for its parent case
            row, failed = _compare_user_song(out, directory, proof)
        else:
            row, failed = _compare_parity(directory)
        if row is not None:
            rows.append(row)
        failures += int(failed)
    print(json.dumps(rows, indent=2))
    (out / "report.json").write_text(json.dumps(rows, indent=2))
    sheet = out / "cases" / "user_song" / "montage.png"
    if sheet.exists():
        shutil.copyfile(sheet, out / "montage.png")
    return 1 if failures else 0


# ------------------------------------------------------------- creator song, once
#
# The creator's song must play exactly once, from the window the plan chose. This
# is judged on the EXPORT (the file the creator gets), never the recipe: KRI-481
# shipped a recipe that read fine and exported the song twice.

SONG_ITEM = "plan-item-1"
SONG_GENERATION = 7
SONG_SECONDS = 40
SONG_TONE_AMPLITUDE = 0.25
CAMERA_TONE_AMPLITUDE = 0.25
OPENING_TEXT = "Tone B window"
OPENING_TEXT_S = 2.0
CLIP_COUNT = 10
# One pure tone per source second: F[k] = 700 + 90 k Hz (700 .. 4210 Hz).
CAMERA_TONES_HZ = [200 + 25 * i for i in range(CLIP_COUNT)]  # 200 .. 425 Hz
PALETTE = {
    "red": (220, 40, 40),
    "green": (40, 180, 60),
    "blue": (50, 80, 220),
    "yellow": (230, 210, 40),
    "magenta": (200, 50, 200),
    "cyan": (40, 200, 210),
}
# Pass thresholds. Leak: the loudest unexpected tone must sit this far below the
# tone it should be (a second copy of the song would be 0 dB or louder at F[0..]).
LEAK_MAX_DB = -30.0
LEVEL_TOLERANCE_DB = 2.0
TEXT_DIFF_MIN = 0.3  # mean luma levels the text adds inside its window
TEXT_DIFF_RATIO = 4.0  # ...and at least this many times the diff after the window


def _song_freq(second: int) -> float:
    return 700.0 + 90.0 * second


def _write_staircase_song(path: Path) -> None:
    import numpy as np  # noqa: PLC0415

    rate = 48000
    samples = np.zeros(SONG_SECONDS * rate, dtype=np.float64)
    ramp = int(0.005 * rate)
    for second in range(SONG_SECONDS):
        t = np.arange(rate) / rate
        tone = SONG_TONE_AMPLITUDE * np.sin(2 * np.pi * _song_freq(second) * t)
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
        samples[second * rate : (second + 1) * rate] = tone
    wav = path.with_suffix(".wav")
    import wave  # noqa: PLC0415

    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    # Stereo like real songs, so the export and this source downmix the same way.
    _ffmpeg("-i", str(wav), "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(path))


def _write_camera_clip(
    path: Path, rgb: tuple[int, int, int], tone_hz: float, length_s: float
) -> None:
    colour = "0x{:02x}{:02x}{:02x}".format(*rgb)
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c={colour}:s=1080x1920:r=30:d={length_s}",
        "-f", "lavfi", "-i", f"sine=frequency={tone_hz}:sample_rate=48000:d={length_s}",
        "-filter:a", f"volume={CAMERA_TONE_AMPLITUDE * 1.41421356:.4f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-shortest", str(path),
    )  # fmt: skip


def _song_recipe(files: dict[str, Path], clip_names: list[str], song: Path):
    """The recipe the REAL server compiler writes for a creator-song montage.

    The song's best window is chosen by `plan_unified_montage` from the beats and
    lyric lines handed in; they start late so the window opens well after source
    time 0 (a copy of the song from 0 s is then plainly a different tone).
    """
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryExecutionPlan,
        compile_execution_plan,
    )
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan  # noqa: PLC0415
    from app.pipeline.phone_recipe_shared import PhoneSongBed  # noqa: PLC0415
    from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage  # noqa: PLC0415
    from app.schemas.user_song import SongLine  # noqa: PLC0415

    beats = [round(10.0 + 0.5 * i, 3) for i in range(int((SONG_SECONDS - 12) / 0.5))]
    lines = [
        SongLine(start_s=11.0 + 4.0 * i, end_s=14.0 + 4.0 * i, text="la la")
        for i in range(6)
    ]
    clips = [
        UnifiedClip(
            media_id=name,
            proxy_path=f"users/u/analysis-proxy-{name}.mp4",
            generation="1",
            duration_s=_duration(files[name]),
            width=1080,
            height=1920,
        )
        for name in clip_names
    ]
    plan = plan_unified_montage(
        clips,
        strategy={
            "opening_title": OPENING_TEXT,
            "opening_title_duration_s": OPENING_TEXT_S,
        },
        song_beats=beats,
        song_lines=lines,
        song_duration_s=float(SONG_SECONDS),
        song_plan_item_id=SONG_ITEM,
        song_generation=SONG_GENERATION,
    )
    bindings = tuple(
        PhoneSourceBinding(
            media_id=ref.media_id,
            proxy_path=ref.gcs_path,
            generation=ref.generation,
            original=OriginalMediaDescriptor(
                sha256=_fingerprint(files[ref.media_id]).sha256,
                byte_count=_fingerprint(files[ref.media_id]).byte_count,
                duration_s=_duration(files[ref.media_id]),
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for ref in plan.snapshot.media
    )
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    recipe = compile_phone_guided_plan(
        execution,
        bindings,
        (),
        song=PhoneSongBed(
            plan_item_id=SONG_ITEM,
            generation=str(SONG_GENERATION),
            fingerprint=_fingerprint(song),
            duration_s=_duration(song),
        ),
    )
    return plan, recipe


def _write_song_case(
    out: Path, case: str, recipe, files: dict[str, Path], proof: dict
) -> None:
    directory = out / "cases" / case
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    assets = {}
    for asset in recipe.asset_manifest.assets:
        assets[asset.id] = str(
            files[asset.media_id if asset.kind == "original" else "song"]
        )
    for asset in recipe.assets:
        if asset.id.startswith("font-"):  # typography the device ships with
            font = asset.id.removeprefix("font-")
            assets[asset.id] = str(REPO / "src/apps/api/assets/fonts" / font)
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))
    (directory / "proof.json").write_text(json.dumps(proof, indent=2))


def prepare_user_song(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sources = out / "user_song_inputs"
    sources.mkdir(exist_ok=True)
    colours = list(PALETTE)
    files: dict[str, Path] = {}
    camera: dict[str, dict] = {}
    for index in range(CLIP_COUNT):
        name = f"cam{index + 1}"
        colour = colours[index % len(colours)]
        files[name] = sources / f"{name}.mp4"
        _write_camera_clip(files[name], PALETTE[colour], CAMERA_TONES_HZ[index], 8.0)
        camera[name] = {"colour": colour, "tone_hz": CAMERA_TONES_HZ[index]}
    files["song"] = sources / "song.m4a"
    _write_staircase_song(files["song"])

    plan, recipe = _song_recipe(files, list(camera), files["song"])
    song_plan = plan.user_song
    bed = next(t for t in recipe.tracks if t.id == "song").clips[0]
    if song_plan.window_start_s < 3.0:
        raise SystemExit(
            f"window opens at {song_plan.window_start_s}s; need >= 3s to tell tones"
        )
    video = next(t for t in recipe.tracks if t.kind == "video")
    proof = {
        "kind": "user_song",
        "duration_s": recipe.duration,
        "song": {
            "window_start_s": bed.source_start,
            "window_duration_s": bed.source_duration,
            "volume": bed.volume,
            "fade_in_s": bed.audio_fade_in,
            "fade_out_s": bed.audio_fade_out,
            "seconds": SONG_SECONDS,
            "freqs_hz": [_song_freq(k) for k in range(SONG_SECONDS)],
            "amplitude": SONG_TONE_AMPLITUDE,
        },
        "camera_tones_hz": [row["tone_hz"] for row in camera.values()],
        "clips": [
            {
                "id": clip.id,
                "media_id": next(
                    a.media_id
                    for a in recipe.asset_manifest.assets
                    if a.id == clip.source_asset_id
                ),
                "start_s": clip.timeline_start,
                "duration_s": clip.source_duration / clip.rate,
                "colour": camera[
                    next(
                        a.media_id
                        for a in recipe.asset_manifest.assets
                        if a.id == clip.source_asset_id
                    )
                ]["colour"],
            }
            for clip in sorted(video.clips, key=lambda c: c.timeline_start)
        ],
        "text": {
            "text": OPENING_TEXT,
            "start_s": min(layer.start for layer in recipe.text_layers),
            "end_s": max(layer.end for layer in recipe.text_layers),
        }
        if recipe.text_layers
        else None,
        "palette": PALETTE,
        "twin": "user_song_notext",
    }
    _write_song_case(out, "user_song", recipe, files, proof)
    # The same edit without its text layer: the reference frame for the text window.
    twin = recipe.model_copy(
        update={
            "text_layers": [],
            "required_capabilities": recipe.required_capabilities
            - {"positionedText", "animatedText"},
        }
    )
    _write_song_case(
        out, "user_song_notext", twin, files, {"kind": "text_twin", "of": "user_song"}
    )
    # NEGATIVE CONTROL: #1441 reverted. The legacy bed plays the song again from 0 s.
    doubled = recipe.model_copy(
        update={"audio": recipe.audio.model_copy(update={"music_volume": 1.0})}
    )
    _write_song_case(
        out,
        "user_song_doubled_bed",
        doubled,
        files,
        {**proof, "expect_failure": "song_plays_twice", "twin": "user_song_notext"},
    )
    print(
        f"prepared user_song cases in {out} (window {bed.source_start:.2f}s +"
        f" {bed.source_duration:.2f}s, {len(video.clips)} cuts)"
    )


def _goertzel(
    samples, rate: int, centre_s: float, half_s: float, freq_hz: float
) -> float:
    """Amplitude (linear, full-scale = 1.0) of one pure tone in a Hann-windowed slice."""
    import numpy as np  # noqa: PLC0415

    start, end = int((centre_s - half_s) * rate), int((centre_s + half_s) * rate)
    window = np.asarray(samples[max(start, 0) : end], dtype=np.float64)
    if len(window) < 16:
        return 0.0
    hann = np.hanning(len(window))
    phase = np.exp(-2j * np.pi * freq_hz * np.arange(len(window)) / rate)
    return float(abs(np.sum(window * hann * phase)) * 2 / np.sum(hann))


def _amp_db(amplitude: float) -> float:
    return 20 * math.log10(amplitude) if amplitude > 1e-9 else -180.0


def _frame_rgb(video: Path, at_s: float):
    import numpy as np  # noqa: PLC0415

    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{at_s:.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=90:160:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.uint8).reshape(160, 90, 3).astype(np.float64)


def _nearest_colour(rgb, palette: dict[str, list[int]]) -> str:
    return min(
        palette, key=lambda name: sum((a - b) ** 2 for a, b in zip(rgb, palette[name]))
    )


def _compare_user_song(out: Path, directory: Path, proof: dict) -> tuple[dict, bool]:
    import numpy as np  # noqa: PLC0415

    name = directory.name
    phone = directory / "phone.mp4"
    row: dict = {"case": name, "kind": "user_song", "checks": {}}
    if not phone.exists():
        print(f"{name}: phone.mp4 missing -- run AudioParityFixtureTests first")
        return row, True
    expect_failure = proof.get("expect_failure")
    checks = row["checks"]

    # 1. Length: the export is the edit (no brand tail in this fixture).
    exported = _duration(phone)
    checks["duration"] = {
        "expected_s": round(proof["duration_s"], 3),
        "actual_s": round(exported, 3),
        "ok": abs(exported - proof["duration_s"]) <= 0.2,
    }

    # 2. Picture order: each cut's centre frame is that cut's own colour.
    palette = proof["palette"]
    order_rows = []
    for cut in proof["clips"]:
        at = cut["start_s"] + cut["duration_s"] / 2
        frame = _frame_rgb(phone, at)[70:90, 35:55].reshape(-1, 3).mean(axis=0)
        seen = _nearest_colour(frame, palette)
        order_rows.append(
            {"cut": cut["id"], "at_s": round(at, 2), "expected": cut["colour"], "seen": seen,
             "ok": seen == cut["colour"]}
        )  # fmt: skip
    checks["picture_order"] = {
        "cuts": order_rows,
        "ok": all(r["ok"] for r in order_rows),
    }

    # 3. Audio: the song once, from its window; camera audio silent.
    song = proof["song"]
    rate = 48000
    pcm = np.asarray(_pcm(phone), dtype=np.float64)
    source = _pcm(directory.parent.parent / "user_song_inputs" / "song.m4a")
    freqs = song["freqs_hz"]
    window_start = song["window_start_s"]
    fade = max(song["fade_in_s"] or 0.0, song["fade_out_s"] or 0.0, 0.5)
    tone_rows = []
    worst_leak = -180.0
    for second in range(SONG_SECONDS):
        centre = second + 0.5 - window_start  # where source second `second` lands
        if centre < fade + 0.25 or centre > song["window_duration_s"] - fade - 0.25:
            continue
        reference = _goertzel(source, rate, second + 0.5, 0.2, freqs[second])
        heard = _goertzel(pcm, rate, centre, 0.2, freqs[second])
        # Anything else audible at this instant: every other song tone + every camera tone.
        others = [f for k, f in enumerate(freqs) if k != second] + proof[
            "camera_tones_hz"
        ]
        leak = max(_goertzel(pcm, rate, centre, 0.2, f) for f in others)
        leak_db = _amp_db(leak) - _amp_db(heard)
        worst_leak = max(worst_leak, leak_db)
        tone_rows.append(
            {"source_second": second, "export_s": round(centre, 2),
             "expected_hz": freqs[second],
             "level_vs_source_db": round(_amp_db(heard) - _amp_db(reference), 2),
             "loudest_other_vs_expected_db": round(leak_db, 1)}
        )  # fmt: skip
    levels_ok = bool(tone_rows) and all(
        abs(r["level_vs_source_db"]) <= LEVEL_TOLERANCE_DB for r in tone_rows
    )
    # Tone A: the song's opening tones (before the window) must be absent from the whole export.
    opening = [k for k in range(int(window_start))]
    tone_a = max(
        (_goertzel(pcm, rate, t + 0.25, 0.25, freqs[k]) for k in opening for t in np.arange(0, exported - 0.5, 0.5)),
        default=0.0,
    )  # fmt: skip
    expected_peak = SONG_TONE_AMPLITUDE * (song["volume"] or 1.0)
    tone_a_db = _amp_db(tone_a) - _amp_db(expected_peak)
    camera_peak = max(
        (_goertzel(pcm, rate, t + 0.25, 0.25, f) for f in proof["camera_tones_hz"] for t in np.arange(0, exported - 0.5, 0.5)),
        default=0.0,
    )  # fmt: skip
    camera_db = _amp_db(camera_peak) - _amp_db(expected_peak)
    checks["song_window"] = {
        "window_start_s": round(window_start, 3),
        "tones": tone_rows,
        "level_within_db": LEVEL_TOLERANCE_DB,
        "ok": levels_ok and worst_leak <= LEAK_MAX_DB,
    }
    checks["tone_a_absent"] = {
        "opening_tones_hz": [freqs[k] for k in opening],
        "loudest_vs_expected_song_level_db": round(tone_a_db, 1),
        "max_db": LEAK_MAX_DB,
        "ok": tone_a_db <= LEAK_MAX_DB,
    }
    checks["song_once"] = {
        "worst_other_tone_vs_expected_db": round(worst_leak, 1),
        "max_db": LEAK_MAX_DB,
        "ok": worst_leak <= LEAK_MAX_DB and levels_ok,
    }
    checks["camera_audio_silent"] = {
        "loudest_camera_tone_vs_expected_song_level_db": round(camera_db, 1),
        "max_db": LEAK_MAX_DB,
        "ok": camera_db <= LEAK_MAX_DB,
    }

    # 4. Opening text: the frame differs from the no-text twin inside the window, not after it.
    twin = directory.parent / proof["twin"] / "phone.mp4"
    text = proof.get("text")
    if text and twin.exists():
        inside = text["start_s"] + (text["end_s"] - text["start_s"]) / 2
        after = min(text["end_s"] + 1.0, proof["duration_s"] - 0.3)

        def diff(at: float) -> float:
            return float(np.abs(_frame_rgb(phone, at) - _frame_rgb(twin, at)).mean())

        inside_diff, after_diff = diff(inside), diff(after)
        checks["opening_text"] = {
            "window_s": [round(text["start_s"], 2), round(text["end_s"], 2)],
            "mean_diff_inside": round(inside_diff, 3),
            "mean_diff_after": round(after_diff, 3),
            "ok": inside_diff >= TEXT_DIFF_MIN
            and inside_diff >= TEXT_DIFF_RATIO * after_diff,
        }
    elif text:
        checks["opening_text"] = {
            "ok": False,
            "reason": f"{proof['twin']}/phone.mp4 missing",
        }

    # Montage: the frame at each cut centre, then the text frame beside its twin.
    montage = directory / "montage.png"
    try:
        from PIL import Image  # noqa: PLC0415

        tiles = [
            Image.fromarray(_frame_rgb(phone, r["at_s"]).astype("uint8"))
            for r in order_rows
        ]
        if text and twin.exists():
            tiles += [
                Image.fromarray(_frame_rgb(phone, inside).astype("uint8")),
                Image.fromarray(_frame_rgb(twin, inside).astype("uint8")),
            ]
        sheet = Image.new("RGB", (90 * len(tiles), 160))
        for position, tile in enumerate(tiles):
            sheet.paste(tile, (90 * position, 0))
        sheet.save(montage)
        row["montage"] = str(montage)
    except ImportError:
        row["montage"] = None

    failed_checks = sorted(k for k, v in checks.items() if not v["ok"])
    row["failed_checks"] = failed_checks
    if expect_failure:
        # Negative control: the harness must SEE the regression it exists to catch.
        caught = {"tone_a_absent", "song_once"} <= set(failed_checks)
        row["expect_failure"] = expect_failure
        row["negative_control_detected"] = caught
        print(
            f"{name}: negative control {'DETECTED' if caught else 'MISSED'} ({failed_checks})"
        )
        return row, not caught
    row["passed"] = not failed_checks
    print(f"{name}: {'PASS' if row['passed'] else 'FAIL'} {failed_checks}")
    return row, bool(failed_checks)


def prepare(out: Path, cases: str = "all") -> None:
    if cases in {"all", "parity"}:
        prepare_parity(out)
    if cases in {"all", "user_song"}:
        prepare_user_song(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    cases = "all"
    if "--cases" in args:
        at = args.index("--cases")
        cases = args[at + 1] if at + 1 < len(args) else ""
        del args[at : at + 2]
    if (
        len(args) != 2
        or args[0] not in {"prepare", "compare"}
        or cases
        not in {
            "all",
            "parity",
            "user_song",
        }
    ):
        raise SystemExit(__doc__)
    target = Path(args[1]).resolve()
    if args[0] == "prepare":
        prepare(target, cases)
    else:
        raise SystemExit(compare(target))
