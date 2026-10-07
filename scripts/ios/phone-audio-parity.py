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

`prepare OUT --cases user_song` builds only the song proof (no `say` needed); the other
values are `parity`, `voiceover_music_authored` and `speech_music`. `compare` judges whatever
is in OUT.

3. Music level and once (KRI-470 PR-G review). `voiceover_music_authored`: a voiceover + music
   edit compiled by `compile_phone_voiceover_montage_plan`, then restored through
   `compile_phone_authored_timeline` (the editor Save path). `speech_music`: the real spoken-
   excerpt compiler given a music bed. Every source is a pure tone at a known amplitude, so
   each window of the export must hold exactly the tones the COMPILED recipe says, at that
   level (music at the compiler's `min(1 - mix, 0.5)`, voice at full), music silent under
   speech, camera audio silent, no second copy of the music from source 0. NEGATIVE CONTROLS
   (compare requires each to fail its named check): `voiceover_music_authored_loud` (music
   at the voice-slider value, the wrong authored level), `voiceover_music_authored_doubled`
   (clip + `music_asset_id` bed, the old authored shape), `speech_music_doubled_bed`.

4. One voice behind footage, once (KRI-479). `voice_behind_footage`: the recipe comes from the
   REAL server composer (`compile_phone_voice_behind_footage_plan`, voice window from
   `select_voice_window`) and passes `verify_phone_recipe` WITH its composition commitments
   before it is exported. Inputs are tone/colour tagged: the voice clip is a staircase (source
   second k = one pure tone) under a colour of its own that must NEVER show, and every picture
   clip is a unique colour carrying its own (muted) tone, listed in a deliberately NON-sorted
   order. The EXPORT must hold: the voice's own tones for the whole window, in order, each
   once (a second copy from source 0 is a different tone), the picture clips' tones nowhere,
   the picture colours in the contract's order at every cut centre, the opening text only in
   its window (against a no-text twin), the length within one frame of the plan, and a
   non-silent loudness. NEGATIVE CONTROLS (compare requires each to fail its named check):
   `voice_behind_footage_unmuted` (picture clips at volume 1), `voice_behind_footage_voice_
   stops_early` (the voice clip cut to half), `voice_behind_footage_wrapped` (a picture clip
   shown twice, another dropped). Command: `prepare OUT --cases voice_behind_footage`.

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
    duration: float, *, mix: float, music: bool, music_start_s: float = MUSIC_START_S
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
        music_start_s=music_start_s if music else None,
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
            if proof["kind"] == "audio_levels":
                row, failed = _compare_audio_levels(directory, proof)
            elif proof["kind"] == "voice_behind_footage":
                row, failed = _compare_voice_behind_footage(out, directory, proof)
            else:
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


# ------------------------------------------------- music level + once (authored, speech)
#
# KRI-470 PR-G changed what two writers emit for a music bed: the authored editor restore
# (the music level lives on the bed's track clip, `music_asset_id=None`) and the spoken-
# excerpt montage (music clips only, no second bed). Both are judged on the EXPORT with
# tone-tagged inputs: every source is a pure tone at a known amplitude, so each window of
# the exported audio must contain exactly the tones the compiled recipe says, at the level
# it says, and nothing else (a second copy of the music from source 0 is a different tone).

VOICE_HZ = 523.0
SPEAKER_HZ = 440.0
FOOTAGE_HZ = 330.0
BROLL_HZ = [250.0, 270.0, 290.0]
LEVELS_MUSIC_START_S = 10.0
LEVELS_VOICE_AMPLITUDE = 0.4
LEVELS_CAMERA_AMPLITUDE = 0.25
LEVELS_SECONDS = 12.0
VOICEOVER_MIX = 0.7


def _write_tone_audio(path: Path, hz: float, amplitude: float, seconds: float) -> None:
    import numpy as np  # noqa: PLC0415
    import wave  # noqa: PLC0415

    rate = 48000
    t = np.arange(int(seconds * rate)) / rate
    samples = amplitude * np.sin(2 * np.pi * hz * t)
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    _ffmpeg("-i", str(wav), "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(path))


def _write_tone_clip(
    path: Path, rgb: tuple[int, int, int], hz: float, seconds: float
) -> None:
    audio = path.with_suffix(".m4a")
    _write_tone_audio(audio, hz, LEVELS_CAMERA_AMPLITUDE, seconds)
    colour = "0x{:02x}{:02x}{:02x}".format(*rgb)
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c={colour}:s=1080x1920:r=30:d={seconds}",
        "-i", str(audio), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-shortest", str(path),
    )  # fmt: skip


def _binding_for(media_id: str, clip: Path) -> PhoneSourceBinding:
    fp = _fingerprint(clip)
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=fp.sha256,
            byte_count=fp.byte_count,
            duration_s=_duration(clip),
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def _tone_amp(path: Path, hz: float, at_s: float = 2.5) -> float:
    """The amplitude of one pure tone in a source file, measured like the export is."""
    return _goertzel(_pcm(path), 48000, at_s, 0.2, hz)


def _write_levels_case(
    out: Path, case: str, recipe, files: dict[str, Path], proof: dict
) -> None:
    directory = out / "cases" / case
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    assets = {}
    for asset in recipe.asset_manifest.assets:
        if asset.kind == "original":
            assets[asset.id] = str(files[asset.media_id])
        elif asset.kind == "library":
            assets[asset.id] = str(files["music"])
        else:
            assets[asset.id] = str(files["voice"])
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))
    (directory / "proof.json").write_text(json.dumps(proof, indent=2))


