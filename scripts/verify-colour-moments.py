#!/usr/bin/env python3
"""Measure an MP4 frame by frame and check its colour moments (KRI-1350).

A colour moment is a window of the finished video drawn in another tone
(black-and-white, warm, faded) whose edges are a cut, a white flash, a warm
light-leak flash or a dip to black, optionally with a sound effect that lands on
the edge, plus an optional fade to black at the very end.

The checker never trusts the recipe or the renderer. It decodes the pixels and the
audio with ffmpeg and finds the events itself:

* flash      a run of frames whose mean luma rises well above the frames around it
* dip        a run of frames whose mean luma falls well below the frames around it
* mono       a run of frames whose mean saturation is near zero
* end fade   the last frames ramping down to black
* sound hit  a frame whose audio level jumps well above the frames before it

``measure`` prints those events; ``check`` compares them with an expectation file
(either a measured answer key, or one written by ``expect-from-recipe``) and exits
1 when any event is missing or more than ``--tolerance`` frames off.

Stdlib only; needs ffmpeg and ffprobe on PATH.

Usage:
  scripts/verify-colour-moments.py measure VIDEO.mp4 [--json out.json]
  scripts/verify-colour-moments.py expect-from-recipe RECIPE.json --fps 30 > expect.json
  scripts/verify-colour-moments.py check VIDEO.mp4 --expect expect.json \
      [--tolerance 1] [--out .colour-moments-verify]
"""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Thresholds are in 8-bit signalstats units (YAVG 0-255, SATAVG 0-~181).
FLASH_RISE = 35.0  # luma above the local baseline that counts as a flash frame
DIP_FALL = 35.0  # luma below the local baseline that counts as a dip frame
MONO_SAT = 6.0  # mean saturation at or below this is black-and-white
END_BLACK_Y = 24.0  # last frame must be at least this dark for an end fade
SOUND_JUMP_DB = 12.0  # level jump over the previous frames that counts as a hit
SOUND_FLOOR_DB = -45.0  # ignore jumps that stay quieter than this
BASELINE_FRAMES = 15  # frames either side used for the local luma baseline


@dataclass
class Event:
    kind: str  # flash | dip | mono | end_fade | sound
    start: int  # first frame (inclusive)
    end: int  # last frame (inclusive)
    peak: int | None = None  # brightest (flash) / darkest (dip) / loudest (sound)
    detail: dict = field(default_factory=dict)

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def probe_fps(video: Path) -> float:
    out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(video),
        ]
    )
    stream = json.loads(out)["streams"][0]
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = stream.get(key, "0/0").partition("/")
        if den and float(den) and float(num):
            return float(num) / float(den)
    raise SystemExit(f"cannot read the frame rate of {video}")


def measure_frames(video: Path) -> tuple[list[float], list[float]]:
    """Per-frame mean luma and mean saturation, in decode order."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-vf",
            "scale=270:-2,signalstats,metadata=print:file=-",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    luma: list[float] = []
    sat: list[float] = []
    for line in out.splitlines():
        m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", line)
        if m:
            luma.append(float(m.group(1)))
        m = re.search(r"lavfi\.signalstats\.SATAVG=([\d.]+)", line)
        if m:
            sat.append(float(m.group(1)))
    if not luma or len(luma) != len(sat):
        raise SystemExit(f"no video frames measured in {video}")
    return luma, sat


def has_audio(video: Path) -> bool:
    out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video),
        ]
    )
    return bool(out.strip())


def measure_audio(video: Path, fps: float, frames: int) -> list[float]:
    """Per-video-frame RMS level in dBFS of the mixed-down audio."""
    if not has_audio(video):
        return []
    rate = 48000
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    hop = rate / fps
    levels: list[float] = []
    for f in range(frames):
        a, b = int(round(f * hop)), int(round((f + 1) * hop))
        chunk = samples[a:b]
        if not chunk:
            levels.append(-120.0)
            continue
        power = sum(x * x for x in chunk) / len(chunk) / (32768.0**2)
        levels.append(10 * math.log10(power + 1e-12))
    return levels


def _runs(flags: list[bool], min_len: int = 1) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for i, on in enumerate(flags + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= min_len:
                runs.append((start, i - 1))
            start = None
    return runs


def _baseline(values: list[float]) -> list[float]:
    """Median of the frames around each frame, a flash's own frames excluded by width."""
    base = []
    n = len(values)
    for i in range(n):
        lo = max(0, i - BASELINE_FRAMES - 6)
        hi = min(n, i + BASELINE_FRAMES + 7)
        around = values[lo : max(lo, i - 6)] + values[min(hi, i + 7) : hi]
        base.append(statistics.median(around) if around else values[i])
    return base


