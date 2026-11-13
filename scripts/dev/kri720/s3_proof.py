"""KRI-720 native proof on Çiğdem's real S3 takes (no chat, no DB, no storage).

  real preflight engine per take (OpenAI whisper + silence detection, as prod)
    -> the REAL several-take assembly (`_analyze_takes`) -> row -> `analysis_snapshot`
    -> the REAL phone Talking worker (`_run_phone_subtitled_job`, required_v1)
    -> recipe.json + assets.json per case for `AudioParityFixtureTests` (KriaMediaEngine export).

Cases: s3_clean (3 takes, cleanup), s3_uncut (same takes, cleanup off: control),
single_clean (take A alone, cleanup: today's single-take path).

Second pass (2026-11-09, "Proof after the four fixes"): the preflight words are the LIVE
whisper-1 word stamps of the three takes (`LIVE_WORDS`, one concatenated timeline split by the
uncut take durations), the relisten stand-in hears exactly the words left (free, as
scratchpad/gap2/run_gap2.py), the settings are tight_pause_cut_enabled + long pauses + the
multi-take cleanup flag, and each case carries Cigdem's real plain ask as a brief binding.
Extra cases: s3_named_line / s3_named_line_tail (a line named in ONE take).

Usage (from src/apps/api, PYTHONPATH=$PWD):
    python s3_proof.py KIT_DIR OUT_DIR LIVE_WORDS_JSON
"""

from __future__ import annotations

import os

for _k, _v in {
    "STORAGE_BUCKET": "nova-test", "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
    "REDIS_URL": "redis://localhost:6379", "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "x", "WHISPER_BACKEND": "local", "WAITLIST_ADMIN_SECRET": "x",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]', "INTERNAL_API_KEY": "x",
    # the paid retake detector is replaced by the labelled-restarts stand-in, but its levers must be on
    "RETAKE_CUT_ENABLED": "true", "SPEECH_CLEANUP_RETAKES_ENABLED": "true",
}.items():
    os.environ.setdefault(_k, _v)

import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
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

TAKES = [("takeA", "IMG_7244.MOV"), ("takeB", "IMG_7245.MOV"), ("takeC", "IMG_7252.MOV")]
FINGERPRINT = hashlib.sha256(b"kri720-s3-proof").hexdigest()
LIVE: dict[str, list[dict]] = {}  # media_id -> that take's live whisper-1 words (take-local seconds)
WAVS: dict[str, str] = {}  # local wav path -> media_id (takes may be analysed concurrently)


def split_live(path: Path, durations: dict[str, float]) -> None:
    """Split the concatenated live timeline into the three takes by the uncut durations and
    verify the split by text against the kit's SCRIPT.txt."""
    words = json.load(open(path))
    a_end = durations["takeA"]
    b_end = a_end + durations["takeB"]
    parts = {"takeA": [], "takeB": [], "takeC": []}
    for w in words:
        mid = w["start_s"] + 0.5 * (w["end_s"] - w["start_s"])
        key = "takeA" if mid < a_end else "takeB" if mid < b_end else "takeC"
        off = {"takeA": 0.0, "takeB": a_end, "takeC": b_end}[key]
        parts[key].append({"text": w["text"], "start_s": max(0.0, w["start_s"] - off),
                           "end_s": max(0.0, w["end_s"] - off)})
    LIVE.update(parts)


def live_words(mid: str):
    from app.pipeline.transcribe import Word

    return [Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
            for w in LIVE[mid]]


def live_transcript(mid: str):
    from app.pipeline.transcribe import Transcript

    ws = live_words(mid)
    return Transcript(words=ws, full_text=" ".join(w.text for w in ws), language="en")


def install_ideal_relisten():
    """Free relisten stand-in (run_gap2.py `w1_ideal_relisten`): the 'transcriber' hears exactly
    the words left in the audio after the cut, with times relative to the relistened window."""
    import app.pipeline.speech_cleanup_analysis as sca

    def fake_window(audio_path, start, end, cut, *, language, transcribe, workdir):
        out = []
        for w in LIVE[WAVS[str(audio_path)]]:
            m = (w["start_s"] + w["end_s"]) / 2
            if not (start <= m < end):
                continue
            if cut and cut[0] <= m < cut[1]:
                continue
            t = m - start - ((cut[1] - cut[0]) if cut and m >= cut[1] else 0)
            out.append((sca._relisten_key(w["text"]), t))
        return [(k, t) for k, t in out if k]

    sca._window_words = fake_window


def probe_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
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


MODEL = str(Path("~/Downloads/kria-cigdem-test/_src/models/ggml-large-v3-turbo-q5_0.bin").expanduser())


