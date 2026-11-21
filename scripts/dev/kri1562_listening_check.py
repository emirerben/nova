#!/usr/bin/env python3
"""KRI-1562 listening check: a song under a Talking edit, measured in the native export.

`prepare RUN` takes every talking clip in TAKES/quiet/ and TAKES/noisy/ (or, with
`--synthetic`, stand-ins it builds from the repo's TTS and noise fixtures), transcribes it
with whisper.cpp, builds its speech windows with the server's own helpers
(`speech_spans_from_words`), and writes four recipes for it, three from the REAL
`compile_phone_subtitled_plan` with the bed from the REAL `music_bed_from_treatment`:

- `nomusic`: the edit with no song (the voice reference);
- `ducked`: the song under the edit, ducking under the windows (what production emits);
- `control`: the same song with no windows, the duck turned off (the check must catch it);
- `ref`: the control with the take's own sound at 0 (harness only: the song alone);
- `masker`: the control with the song at its full mastered level (harness only), which the
  word check must flag, so a pass on `ducked` means something.

`AudioParityFixtureTests` renders every case through KriaMediaEngine's production exporter.
`measure RUN` then reads the exports. Per 20 ms frame, the gain the song plays at is the
projection of (variant - nomusic) on ref, so neither the voice nor the song's own dynamics
enter it, and the control is measured exactly like the ducked edit. Per take it reports the
gain in speech and in pauses over 1 s, the recovery after each window, the voice and mix
loudness, the voice-to-music ratio in speech, and the words whisper hears in each export
against the no-music export. Criteria (exit 1 when one fails):

- duck: the ducked song sits at least 9 dB under its pause level while she speaks;
- recovery: back within 1 dB of the pause level at most 0.5 s after a window ends;
- control caught: the control does NOT meet the duck criterion;
- voice intelligible: against the no-music export's words, the ducked export's word error is
  at most 2 points over the floor (the most whisper changes when nothing that matters does:
  the take before the exporter, the no-music export 1 dB quieter or louder), else 'voice
  masked, consider separation'. The report also gives the literal reading (at most 2 points
  over 0, the no-music export against itself);
- masking caught: the masker's word error is more than 2 points over the floor.

One command runs all three steps: `scripts/dev/kri1562-listening-check.sh`. By hand:

    PY=src/apps/api/.venv/bin/python; RUN=$PWD/.dev/kri-1562-listening/manual
    $PY scripts/dev/kri1562_listening_check.py prepare $RUN [--synthetic] [--song PATH]
    (cd src/apps/ios/Packages/KriaMediaEngine && \
      KRIA_AUDIO_PARITY_DIR=$RUN swift test --filter AudioParityFixtureTests)
    $PY scripts/dev/kri1562_listening_check.py measure $RUN

Without `--song` the song is a stand-in: the repo's synthesized 3 s `music_02` drum loop with
a speech-shaped noise layer for vocals and mid-range instruments, set to -14 LUFS. A real
catalog song is what the masking decision needs.

Needs ffmpeg/ffprobe and whisper.cpp's `whisper-cli` on PATH. The multilingual ggml-small
model is fetched once into ~/.cache/kria-listening (or `--model` / KRIA_WHISPER_MODEL) and
checked against its pinned sha256. Takes, exports and reports stay under the gitignored .dev/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
FIXTURES = API / "tests/fixtures/clip_signals/audio"
DEFAULT_TAKES = REPO / ".dev/kri-1562-takes"
MEDIA_SUFFIXES = {".mov", ".mp4", ".m4v"}

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
# Hugging Face's LFS sha256 for ggml-small.bin; the file also matches the SHA-1 that
# whisper.cpp's models/README.md publishes (55356645c2b361a969dfd0ef2c5a50d530afd8d5).
MODEL_SHA256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
DEFAULT_MODEL = Path(
    os.environ.get("KRIA_WHISPER_MODEL", "~/.cache/kria-listening/ggml-small.bin")
).expanduser()

SR = 48_000
FRAME_S, HOP_S = 0.02, 0.01
# `AudioDuckEnvelope.speech` (AudioDynamics.swift): down over 0.15 s before a window, up over
# 0.35 s after it. Steady-level scoring stays clear of both ramps.
ATTACK_S, RELEASE_S = 0.15, 0.35
MIN_PAUSE_S = 1.0
DUCK_MIN_DB = 9.0
RECOVERY_MAX_S = 0.5
RECOVERED_DB = -1.0
WER_MAX_PP = 2.0
SPEECH_BAND_HZ = (300.0, 4000.0)
VARIANTS = ("nomusic", "ducked", "control", "ref", "masker")
# More words than this share whisper's alignment with their neighbour when the alignment
# collapses (a run of words all ending at one time), and the windows then miss speech.
MAX_COLLAPSED_WORDS = 0.05

# The macOS voices the repo's tts_*.m4a fixtures were made with. The fixtures themselves all
# say ONE sentence, and whisper's alignment mapped one copy of it onto the next (a whole
# sentence fell outside every window), a failure real speech does not have.
SYNTHETIC_LINES = (
    (
        "Daniel",
        "Okay, so this is the little bakery I keep telling everyone about, right at "
        "the end of my street.",
    ),
    (
        "Karen",
        "Every morning they pull fresh bread out of the oven, and the whole block "
        "smells like cinnamon.",
    ),
    (
        "Moira",
        "I usually grab a coffee here before work, and honestly it is the best part of my day.",
    ),
    (
        "Samantha",
        "If you are ever in the neighbourhood, come early, because the cardamom buns "
        "sell out by nine.",
    ),
    (
        "Tessa",
        "Next week I will try their new rye loaf and tell you whether it is worth the queue.",
    ),
)
# Lead silence, the four pauses between the five voices, trailing silence. Past 35 s, so
# whisper aligns across two 30 s windows like a long real take.
SYNTHETIC_GAPS_S = (1.0, 1.6, 1.5, 2.0, 1.2, 1.5)
SYNTHETIC_VOICE_LUFS = -23.0
SYNTHETIC_SNR_DB = 6.0
# labels.json marks crowd_03 "pure": babble with no voice of its own on top.
SYNTHETIC_NOISE = "crowd_03.m4a"
# A synthesized "full" loop; music_04 ("full_speech") carries a voice and would read as speech.
SYNTHETIC_SONG = "music_02.m4a"
SYNTHETIC_SONG_LUFS = -14.0


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _ffmpeg(*args: str) -> None:
    _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args)


def _probe(path: Path) -> dict:
    out = _run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path))
    return json.loads(out.stdout)


def _pcm(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm16 = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SR),
         "-ac", "1", "-i", "-", str(path)],
        input=pcm16, check=True,
    )  # fmt: skip


def _lufs(path: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128",
         "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr  # fmt: skip
    return float(re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", out)[-1])


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def ensure_model(path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        print(f"fetching {MODEL_URL} -> {path}")
        _run("curl", "-fsSL", "-o", str(part), MODEL_URL)
        part.rename(path)
    digest = _sha256(path)
    if digest != MODEL_SHA256:
        raise SystemExit(f"{path}: sha256 {digest} is not the pinned ggml-small.bin")
    return path


def transcribe(
    model: Path, media: Path, stem: Path, language: str, gain_db: float = 0.0
) -> tuple[str, list[list]]:
    """``(language, [[text, start_s, end_s], ...])`` from whisper.cpp's DTW token times.

    The segment/token offsets whisper.cpp prints smear words over pauses and pin a take's
    first word to 0 s, so words come from the cross-attention alignment (`-dtw`), where each
    token's time is where it ends (`-nfa`: with flash attention on, whisper.cpp leaves the DTW
    times unset). A word ends at its last token with a letter or digit and
    starts where the token before it ended, at most twice the median word length before its
    end (openai-whisper's rule for the word after a sentence end, applied to every word).
    ``gain_db`` perturbs the input for the word-error floor.
    """
    wav = stem.with_suffix(".16k.wav")
    _ffmpeg("-i", str(media), "-vn", "-af", f"volume={gain_db}dB", "-ac", "1", "-ar", "16000",
            str(wav))  # fmt: skip
    whisper = os.environ.get("WHISPER_CLI") or shutil.which("whisper-cli") or "whisper-cli"
    _run(whisper, "-m", str(model), "-f", str(wav), "-l", language, "-dtw", "small", "-nfa",
         "-ojf", "-of", str(stem), "-np")  # fmt: skip
    raw = json.loads(stem.with_suffix(".json").read_bytes().decode("utf-8", "surrogateescape"))
    words: list[dict] = []
    previous_end = 0.0
    for segment in raw["transcription"]:
        for token in segment["tokens"]:
            text, at = token["text"], token["t_dtw"] / 100
            if text.startswith("[_") or token["t_dtw"] < 0:
                continue
            if text.startswith(" ") or not words:
                words.append({"text": "", "start": previous_end, "end": None})
            words[-1]["text"] += text
            if any(ch.isalnum() for ch in text):
                words[-1]["end"] = at
            previous_end = at
    words = [w for w in words if w["end"] is not None]
    if not words:
        return raw["result"]["language"], []
    longest = 2 * float(np.median([w["end"] - w["start"] for w in words]))
    spoken = [
        [
            w["text"].encode("utf-8", "surrogateescape").decode("utf-8", "replace").strip(),
            round(max(w["start"], w["end"] - longest), 3),
            round(w["end"], 3),
        ]
        for w in words
    ]
    return raw["result"]["language"], spoken


# ---------------------------------------------------------------- synthetic stand-ins


def _voiced_spans(samples: np.ndarray, floor_dbfs: float = -45.0) -> list[list[float]]:
    """Where the clean synthetic voice is above `floor_dbfs`, merged like the server merges."""
    from app.pipeline.phone_recipe_shared import merge_speech_spans  # noqa: PLC0415

    n, hop = int(FRAME_S * SR), int(HOP_S * SR)
    frames = np.lib.stride_tricks.sliding_window_view(samples, n)[::hop]
    loud = 10 * np.log10(np.mean(frames**2, axis=1) + 1e-20) > floor_dbfs
    spans = [(i * HOP_S, i * HOP_S + FRAME_S) for i in np.flatnonzero(loud)]
    return [[round(a, 3), round(b, 3)] for a, b in merge_speech_spans(spans)]


def build_synthetic(directory: Path) -> Path:
    """A quiet take (four TTS voices, pauses over 1 s) and the same take under babble. The
    noisy take is a rotated .mov like an iPhone portrait clip, so the probe path real takes go
    through is exercised too."""
    scratch = directory / "build"
    scratch.mkdir(parents=True, exist_ok=True)
    parts = []
    for gap, (voice, line) in zip(SYNTHETIC_GAPS_S, SYNTHETIC_LINES, strict=False):
        spoken = scratch / f"{voice}.aiff"
        _run("say", "-v", voice, "-o", str(spoken), line)
        parts += [np.zeros(int(gap * SR)), _pcm(spoken)]
    voice = np.concatenate([*parts, np.zeros(int(SYNTHETIC_GAPS_S[-1] * SR))])
    _write_wav(scratch / "voice_raw.wav", voice)
    voice *= 10 ** ((SYNTHETIC_VOICE_LUFS - _lufs(scratch / "voice_raw.wav")) / 20)
    truth = _voiced_spans(voice)
    noise = np.resize(_pcm(FIXTURES / SYNTHETIC_NOISE), voice.size)
    speech = np.concatenate([voice[int(a * SR) : int(b * SR)] for a, b in truth])
    noise *= np.sqrt(np.mean(speech**2) / np.mean(noise**2)) / 10 ** (SYNTHETIC_SNR_DB / 20)
    seconds = f"{voice.size / SR:.3f}"
    for kind, samples, size, suffix in (
        ("quiet", voice, "1080x1920", ".mp4"),
        ("noisy", voice + noise, "1920x1080", ".mov"),
    ):
        wav, flat = scratch / f"{kind}.wav", scratch / f"{kind}_flat.mp4"
        _write_wav(wav, samples)
        _ffmpeg(
            "-f", "lavfi", "-i", f"color=c=0x203040:s={size}:r=30:d={seconds}", "-i", str(wav),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(flat),
        )  # fmt: skip
        take = directory / kind / f"synthetic_{kind}{suffix}"
        take.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".mov":
            _ffmpeg("-display_rotation:v:0", "-90", "-i", str(flat), "-c", "copy", str(take))
        else:
            shutil.copyfile(flat, take)
        take.with_suffix(".truth.json").write_text(json.dumps({"voiced_spans": truth}))
    return directory


def fixture_song(directory: Path) -> Path:
    """The repo's 3 s `music_02` drum loop repeated to 2 minutes (one bed clip, no loop joins
    under the take), plus an equally loud speech-shaped noise layer standing in for vocals and
    mid-range instruments, set to a catalog master's -14 LUFS. The drum loop alone has almost
    nothing in the speech band and could never mask a voice."""
    song, scratch = directory / "fixture_song.m4a", directory / "fixture_song_mix.wav"
    drums = np.resize(_pcm(FIXTURES / SYNTHETIC_SONG), 120 * SR)
    spectrum = np.fft.rfft(np.random.default_rng(1562).standard_normal(drums.size))
    hz = np.fft.rfftfreq(drums.size, 1 / SR)
    spectrum *= np.where((hz > 200) & (hz < 5000), np.minimum(1.0, 1000 / np.maximum(hz, 1)), 0)
    mids = np.fft.irfft(spectrum, drums.size)
    mids *= np.sqrt(np.mean(drums**2) / np.mean(mids**2))
    _write_wav(scratch, (drums + mids) / 4)
    gain_db = SYNTHETIC_SONG_LUFS - _lufs(scratch)
    _ffmpeg("-i", str(scratch), "-af", f"volume={gain_db:.3f}dB", "-ac", "2", "-c:a", "aac",
            "-b:a", "256k", str(song))  # fmt: skip
    return song


# ------------------------------------------------------------------------ prepare


def _api_imports() -> None:
    sys.path.insert(0, str(API))
    for name, value in {
        "STORAGE_BUCKET": "nova-test",
        "DATABASE_URL": "postgresql://localhost/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "INTERNAL_API_KEY": "test",
    }.items():
        os.environ.setdefault(name, value)


def _binding(media: Path):  # noqa: ANN202
    from app.kria.media_sources import OriginalMediaDescriptor  # noqa: PLC0415
    from app.services.phone_sources import PhoneSourceBinding  # noqa: PLC0415

    info = _probe(media)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    rotation = next(
        (int(d["rotation"]) for d in video.get("side_data_list", []) if "rotation" in d), 0
    )
    digest = _sha256(media)
    return PhoneSourceBinding(
        media_id="speaker",
        proxy_path="users/listening/analysis-proxy-speaker.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=digest,
            byte_count=media.stat().st_size,
            duration_s=float(info["format"]["duration"]),
            width=int(video["width"]),
            height=int(video["height"]),
            orientation_degrees=(-rotation) % 360,
            has_audio=any(s["codec_type"] == "audio" for s in info["streams"]),
        ),
    )


def _without_duck(recipe) -> dict:  # noqa: ANN001
    data = recipe.model_dump(mode="json")
    data["audio"].pop("speech_windows", None)
    data["required_capabilities"] = sorted(set(data["required_capabilities"]) - {"speechDuck"})
    return data


def compile_variants(binding, words: list[list], song: Path) -> dict:  # noqa: ANN001
    from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint  # noqa: PLC0415
    from app.pipeline.captions import PHONE_CUE_BUDGET, build_plain_cues  # noqa: PLC0415
    from app.pipeline.phone_recipe_shared import (  # noqa: PLC0415
        music_bed_from_treatment,
        speech_spans_from_words,
    )
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan  # noqa: PLC0415
    from app.pipeline.transcribe import Word  # noqa: PLC0415

    timed = [Word(text=t, start_s=a, end_s=b, confidence=1.0) for t, a, b in words]
    cues = build_plain_cues(timed, attach_words=True, budget=PHONE_CUE_BUDGET)
    song_s = float(_probe(song)["format"]["duration"])
    fingerprint = RenderFingerprint(sha256=_sha256(song), byte_count=song.stat().st_size)

    def pin(track_id: str, _path: str | None = None):  # noqa: ANN202
        asset = LibraryRenderAsset(
            id=f"music-{track_id}",
            catalog="music",
            catalog_id=track_id,
            generation="1",
            fingerprint=fingerprint,
        )
        return asset, song_s

    # A creator's song choice as `smart_music_treatment` stores it; gain and duck stay at the
    # treatment defaults (-18 dB bed, -12 dB under speech).
    treatment = {
        "track_id": "listening-song",
        "src_gcs_path": "music/listening-song/song.m4a",
        "section_start_s": 0.0,
        "section_end_s": song_s,
    }
    bed = music_bed_from_treatment(treatment, pin)
    spans = speech_spans_from_words(timed)
    common = {"caption_cues": cues}
    recipes = {
        "nomusic": compile_phone_subtitled_plan((binding,), **common),
        "ducked": compile_phone_subtitled_plan(
            (binding,), **common, music=bed, speech_source_windows={binding.media_id: spans}
        ),
        # `{}`, never None: None means a legacy variant and ducks under the caption words.
        "control": compile_phone_subtitled_plan(
            (binding,), **common, music=bed, speech_source_windows={}
        ),
    }
    ducked, control = recipes["ducked"], recipes["control"]
    assert ducked.audio.speech_windows, "no windows: the words never reached the speaker clip"
    assert "speechDuck" in ducked.required_capabilities
    assert not control.audio.speech_windows
    assert _without_duck(ducked) == _without_duck(control), "control differs beyond the duck"
    recipes["ref"] = control.model_copy(
        update={"audio": control.audio.model_copy(update={"original_volume": 0.0})}
    )
    bed_track = next(track for track in control.tracks if track.role == "music")
    full = bed_track.model_copy(
        update={"clips": [clip.model_copy(update={"volume": 1.0}) for clip in bed_track.clips]}
    )
    recipes["masker"] = control.model_copy(
        update={"tracks": [full if track is bed_track else track for track in control.tracks]}
    )
    return {"recipes": recipes, "bed": bed, "source_windows": [list(s) for s in spans]}


def _write_case(directory: Path, recipe, media: Path, song: Path) -> None:  # noqa: ANN001
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    stale = directory / "phone.mp4"
    stale.unlink(missing_ok=True)
    assets = {}
    for asset in recipe.asset_manifest.assets:
        assets[asset.id] = str(song if asset.kind == "library" else media)
    for asset in recipe.assets:
        if asset.id.startswith("font-"):  # typography the device ships with
            assets[asset.id] = str(API / "assets/fonts" / asset.id.removeprefix("font-"))
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))


def _takes(directory: Path) -> list[tuple[str, Path]]:
    found = [
        (kind, path)
        for kind in ("quiet", "noisy")
        for path in sorted((directory / kind).glob("*"))
        if path.suffix.lower() in MEDIA_SUFFIXES
    ]
    kinds = {kind for kind, _ in found}
    if kinds != {"quiet", "noisy"}:
        raise SystemExit(f"{directory}: need a talking clip in quiet/ and in noisy/, got {found}")
    return found


def prepare(run: Path, takes_dir: Path, song: Path | None, model: Path, synthetic: bool) -> None:
    _api_imports()
    run.mkdir(parents=True, exist_ok=True)
    model = ensure_model(model)
    if synthetic:
        takes_dir = build_synthetic(run / "synthetic-takes")
    found = _takes(takes_dir)
    song = song.resolve() if song else fixture_song(run)
    takes = []
    for kind, media in found:
        name = f"{kind}-{re.sub(r'[^A-Za-z0-9]+', '-', media.stem).strip('-')}"
        work = run / name
        work.mkdir(parents=True, exist_ok=True)
        language, words = transcribe(model, media, work / "source", "auto")
        collapsed = sum(1 for _, start, end in words if end - start < 0.01)
        if collapsed > MAX_COLLAPSED_WORDS * len(words):
            raise SystemExit(
                f"{media}: whisper's alignment collapsed on {collapsed} of {len(words)} words "
                f"(see {work / 'source.json'}); its windows would miss speech"
            )
        compiled = compile_variants(_binding(media), words, song)
        for variant, recipe in compiled["recipes"].items():
            _write_case(run / "cases" / f"{name}__{variant}", recipe, media, song)
        ducked = compiled["recipes"]["ducked"]
        music = next(track for track in ducked.tracks if track.role == "music")
        truth = media.with_suffix(".truth.json")
        take = {
            "name": name,
            "kind": kind,
            "synthetic": synthetic,
            "source": str(media),
            "language": language,
            "words": words,
            "source_windows": compiled["source_windows"],
            "timeline_windows": [[w.start, w.end] for w in ducked.audio.speech_windows],
            "speech_duck_gain": compiled["bed"].speech_duck_gain,
            "bed_volume": compiled["bed"].volume,
            "music_clips": [
                [
                    c.timeline_start,
                    c.timeline_start + c.source_duration / c.rate,
                    c.audio_fade_in or 0.0,
                    c.audio_fade_out or 0.0,
                ]
                for c in music.clips
            ],
            "duration_s": ducked.duration,
            "truth_voiced_spans": (
                json.loads(truth.read_text())["voiced_spans"] if truth.exists() else None
            ),
        }
        (work / "take.json").write_text(json.dumps(take, indent=2))
        takes.append(name)
        print(
            f"prepared {name}: {language}, {len(words)} words, "
            f"{len(take['timeline_windows'])} windows, {ducked.duration:.2f} s"
        )
    meta = {
        "model": str(model),
        "song": str(song),
        "song_lufs": _lufs(song),
        "synthetic": synthetic,
        "takes": takes,
    }
    (run / "run.json").write_text(json.dumps(meta, indent=2))


# ------------------------------------------------------------------------ measure


def _frames(x: np.ndarray) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(x, int(FRAME_S * SR))[:: int(HOP_S * SR)]


def _band(x: np.ndarray) -> np.ndarray:
    spectrum = np.fft.rfft(x)
    hz = np.fft.rfftfreq(x.size, 1 / SR)
    spectrum[(hz < SPEECH_BAND_HZ[0]) | (hz > SPEECH_BAND_HZ[1])] = 0
    return np.fft.irfft(spectrum, x.size)


def _lag(a: np.ndarray, b: np.ndarray, max_lag: int = 4800) -> int:
    """Samples by which ``b`` trails ``a`` (cross-correlation peak over the first 12 s)."""
    n = min(a.size, b.size, 12 * SR)
    size = 1 << (2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b[:n], size) * np.conj(np.fft.rfft(a[:n], size)), size)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    return int(lags[np.argmax(np.concatenate([corr[: max_lag + 1], corr[-max_lag:]]))])


def _db(energy: float) -> float:
    return round(10 * np.log10(energy + 1e-20), 2)


def _normalise(words: list[list]) -> list[str]:
    # Every i folds to "i": whisper writes Turkish İ/ı and English I inconsistently.
    fold = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
    out = []
    for text, *_ in words:
        token = unicodedata.normalize("NFC", text).translate(fold).casefold()
        token = "".join(ch for ch in token if unicodedata.category(ch)[0] in "LN")
        if token:
            out.append(token)
    return out


def word_errors(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, 1):
        prev, row[0] = row[0], i
        for j, hyp in enumerate(hypothesis, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ref != hyp))
    return row[-1]


def _regions(take: dict, offset_s: float) -> tuple[list, list, list]:
    """Steady speech spans, steady pause spans, and (window end, pause span) pairs, on the
    export's clock; music clip fades and joins are cut out of both."""
    windows = [(a + offset_s, b + offset_s) for a, b in take["timeline_windows"]]
    holes = []
    for start, end, fade_in, fade_out in take["music_clips"]:
        holes += [(start - 0.05, start + max(fade_in, 0.05)), (end - max(fade_out, 0.05), end)]

    def minus(span: tuple[float, float]) -> list[tuple[float, float]]:
        pieces = [span]
        for a, b in holes:
            pieces = [p for s, e in pieces for p in ((s, min(e, a)), (max(s, b), e)) if p[1] > p[0]]
        return pieces

    speech = [p for w in windows for p in minus(w)]
    pauses, recoveries = [], []
    for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
        if start - end > MIN_PAUSE_S:
            steady = minus((end + RELEASE_S, start - ATTACK_S))
            pauses += steady
            recoveries.append((end, start - ATTACK_S, steady))
    return speech, pauses, recoveries