def detect(luma: list[float], sat: list[float], audio: list[float]) -> list[Event]:
    events: list[Event] = []
    base = _baseline(luma)

    for a, b in _runs([y - m >= FLASH_RISE for y, m in zip(luma, base)]):
        peak = max(range(a, b + 1), key=lambda i: luma[i])
        events.append(
            Event("flash", a, b, peak, {"peak_luma": round(luma[peak], 1)})
        )

    # The end fade is found first so its frames are not also reported as a dip.
    end_fade = None
    n = len(luma)
    if luma[-1] <= END_BLACK_Y:
        start = n - 1
        while start > 0 and luma[start - 1] >= luma[start] - 0.5:
            start -= 1
        # Walk forward past any flat head: the fade starts where luma begins to fall.
        while start < n - 1 and luma[start] - luma[start + 1] < 0.3:
            start += 1
        if n - start >= 3:
            end_fade = Event(
                "end_fade",
                start,
                n - 1,
                None,
                {
                    "from_luma": round(luma[start], 1),
                    "to_luma": round(luma[-1], 1),
                },
            )

    dip_flags = [m - y >= DIP_FALL for y, m in zip(luma, base)]
    if end_fade:
        for i in range(end_fade.start, n):
            dip_flags[i] = False
    for a, b in _runs(dip_flags):
        peak = min(range(a, b + 1), key=lambda i: luma[i])
        events.append(Event("dip", a, b, peak, {"low_luma": round(luma[peak], 1)}))

    # Near-black frames have no saturation either; frames inside the end fade or a
    # dip are not black-and-white just because they are dark.
    dark = [y < 40.0 for y in luma]
    if end_fade:
        for i in range(end_fade.start, n):
            dark[i] = True
    for a, b in _runs(
        [s <= MONO_SAT and not d for s, d in zip(sat, dark)], min_len=3
    ):
        events.append(Event("mono", a, b))

    if end_fade:
        events.append(end_fade)

    for i in range(6, len(audio)):
        prev = sum(audio[i - 6 : i]) / 6
        if audio[i] >= SOUND_FLOOR_DB and audio[i] - prev >= SOUND_JUMP_DB:
            if events and events[-1].kind == "sound" and i - events[-1].end <= 3:
                events[-1].end = i
                continue
            events.append(
                Event("sound", i, i, i, {"level_db": round(audio[i], 1)})
            )

    events.sort(key=lambda e: (e.start, e.kind))
    return events


def measure(video: Path) -> dict:
    fps = probe_fps(video)
    luma, sat = measure_frames(video)
    audio = measure_audio(video, fps, len(luma))
    events = detect(luma, sat, audio)
    return {
        "video": str(video),
        "fps": round(fps, 3),
        "frames": len(luma),
        "events": [asdict(e) | {"length": e.length} for e in events],
        "luma": [round(v, 1) for v in luma],
        "saturation": [round(v, 1) for v in sat],
        "audio_db": [round(v, 1) for v in audio],
    }


# ── expectations from a recipe ────────────────────────────────────────────────

FLASH_EDGES = {"flash_white", "light_leak"}


