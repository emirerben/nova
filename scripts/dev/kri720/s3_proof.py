"""KRI-720 native proof on Çiğdem's real S3 takes (no chat, no DB, no storage).

  real preflight engine per take (OpenAI whisper + silence detection, as prod)
    -> the REAL several-take assembly (`_analyze_takes`) -> row -> `analysis_snapshot`
    -> the REAL phone Talking worker (`_run_phone_subtitled_job`, required_v1)
    -> recipe.json + assets.json per case for `AudioParityFixtureTests` (KriaMediaEngine export).

Cases: s3_clean (3 takes, cleanup), s3_uncut (same takes, cleanup off: control),
single_clean (take A alone, cleanup: today's single-take path).

Usage (from src/apps/api, PYTHONPATH=$PWD, API keys in env):
    python kri720_s3_proof.py KIT_DIR OUT_DIR
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
)
from app.services.clip_speech import audio_presence
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    SPEECH_CLEANUP_MIXED_GAP_MODE,
    SPEECH_CLEANUP_OVER_BUDGET_POLICY,
    ClaimedSpeechCleanupAnalysis,
    analysis_snapshot,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION
from app.tasks import generative_build as gb
from app.tasks import speech_cleanup_analysis as task_module
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _TALKING_HEAD_FEATURES,
    _job_and_session,
)

TAKES = [
    ("takeA", "IMG_7244.MOV"),
    ("takeB", "IMG_7245.MOV"),
    ("takeC", "IMG_7252.MOV"),
]
FINGERPRINT = hashlib.sha256(b"kri720-s3-proof").hexdigest()


def probe_s(path: Path) -> float:
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
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def binding(media_id: str, path: Path) -> PhoneSourceBinding:
    data = path.read_bytes()
    return PhoneSourceBinding.model_validate(
        {
            "media_id": media_id,
            "proxy_path": f"user/analysis-proxy-{media_id}.mp4",
            "generation": "1",
            "original": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
                "duration_s": probe_s(path),
                "width": 1080,
                "height": 1920,
                "has_audio": True,
            },
        }
    )


MODEL = str(
    Path(
        "~/Downloads/kria-cigdem-test/_src/models/ggml-large-v3-turbo-q5_0.bin"
    ).expanduser()
)


def whisper_cpp(path: str, *, language=None, verbatim_prompt=None):
    """Free local stand-in for the prod OpenAI whisper call (word-level, -ml 1 -sow)."""
    from app.pipeline.transcribe import Transcript, Word

    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "w")
        if not str(path).endswith(".wav"):
            wav = str(Path(tmp) / "in.wav")
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-i",
                    str(path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    wav,
                ],
                check=True,
            )
            path = wav
        cmd = [
            "whisper-cli",
            "-m",
            MODEL,
            "-l",
            language or "en",
            "-f",
            path,
            "-ml",
            "1",
            "-sow",
            "-ojf",
            "-of",
            base,
            "-t",
            "8",
            "-np",
        ]
        if verbatim_prompt:
            cmd += ["--prompt", verbatim_prompt]
        subprocess.run(cmd, check=True, capture_output=True)
        data = json.load(open(base + ".json"))
    words = []
    for seg in data["transcription"]:
        text = seg["text"].strip()
        if not text:
            continue
        probs = [
            t.get("p", 1.0)
            for t in seg.get("tokens", [])
            if not t["text"].startswith("[")
        ]
        words.append(
            Word(
                text=text,
                start_s=seg["offsets"]["from"] / 1000,
                end_s=seg["offsets"]["to"] / 1000,
                confidence=min(probs) if probs else 1.0,
            )
        )
    return Transcript(
        words=words, full_text=" ".join(w.text for w in words), language="en"
    )


RESTARTS = [
    ("wait", "okay"),
    ("and", "again"),
]  # take A: the slate, then "…perfume on— oh sorry, again"


def labelled_restarts(words, language):
    folded = [w.text.lower().strip(".,?!…") for w in words]
    spans = []
    for first, last in RESTARTS:
        starts = [i for i, t in enumerate(folded) if t == first]
        for start in starts:
            end = next(
                (
                    j
                    for j in range(start, min(start + 8, len(folded)))
                    if folded[j] == last
                ),
                None,
            )
            if end is not None:
                spans.append((start, end))
                break
    return tuple(spans)


def analyse_take(path: Path, duration_s: float):
    """One take exactly as the preflight worker analyses it (local audio, real engine)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "narration.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],
            check=True,
        )
        presence = audio_presence(str(wav))
        if presence.status in ("silent", "inaudible"):
            return None
        return analyze_speech_cleanup(
            SpeechCleanupAnalysisInput(
                source_fingerprint=FINGERPRINT,
                local_media_path=str(wav),
                duration_s=duration_s,
                source_window_start_s=0.0,
                source_window_end_s=duration_s,
                detector_version=DETECTOR_VERSION,
                mixed_gap_mode=SPEECH_CLEANUP_MIXED_GAP_MODE,
                include_silence_and_fillers=True,
                over_budget_policy=SPEECH_CLEANUP_OVER_BUDGET_POLICY,
                max_removal_frac_required=gb.settings.speech_cleanup_max_removal_frac_required,
                filler_check=gb.settings.speech_cleanup_filler_check_enabled,
            ),
            transcribe_fn=whisper_cpp,
            # Stand-in for the paid retake detector (an LLM): the two restarts a person marks
            # in take A by listening (answer key `must_not_hear`). Words are located by text, so
            # the engine still decides the exact cut edges from the audio.
            retake_detect_fn=labelled_restarts,
        )