def _mask(starts: np.ndarray, spans: list) -> np.ndarray:
    inside = np.zeros(starts.size, dtype=bool)
    for a, b in spans:
        inside |= (starts >= a) & (starts + FRAME_S <= b)
    return inside


def measure_take(run: Path, take: dict, model: Path) -> dict:
    cases = {v: run / "cases" / f"{take['name']}__{v}" for v in VARIANTS}
    for variant, case in cases.items():
        export, recipe = case / "phone.mp4", case / "recipe.json"
        if not export.exists() or export.stat().st_mtime < recipe.stat().st_mtime:
            raise SystemExit(f"{export}: missing or older than its recipe; run the package test")
    streams = {
        v: next(s for s in _probe(c / "phone.mp4")["streams"] if s["codec_type"] == "audio")
        for v, c in cases.items()
    }
    shape = {(s["sample_rate"], s["channels"]) for s in streams.values()}
    lengths = [float(s["duration"]) for s in streams.values()]
    assert len(shape) == 1, f"exports differ in sample rate or channels: {shape}"
    assert max(lengths) - min(lengths) < 0.03, f"exports differ in length: {lengths}"
    pcm = {v: _pcm(c / "phone.mp4") for v, c in cases.items()}
    n = min(x.size for x in pcm.values())
    pcm = {v: x[:n] for v, x in pcm.items()}
    between = _lag(pcm["nomusic"], pcm["ducked"])
    assert between == 0, f"ducked export is {between} samples off the no-music export"
    offset_s = _lag(_pcm(Path(take["source"])), pcm["nomusic"]) / SR

    speech, pauses, recoveries = _regions(take, offset_s)
    starts = np.arange(_frames(pcm["ref"]).shape[0]) * HOP_S
    in_speech, in_pause = _mask(starts, speech), _mask(starts, pauses)
    ref_frames = _frames(pcm["ref"])
    ref_energy = np.sum(ref_frames**2, axis=1)
    audible = ref_energy / ref_frames.shape[1] > 1e-8
    voice = np.mean(_frames(pcm["nomusic"]) ** 2, axis=1)
    voice_band = np.mean(_frames(_band(pcm["nomusic"])) ** 2, axis=1)
    ref_band = _frames(_band(pcm["ref"]))

    def heard(variant: str, label: str, gain_db: float = 0.0) -> list[str]:
        media = Path(take["source"]) if variant == "source" else cases[variant] / "phone.mp4"
        stem = run / take["name"] / label
        return _normalise(transcribe(model, media, stem, take["language"], gain_db)[1])

    reference = heard("nomusic", "nomusic")
    # The floor: how many words whisper changes when nothing that matters changed (the take
    # before the exporter, the no-music export 1 dB quieter or louder). A music export only
    # counts as masking the voice by what it adds on top.
    floor = {
        "source": word_errors(reference, heard("source", "source_floor")),
        "nomusic_minus_1db": word_errors(reference, heard("nomusic", "nomusic_m1", -1.0)),
        "nomusic_plus_1db": word_errors(reference, heard("nomusic", "nomusic_p1", 1.0)),
    }
    floor_pct = round(100 * max(floor.values()) / max(1, len(reference)), 2)

    rows, gains = {}, {}
    for variant in ("ducked", "control", "masker"):
        diff = pcm[variant] - pcm["nomusic"]
        frames = _frames(diff)
        gain = np.where(audible, np.sum(frames * ref_frames, axis=1) / ref_energy, np.nan)
        gains[variant] = gain
        music = gain**2 * ref_energy / ref_frames.shape[1]
        residual = np.mean((frames - gain[:, None] * ref_frames) ** 2, axis=1)
        band_frames = _frames(_band(diff))
        band_gain = np.sum(band_frames * ref_band, axis=1) / np.sum(ref_band**2, axis=1)
        music_band = band_gain**2 * np.mean(ref_band**2, axis=1)
        use_speech, use_pause = in_speech & audible, in_pause & audible
        g_speech = float(np.median(gain[use_speech]))
        g_pause = float(np.median(gain[use_pause])) if use_pause.any() else float("nan")
        times = []
        for end, until, steady in recoveries:
            full = np.nanmedian(gain[_mask(starts, steady) & audible])
            after = np.flatnonzero((starts >= end) & (starts + FRAME_S <= until) & audible)
            back = [i for i in after if gain[i] >= full * 10 ** (RECOVERED_DB / 20)]
            times.append(round(starts[back[0]] + FRAME_S / 2 - end, 3) if back else None)
        hyp = heard(variant, variant)
        rows[variant] = {
            "gain_in_speech": round(g_speech, 4),
            "gain_in_pauses": round(g_pause, 4),
            "duck_db": round(20 * np.log10(g_pause / g_speech), 2),
            "recovery_s": times,
            "music_in_speech_dbfs": _db(float(np.mean(music[use_speech]))),
            "music_in_pauses_dbfs": _db(float(np.mean(music[use_pause]))),
            "difference_in_speech_dbfs": _db(
                float(np.mean(np.mean(frames**2, axis=1)[use_speech]))
            ),
            "codec_residual_in_speech_dbfs": _db(float(np.mean(residual[use_speech]))),
            "voice_to_music_db": round(
                _db(float(np.mean(voice[use_speech]))) - _db(float(np.mean(music[use_speech]))), 2
            ),
            "voice_to_music_speech_band_db": round(
                _db(float(np.mean(voice_band[use_speech])))
                - _db(float(np.mean(music_band[use_speech]))),
                2,
            ),
            "mix_lufs": _lufs(cases[variant] / "phone.mp4"),
            "word_errors": word_errors(reference, hyp),
            "reference_words": len(reference),
            "wer_pct": round(100 * word_errors(reference, hyp) / max(1, len(reference)), 2),
        }
    ducked, control, masker = rows["ducked"], rows["control"], rows["masker"]
    measured = [t for t in ducked["recovery_s"] if t is not None]
    criteria = {
        "duck": {
            "duck_db": ducked["duck_db"],
            "min_db": DUCK_MIN_DB,
            "ok": ducked["duck_db"] >= DUCK_MIN_DB,
        },
        "recovery": {
            "worst_s": max(measured) if measured else None,
            "max_s": RECOVERY_MAX_S,
            "pauses_over_1s": len(recoveries),
            "ok": bool(recoveries)
            and len(measured) == len(recoveries)
            and max(measured) <= RECOVERY_MAX_S,
        },
        "control_caught": {
            "control_duck_db": control["duck_db"],
            "ok": bool(recoveries) and not control["duck_db"] >= DUCK_MIN_DB,
        },
        "voice_intelligible": {
            "ducked_wer_pct": ducked["wer_pct"],
            "floor_pct": floor_pct,
            "max_pp_over_floor": WER_MAX_PP,
            "literal_ok": ducked["wer_pct"] <= WER_MAX_PP,
            "ok": ducked["wer_pct"] - floor_pct <= WER_MAX_PP,
        },
        "masking_caught": {
            "masker_wer_pct": masker["wer_pct"],
            "floor_pct": floor_pct,
            "ok": masker["wer_pct"] - floor_pct > WER_MAX_PP,
        },
    }
    result = {
        "take": take["name"],
        "kind": take["kind"],
        "synthetic": take["synthetic"],
        "language": take["language"],
        "duration_s": take["duration_s"],
        "windows": len(take["timeline_windows"]),
        "export_offset_ms": round(offset_s * 1000, 2),
        "voice_lufs": _lufs(cases["nomusic"] / "phone.mp4"),
        "bed_lufs": _lufs(cases["ref"] / "phone.mp4"),
        # How much of the song sits where speech does: speech-band level minus broadband level.
        "bed_speech_band_share_db": round(
            _db(float(np.mean(_band(pcm["ref"]) ** 2))) - _db(float(np.mean(pcm["ref"] ** 2))), 2
        ),
        "expected_gain_in_speech": take["speech_duck_gain"],
        "variants": rows,
        "criteria": criteria,
        "wer_floor_errors": floor,
        "exports": {v: str(c / "phone.mp4") for v, c in cases.items()},
        "window_accuracy": _window_accuracy(take, starts, gains["ducked"], offset_s),
    }
    return result


