#!/usr/bin/env python3
# ruff: noqa: E402 -- the harness sets sys.path and the environment before app imports.
"""KRI-1524 offline replay: Cusco DJI T1 "Good Take" (prod job d988bb99), tight pauses on two
takes recorded hot, with TIGHT_PAUSE_RELATIVE_FLOOR_ENABLED off (the incident) and on (the fix).

Inputs, all local and free (no model call):
  --kit        the kit folder holding airdrop/T1_01.mp4 and airdrop/T1_02.mp4 (the two takes the
               creator attached, in play order)
  --job-debug  the run's job_debug.json: each take's consented speech-cleanup cut (the plan the
               creator approved) and the caption-cue word stamps (output timeline; mapped back to
               each take's own timeline here, the stamps production's whisper-1 wrote)
  --model      optional whisper.cpp model: transcribes both outputs for a word-by-word diff
  --control    other local takes (repeatable): each one's verdict with the switch on and off, and,
               for a noisy one, the tight pass's own report and the reply line it produces (the
               wrong-content control: a restaurant take must stay untouched and say why)

Per arm it runs the REAL worker glue on each take, in play order, with the worker's several-take
arguments (`generative_build._phone_tight_pause_cut`, `_take_clip_budget`, the fallback
`_phone_long_pause_cut` + `_trim_take_joins` for a take the tight pass could not do, then
`_measure_take_join_gaps`), builds the finished-video receipt line from those reports
(`brief_checks.check_requirement`), renders the kept audio of both takes back to back with
ffmpeg, and measures the silent gaps of that audio with an INDEPENDENT check: the ticket's own
method (voice-band 300-3400 Hz level, 50 ms steps, quiet = under the file's 5th percentile +
10 dB). It writes `<out>/report.json` and the two WAVs (`<out>/off.wav`, `<out>/on.wav`).

Usage (repo root):
  KIT=~/Downloads/kria-cusco-dji-test/T1_two_takes_false_start
  src/apps/api/.venv/bin/python scripts/dev/kri1524_tight_pause_replay.py --kit $KIT \
      --job-debug $KIT/runs/2026-11-17_good_take/job_debug.json \
      --out .dev/kri1524 [--model ggml-large-v3-turbo-q5_0.bin] [--control OTHER_TAKE ...]
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for _name, _value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "OPENAI_API_KEY": "sk-test",
}.items():
    os.environ.setdefault(_name, _value)

import numpy as np

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import check_requirement, plan_facts_from_phone_variant
from app.pipeline.silence_cut import CutPlan, Removal
from app.pipeline.transcribe import Word
from app.tasks import generative_build as gb

SR = 16_000
TAKES = ("T1_01.mp4", "T1_02.mp4")
DURATIONS = (11.5, 19.9)
ASK = "Use only the good take and cut my pauses so it's tight. Turkish captions."
GAP_REPORT_S = 0.35  # report every gap the receipt's 0.35 s limit would count
GAP_TICKET_S = 0.5  # the ticket's table: gaps of 0.5 s or more


def _keep(removed: list[dict], duration: float) -> list[tuple[float, float]]:
    keep, cursor = [], 0.0
    for r in sorted(removed, key=lambda r: r["start_s"]):
        if r["start_s"] > cursor:
            keep.append((cursor, r["start_s"]))
        cursor = max(cursor, r["end_s"])
    if duration > cursor:
        keep.append((cursor, duration))
    return keep


def _consented(job_debug: dict) -> tuple[list[CutPlan], list[list[Word]]]:
    """Each take's consented cut and its words on its own timeline."""
    variant = job_debug["job"]["assembly_plan"]["variants"][0]
    removed = [
        variant["silence_cut"]["removed"],
        variant["phone_talking_multi_clip"]["speaker_removed"][0]["removed"],
    ]
    cues = [w for cue in variant["caption_cues"] for w in cue["words"]]
    plans: list[CutPlan] = []
    words: list[list[Word]] = []
    offset = 0.0
    for rows, duration in zip(removed, DURATIONS, strict=True):
        keep = _keep(rows, duration)
        plan = CutPlan(
            keep_segments=keep,
            removed=[Removal(r["start_s"], r["end_s"], r["reason"]) for r in rows],
            time_saved_s=sum(r["end_s"] - r["start_s"] for r in rows),
        )
        total = sum(b - a for a, b in keep)

        def source_t(x: float, keep=keep) -> float:
            acc = 0.0
            for a, b in keep:
                if x <= acc + (b - a) + 1e-6:
                    return a + (x - acc)
                acc += b - a
            return keep[-1][1]

        take_words = [
            Word(
                text=w["text"],
                start_s=round(source_t(w["start_s"] - offset), 3),
                end_s=round(source_t(w["end_s"] - offset), 3),
                confidence=1.0,
            )
            for w in cues
            if offset - 1e-3 <= w["start_s"] < offset + total - 1e-3
        ]
        plans.append(plan)
        words.append(take_words)
        offset += total
    return plans, words


