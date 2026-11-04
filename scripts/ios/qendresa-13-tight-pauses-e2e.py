#!/usr/bin/env python3
# ruff: noqa: E402 -- executable harness configures sys.path and environment before app imports.
"""Qendresa 13 proof: the same real talking take, rendered natively with and without tight pauses.

Two arms, both through the real iPhone engine (`DeviceMontageRenderE2ETests`):
  q13_consented  today's consented speech cleanup only
  q13_tight      consented cleanup + tight pauses (`settings.tight_pause_cut_enabled`)

`prepare` runs the REAL cleanup analysis engine and the REAL worker glue
(`generative_build._phone_tight_pause_cut`) on the take, compiles both phone recipes and writes
the fixture. The word stamps come from a LOCAL free transcriber (faster-whisper), a stand-in for
production's paid whisper-1 stamps; the file is passed in with --words. Nothing here calls a paid
model.

`compare` scores the two rendered MP4s with whisper.cpp: pauses measured the way the acceptance
tool measures them (PASS when the longest pause is <= 0.35 s), a word-by-word diff of consented vs
tight, and a hard negative control: one whole word removed from the consented render must be
heard as missing (exit 3 if not). Partial clips (0.4 s mid-word, last 0.15 s of a word) are
recorded in report.json as informational only: whisper cannot see partial clipping (it fills a
partly cut word in from context), which is why the cutter's quiet-floor rule and a listening
check exist.

Usage (repo root):
  src/apps/api/.venv/bin/python scripts/ios/qendresa-13-tight-pauses-e2e.py prepare OUT \
      --take VIDEO --words WORDS_JSON
  TEST_RUNNER_KRIA_E2E_DIR=OUT xcodebuild ... (printed by prepare)
  src/apps/api/.venv/bin/python scripts/ios/qendresa-13-tight-pauses-e2e.py compare OUT \
      --model ggml-model.bin

WORDS_JSON: {"language": "tr", "words": [{"text","start_s","end_s","confidence",
"segment_avg_logprob","segment_no_speech_prob"}, ...]}
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for _name, _value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(_name, _value)

PAUSE_LIMIT_S = 0.35
OUTRO_S = 1.6  # Kria brand outro: ignored by the pause check
MEDIA_ID = "q13-take"
TAKE_FILE = "q13-take.mp4"
WAV_FILE = "q13-take.wav"
SILENCE_FILTER = "highpass=f=90,silencedetect=n=-38dB:d=0.12"
CONTROL_CUT_S = 0.4
TAIL_CONTROL_CUT_S = 0.15  # the realistic failure: a word's last 0.15 s clipped


# --------------------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------------------


def _run(cmd: list[str], **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _duration(path: Path) -> float:
    return float(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nk=1:nw=1",
                str(path),
            ]
        ).stdout.strip()
    )


def _probe_video(path: Path) -> tuple[float, int, int, int, bool]:
    info = json.loads(
        _run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]
        ).stdout
    )["streams"]
    video = next(s for s in info if s.get("codec_type") == "video")
    has_audio = any(s.get("codec_type") == "audio" for s in info)
    rotation = next(
        (
            e.get("rotation", 0)
            for e in video.get("side_data_list", [])
            if "rotation" in e
        ),
        0,
    )
    return (
        _duration(path),
        int(video["width"]),
        int(video["height"]),
        int(rotation) % 360,
        has_audio,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _to_wav(src: Path, dst: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )


# --------------------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------------------


def prepare(args: argparse.Namespace) -> int:
    from app.config import settings
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.brief_binding import BriefBinding
    from app.kria.device_render import DeviceRenderStatus, make_device_request
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.pipeline.captions import PHONE_CUE_BUDGET, build_plain_cues
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.pipeline.silence_cut import is_filler_token, remap_words
    from app.pipeline.speech_cleanup_analysis import (
        SpeechCleanupAnalysisInput,
        analyze_speech_cleanup,
    )
    from app.pipeline.transcribe import Transcript, Word
    from app.services.phone_rollout import validate_phone_pilot_recipe
    from app.services.phone_sources import PhoneSourceBinding
    from app.services.speech_cleanup_preflight import (
        SPEECH_CLEANUP_MIXED_GAP_MODE,
        SPEECH_CLEANUP_OVER_BUDGET_POLICY,
    )
    from app.tasks import generative_build as gb

    take = Path(args.take).expanduser().resolve()
    words_path = Path(args.words).expanduser().resolve()
    if not take.is_file():
        raise SystemExit(f"missing take: {take}")
    if not words_path.is_file():
        raise SystemExit(f"missing words file: {words_path}")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    payload = json.loads(words_path.read_text())
    language = str(payload.get("language") or "")
    stand_in = Transcript(
        words=[
            Word(
                text=w["text"],
                start_s=float(w["start_s"]),
                end_s=float(w["end_s"]),
                confidence=float(w.get("confidence", 1.0)),
                segment_avg_logprob=w.get("segment_avg_logprob"),
                segment_no_speech_prob=w.get("segment_no_speech_prob"),
            )
            for w in payload["words"]
        ],
        full_text=" ".join(w["text"] for w in payload["words"]),
        language=language,
    )

    shutil.copy2(take, out / TAKE_FILE)
    wav = out / WAV_FILE
    _to_wav(take, wav)
    duration_s, width, height, orientation, has_audio = _probe_video(take)
    if not has_audio:
        raise SystemExit("the take has no audio track")
    wav_duration_s = _duration(wav)

    def local_transcribe(path, *, language=None, verbatim_prompt=None):  # noqa: ANN001, ARG001
        return stand_in

    def local_excerpt(path, *, start_s, end_s):  # noqa: ANN001, ARG001
        raise RuntimeError("excerpt re-transcription is not available offline")

    analysis_input = SpeechCleanupAnalysisInput(
        source_fingerprint=_sha256(take),
        local_media_path=str(wav),
        duration_s=wav_duration_s,
        detector_version="q13-local-replay",
        mixed_gap_mode=SPEECH_CLEANUP_MIXED_GAP_MODE,
        include_silence_and_fillers=True,
        over_budget_policy=SPEECH_CLEANUP_OVER_BUDGET_POLICY,
        max_removal_frac_required=settings.speech_cleanup_max_removal_frac_required,
        filler_check=settings.speech_cleanup_filler_check_enabled,
    )
    result = analyze_speech_cleanup(
        analysis_input, transcribe_fn=local_transcribe, excerpt_fn=local_excerpt
    )
    consented_plan = result.cut_plan.to_cut_plan()
    snapshot_words = result.timed_words

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="tight",
                kind="audio",
                scope="global",
                description="Cut out all my pauses so it's tight",
                facts={"speech_cleanup": True, "pauses": "tight"},
            )
        ],
    )
    binding = BriefBinding.create(
        uuid.UUID("0b1c3a52-7d52-4b3e-9a0e-4d2f1d6c13a1"), brief
    )
    snapshot = {"creator_brief_binding": binding.model_dump(mode="json")}

    settings.tight_pause_cut_enabled = True
    tight_plan, tight_report = gb._phone_tight_pause_cut(
        snapshot,
        consented_plan,
        snapshot_words,
        clip_path=str(wav),
        duration_s=wav_duration_s,
    )
    if tight_report is None:
        print(
            "tight pauses did not run (report is None): the ask or the flag was not seen"
        )
        return 2

    source = PhoneSourceBinding(
        media_id=MEDIA_ID,
        proxy_path="users/q13/analysis-proxy-q13-take.mp4",
        generation="q13-replay",
        original=OriginalMediaDescriptor(
            sha256=_sha256(take),
            byte_count=take.stat().st_size,
            duration_s=duration_s,
            width=width,
            height=height,
            orientation_degrees=orientation,
            has_audio=True,
        ),
    )

    def captions(plan) -> list[dict]:  # noqa: ANN001
        # The phone Talking worker's path: snapshot words through the plan, fillers dropped,
        # raw float times, word captions.
        cut_words = [
            Word(text=i["text"], start_s=i["start_s"], end_s=i["end_s"], confidence=1.0)
            for i in remap_words(snapshot_words, plan)
            if not is_filler_token(i["text"])
        ]
        return build_plain_cues(cut_words, attach_words=True, budget=PHONE_CUE_BUDGET)

    arms = {"q13_consented": consented_plan, "q13_tight": tight_plan}
    recipes = {}
    for case_id, plan in arms.items():
        recipes[case_id] = compile_phone_subtitled_plan(
            (source,), caption_cues=captions(plan), caption_style="word", cut_plan=plan
        )
    union = set()
    for recipe in recipes.values():
        union |= set(recipe.required_capabilities)
    settings.phone_render_verified_features = sorted(union)
    for recipe in recipes.values():
        validate_phone_pilot_recipe(recipe)

    cases = {}
    for index, (case_id, recipe) in enumerate(recipes.items()):
        status_file = f"status-{case_id.replace('_', '-')}.json"
        request = make_device_request(
            job_id=uuid.UUID(f"d83cb0f5-d145-43dc-8f80-a0fa7b0a08e{index}"),
            variant_id=case_id,
            revision=1,
            recipe=recipe,
        )
        (out / status_file).write_text(
            DeviceRenderStatus(
                phase="awaiting_device", request=request
            ).model_dump_json(indent=2)
        )
        cases[case_id] = {
            "status_file": status_file,
            "duration_s": recipe.duration,
            "required_capabilities": sorted(recipe.required_capabilities),
            "drop_capability": "animatedText",
            "clips": [{"media_id": MEDIA_ID, "file": TAKE_FILE}],
            "music_asset_id": None,
            "music_file": None,
            "expects_source_audio": True,
            "expects_music_audio": False,
            "expects_narration_audio": False,
            "samples": [],
            "caption_samples": [],
            "removed_s": round(duration_s - recipe.duration, 3),
        }

    e2e = {
        "word_stamps": "local faster-whisper stand-in",
        "language": language,
        "take_duration_s": duration_s,
        "verified_features": sorted(union),
        "tight_report": tight_report,
        "plans": {
            "consented": {
                "keep_segments": len(consented_plan.keep_segments),
                "removed": len(consented_plan.removed),
            },
            "tight": {
                "keep_segments": len(tight_plan.keep_segments),
                "removed": len(tight_plan.removed),
            },
        },
        "cases": cases,
    }
    (out / "e2e.json").write_text(json.dumps(e2e, indent=2, default=str) + "\n")

    print(f"wrote Qendresa 13 fixture to {out}")
    print(
        f"  take {duration_s:.2f}s, language={language!r}, words={len(snapshot_words)}"
    )
    consented_s = recipes["q13_consented"].duration
    print(f"  consented plan: {e2e['plans']['consented']}, recipe {consented_s:.2f}s")
    print(
        f"  tight plan:     {e2e['plans']['tight']}, recipe {recipes['q13_tight'].duration:.2f}s"
    )
    print(f"  tight report: {json.dumps(tight_report, default=str)}")
    print("Render both arms on a simulator from src/apps/ios:")
    print(
        f"  TEST_RUNNER_KRIA_E2E_DIR={out} xcodebuild -project Kria.xcodeproj -scheme Kria "
        "-skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO "
        '-destination "platform=iOS Simulator,name=<iPhone>" '
        "-only-testing:KriaTests/DeviceMontageRenderE2ETests/testQendresa13ConsentedCleanupReplay "
        "-only-testing:KriaTests/DeviceMontageRenderE2ETests/testQendresa13TightPausesReplay test"
    )
    print(f"Then: compare {out} --model <ggml model>. MP4s land in {out / 'frames'}.")
    return 0


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(
        r"[^\w']", "", text.lower().replace("’", "'"), flags=re.UNICODE
    ).replace("_", "")


def _whisper_words(wav: Path, model: Path, work: Path) -> list[dict]:
    base = work / (wav.stem + "-ww")
    _run(
        [
            "whisper-cli",
            "-m",
            str(model),
            "-f",
            str(wav),
            "-l",
            "auto",
            "-ml",
            "1",
            "-sow",
            "-oj",
            "-of",
            str(base),
        ]
    )
    data = json.loads(Path(str(base) + ".json").read_text())
    words = []
    for item in data.get("transcription", []):
        text = _norm(item.get("text", ""))
        if not text:
            continue
        words.append(
            {
                "text": text,
                "start_s": item["offsets"]["from"] / 1000.0,
                "end_s": item["offsets"]["to"] / 1000.0,
            }
        )
    return words


def _pauses(wav: Path, words: list[dict], duration_s: float) -> dict:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(wav),
            "-af",
            SILENCE_FILTER,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    starts = [float(m) for m in re.findall(r"silence_start: (-?[\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    spans = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration_s
        spans.append((max(0.0, start), end))
    if not words:
        return {
            "longest_s": 0.0,
            "over_limit": [],
            "all": [],
            "pass": False,
            "note": "no words",
        }
    lo = words[0]["start_s"] + 0.2
    hi = words[-1]["end_s"] - 0.2
    kept = [
        {"start_s": round(s, 3), "end_s": round(e, 3), "duration_s": round(e - s, 3)}
        for s, e in spans
        if s >= lo and s < hi and s < duration_s - OUTRO_S
    ]
    longest = max((p["duration_s"] for p in kept), default=0.0)
    return {
        "longest_s": longest,
        "over_limit": [p for p in kept if p["duration_s"] > PAUSE_LIMIT_S],
        "all": kept,
        "pass": longest <= PAUSE_LIMIT_S,
    }


def _word_diff(before: list[dict], after: list[dict]) -> list[dict]:
    a = [w["text"] for w in before]
    b = [w["text"] for w in after]
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=a, b=b, autojunk=False
    ).get_opcodes():
        if tag not in {"delete", "replace"}:
            continue
        ops.append(
            {
                "op": tag,
                "consented": [
                    {"text": w["text"], "start_s": w["start_s"], "end_s": w["end_s"]}
                    for w in before[i1:i2]
                ],
                "tight": [
                    {"text": w["text"], "start_s": w["start_s"], "end_s": w["end_s"]}
                    for w in after[j1:j2]
                ],
            }
        )
    return ops


def _negative_control(
    consented_wav: Path,
    words: list[dict],
    model: Path,
    work: Path,
    *,
    mode: str = "word",
) -> dict:
    """Cut audio out of the consented render and see whether the word diff notices.

    ``word`` (the hard control): one WHOLE word (its span +-30 ms), a word of >= 3 letters from
    the middle of the render, chosen deterministically. ``mid`` / ``tail``: partial clips,
    informational only."""
    dur = _duration(consented_wav)
    candidates = [w for w in words if w["start_s"] > 1.0 and w["end_s"] < dur - 2.0]
    if mode == "word":
        candidates = [w for w in candidates if len(_norm(w["text"])) >= 3]
    if not candidates:
        return {"mode": mode, "caught": False, "note": "no usable word for the control"}
    if mode == "word":
        pick = candidates[len(candidates) // 2]
        cut_a, cut_b = pick["start_s"] - 0.03, pick["end_s"] + 0.03
    else:
        pick = max(candidates, key=lambda w: w["end_s"] - w["start_s"])
        if mode == "tail":
            cut_a, cut_b = pick["end_s"] - TAIL_CONTROL_CUT_S, pick["end_s"]
        else:
            mid = (pick["start_s"] + pick["end_s"]) / 2
            cut_a, cut_b = mid - CONTROL_CUT_S / 2, mid + CONTROL_CUT_S / 2
    cut_a = max(0.0, cut_a)
    control = work / f"control_{mode}.wav"
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(consented_wav),
            "-filter_complex",
            f"[0:a]atrim=0:{cut_a:.3f},asetpts=PTS-STARTPTS[a];"
            f"[0:a]atrim={cut_b:.3f},asetpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=0:a=1[o]",
            "-map",
            "[o]",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(control),
        ]
    )
    control_words = _whisper_words(control, model, work)
    ops = _word_diff(words, control_words)
    hits = [
        op
        for op in ops
        if any(w["end_s"] > cut_a and w["start_s"] < cut_b for w in op["consented"])
    ]
    return {
        "mode": mode,
        "caught": bool(hits),
        "cut_word": pick["text"],
        "cut_span_s": [round(cut_a, 3), round(cut_b, 3)],
        "ops_at_cut": hits,
    }


def compare(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    model = Path(args.model).expanduser().resolve()
    if not model.is_file():
        raise SystemExit(f"missing whisper.cpp model: {model}")
    if shutil.which("whisper-cli") is None:
        raise SystemExit("whisper-cli is not on PATH")
    frames = out / "frames"
    videos = sorted(frames.glob("q13_*.mp4"))
    by_case = {v.stem: v for v in videos}
    missing = [c for c in ("q13_consented", "q13_tight") if c not in by_case]
    report: dict = {"cases": {}, "missing": missing}
    prepared = out / "e2e.json"
    if prepared.is_file():
        report["tight_report"] = json.loads(prepared.read_text()).get("tight_report")
    if missing:
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"missing rendered video(s): {missing}")
        return 2

    with tempfile.TemporaryDirectory(prefix="q13-compare-") as tmp:
        work = Path(tmp)
        words_by_case: dict[str, list[dict]] = {}
        consented_wav = work / "q13_consented.wav"
        for case_id, video in by_case.items():
            wav = work / f"{case_id}.wav"
            _to_wav(video, wav)
            dur = _duration(video)
            words = _whisper_words(wav, model, work)
            words_by_case[case_id] = words
            report["cases"][case_id] = {
                "video": str(video),
                "duration_s": dur,
                "word_count": len(words),
                "pauses": _pauses(wav, words, dur),
            }
        report["word_diff"] = _word_diff(
            words_by_case["q13_consented"], words_by_case["q13_tight"]
        )
        report["control"] = _negative_control(
            consented_wav, words_by_case["q13_consented"], model, work
        )
        # Informational only: whisper fills in a partly cut word from context.
        for mode in ("mid", "tail"):
            report[f"control_{mode}"] = _negative_control(
                consented_wav, words_by_case["q13_consented"], model, work, mode=mode
            )

    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    for case_id, info in report["cases"].items():
        pauses = info["pauses"]
        verdict = "PASS" if pauses["pass"] else "FAIL"
        print(
            f"{case_id}: {info['duration_s']:.2f}s, {info['word_count']} words, "
            f"longest pause {pauses['longest_s']:.3f}s -> {verdict}; "
            f"over {PAUSE_LIMIT_S}s: {[p['duration_s'] for p in pauses['over_limit']]}"
        )
    print(f"word diffs (consented -> tight): {len(report['word_diff'])}")
    for op in report["word_diff"]:
        before = " ".join(f"{w['text']}@{w['start_s']:.2f}" for w in op["consented"])
        after = " ".join(f"{w['text']}@{w['start_s']:.2f}" for w in op["tight"])
        print(f"  {op['op']}: [{before}] -> [{after}]")
    control = report["control"]
    print(f"whole-word control caught: {control['caught']} ({control.get('cut_word')})")
    for mode in ("mid", "tail"):
        info = report[f"control_{mode}"]
        print(
            f"partial control {mode} caught: {info['caught']} ({info.get('cut_word')}) "
            "[informational]"
        )
    print(f"wrote {out / 'report.json'}")
    if not control["caught"]:
        return 3
    if not report["cases"]["q13_tight"]["pauses"]["pass"]:
        print("q13_tight FAILED the pause rule")
        return 4
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("out")
    p.add_argument("--take", required=True)
    p.add_argument("--words", required=True)
    p.set_defaults(fn=prepare)
    c = sub.add_parser("compare")
    c.add_argument("out")
    c.add_argument("--model", required=True)
    c.set_defaults(fn=compare)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