def _window_accuracy(
    take: dict, starts: np.ndarray, gain: np.ndarray, offset_s: float
) -> dict | None:
    """Synthetic takes only: the windows against the clean voice's true voiced spans, and how
    long the EXPORTED song stays over 3 dB above its ducked level while that voice is heard."""
    truth, windows = take["truth_voiced_spans"], take["source_windows"]
    if not truth:
        return None
    voiced = _mask(starts, [(a + offset_s, b + offset_s) for a, b in truth])
    raised = gain > take["speech_duck_gain"] * 10 ** (3 / 20)
    pauses = [
        (a[1], b[0]) for a, b in zip(truth, truth[1:], strict=False) if b[0] - a[1] > MIN_PAUSE_S
    ]
    end_errors, start_errors = [], []
    for end, start in pauses:
        middle = (end + start) / 2
        before = max((y for _, y in windows if y <= middle), default=end)
        after = min((x for x, _ in windows if x >= middle), default=start)
        end_errors.append(round(before - end, 3))
        start_errors.append(round(after - start, 3))
    gaps = sum(1 for a, b in zip(windows, windows[1:], strict=False) if b[0] - a[1] > MIN_PAUSE_S)
    return {
        "voiced_s": round(float(np.sum(voiced)) * HOP_S, 2),
        "voiced_with_song_above_duck_s": round(float(np.sum(voiced & raised)) * HOP_S, 2),
        "true_pauses_over_1s": len(pauses),
        "window_pauses_over_1s": gaps,
        "window_end_minus_true_end_s": end_errors,
        "window_start_minus_true_start_s": start_errors,
    }