def _run_arm(paths: list[str], plans: list[CutPlan], words: list[list[Word]]) -> dict:
    """The worker's several-take pause passes (tight on every take, fallback where it fell back)."""
    order = [None, "t2"]
    cut = {None: plans[0], "t2": plans[1]}
    reports: list[dict] = []
    fallback: set = set()
    for position, cid in enumerate(order):
        budget = gb._take_clip_budget(cut, order, cid)
        new_plan, report = gb._phone_tight_pause_cut(
            {},
            cut[cid],
            words[position],
            clip_path=paths[position],
            duration_s=DURATIONS[position],
            multi_take=True,
            trim_head=position > 0,
            trim_tail=position < len(order) - 1,
            max_keep_segments=budget,
        )
        reports.append({"take": position, **(report or {})})
        if gb._tight_pass_fell_back(report):
            fallback.add(cid)
            new_plan, _long = gb._phone_long_pause_cut(
                {},
                new_plan,
                words[position],
                clip_path=paths[position],
                duration_s=DURATIONS[position],
                ignore_tight_brief=True,
            )
        cut[cid] = new_plan
    trims: list[dict] = []
    if fallback:
        joined, rows = gb._trim_take_joins(
            [(cut[cid], paths[i], DURATIONS[i]) for i, cid in enumerate(order)]
        )
        for i, cid in enumerate(order):
            if cid in fallback:
                cut[cid] = joined[i]
        trims = [row for row in rows if order[row["take"]] in fallback]
    gaps = gb._measure_take_join_gaps(
        [(cut[cid], paths[i], DURATIONS[i]) for i, cid in enumerate(order)]
    )
    variant = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "ok": True,
        "render_status": "ready",
        "caption_cues": [{"start_s": 0.0, "end_s": 1.0, "text": "x"}],
        "silence_cut_outcome": "applied",
        "silence_cut": {"removed": [r.__dict__ for r in cut[None].removed]},
        "tight_pauses": {k: v for k, v in reports[0].items() if k != "take"},
        "tight_pauses_other_takes": reports[1:],
        "take_join_gaps": gaps,
    }
    req = BriefRequirement(
        id="r2",
        kind="audio",
        scope="global",
        description="cut my pauses so it's tight",
        facts={"speech_cleanup": True, "pauses": "tight"},
    )
    receipt = check_requirement(req, plan_facts_from_phone_variant(variant))
    return {
        "keep_segments": [
            [list(map(lambda x: round(x, 3), s)) for s in cut[c].keep_segments] for c in order
        ],
        "added_removed_ms": [r.get("added_removed_ms") for r in reports],
        "reports": reports,
        "join_trims": trims,
        "take_join_gaps": gaps,
        "receipt": {"status": receipt.status, "reason": receipt.reason},
    }