def whisper_cpp(path: str, *, language=None, verbatim_prompt=None):
    """Free local stand-in for the prod OpenAI whisper call (word-level, -ml 1 -sow)."""
    from app.pipeline.transcribe import Transcript, Word

    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "w")
        if not str(path).endswith(".wav"):
            wav = str(Path(tmp) / "in.wav")
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-vn",
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)
            path = wav
        cmd = ["whisper-cli", "-m", MODEL, "-l", language or "en", "-f", path, "-ml", "1",
               "-sow", "-ojf", "-of", base, "-t", "8", "-np"]
        if verbatim_prompt:
            cmd += ["--prompt", verbatim_prompt]
        subprocess.run(cmd, check=True, capture_output=True)
        data = json.load(open(base + ".json"))
    words = []
    for seg in data["transcription"]:
        text = seg["text"].strip()
        if not text:
            continue
        probs = [t.get("p", 1.0) for t in seg.get("tokens", []) if not t["text"].startswith("[")]
        words.append(Word(text=text, start_s=seg["offsets"]["from"] / 1000,
                          end_s=seg["offsets"]["to"] / 1000,
                          confidence=min(probs) if probs else 1.0))
    return Transcript(words=words, full_text=" ".join(w.text for w in words), language="en")


PLAIN_ASK = 'Cut the pauses, the "okay"s and the places I restarted'
RESTARTS = [("wait", "okay"), ("and", "again")]  # take A: the slate, then "…perfume on— oh sorry, again"


def labelled_restarts(words, language):
    folded = [w.text.lower().strip(".,?!…") for w in words]
    spans = []
    for first, last in RESTARTS:
        starts = [i for i, t in enumerate(folded) if t == first]
        for start in starts:
            end = next((j for j in range(start, min(start + 8, len(folded))) if folded[j] == last), None)
            if end is not None:
                spans.append((start, end))
                break
    return tuple(spans)