def _markdown(meta: dict, results: list[dict]) -> str:
    label = "SYNTHETIC stand-ins, not real takes" if meta["synthetic"] else "real takes"
    lines = [
        f"# KRI-1562 listening check ({label})",
        "",
        f"Song `{Path(meta['song']).name}` at {meta['song_lufs']} LUFS; whisper "
        f"`{Path(meta['model']).name}`. Gains are the song's level against the no-window bed; "
        "voice-to-music is the take's whole sound (voice and room) against the song in speech.",
        "",
        "| take | variant | gain in speech | gain in pauses | duck dB | worst recovery s "
        "| voice-to-music dB (speech band) | mix LUFS | word errors / ref words (floor) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        floor = max(r["wer_floor_errors"].values())
        for variant, v in r["variants"].items():
            worst = max((t for t in v["recovery_s"] if t is not None), default=None)
            lines.append(
                f"| {r['take']} | {variant} | {v['gain_in_speech']} | {v['gain_in_pauses']} "
                f"| {v['duck_db']} | {worst if variant == 'ducked' else '-'} "
                f"| {v['voice_to_music_db']} ({v['voice_to_music_speech_band_db']}) "
                f"| {v['mix_lufs']} | {v['word_errors']} / {v['reference_words']} ({floor}) |"
            )
    lines += [
        "",
        "| take | duck >= 9 dB | recovery <= 0.5 s | control caught "
        "| voice intelligible (<= floor + 2 pp) | literal reading (<= 2 pp) | masking caught |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        c = r["criteria"]
        cells = [
            ("PASS" if c[k]["ok"] else "FAIL")
            for k in ("duck", "recovery", "control_caught", "voice_intelligible")
        ]
        if not c["recovery"]["pauses_over_1s"]:
            cells = [f"{cell} (no pause over 1 s)" for cell in cells[:3]] + cells[3:]
        cells.append("PASS" if c["voice_intelligible"]["literal_ok"] else "FAIL (informative)")
        cells.append("PASS" if c["masking_caught"]["ok"] else "FAIL")
        lines.append(f"| {r['take']} | " + " | ".join(cells) + " |")
    masked = [r["take"] for r in results if not r["criteria"]["voice_intelligible"]["ok"]]
    lines += [
        "",
        f"Voice masked, consider separation: {', '.join(masked) if masked else 'no'}.",
        "",
    ]
    for r in results:
        lines.append(
            f"- {r['take']} ({r['language']}, {r['duration_s']:.1f} s, {r['windows']} windows): "
            f"voice {r['voice_lufs']} LUFS, bed {r['bed_lufs']} LUFS, song's speech-band share "
            f"{r['bed_speech_band_share_db']} dB, export offset {r['export_offset_ms']} ms. "
            f"Listen: `{r['exports']['ducked']}` against `{r['exports']['control']}`."
        )
        if r["window_accuracy"]:
            lines.append(f"  Windows against the true voice: {json.dumps(r['window_accuracy'])}")
    return "\n".join(lines) + "\n"


def measure(run: Path) -> int:
    meta = json.loads((run / "run.json").read_text())
    model = Path(meta["model"])
    results = [
        measure_take(run, json.loads((run / name / "take.json").read_text()), model)
        for name in meta["takes"]
    ]
    report = {"run": meta, "takes": results}
    (run / "report.json").write_text(json.dumps(report, indent=2, default=lambda v: v.item()))
    (run / "report.md").write_text(_markdown(meta, results))
    print((run / "report.md").read_text())
    failed = [(r["take"], k) for r in results for k, c in r["criteria"].items() if not c["ok"]]
    for take, key in failed:
        print(f"FAIL {take}: {key}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="step", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("run", type=Path)
    prep.add_argument("--takes", type=Path, default=DEFAULT_TAKES)
    prep.add_argument("--synthetic", action="store_true")
    prep.add_argument("--song", type=Path)
    prep.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    meas = sub.add_parser("measure")
    meas.add_argument("run", type=Path)
    args = parser.parse_args()
    if args.step == "prepare":
        prepare(args.run.resolve(), args.takes.resolve(), args.song, args.model, args.synthetic)
        return 0
    return measure(args.run.resolve())


if __name__ == "__main__":
    sys.exit(main())