def _new_cuts(paths: list[str], before: list, after: list) -> list[dict]:
    """Every audio range the switch-on arm cut that the switch-off arm kept, with the loudest
    20 ms of it against the take's quiet floor (5th percentile of 20 ms frames): a cut that is
    silence sits within a few dB of the floor, a clipped word would read 20+ dB over it."""
    rows = []
    for take, (path, keep_off, keep_on) in enumerate(zip(paths, before, after, strict=True)):
        x = _decode(Path(path))
        win = int(0.02 * SR)
        hop = int(0.01 * SR)
        starts = np.arange(0, len(x) - win + 1, hop)
        csum = np.concatenate([[0.0], np.cumsum(x * x)])
        level = 10 * np.log10((csum[starts + win] - csum[starts]) / win + 1e-12)
        floor = float(np.percentile(level, 5))

        def kept(t: float, keep) -> bool:
            return any(a <= t < b for a, b in keep)

        t, cut_start = 0.0, None
        step = 0.001
        while t <= len(x) / SR:
            gone = kept(t, keep_off) and not kept(t, keep_on)
            if gone and cut_start is None:
                cut_start = t
            elif not gone and cut_start is not None:
                inside = level[(starts >= cut_start * SR) & (starts + win <= t * SR)]
                rows.append(
                    {
                        "take": take,
                        "start_s": round(cut_start, 3),
                        "end_s": round(t, 3),
                        "loudest_20ms_over_floor_db": round(float(inside.max()) - floor, 1)
                        if len(inside)
                        else None,
                    }
                )
                cut_start = None
            t += step
    return rows


def _render(paths: list[str], keeps: list[list[list[float]]], out: Path) -> None:
    """The kept audio of every take, back to back (16 kHz mono WAV)."""
    parts, labels = [], []
    for i, keep in enumerate(keeps):
        for j, (a, b) in enumerate(keep):
            label = f"a{i}_{j}"
            parts.append(f"[{i}:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[{label}]")
            labels.append(f"[{label}]")
    graph = ";".join(parts) + f";{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]"
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for p in paths:
        cmd += ["-i", p]
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", str(SR), str(out)]
    subprocess.run(cmd, check=True)


def _decode(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SR),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def independent_gaps(path: Path, end_s: float | None = None) -> dict:
    """The ticket's check: voice-band (300-3400 Hz) level in 50 ms steps, quiet = under the
    file's own 5th percentile + 10 dB; gaps between the first and last sound."""
    x = _decode(path)
    if end_s:
        x = x[: int(end_s * SR)]
    n = len(x)
    size = 1 << int(np.ceil(np.log2(n)))
    spectrum = np.fft.rfft(x, size)
    freqs = np.fft.rfftfreq(size, 1 / SR)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0
    y = np.fft.irfft(spectrum, size)[:n]
    step = int(0.05 * SR)
    usable = n - n % step
    level = 10 * np.log10(np.mean(y[:usable].reshape(-1, step) ** 2, axis=1) + 1e-12)
    floor = float(np.percentile(level, 5))
    quiet = level < floor + 10.0
    loud = np.where(~quiet)[0]
    first, last = (int(loud[0]), int(loud[-1])) if len(loud) else (0, 0)
    gaps, start = [], None
    for i, q in enumerate([*quiet.tolist(), False]):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if start > first and i <= last:
                gaps.append(
                    [round(start * 0.05, 2), round(i * 0.05, 2), round((i - start) * 0.05, 2)]
                )
            start = None
    return {
        "duration_s": round(n / SR, 2),
        "voice_band_floor_db": round(floor, 1),
        "gaps_over_0_35_s": [g for g in gaps if g[2] > GAP_REPORT_S + 1e-9],
        "gaps_0_5_s_or_more": [g for g in gaps if g[2] >= GAP_TICKET_S - 1e-9],
        "longest_gap_s": max((g[2] for g in gaps), default=0.0),
    }