def _music_windows(
    recipe, music_amp_by_second: dict[int, float], *, skip_s: float = 0.6
) -> list[dict]:
    """One window per source second of every music clip, at that second's centre."""
    windows = []
    for track in recipe.tracks:
        if track.id != "music":
            continue
        for clip in track.clips:
            end = clip.timeline_start + clip.source_duration
            second = math.floor(clip.source_start + skip_s)
            while True:
                t = clip.timeline_start + (second + 0.5 - clip.source_start)
                if t > end - skip_s:
                    break
                if t >= clip.timeline_start + skip_s:
                    windows.append(
                        {"t": round(t, 3), "expect": [
                            {"hz": _song_freq(second), "group": "music",
                             "amp": music_amp_by_second[second] * clip.volume}
                        ]}
                    )  # fmt: skip
                second += 1
    return windows


def _levels_groups() -> dict[str, list[float]]:
    return {
        "music": [_song_freq(k) for k in range(SONG_SECONDS)],
        "voice": [VOICE_HZ, SPEAKER_HZ],
        "camera": [FOOTAGE_HZ, *BROLL_HZ],
    }


def prepare_voiceover_music_authored(out: Path) -> None:
    """A voiceover + music edit restored through the authored editor path."""
    from types import SimpleNamespace  # noqa: PLC0415

    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD  # noqa: PLC0415

    out.mkdir(parents=True, exist_ok=True)
    sources = out / "voiceover_music_inputs"
    sources.mkdir(exist_ok=True)
    files = {
        "voice": sources / "voice.m4a",
        "music": sources / "music.m4a",
        "footage": sources / "footage.mp4",
    }
    _write_tone_audio(files["voice"], VOICE_HZ, LEVELS_VOICE_AMPLITUDE, LEVELS_SECONDS)
    _write_staircase_song(files["music"])
    _write_tone_clip(files["footage"], (74, 107, 138), FOOTAGE_HZ, LEVELS_SECONDS + 2)
    binding = _binding_for("footage", files["footage"])
    music = PhoneMusicBed(
        catalog_id="track",
        generation="1",
        fingerprint=_fingerprint(files["music"]),
        duration_s=_duration(files["music"]),
        start_s=LEVELS_MUSIC_START_S,
    )
    decision = _montage_decision(
        LEVELS_SECONDS,
        mix=VOICEOVER_MIX,
        music=True,
        music_start_s=LEVELS_MUSIC_START_S,
    )
    previous = compile_phone_voiceover_montage_plan(
        decision, (binding,), music=music, narration=_narration(files["voice"])
    )
    compiled_gain = next(t for t in previous.tracks if t.id == "music").clips[0].volume
    job = SimpleNamespace(
        assembly_plan={PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")]},
        all_candidates={"clip_paths": [binding.proxy_path]},
    )
    variant = {
        "variant_id": "voiceover_music",
        "resolved_archetype": "voiceover",
        "render_destination": "device",
        "editor_timeline_mode": "authored",
        "caption_cues": [],
        "text_elements": [],
        "text_elements_user_edited": True,
        "music_track_id": "track",
        "mix": VOICEOVER_MIX,
        "user_timeline": {
            "slots": [
                {
                    "slot_id": "s0",
                    "clip_index": 0,
                    "in_s": 0,
                    "duration_s": LEVELS_SECONDS,
                }
            ]
        },
    }
    restored = compile_phone_authored_timeline(job, variant, previous)

    staircase = {
        k: _tone_amp(files["music"], _song_freq(k), k + 0.5)
        for k in range(SONG_SECONDS)
    }
    voice_amp = _tone_amp(files["voice"], VOICE_HZ)
    # The EXPECTED level is what the COMPILER wrote (previous), not what the restore wrote.
    reference = previous.model_copy(deep=True)
    windows = _music_windows(reference, staircase)
    for window in windows:
        window["expect"].append({"hz": VOICE_HZ, "group": "voice", "amp": voice_amp})
    proof = {
        "kind": "audio_levels",
        "duration_s": restored.duration,
        "compiled_music_gain": compiled_gain,
        "windows": windows,
        "groups": _levels_groups(),
    }
    music_asset = next(
        a.id for a in restored.asset_manifest.assets if a.kind == "library"
    )
    loud = restored.model_copy(deep=True)
    for track in loud.tracks:
        if track.id == "music":
            track.clips[0] = track.clips[0].model_copy(update={"volume": VOICEOVER_MIX})
    doubled = restored.model_copy(
        update={
            "audio": restored.audio.model_copy(
                update={"music_asset_id": music_asset, "music_volume": VOICEOVER_MIX}
            )
        }
    )
    _write_levels_case(out, "voiceover_music_authored", restored, files, proof)
    # NEGATIVE CONTROLS: the old authored shapes. compare must catch both.
    _write_levels_case(
        out, "voiceover_music_authored_loud", loud, files,
        {**proof, "expect_failure": ["music_level"]},
    )  # fmt: skip
    _write_levels_case(
        out, "voiceover_music_authored_doubled", doubled, files,
        {**proof, "expect_failure": ["music_once"]},
    )  # fmt: skip
    print(
        f"prepared voiceover_music_authored in {out} (music gain {compiled_gain:.2f})"
    )


def prepare_speech_music(out: Path) -> None:
    """The real spoken-excerpt compiler given a music bed: music under montage runs only."""
    from app.pipeline.phone_speech_montage_plan import (  # noqa: PLC0415
        PhoneSpeechSection,
        compile_phone_speech_montage_plan,
    )

    out.mkdir(parents=True, exist_ok=True)
    sources = out / "speech_music_inputs"
    sources.mkdir(exist_ok=True)
    files = {"music": sources / "music.m4a", "voice": sources / "voice.m4a"}
    _write_staircase_song(files["music"])
    _write_tone_audio(
        files["voice"], VOICE_HZ, LEVELS_VOICE_AMPLITUDE, 2
    )  # unused placeholder
    files["speaker"] = sources / "speaker.mp4"
    _write_tone_clip(files["speaker"], (120, 120, 120), SPEAKER_HZ, 12)
    broll = []
    for index, hz in enumerate(BROLL_HZ):
        name = f"broll{index + 1}"
        files[name] = sources / f"{name}.mp4"
        _write_tone_clip(files[name], list(PALETTE.values())[index], hz, 9)
        broll.append(_binding_for(name, files[name]))
    speaker = _binding_for("speaker", files["speaker"])
    music = PhoneMusicBed(
        catalog_id="track",
        generation="1",
        fingerprint=_fingerprint(files["music"]),
        duration_s=_duration(files["music"]),
        start_s=LEVELS_MUSIC_START_S,
        volume=0.4,
    )
    recipe, receipt = compile_phone_speech_montage_plan(
        (
            PhoneSpeechSection(kind="montage", duration_s=4.0),
            PhoneSpeechSection(
                kind="speech", speaker=speaker, source_start_s=1.0, source_end_s=6.0,
                visual="cutaways",
            ),
            PhoneSpeechSection(kind="montage", duration_s=4.0),
        ),
        (speaker, *broll),
        music=music,
    )  # fmt: skip
    assert receipt.music, "the compiler produced no music for this shape"
    staircase = {
        k: _tone_amp(files["music"], _song_freq(k), k + 0.5)
        for k in range(SONG_SECONDS)
    }
    speaker_amp = _tone_amp(files["speaker"], SPEAKER_HZ)
    windows = _music_windows(recipe, staircase)
    for track in recipe.tracks:
        if track.id != "speech-audio":
            continue
        for clip in track.clips:
            mid = clip.timeline_start + clip.source_duration / 2
            windows.append(
                {"t": round(mid, 3), "expect": [
                    {"hz": SPEAKER_HZ, "group": "voice", "amp": speaker_amp * clip.volume}
                ]}
            )  # fmt: skip
    proof = {
        "kind": "audio_levels",
        "duration_s": recipe.duration,
        "windows": windows,
        "groups": _levels_groups(),
    }
    music_asset = next(
        a.id for a in recipe.asset_manifest.assets if a.kind == "library"
    )
    doubled = recipe.model_copy(
        update={
            "audio": recipe.audio.model_copy(
                update={"music_asset_id": music_asset, "music_volume": 0.4}
            )
        }
    )
    _write_levels_case(out, "speech_music", recipe, files, proof)
    _write_levels_case(
        out, "speech_music_doubled_bed", doubled, files,
        {**proof, "expect_failure": ["music_once"]},
    )  # fmt: skip
    print(f"prepared speech_music in {out} ({len(windows)} windows)")


def _compare_audio_levels(directory: Path, proof: dict) -> tuple[dict, bool]:
    import numpy as np  # noqa: PLC0415

    name = directory.name
    phone = directory / "phone.mp4"
    row: dict = {"case": name, "kind": "audio_levels", "checks": {}}
    if not phone.exists():
        print(f"{name}: phone.mp4 missing -- run AudioParityFixtureTests first")
        return row, True
    pcm = np.asarray(_pcm(phone), dtype=np.float64)
    groups = proof["groups"]
    level_rows = []
    worst = {"music": -180.0, "camera": -180.0, "voice": -180.0}
    level_error = {"music": 0.0, "voice": 0.0}
    for window in proof["windows"]:
        expected = {e["hz"]: e for e in window["expect"]}
        floor_db = min(_amp_db(e["amp"]) for e in expected.values())
        heard = {}
        for e in window["expect"]:
            measured = _goertzel(pcm, 48000, window["t"], 0.2, e["hz"])
            diff = _amp_db(measured) - _amp_db(e["amp"])
            heard[str(e["hz"])] = round(diff, 2)
            level_error[e["group"]] = max(level_error[e["group"]], abs(diff))
        for group, hzs in groups.items():
            for hz in hzs:
                if hz in expected:
                    continue
                leak = _amp_db(_goertzel(pcm, 48000, window["t"], 0.2, hz)) - floor_db
                worst[group] = max(worst[group], leak)
        level_rows.append({"t": window["t"], "level_vs_expected_db": heard})
    checks = row["checks"]
    checks["duration"] = {
        "expected_s": round(proof["duration_s"], 3),
        "actual_s": round(_duration(phone), 3),
        "ok": abs(_duration(phone) - proof["duration_s"]) <= 0.2,
    }
    checks["music_level"] = {
        "windows": len(level_rows),
        "max_abs_error_db": round(level_error["music"], 2),
        "tolerance_db": LEVEL_TOLERANCE_DB,
        "ok": bool(level_rows) and level_error["music"] <= LEVEL_TOLERANCE_DB,
    }
    checks["voice_level"] = {
        "max_abs_error_db": round(level_error["voice"], 2),
        "tolerance_db": LEVEL_TOLERANCE_DB,
        "ok": level_error["voice"] <= LEVEL_TOLERANCE_DB,
    }
    checks["music_once"] = {
        "loudest_unexpected_music_tone_db": round(worst["music"], 1),
        "max_db": LEAK_MAX_DB,
        "ok": worst["music"] <= LEAK_MAX_DB,
    }
    checks["camera_audio_silent"] = {
        "loudest_camera_tone_db": round(worst["camera"], 1),
        "max_db": LEAK_MAX_DB,
        "ok": worst["camera"] <= LEAK_MAX_DB,
    }
    row["windows"] = level_rows
    failed_checks = sorted(k for k, v in checks.items() if not v["ok"])
    row["failed_checks"] = failed_checks
    wanted = proof.get("expect_failure")
    if wanted:
        caught = set(wanted) <= set(failed_checks)
        row["expect_failure"] = wanted
        row["negative_control_detected"] = caught
        print(
            f"{name}: negative control {'DETECTED' if caught else 'MISSED'} ({failed_checks})"
        )
        return row, not caught
    row["passed"] = not failed_checks
    print(f"{name}: {'PASS' if row['passed'] else 'FAIL'} {failed_checks}")
    return row, bool(failed_checks)



# ----------------------------------------------------- one voice behind footage (KRI-479)

VOICE_ITEM_ID = "voice"
VOICE_PICTURE_IDS = ["cam4", "cam1", "cam6", "cam2", "cam5", "cam3"]  # NOT sorted
VOICE_DURATION_S = 20.0
VOICE_TITLE = "Summer in Lisbon"
VOICE_TITLE_HOLD_S = 3.0
VOICE_PRESENT_MIN_DB = -3.0  # the heard tone vs the source tone, per window
VOICE_FRAME_S = 1 / 30


def _synthetic_voice_words(seconds: float = SONG_SECONDS - 1.0) -> list[dict]:
    """Word timings for the staircase voice: a word every 0.5 s, a sentence every 5 words."""
    out, t, i = [], 0.4, 0
    while t + 0.3 < seconds:
        mark = "." if (i + 1) % 5 == 0 else ""
        out.append({"text": f"w{i}{mark}", "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += 0.5
        i += 1
    return out


def _write_voice_clip(path: Path, rgb: tuple[int, int, int]) -> None:
    """A colour-only picture over the staircase voice (the picture must never show)."""
    audio = path.with_suffix(".staircase.m4a")
    _write_staircase_song(audio)
    colour = "0x{:02x}{:02x}{:02x}".format(*rgb)
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c={colour}:s=1080x1920:r=30:d={SONG_SECONDS}",
        "-i", str(audio), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-shortest", str(path),
    )  # fmt: skip


def _voice_binding(media_id: str, clip: Path) -> PhoneSourceBinding:
    fingerprint = _fingerprint(clip)
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"users/u/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=fingerprint.sha256,
            byte_count=fingerprint.byte_count,
            duration_s=_duration(clip),
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def _write_voice_case(out: Path, case: str, recipe, files: dict[str, Path], proof: dict) -> None:
    directory = out / "cases" / case
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    assets = {}
    for asset in recipe.asset_manifest.assets:
        if asset.kind == "original":
            assets[asset.id] = str(files[asset.media_id])
    for asset in recipe.assets:
        if asset.id.startswith("font-"):  # typography the device ships with
            assets[asset.id] = str(
                REPO / "src/apps/api/assets/fonts" / asset.id.removeprefix("font-")
            )
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))
    (directory / "proof.json").write_text(json.dumps(proof, indent=2))