def _recipe_duration(recipe: dict) -> float:
    end = 0.0
    for track in recipe.get("tracks") or []:
        for clip in track.get("clips") or []:
            length = float(clip["source_duration"]) / float(clip.get("rate") or 1)
            end = max(end, float(clip.get("timeline_start") or 0) + length + float(clip.get("hold_duration") or 0))
    return end


def expect_from_recipe(recipe: dict, fps: float) -> dict:
    """What the pixels and audio must show for a phone recipe (snake_case wire).

    Edge timing contract (shared with the engine, ColorMomentTiming.swift): an edge
    at time t with duration d is drawn over [t - d/2, t + d/2]; a flash peaks and a
    dip bottoms out exactly at t, and the tone switches at t under the flash. Frames
    are sampled at their presentation time k / fps. A sound effect locked to an edge
    is an audio-track clip on the `sfx` track starting at that edge.
    """
    expected: list[dict] = []
    for moment in recipe.get("color_moments") or []:
        start_s, end_s = float(moment["start"]), float(moment["end"])
        if moment.get("tone") == "mono":
            first = math.ceil(start_s * fps - 1e-6)
            last = math.ceil(end_s * fps - 1e-6) - 1
            row = {"kind": "mono", "start": first, "end": last}
            # The tone switches under a flash or dip, so it only shows once the edge's
            # half-window has passed: accept the observed edge anywhere in that span.
            for side, key, bound in (("enter", "start", "start_max"), ("exit", "end", "end_min")):
                edge = moment.get(side) or {}
                if edge.get("style", "cut") != "cut":
                    half = math.ceil(float(edge.get("duration", 1 / 3)) / 2 * fps)
                    row[bound] = first + half if key == "start" else last - half
            expected.append(row)
        for side, t in (("enter", start_s), ("exit", end_s)):
            style = (moment.get(side) or {}).get("style", "cut")
            if style == "cut":
                continue
            kind = "flash" if style in FLASH_EDGES else "dip"
            expected.append({"kind": kind, "peak": round(t * fps), "style": style})
    for track in recipe.get("tracks") or []:
        if track.get("kind") != "audio" or not str(track.get("id", "")).startswith("sfx"):
            continue
        for clip in track.get("clips") or []:
            expected.append({"kind": "sound", "start": round(float(clip["timeline_start"]) * fps)})
    end_fade = recipe.get("end_fade")
    if end_fade:
        frames = round(_recipe_duration(recipe) * fps)
        expected.append(
            {
                "kind": "end_fade",
                "start": frames - round(float(end_fade["duration"]) * fps),
                "end": frames - 1,
            }
        )
    return {"fps": fps, "expected": expected}


# ── comparison ────────────────────────────────────────────────────────────────


def _closest(events: list[dict], kind: str, key: str, frame: int) -> dict | None:
    pool = [e for e in events if e["kind"] == kind and e.get(key) is not None]
    return min(pool, key=lambda e: abs(e[key] - frame)) if pool else None


def compare(measured: dict, expect: dict, tolerance: int) -> dict:
    events = measured["events"]
    rows = []
    used: set[int] = set()
    for exp in expect["expected"]:
        kind = exp["kind"]
        keys = [k for k in ("start", "peak", "end") if k in exp]
        anchor = keys[0]
        got = _closest(
            [e for e in events if id(e) not in used], kind, anchor, exp[anchor]
        )
        row = {"expected": exp, "measured": None, "offsets": {}, "pass": False}
        if got is not None:
            used.add(id(got))
            row["measured"] = {k: got[k] for k in ("start", "end", "peak", "length")}
            offsets = {}
            for k in keys:
                if got.get(k) is None:
                    continue
                offset = got[k] - exp[k]
                if k == "start" and "start_max" in exp and exp[k] <= got[k] <= exp["start_max"]:
                    offset = 0
                if k == "end" and "end_min" in exp and exp["end_min"] <= got[k] <= exp[k]:
                    offset = 0
                offsets[k] = offset
            row["offsets"] = offsets
            row["pass"] = bool(row["offsets"]) and all(
                abs(v) <= tolerance for v in row["offsets"].values()
            )
        rows.append(row)
    expected_kinds = {e["kind"] for e in expect["expected"]}
    unexpected = [
        e
        for e in events
        if id(e) not in used
        and e["kind"] in {"flash", "dip", "mono", "end_fade"}
        and (expect.get("strict", True) or e["kind"] in expected_kinds)
    ]
    ok = all(r["pass"] for r in rows) and not unexpected
    return {
        "pass": ok,
        "tolerance_frames": tolerance,
        "rows": rows,
        "unexpected": [{k: e[k] for k in ("kind", "start", "end", "peak")} for e in unexpected],
    }