def _transcribe(model: str, wav: Path) -> list[str]:
    out = subprocess.run(
        ["whisper-cli", "-m", model, "-l", "tr", "-nt", "-np", "-f", str(wav)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return re.findall(r"[\wçğıöşüÇĞİÖŞÜ']+", out.lower())


def _control(path: str) -> dict:
    """One take's verdict either way and, switch on, what the tight pass does with it."""
    from app.pipeline import audio_pcm
    from app.pipeline import tight_pause_cut as tpc

    samples = audio_pcm.decode_pcm_f32(path)
    duration = len(samples) / SR
    floor = tpc._room_floor_db(samples, SR)
    row = {
        "take": Path(path).name,
        "room_floor_dbfs": round(floor, 1),
        "speech_over_floor_db": round(tpc._speech_level_db(samples, SR) - floor, 1),
        "verdict_off": tpc.take_noise(samples, SR, relative=False),
        "verdict_on": tpc.take_noise(samples, SR, relative=True),
    }
    settings.tight_pause_relative_floor_enabled = True
    whole = CutPlan(keep_segments=[(0.0, duration)], removed=[], time_saved_s=0.0)
    plan, report = gb._phone_tight_pause_cut(
        {}, whole, [], clip_path=path, duration_s=duration, multi_take=True, max_keep_segments=85
    )
    row["switch_on_report"] = {k: report.get(k) for k in ("status", "reason", "added_cuts")}
    row["plan_untouched"] = plan is whole
    if report.get("reason") == "noisy_take":
        variant = {
            "resolved_archetype": "subtitled",
            "render_destination": "device",
            "ok": True,
            "render_status": "ready",
            "caption_cues": [{"start_s": 0.0, "end_s": 1.0, "text": "x"}],
            "silence_cut_outcome": "applied",
            "silence_cut": {"removed": []},
            "tight_pauses": report,
        }
        req = BriefRequirement(
            id="r2",
            kind="audio",
            scope="global",
            description="cut my pauses so it's tight",
            facts={"speech_cleanup": True, "pauses": "tight"},
        )
        receipt = check_requirement(req, plan_facts_from_phone_variant(variant))
        row["reply"] = {"status": receipt.status, "reason": receipt.reason}
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kit", required=True, type=Path)
    ap.add_argument("--job-debug", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model")
    ap.add_argument("--control", action="append", default=[])
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    paths = [str(args.kit / "airdrop" / name) for name in TAKES]
    plans, words = _consented(json.loads(args.job_debug.read_text(encoding="utf-8")))
    settings.tight_pause_cut_enabled = True
    settings.speech_cleanup_long_pauses_enabled = False  # production default (snapshot)
    report: dict = {
        "ticket": "KRI-1524",
        "incident": "prod job d988bb99-bf5c-4bc5-9e83-aa2ffae1ca76, server 80c96b04c",
        "ask": ASK,
        "takes": TAKES,
        "arms": {},
    }
    incident = args.kit / "runs" / "2026-11-17_good_take" / "kria_output.mp4"
    if incident.exists():
        # The tool against the run's own video (end card excluded): it must find the ticket's
        # seven 0.5-1.2 s gaps, or it does not measure what the ticket measured.
        report["incident_video_check"] = independent_gaps(incident, end_s=23.3)
    for arm, on in (("off", False), ("on", True)):
        settings.tight_pause_relative_floor_enabled = on
        result = _run_arm(paths, plans, words)
        wav = args.out / f"{arm}.wav"
        _render(paths, result["keep_segments"], wav)
        result["independent_check"] = independent_gaps(wav)
        report["arms"][arm] = result
    report["new_cuts"] = _new_cuts(
        paths, report["arms"]["off"]["keep_segments"], report["arms"]["on"]["keep_segments"]
    )
    if args.model:
        heard = {arm: _transcribe(args.model, args.out / f"{arm}.wav") for arm in ("off", "on")}
        diff = [
            op
            for op in difflib.SequenceMatcher(a=heard["off"], b=heard["on"]).get_opcodes()
            if op[0] != "equal"
        ]
        report["words"] = {
            "model": Path(args.model).name,
            "off": heard["off"],
            "on": heard["on"],
            "differences": [
                {"op": tag, "off": heard["off"][i1:i2], "on": heard["on"][j1:j2]}
                for tag, i1, i2, j1, j2 in diff
            ],
        }
    report["controls"] = [_control(path) for path in args.control]
    (args.out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for arm in ("off", "on"):
        r = report["arms"][arm]
        print(
            arm,
            r["receipt"],
            r["independent_check"]["gaps_0_5_s_or_more"],
            r["independent_check"]["longest_gap_s"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