def _voice_recipe(files: dict[str, Path]):
    """The REAL composer's recipe, verified with its commitments, plus the contract facts."""
    from app.pipeline.phone_speech_montage_plan import (  # noqa: PLC0415
        compile_phone_voice_behind_footage_plan,
        select_voice_window,
    )
    from app.services.creator_render_contract import (  # noqa: PLC0415
        CompositionCommitments,
        CreatorRenderContract,
        TextRequirement,
        verify_phone_recipe,
    )

    voice = _voice_binding(VOICE_ITEM_ID, files[VOICE_ITEM_ID])
    picture = tuple(_voice_binding(m, files[m]) for m in VOICE_PICTURE_IDS)
    window = select_voice_window(
        _synthetic_voice_words(),
        source_duration_s=float(voice.original.duration_s),
        max_length_s=VOICE_DURATION_S - 0.05,
    )
    recipe, receipt = compile_phone_voice_behind_footage_plan(
        voice,
        window,
        picture,
        duration_s=VOICE_DURATION_S,
        opening_title=VOICE_TITLE,
        opening_title_hold_s=VOICE_TITLE_HOLD_S,
    )
    contract = CreatorRenderContract(generation_id="proof").rebind(
        duration_s=VOICE_DURATION_S,
        audio_source_ids=(VOICE_ITEM_ID,),
        original_audio="require",
        order_required=True,
        order_ids=tuple(VOICE_PICTURE_IDS),
        order_basis="capture_time",
        exact_texts=(
            TextRequirement(role="opening", text=VOICE_TITLE, duration_s=VOICE_TITLE_HOLD_S),
        ),
    )
    verify_phone_recipe(
        contract,
        recipe,
        source_audio={m: True for m in [VOICE_ITEM_ID, *VOICE_PICTURE_IDS]},
        composition=CompositionCommitments(voice_picture="hidden"),
    )
    return recipe, receipt, window