def main(kit: Path, out: Path) -> None:
    from app.services.active_narration_source import NarrationTake

    upload = kit / "upload" / "S3"
    files = {mid: upload / name for mid, name in TAKES}
    bindings = {mid: binding(mid, files[mid]) for mid, _ in TAKES}
    durations = {mid: bindings[mid].original.duration_s for mid, _ in TAKES}

    # 1. The several-take analysis through the REAL assembly (`_analyze_takes`).
    by_path = {bindings[mid].proxy_path: mid for mid in bindings}
    results = {}

    def local_source(work, *, allow_silent=False):
        mid = by_path[work.source_storage_path]
        results[mid] = analyse_take(files[mid], durations[mid])
        return results[mid]

    takes = tuple(
        (
            NarrationTake(
                media_id=mid,
                storage_path=bindings[mid].proxy_path,
                generation="1",
                manifest_identity=mid,
                window_start_s=0.0,
                window_end_s=round(durations[mid], 6),
            ),
            None,
        )
        for mid, _ in TAKES
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(task_module, "_analyze_source", local_source)
        multi = task_module._analyze_takes(
            task_module._ClaimedWork(
                claim=ClaimedSpeechCleanupAnalysis(uuid.uuid4(), "t", FINGERPRINT),
                source_storage_path=takes[0][0].storage_path,
                source_generation="1",
                window_start_s=0.0,
                window_end_s=takes[0][0].window_end_s,
                engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
                detector_version=DETECTOR_VERSION,
                source_kind="embedded_spine",
                multi_take=True,
                takes=takes,
            )
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(multi.to_payload(), indent=1))
    first = takes[0][0]
    row = SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        analysis_payload=multi.to_payload(),
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        source_kind="embedded_spine",
        source_media_identity=first.media_id,
        source_storage_path=first.storage_path,
        source_generation="1",
        window_start_s=0.0,
        window_end_s=first.window_end_s,
        source_policy_fingerprint=FINGERPRINT,
    )
    multi_snapshot = analysis_snapshot(row)

    # Single-take control: take A's own analysis as today's schema-1 snapshot.
    a = results["takeA"]
    single_snapshot = {
        **multi_snapshot,
        "schema_version": 1,
        "analysis": a.to_payload(),
    }

    cases = {
        "s3_clean": (["takeA", "takeB", "takeC"], multi_snapshot, True),
        "s3_uncut": (["takeA", "takeB", "takeC"], None, True),
        # Take A (the one with a cut) played LAST: its cut lands on an extra speaker.
        "s3_clean_a_last": (["takeB", "takeC", "takeA"], multi_snapshot, True),
        "single_clean": (["takeA"], single_snapshot, False),
    }
    summary = {}
    for case, (mids, preflight, several) in cases.items():
        with pytest.MonkeyPatch.context() as mp:
            snapshot = {
                PHONE_SOURCES_FIELD: [
                    bindings[m].model_dump(mode="json") for m in mids
                ],
                "creator_generation_id": "generation",
            }
            all_candidates = {
                "clip_paths": [bindings[m].proxy_path for m in mids],
                "edit_format": "subtitled",
                "language": "en",
            }
            job, _session = _job_and_session(
                mp, assembly_plan=snapshot, all_candidates=all_candidates
            )
            mp.setattr(
                gb.settings, "phone_render_verified_features", _TALKING_HEAD_FEATURES
            )
            mp.setattr(gb.settings, "talking_multi_clip_enabled", True)
            mp.setattr(gb.settings, "talking_multi_take_cleanup_enabled", True)
            mp.setattr(gb.settings, "speech_cleanup_long_pauses_enabled", True)
            local = {m: str(files[m]) for m in mids}
            mp.setattr(
                gb,
                "_ingest_clips",
                lambda *a, _m=mids, _l=local, **k: {
                    "clip_metas": [_Meta(m, 5.0) for m in _m],
                    "clip_id_to_gcs": {m: bindings[m].proxy_path for m in _m},
                    "clip_id_to_local": dict(_l),
                    "probe_map": {
                        _l[m]: SimpleNamespace(duration_s=durations[m]) for m in _m
                    },
                    "hero": _Meta(_m[0], 5.0),
                },
                raising=False,
            )
            import app.pipeline.transcribe as transcribe_mod

            mp.setattr(
                transcribe_mod,
                "transcribe_whisper_cached",
                lambda p, **k: whisper_cpp(p),
                raising=False,
            )
            if preflight is not None:
                snapshot["speech_cleanup_contract"] = "required_v1"
                snapshot["_speech_cleanup_internal"] = {"preflight_snapshot": preflight}
            if several and preflight is None:
                snapshot["speech_cleanup_contract"] = "off_v1"
            gb._run_phone_subtitled_job(
                str(job.id), snapshot, all_candidates, ownership_epoch=3
            )
            assert job.status == "awaiting_device", job.status
            recipe = device_status(job, "subtitled").request.recipe
            variant = job.assembly_plan["variants"][0]
        directory = out / "cases" / case
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "recipe.json").write_text(recipe.model_dump_json())
        (directory / "assets.json").write_text(
            json.dumps(
                {
                    asset.id: str(files[asset.id])
                    if asset.id in files
                    else str(
                        Path(gb.__file__).parents[2]
                        / "assets"
                        / "fonts"
                        / asset.id.removeprefix("font-")
                    )
                    for asset in recipe.asset_manifest.assets
                },
                indent=2,
            )
        )
        (directory / "variant.json").write_text(
            json.dumps(
                {
                    k: variant.get(k)
                    for k in (
                        "caption_cues",
                        "silence_cut",
                        "phone_talking_multi_clip",
                        "_speech_cleanup_outcome_context",
                        "long_pauses",
                        "long_pauses_other_takes",
                    )
                },
                indent=1,
                default=str,
            )
        )
        main = next(t for t in recipe.tracks if t.id == "subtitled").clips
        summary[case] = {
            "duration_s": recipe.duration,
            "main": [
                (
                    c.id,
                    c.source_asset_id,
                    round(c.source_start, 3),
                    round(c.source_duration, 3),
                    round(c.timeline_start, 3),
                )
                for c in main
            ],
        }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser())