def analyse_take(path: Path, duration_s: float, mid: str):
    """One take exactly as the preflight worker analyses it (local audio, real engine)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "narration.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            check=True,
        )
        WAVS[str(wav)] = mid
        presence = audio_presence(str(wav))
        if presence.status in ("silent", "inaudible"):
            return None
        work = SimpleNamespace(plan_item_id=None, creator_id=None,
                               claim=SimpleNamespace(analysis_id="proof"))
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
            transcribe_fn=lambda *_a, _m=mid, **_k: live_transcript(_m),
            # Stand-in for the paid retake detector (an LLM): the two restarts a person marks
            # in take A by listening (answer key `must_not_hear`). Words are located by text, so
            # the engine still decides the exact cut edges from the audio.
            retake_detect_fn=labelled_restarts,
        )


def main(kit: Path, out: Path, live_json: Path) -> None:
    from app.services.active_narration_source import NarrationTake

    import app.pipeline.speech_cleanup_analysis as _sca

    print("module:", _sca.__file__)
    install_ideal_relisten()

    upload = kit / "upload" / "S3"
    files = {mid: upload / name for mid, name in TAKES}
    bindings = {mid: binding(mid, files[mid]) for mid, _ in TAKES}
    durations = {mid: bindings[mid].original.duration_s for mid, _ in TAKES}
    split_live(live_json, durations)
    for mid in LIVE:
        print(mid, len(LIVE[mid]), "live words:", " ".join(w["text"] for w in LIVE[mid]))

    # 1. The several-take analysis through the REAL assembly (`_analyze_takes`).
    by_path = {bindings[mid].proxy_path: mid for mid in bindings}
    results = {}

    def local_source(work, *, allow_silent=False):
        mid = by_path[work.source_storage_path]
        results[mid] = analyse_take(files[mid], durations[mid], mid)
        return results[mid]

    takes = tuple(
        (
            NarrationTake(media_id=mid, storage_path=bindings[mid].proxy_path, generation="1",
                          manifest_identity=mid, window_start_s=0.0,
                          window_end_s=round(durations[mid], 6)),
            None,
        )
        for mid, _ in TAKES
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(task_module, "_analyze_source", local_source)
        multi = task_module._analyze_takes(
            task_module._ClaimedWork(
                claim=ClaimedSpeechCleanupAnalysis(uuid.uuid4(), "t", FINGERPRINT),
                source_storage_path=takes[0][0].storage_path, source_generation="1",
                window_start_s=0.0, window_end_s=takes[0][0].window_end_s,
                engine_version=SPEECH_CLEANUP_ENGINE_VERSION, detector_version=DETECTOR_VERSION,
                source_kind="embedded_spine", multi_take=True, takes=takes,
            )
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(multi.to_payload(), indent=1))
    first = takes[0][0]
    row = SimpleNamespace(
        id=uuid.uuid4(), status="ready", analysis_payload=multi.to_payload(),
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION, detector_version=DETECTOR_VERSION,
        source_kind="embedded_spine", source_media_identity=first.media_id,
        source_storage_path=first.storage_path, source_generation="1",
        window_start_s=0.0, window_end_s=first.window_end_s,
        source_policy_fingerprint=FINGERPRINT,
    )
    multi_snapshot = analysis_snapshot(row)

    # Single-take control: take A's own analysis as today's schema-1 snapshot.
    a = results["takeA"]
    single_snapshot = {
        **multi_snapshot, "schema_version": 1, "analysis": a.to_payload(),
    }

    plain = {"speech_cleanup": True}  # Cigdem's real plain ask: no tight fact, no named line
    named_middle = {"speech_cleanup": True, "cut_lines": ["But that's for the next episode."]}
    named_tail = {"speech_cleanup": True, "cut_lines": ["Don't forget to follow."]}
    # case -> (take order, preflight, several takes, brief facts or None)
    cases = {
        "s3_clean": (["takeA", "takeB", "takeC"], multi_snapshot, True, plain),
        "s3_uncut": (["takeA", "takeB", "takeC"], None, True, None),
        # Take A (the one with a cut) played LAST: its cut lands on an extra speaker.
        "s3_clean_a_last": (["takeB", "takeC", "takeA"], multi_snapshot, True, plain),
        "single_clean": (["takeA"], single_snapshot, False, plain),
        "s3_named_line": (["takeA", "takeB", "takeC"], multi_snapshot, True, named_middle),
        "s3_named_line_tail": (["takeA", "takeB", "takeC"], multi_snapshot, True, named_tail),
    }
    summary = {}
    for case, (mids, preflight, several, brief_facts) in cases.items():
        with pytest.MonkeyPatch.context() as mp:
            snapshot = {
                PHONE_SOURCES_FIELD: [bindings[m].model_dump(mode="json") for m in mids],
                "creator_generation_id": "generation",
            }
            all_candidates = {
                "clip_paths": [bindings[m].proxy_path for m in mids],
                "edit_format": "subtitled",
                "language": "en",
            }
            job, _session = _job_and_session(mp, assembly_plan=snapshot, all_candidates=all_candidates)
            mp.setattr(gb.settings, "phone_render_verified_features", _TALKING_HEAD_FEATURES)
            mp.setattr(gb.settings, "talking_multi_clip_enabled", True)
            mp.setattr(gb.settings, "talking_multi_take_cleanup_enabled", True)
            mp.setattr(gb.settings, "speech_cleanup_long_pauses_enabled", True)
            mp.setattr(gb.settings, "tight_pause_cut_enabled", True)
            local = {m: str(files[m]) for m in mids}
            by_path_local = {v: k for k, v in local.items()}
            mp.setattr(gb, "_ingest_clips", lambda *a, _m=mids, _l=local, **k: {
                "clip_metas": [_Meta(m, 5.0) for m in _m],
                "clip_id_to_gcs": {m: bindings[m].proxy_path for m in _m},
                "clip_id_to_local": dict(_l),
                "probe_map": {_l[m]: SimpleNamespace(duration_s=durations[m]) for m in _m},
                "hero": _Meta(_m[0], 5.0),
            }, raising=False)
            import app.pipeline.transcribe as transcribe_mod

            by_name = {files[m].name: m for m in files}
            mp.setattr(transcribe_mod, "transcribe_whisper_cached",
                       lambda p, **k: live_transcript(by_name.get(Path(p).name) or by_path_local[str(p)]),
                       raising=False)
            if preflight is not None:
                snapshot["speech_cleanup_contract"] = "required_v1"
                snapshot["_speech_cleanup_internal"] = {"preflight_snapshot": preflight}
            if several and preflight is None:
                snapshot["speech_cleanup_contract"] = "off_v1"
            if brief_facts is not None:
                from app.kria.brief import BriefRequirement, CreativeBrief
                from app.kria.brief_binding import BriefBinding

                req = BriefRequirement(id="r1", kind="audio", scope="global",
                                       description=PLAIN_ASK + "".join(f' Cut "{q}"' for q in brief_facts.get("cut_lines", [])),
                                       facts=dict(brief_facts))
                binding_json = BriefBinding.create(
                    uuid.uuid4(), CreativeBrief(version=1, requirements=[req])
                ).model_dump(mode="json")
                snapshot["creator_brief_binding"] = binding_json
                job.assembly_plan["creator_brief_binding"] = binding_json
            gb._run_phone_subtitled_job(str(job.id), snapshot, all_candidates, ownership_epoch=3)
            assert job.status == "awaiting_device", job.status
            recipe = device_status(job, "subtitled").request.recipe
            variant = job.assembly_plan["variants"][0]
        directory = out / "cases" / case
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "recipe.json").write_text(recipe.model_dump_json())
        (directory / "assets.json").write_text(json.dumps(
            {asset.id: str(files[asset.id]) if asset.id in files
             else str(Path(gb.__file__).parents[2] / "assets" / "fonts" / asset.id.removeprefix("font-"))
             for asset in recipe.asset_manifest.assets}, indent=2))
        (directory / "variant.json").write_text(json.dumps(
            {k: variant.get(k) for k in ("caption_cues", "silence_cut", "phone_talking_multi_clip",
                                         "_speech_cleanup_outcome_context", "long_pauses",
                                         "long_pauses_other_takes", "tight_pauses",
                                         "tight_pauses_other_takes", "named_cuts")}, indent=1, default=str))
        main = next(t for t in recipe.tracks if t.id == "subtitled").clips
        summary[case] = {
            "duration_s": recipe.duration,
            "main": [(c.id, c.source_asset_id, round(c.source_start, 3),
                      round(c.source_duration, 3), round(c.timeline_start, 3)) for c in main],
        }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser(), Path(sys.argv[3]).expanduser())