def prepare_voice_behind_footage(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sources = out / "voice_behind_footage_inputs"
    sources.mkdir(exist_ok=True)
    colours = list(PALETTE)
    files: dict[str, Path] = {}
    camera: dict[str, dict] = {}
    for index, media_id in enumerate(sorted(VOICE_PICTURE_IDS)):
        colour = colours[index % len(colours)]
        files[media_id] = sources / f"{media_id}.mp4"
        _write_camera_clip(files[media_id], PALETTE[colour], CAMERA_TONES_HZ[index], 8.0)
        camera[media_id] = {"colour": colour, "tone_hz": CAMERA_TONES_HZ[index]}
    files[VOICE_ITEM_ID] = sources / "voice.mp4"
    # A colour no picture clip uses: if the voice clip's own picture shows, the check sees it.
    _write_voice_clip(files[VOICE_ITEM_ID], (90, 90, 90))
    recipe, receipt, window = _voice_recipe(files)
    footage = next(t for t in recipe.tracks if t.id == "voice-footage")
    voice = next(t for t in recipe.tracks if t.id == "voice").clips[0]
    proof = {
        "kind": "voice_behind_footage",
        "duration_s": recipe.duration,
        "voice": {
            "source_start_s": voice.source_start,
            "span_s": voice.source_duration,
            "fade_in_s": voice.audio_fade_in,
            "fade_out_s": voice.audio_fade_out,
            "volume": voice.volume,
            "seconds": SONG_SECONDS,
            "freqs_hz": [_song_freq(k) for k in range(SONG_SECONDS)],
            "amplitude": SONG_TONE_AMPLITUDE,
            "file": "voice.mp4",
        },
        "camera_tones_hz": [row["tone_hz"] for row in camera.values()],
        "clips": [
            {
                "id": clip.id,
                "media_id": clip.source_asset_id,
                "start_s": clip.timeline_start,
                "duration_s": clip.source_duration / clip.rate,
                "colour": camera[clip.source_asset_id]["colour"],
            }
            for clip in sorted(footage.clips, key=lambda c: c.timeline_start)
        ],
        "text": {
            "text": VOICE_TITLE,
            "start_s": min(layer.start for layer in recipe.text_layers),
            "end_s": max(layer.end for layer in recipe.text_layers),
        },
        "palette": PALETTE,
        "twin": "voice_behind_footage_notext",
    }
    _write_voice_case(out, "voice_behind_footage", recipe, files, proof)
    twin = recipe.model_copy(
        update={
            "text_layers": [],
            "required_capabilities": recipe.required_capabilities
            - {"positionedText", "animatedText"},
        }
    )
    _write_voice_case(
        out, "voice_behind_footage_notext", twin, files, {"kind": "text_twin", "of": "voice"}
    )

    def control(name: str, expect: list[str], mutated) -> None:  # noqa: ANN001
        _write_voice_case(
            out,
            name,
            mutated,
            files,
            {**proof, "expect_failure": expect, "twin": "voice_behind_footage_notext"},
        )

    def with_clips(track_id: str, clips: list) -> object:
        return recipe.model_copy(
            update={
                "tracks": [
                    t.model_copy(update={"clips": clips}) if t.id == track_id else t
                    for t in recipe.tracks
                ]
            }
        )

    # 1. The picture clips' own sound switched back on.
    control(
        "voice_behind_footage_unmuted",
        ["camera_audio_silent"],
        with_clips("voice-footage", [c.model_copy(update={"volume": 1.0}) for c in footage.clips]),
    )
    # 2. The voice stops at half its span.
    control(
        "voice_behind_footage_voice_stops_early",
        ["voice_present_throughout"],
        with_clips("voice", [voice.model_copy(update={"source_duration": voice.source_duration / 2})]),
    )
    # 3. A picture clip shown twice (the wrap-around), another one never shown.
    wrapped = list(footage.clips)
    wrapped[3] = wrapped[3].model_copy(update={"source_asset_id": wrapped[0].source_asset_id})
    control("voice_behind_footage_wrapped", ["picture_order"], with_clips("voice-footage", wrapped))
    print(
        f"prepared voice_behind_footage cases in {out} (voice {voice.source_start:.2f}s +"
        f" {voice.source_duration:.2f}s, {len(footage.clips)} cuts, {recipe.duration:.2f}s)"
    )


def _compare_voice_behind_footage(out: Path, directory: Path, proof: dict) -> tuple[dict, bool]:
    import numpy as np  # noqa: PLC0415

    name = directory.name
    phone = directory / "phone.mp4"
    row: dict = {"case": name, "kind": "voice_behind_footage", "checks": {}}
    if not phone.exists():
        print(f"{name}: phone.mp4 missing -- run AudioParityFixtureTests first")
        return row, True
    checks = row["checks"]
    exported = _duration(phone)
    checks["duration"] = {
        "expected_s": round(proof["duration_s"], 3),
        "actual_s": round(exported, 3),
        "ok": abs(exported - proof["duration_s"]) <= 0.1,
    }

    order_rows = []
    for cut in proof["clips"]:
        at = cut["start_s"] + cut["duration_s"] / 2
        frame = _frame_rgb(phone, at)[70:90, 35:55].reshape(-1, 3).mean(axis=0)
        seen = _nearest_colour(frame, proof["palette"])
        order_rows.append(
            {"cut": cut["id"], "at_s": round(at, 2), "expected": cut["colour"], "seen": seen,
             "ok": seen == cut["colour"]}
        )  # fmt: skip
    checks["picture_order"] = {"cuts": order_rows, "ok": all(r["ok"] for r in order_rows)}

    voice = proof["voice"]
    rate = 48000
    pcm = np.asarray(_pcm(phone), dtype=np.float64)
    source = _pcm(directory.parent.parent / "voice_behind_footage_inputs" / voice["file"])
    freqs = voice["freqs_hz"]
    start = voice["source_start_s"]
    span = voice["span_s"]
    fade = max(voice["fade_in_s"] or 0.0, voice["fade_out_s"] or 0.0, 0.5)
    expected_peak = voice["amplitude"] * (voice["volume"] or 1.0)
    rows, worst_leak, covered_to = [], -180.0, 0.0
    for second in range(int(start), SONG_SECONDS):
        centre = second + 0.5 - start  # where source second `second` lands in the export
        if centre < fade + 0.25 or centre > span - fade - 0.25:
            continue
        reference = _goertzel(source, rate, second + 0.5, 0.2, freqs[second])
        heard = _goertzel(pcm, rate, centre, 0.2, freqs[second])
        others = [f for k, f in enumerate(freqs) if k != second] + proof["camera_tones_hz"]
        leak_db = max(_goertzel(pcm, rate, centre, 0.2, f) for f in others)
        leak_db = _amp_db(leak_db) - _amp_db(heard)
        worst_leak = max(worst_leak, leak_db)
        covered_to = max(covered_to, centre)
        rows.append(
            {"source_second": second, "export_s": round(centre, 2), "expected_hz": freqs[second],
             "level_vs_source_db": round(_amp_db(heard) - _amp_db(reference), 2),
             "loudest_other_vs_expected_db": round(leak_db, 1)}
        )  # fmt: skip
    present = bool(rows) and all(
        r["level_vs_source_db"] >= VOICE_PRESENT_MIN_DB
        and abs(r["level_vs_source_db"]) <= LEVEL_TOLERANCE_DB
        for r in rows
    )
    # The measured windows must reach the voice's planned end (the last one-second window
    # before the closing fade), and that planned end must itself sit within the sentence-snap
    # slack of the picture's end: a voice cut at half its span fails the first, a voice
    # planned short of the picture fails the second.
    reaches = covered_to >= span - fade - 0.25 - 1.0
    fills_picture = span >= proof["duration_s"] - 3.0 - 0.05
    checks["voice_present_throughout"] = {
        "windows": rows,
        "covered_to_s": round(covered_to, 2),
        "planned_voice_span_s": round(span, 2),
        "level_within_db": LEVEL_TOLERANCE_DB,
        "ok": present and reaches and fills_picture,
    }
    checks["voice_once"] = {
        "worst_other_tone_vs_expected_db": round(worst_leak, 1),
        "max_db": LEAK_MAX_DB,
        "ok": bool(rows) and worst_leak <= LEAK_MAX_DB,
    }
    camera_peak = max(
        (_goertzel(pcm, rate, t + 0.25, 0.25, f) for f in proof["camera_tones_hz"]
         for t in np.arange(0, exported - 0.5, 0.5)),
        default=0.0,
    )  # fmt: skip
    camera_db = _amp_db(camera_peak) - _amp_db(expected_peak)
    checks["camera_audio_silent"] = {
        "loudest_camera_tone_vs_expected_voice_level_db": round(camera_db, 1),
        "max_db": LEAK_MAX_DB,
        "ok": camera_db <= LEAK_MAX_DB,
    }
    integrated, _peak = _ebur128(phone)
    checks["loudness_sane"] = {"integrated_lufs": integrated, "ok": -45.0 < integrated < -5.0}

    twin = directory.parent / proof["twin"] / "phone.mp4"
    text = proof["text"]
    if twin.exists():
        inside = text["start_s"] + (text["end_s"] - text["start_s"]) / 2
        after = min(text["end_s"] + 1.0, proof["duration_s"] - 0.3)

        def diff(at: float) -> float:
            return float(np.abs(_frame_rgb(phone, at) - _frame_rgb(twin, at)).mean())

        inside_diff, after_diff = diff(inside), diff(after)
        checks["opening_text"] = {
            "window_s": [round(text["start_s"], 2), round(text["end_s"], 2)],
            "mean_diff_inside": round(inside_diff, 3),
            "mean_diff_after": round(after_diff, 3),
            "ok": inside_diff >= TEXT_DIFF_MIN and inside_diff >= TEXT_DIFF_RATIO * after_diff,
        }
    else:
        checks["opening_text"] = {"ok": False, "reason": f"{proof['twin']}/phone.mp4 missing"}

    montage = directory / "montage.png"
    try:
        from PIL import Image  # noqa: PLC0415

        tiles = [Image.fromarray(_frame_rgb(phone, r["at_s"]).astype("uint8")) for r in order_rows]
        if twin.exists():
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
    expect = proof.get("expect_failure")
    if expect:
        # Negative control: the harness must SEE the regression it exists to catch.
        caught = set(expect) <= set(failed_checks)
        row["expect_failure"] = expect
        row["negative_control_detected"] = caught
        print(f"{name}: negative control {'DETECTED' if caught else 'MISSED'} ({failed_checks})")
        return row, not caught
    row["passed"] = not failed_checks
    print(f"{name}: {'PASS' if row['passed'] else 'FAIL'} {failed_checks}")
    return row, bool(failed_checks)


CASES = {
    "all",
    "parity",
    "user_song",
    "voiceover_music_authored",
    "speech_music",
    "voice_behind_footage",
}


def prepare(out: Path, cases: str = "all") -> None:
    if cases in {"all", "parity"}:
        prepare_parity(out)
    if cases in {"all", "user_song"}:
        prepare_user_song(out)
    if cases in {"all", "voiceover_music_authored"}:
        prepare_voiceover_music_authored(out)
    if cases in {"all", "speech_music"}:
        prepare_speech_music(out)
    if cases in {"all", "voice_behind_footage"}:
        prepare_voice_behind_footage(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    cases = "all"
    if "--cases" in args:
        at = args.index("--cases")
        cases = args[at + 1] if at + 1 < len(args) else ""
        del args[at : at + 2]
    if len(args) != 2 or args[0] not in {"prepare", "compare"} or cases not in CASES:
        raise SystemExit(__doc__)
    target = Path(args[1]).resolve()
    if args[0] == "prepare":
        prepare(target, cases)
    else:
        raise SystemExit(compare(target))
