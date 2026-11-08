#!/usr/bin/env python3
"""Turn the folders `scripts/ios/talk-bench.sh` pulls from the phone into `.talk-bench/report.json`.

  talk_bench_report.py analyze-take <run-dir>        # ffprobe -show_streams + one showspectrumpic PNG per audio stream
  talk_bench_report.py report --runs <dir> --out <report.json> [--label L --sha S --device D]

A run folder holds `summary.json` (written by the app's `TalkBench` recorder while `KRIA_TALK_BENCH=1`), `take-N.mov`
and, after `analyze-take`, `ffprobe.json` and `spectrum-<stream>.png`. The run folders are named
`<scenario>--run<N>` where the scenario is `<background>-cin<0|1>`.

Every number in the report is MEASURED on the device by the app's own clock. What each one covers is in `notes`.
Per-frame series (the live preview) are reduced to one median and one p95 per run; the report then gives the median,
min, max and MAD across runs. Fewer than 3 runs per scenario is flagged: a spread of one run is not a spread.

Stdlib only; ffprobe/ffmpeg are optional and their absence is reported, not hidden.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

# The first seconds of the idle window include camera start-up and the first Vision model load.
IDLE_WARMUP_MS = 2000
# `talk.live.*` series are sliced into the three windows the test produces.
WINDOWED_SERIES = ["talk.live.consume", "talk.live.segment", "talk.live.render", "talk.live.captureToLayer", "talk.live.maskOffset"]
WINDOWED_COUNTERS = ["talk.live.delivered", "talk.live.presented", "talk.live.dropped"]
# One value per run, the first time the interval appears in the run.
ONE_SHOT = [
    "talk.stop.finalize", "talk.stop.finishRecording", "talk.stop.validate", "talk.stop.cinematicBake",
    "talk.stop.audioMeasurement", "talk.stop.toReview",
    "talk.use.spinner", "talk.use.intake", "talk.use.stageCopy", "talk.use.proxyGateWait", "talk.use.import",
    "talk.use.proxyTranscode", "talk.use.contract", "talk.use.reserve", "talk.use.put", "talk.use.attach",
    "talk.use.pollToDone", "talk.use.backgroundUpload",
    "talk.use.prewarmHashWait", "talk.use.prewarmProxyWait", "talk.prewarm.hash", "talk.prewarm.proxy",
    "talk.save.editorCommit", "talk.save.resolveAssets", "talk.save.compose", "talk.save.loudnessPrepass",
    "talk.save.encode", "talk.save.export", "talk.save.hashOutput", "talk.save.publish", "talk.save.publishHash",
    "talk.save.publishPut", "talk.save.complete", "talk.save.serverChecking", "talk.bench.backgroundMake",
]
# Thermal and storage facts the app writes into summary.json. Strings for thermal, MB for free space.
THERMAL_FACTS = ("thermal.start", "thermal.end", "thermal.max")
FREE_SPACE_FACTS = ("disk.free_mb.start", "disk.free_mb.end")
# A `talk.hash` is credited to the smallest of these intervals that contains it.
HASH_OWNERS = [
    "talk.use.import", "talk.use.contract", "talk.prewarm.hash", "talk.save.hashOutput", "talk.save.publishHash", "talk.save.resolveAssets",
]
NOTES = {
    "talk.live.consume": "Whole per-frame call on the camera frame queue: background pull + Vision + composite + render + hand-off. "
                         "Budget at 30 fps is 33.3 ms.",
    "talk.live.captureToLayer": "Sample PTS (host clock) to the moment the layer contents were set. A lower bound on what the creator "
                                "sees: compositor and panel scan-out come after it.",
    "talk.live.maskOffset": "Capture time of the frame on screen minus capture time of the frame its mask was computed from. 0 when "
                            "segmentation runs on the display path; about 33 ms is one frame of trailing edge.",
    "talk.stop.finalize": "Stop request to AVCaptureMovieFileOutput's didFinishRecording callback (the file closing).",
    "talk.stop.finishRecording": "Callback to Review-ready: validation + Cinematic bake + audio level measurement + track count.",
    "talk.hash": "Attributed to the enclosing phase by start/end containment; see hash_in_*.",
    "thermal.start|end|max": "ProcessInfo.thermalState at app launch, at the last flush, and the worst seen in between. A run that "
                             "began warm is not comparable to one that began nominal.",
    "talk.live.backgroundAttached": "When the looping video or photo was handed to the live preview. `background_attached.before_record_ms` "
                                    "is how long the creator had it on screen before Record; the idle window starts at the first live frame.",
    "talk.prewarm.hash": "SHA-256 of the take done while the creator reviews it (not under the Use take spinner).",
    "talk.prewarm.proxy": "640x360 analysis proxy built while the creator reviews the take (not under the Use take spinner).",
    "talk.use.prewarmHashWait": "How long Use take waited for the review-time hash; 0-ish when the creator reviewed longer than the hash took.",
    "talk.use.prewarmProxyWait": "How long Use take waited for the review-time proxy; replaces talk.use.proxyTranscode when it hits.",
    "talk.matte.held": "Export frames answered from the held last-good mask, counted inside the talk.save.export window.",
}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))]


def mad(values: list[float]) -> float:
    median = statistics.median(values)
    return statistics.median(abs(v - median) for v in values)


def spread(values: list[float]) -> dict:
    return {
        "median": round(statistics.median(values), 2), "min": round(min(values), 2), "max": round(max(values), 2),
        "mad": round(mad(values), 2), "runs": len(values), "per_run": [round(v, 2) for v in values],
    }


# --- take analysis ------------------------------------------------------------------------------------------------

def analyze_take(run_dir: Path) -> dict:
    result: dict = {}
    for take in sorted(run_dir.glob("take-*.mov")) + sorted(run_dir.glob("take-*.mp4")):
        entry: dict = {"file": take.name}
        if shutil.which("ffprobe"):
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(take)],
                                   capture_output=True, text=True)
            if probe.returncode == 0:
                data = json.loads(probe.stdout)
                (run_dir / f"ffprobe-{take.stem}.json").write_text(json.dumps(data, indent=2))
                streams = data.get("streams", [])
                entry["streams"] = [
                    {k: s.get(k) for k in ("index", "codec_type", "codec_name", "sample_rate", "channels", "channel_layout",
                                           "width", "height", "avg_frame_rate", "nb_frames", "duration", "bit_rate")}
                    for s in streams
                ]
                entry["audio_stream_count"] = sum(1 for s in streams if s.get("codec_type") == "audio")
                entry["video_stream_count"] = sum(1 for s in streams if s.get("codec_type") == "video")
            else:
                entry["ffprobe_error"] = probe.stderr.strip()[:300]
        else:
            entry["ffprobe_error"] = "ffprobe not installed"
        entry["spectrograms"] = []
        if shutil.which("ffmpeg"):
            for position in range(entry.get("audio_stream_count", 0)):
                png = run_dir / f"spectrum-{take.stem}-a{position}.png"
                made = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", str(take), "-filter_complex",
                     f"[0:a:{position}]showspectrumpic=s=1280x640:legend=1:scale=log[spectrum]",
                     "-map", "[spectrum]", "-frames:v", "1", str(png)],
                    capture_output=True, text=True)
                if made.returncode == 0 and png.exists():
                    entry["spectrograms"].append(png.name)
                else:
                    entry.setdefault("spectrogram_errors", []).append(made.stderr.strip()[:300])
        else:
            entry["spectrogram_errors"] = ["ffmpeg not installed"]
        result[take.name] = entry
    (run_dir / "take-analysis.json").write_text(json.dumps(result, indent=2))
    return result


# --- one run ------------------------------------------------------------------------------------------------------

def load_summary(run_dir: Path) -> dict | None:
    path = run_dir / "summary.json"
    if not path.exists():
        # `devicectl copy from <dir>` may nest the folder; look one level down before giving up.
        nested = list(run_dir.glob("**/summary.json"))
        if not nested:
            return None
        path = nested[0]
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def intervals(summary: dict, name: str) -> list[tuple[float, float, str]]:
    return [(float(e[0]), float(e[1]), e[2] if len(e) > 2 else "") for e in summary.get("intervals", {}).get(name, [])]


def counts(summary: dict, name: str) -> list[float]:
    return [float(t) for t in summary.get("counters", {}).get(name, [])]


def windows(summary: dict) -> dict[str, tuple[float, float]]:
    """idle / recording / post, from the recording-started tally and the stop request."""
    started = counts(summary, "talk.record.started")
    stops = intervals(summary, "talk.stop.toReview")
    end = float(summary.get("elapsed_ms", 0))
    if not started or not stops:
        return {}
    first_start = started[0]
    stop_at = next((s for s, _, _ in stops if s >= first_start), None)
    if stop_at is None:
        return {}
    # Idle runs from the first live frame (plus warm-up) to the start of recording. Without a background there are no live
    # frames, so the idle window is empty and only `recording` has data.
    frames = [t for t in counts(summary, "talk.live.delivered") if t < first_start]
    idle_start = (frames[0] + IDLE_WARMUP_MS) if frames else first_start
    return {"idle": (idle_start, first_start), "recording": (first_start, stop_at), "post": (stop_at, end)}


def inside(t: float, window: tuple[float, float]) -> bool:
    return window[0] <= t < window[1]


def run_metrics(summary: dict) -> dict:
    metrics: dict[str, float] = {}
    wins = windows(summary)
    for name in WINDOWED_SERIES:
        for label, window in wins.items():
            if label == "post":
                continue
            values = [ms for t, ms, _ in intervals(summary, name) if inside(t, window)]
            if len(values) >= 5:
                metrics[f"{name}@{label}.p50_ms"] = statistics.median(values)
                metrics[f"{name}@{label}.p95_ms"] = percentile(values, 0.95)
                metrics[f"{name}@{label}.frames"] = float(len(values))
    for name in WINDOWED_COUNTERS:
        for label, window in wins.items():
            if label == "post":
                continue
            seconds = max(0.001, (window[1] - window[0]) / 1000)
            total = sum(1 for t in counts(summary, name) if inside(t, window))
            metrics[f"{name}@{label}.count"] = float(total)
            metrics[f"{name}@{label}.per_s"] = total / seconds
    for reason in sorted({k for k in summary.get("counters", {}) if k.startswith("talk.live.dropped.")}):
        for label, window in wins.items():
            if label != "post":
                metrics[f"{reason}@{label}.count"] = float(sum(1 for t in counts(summary, reason) if inside(t, window)))
    attached = counts(summary, "talk.live.backgroundAttached")
    started = counts(summary, "talk.record.started")
    if attached:
        metrics["background_attached.at_ms"] = attached[0]
        if started:
            metrics["background_attached.before_record_ms"] = started[0] - attached[0]
    for name in ONE_SHOT:
        found = intervals(summary, name)
        if found:
            metrics[f"{name}.ms"] = found[0][1]
            if len(found) > 1:
                metrics[f"{name}.occurrences"] = float(len(found))
    # SHA-256 time and bytes, credited to the phase they happened inside.
    owners = {name: intervals(summary, name) for name in HASH_OWNERS}
    for t, ms, detail in intervals(summary, "talk.hash"):
        owner = next((name for name, spans in owners.items() if any(s <= t and t + ms <= s + d + 1 for s, d, _ in spans)), "unattributed")
        metrics[f"hash_in_{owner}.ms"] = metrics.get(f"hash_in_{owner}.ms", 0) + ms
        metrics[f"hash_in_{owner}.files"] = metrics.get(f"hash_in_{owner}.files", 0) + 1
        if detail.startswith("bytes="):
            metrics[f"hash_in_{owner}.bytes"] = metrics.get(f"hash_in_{owner}.bytes", 0) + float(detail.removeprefix("bytes=") or 0)
    # Export-side matting: segmentations, held and original answers inside the export window.
    export = intervals(summary, "talk.save.export")
    if export:
        window = (export[0][0], export[0][0] + export[0][1])
        segments = [ms for t, ms, _ in intervals(summary, "talk.matte.segment") if inside(t, window)]
        if segments:
            metrics["talk.matte.segment.p50_ms"] = statistics.median(segments)
            metrics["talk.matte.segment.p95_ms"] = percentile(segments, 0.95)
            metrics["talk.matte.segment.frames"] = float(len(segments))
        metrics["talk.matte.held.frames"] = float(sum(1 for t in counts(summary, "talk.matte.held") if inside(t, window)))
        metrics["talk.matte.original.frames"] = float(sum(1 for t in counts(summary, "talk.matte.original") if inside(t, window)))
    # Whole after-Stop and whole editor-Save spans from the step timestamps, when the steps are present.
    for label, first, last in (("talk.save.editorCommit->publish", "talk.save.editorCommit", "talk.save.publish"),):
        a, b = intervals(summary, first), intervals(summary, last)
        if a and b:
            metrics[f"{label}.ms"] = (b[-1][0] + b[-1][1]) - a[0][0]
    return metrics


def run_environment(summary: dict) -> dict:
    facts = summary.get("facts", {})
    environment: dict = {k: facts.get(k) for k in THERMAL_FACTS}
    for key in FREE_SPACE_FACTS:
        if key in facts:
            environment[key] = float(facts[key])
    environment["low_power_mode"] = facts.get("power.low_power_mode")
    return environment


def background_drew(summary: dict, scenario: str) -> bool | None:
    """True when a background scenario segmented and presented frames before Record, None for a scenario with no background.
    `segment` only runs while a background provider holds a frame, so its presence is proof the background was drawing; the
    app-side counters are the evidence, the UI-test element is not."""
    if scenario.startswith("none-"):
        return None
    started = counts(summary, "talk.record.started")
    first_record = started[0] if started else float("inf")
    segments = [t for t, _, _ in intervals(summary, "talk.live.segment") if t < first_record]
    presented = [t for t in counts(summary, "talk.live.presented") if t < first_record]
    return bool(segments) and bool(presented)


def run_audio_facts(summary: dict) -> dict:
    facts = summary.get("facts", {})
    raw = facts.get("recorder.take_finished")
    if not raw:
        return {}
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    keep = ("finalize_ms", "finish_ms", "audio_category", "audio_mode", "route_inputs", "route_outputs", "audio_sample_rate_hz",
            "microphone_mode", "audio_track_count", "other_audio_playing", "channel_count", "max_power_db", "stop_reason",
            "take_duration_s")
    return {k: report.get(k) for k in keep if k in report}


# --- report -------------------------------------------------------------------------------------------------------

def build_report(runs_dir: Path, label: str, sha: str, device: str, planned_runs: int | None) -> dict:
    scenarios: dict[str, dict] = {}
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir() and "--run" in p.name):
        scenario, _, run_name = run_dir.name.partition("--run")
        entry = scenarios.setdefault(scenario, {"runs": [], "failed_runs": []})
        status = json.loads((run_dir / "status.json").read_text()) if (run_dir / "status.json").exists() else {}
        summary = load_summary(run_dir)
        if summary is not None and status.get("xcodebuild_exit") not in (0, None):
            entry["failed_runs"].append({"run": run_name, "reason": status.get("reason"), "xcodebuild_exit": status.get("xcodebuild_exit"),
                                         "partial_metrics": run_metrics(summary)})
            continue
        if summary is None:
            entry["failed_runs"].append({"run": run_name, "reason": status.get("reason", "no summary.json pulled from the device"),
                                         "xcodebuild_exit": status.get("xcodebuild_exit")})
            continue
        metrics = run_metrics(summary)
        analysis_path = run_dir / "take-analysis.json"
        entry["runs"].append({
            "run": run_name, "metrics": metrics, "audio": run_audio_facts(summary), "environment": run_environment(summary),
            "background_drew": background_drew(summary, scenario), "attempts": status.get("attempts"), "transport": status.get("transport"),
            "thermal_before_run": status.get("thermal_before_run"),
            "takes": json.loads(analysis_path.read_text()) if analysis_path.exists() else {},
            "xcodebuild_exit": status.get("xcodebuild_exit"), "truncated_series": summary.get("truncated", []),
            "facts": {k: v for k, v in summary.get("facts", {}).items() if not k.startswith("recorder.")},
        })
    report_scenarios = {}
    for scenario, entry in scenarios.items():
        names = sorted({m for run in entry["runs"] for m in run["metrics"]})
        timings = {}
        for name in names:
            values = [run["metrics"][name] for run in entry["runs"] if name in run["metrics"]]
            timings[name] = spread(values)
        audio = {}
        for key in sorted({k for run in entry["runs"] for k in run["audio"]}):
            audio[key] = [run["audio"].get(key) for run in entry["runs"]]
        streams = [t.get("audio_stream_count") for run in entry["runs"] for t in run["takes"].values()]
        report_scenarios[scenario] = {
            "run_count": len(entry["runs"]), "failed_runs": entry["failed_runs"],
            "thermal": {k: [run["environment"].get(k) for run in entry["runs"]] for k in THERMAL_FACTS},
            "free_space_mb": {k: [run["environment"].get(k) for run in entry["runs"]] for k in FREE_SPACE_FACTS},
            "background_drew_per_run": [run["background_drew"] for run in entry["runs"]],
            "enough_runs": len(entry["runs"]) >= 3,
            "timings": timings, "audio_per_run": audio, "ffprobe_audio_stream_counts": streams,
            "runs": [{"run": r["run"], "xcodebuild_exit": r["xcodebuild_exit"], "attempts": r["attempts"], "transport": r["transport"], "thermal_before_run": r["thermal_before_run"],
                      "truncated_series": r["truncated_series"],
                      "takes": r["takes"], "facts": r["facts"]} for r in entry["runs"]],
        }
    return {
        "label": label, "sha": sha, "device": device, "evidence": "measured on the device by the app's own clock",
        "planned_runs_per_scenario": planned_runs, "idle_warmup_ms_excluded": IDLE_WARMUP_MS,
        "scenarios": report_scenarios,
        "notes": NOTES,
        "reading_it": "timings.<metric>.median/min/max/mad are across runs; per-frame series are first reduced to p50/p95 within each run. "
                      "A scenario with enough_runs=false has fewer than 3 runs and has no usable spread.",
    }


def print_table(report: dict) -> None:
    for scenario, data in sorted(report["scenarios"].items()):
        flag = "" if data["enough_runs"] else "  (FEWER THAN 3 RUNS: no usable spread)"
        print(f"\n== {scenario}: {data['run_count']} run(s), {len(data['failed_runs'])} failed{flag}")
        print(f"  thermal start/end/max per run: {data['thermal']['thermal.start']} / {data['thermal']['thermal.end']} / {data['thermal']['thermal.max']}")
        if any(v is False for v in data["background_drew_per_run"]):
            print(f"  WARNING background did not draw before Record in some runs: {data['background_drew_per_run']}")
        for name, t in sorted(data["timings"].items()):
            print(f"  {name:<58} median {t['median']:>10} min {t['min']:>10} max {t['max']:>10} mad {t['mad']:>8} n={t['runs']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    take = sub.add_parser("analyze-take")
    take.add_argument("run_dir", type=Path)
    report = sub.add_parser("report")
    report.add_argument("--runs", type=Path, required=True)
    report.add_argument("--out", type=Path, required=True)
    report.add_argument("--label", default="")
    report.add_argument("--sha", default="")
    report.add_argument("--device", default="")
    report.add_argument("--planned-runs", type=int)
    args = parser.parse_args()
    if args.command == "analyze-take":
        print(json.dumps(analyze_take(args.run_dir), indent=2))
        return 0
    result = build_report(args.runs, args.label, args.sha, args.device, args.planned_runs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print_table(result)
    print(f"\ntalk-bench: report -> {args.out}")
    return 0 if result["scenarios"] else 1


if __name__ == "__main__":
    sys.exit(main())