def write_montage(video: Path, frames: list[int], fps: float, out: Path) -> None:
    """A strip of the frames around each event, for the PR evidence."""
    if not frames:
        return
    picks = sorted({max(0, f + d) for f in frames for d in (-3, 0, 3)})[:40]
    expr = "+".join(f"eq(n\\,{f})" for f in picks)
    cols = min(len(picks), 12)
    rows = math.ceil(len(picks) / cols)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"select='{expr}',scale=180:-2,"
            "drawtext=text='%{n}':x=4:y=4:fontsize=18:fontcolor=yellow:box=1:boxcolor=black@0.6,"
            f"tile={cols}x{rows}",
            "-frames:v",
            "1",
            "-vsync",
            "vfr",
            str(out),
        ],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_measure = sub.add_parser("measure", help="print the events found in a video")
    p_measure.add_argument("video", type=Path)
    p_measure.add_argument("--json", type=Path, help="also write the full curves here")

    p_expect = sub.add_parser("expect-from-recipe", help="expected events for a recipe")
    p_expect.add_argument("recipe", type=Path)
    p_expect.add_argument("--fps", type=float, default=30.0)

    p_check = sub.add_parser("check", help="compare a video with an expectation file")
    p_check.add_argument("video", type=Path)
    p_check.add_argument("--expect", type=Path, required=True)
    p_check.add_argument("--tolerance", type=int, default=1)
    p_check.add_argument("--out", type=Path, default=Path(".colour-moments-verify"))

    args = parser.parse_args(argv)

    if args.cmd == "expect-from-recipe":
        recipe = json.loads(args.recipe.read_text())
        json.dump(expect_from_recipe(recipe, args.fps), sys.stdout, indent=2)
        print()
        return 0

    data = measure(args.video)
    if args.cmd == "measure":
        if args.json:
            args.json.write_text(json.dumps(data, indent=1))
        for e in data["events"]:
            print(
                f"{e['kind']:8s} frames {e['start']}-{e['end']} "
                f"({e['start'] / data['fps']:.2f}s, {e['length']}f)"
                + (f" peak {e['peak']}" if e["peak"] is not None else "")
                + (f" {e['detail']}" if e["detail"] else "")
            )
        return 0

    expect = json.loads(args.expect.read_text())
    report = compare(data, expect, args.tolerance)
    report["video"] = str(args.video)
    report["fps"] = data["fps"]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    (args.out / "curves.json").write_text(
        json.dumps({k: data[k] for k in ("luma", "saturation", "audio_db")})
    )
    anchors = [
        r["measured"].get("peak") or r["measured"]["start"]
        for r in report["rows"]
        if r["measured"]
    ]
    write_montage(args.video, anchors, data["fps"], args.out / "montage.png")
    for r in report["rows"]:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{mark} {r['expected']} -> {r['measured']} offsets {r['offsets']}")
    for u in report["unexpected"]:
        print(f"FAIL unexpected {u}")
    print("PASS" if report["pass"] else "FAIL", f"({args.out / 'report.json'})")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
