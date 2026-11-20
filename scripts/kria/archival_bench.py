"""Prepare offline montage inputs and score simulator exports against pinned archive footage."""

import argparse
import hashlib
import json
import runpy
import subprocess
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "archival-cases.json").read_text())
# Preserve the full archival image in a portrait canvas.
PORTRAIT = "scale=360:640:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=360:640:(ow-iw)/2:(oh-ih)/2,setsar=1"
CONTROL = "archive_wrong_arrival"


def raw_file(raw, name):
    path = raw / (name + ".ogv")
    if (
        hashlib.sha256(path.read_bytes()).hexdigest()
        != FIXTURE["sources"][name]["sha256"]
    ):
        raise ValueError(f"Unexpected source bytes: {path}")
    return path


def ffmpeg(*args):
    return subprocess.check_output(
        ["ffmpeg", "-nostdin", "-v", "error", *map(str, args)]
    )


def prepare(raw, out, download_sources=False):
    # The simulator fixture helper configures isolated local storage and the API import path.
    phone = runpy.run_path(str(HERE.parent / "ios/phone-photo-render-e2e.py"))
    from app.pipeline.guided_story import (
        GuidedStoryExecutionPlan,
        compile_execution_plan,
    )
    from app.pipeline.unified_montage import (
        BriefView,
        UnifiedClip,
        plan_unified_montage,
    )

    out.mkdir(parents=True, exist_ok=False)
    if download_sources:
        raw = out / "sources"
        raw.mkdir()
        for name, source in FIXTURE["sources"].items():
            request = urllib.request.Request(
                source["url"],
                headers={
                    "User-Agent": "Nova-archival-bench/1.0 (https://github.com/emirerben/nova)"
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(1_000_001)
            if (
                len(data) > 1_000_000
                or hashlib.sha256(data).hexdigest() != source["sha256"]
            ):
                raise ValueError(f"Unexpected download from Commons for {name}")
            (raw / (name + ".ogv")).write_bytes(data)
    clips, bindings = {}, {}
    for name in FIXTURE["sources"]:
        file = out / (name + ".mp4")
        ffmpeg(
            "-i",
            raw_file(raw, name),
            "-t",
            "2",
            "-vf",
            PORTRAIT + ",fps=30",
            "-an",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            file,
        )
        proxy = f"users/archival/creation/analysis-proxy-{name}.mp4"
        clips[name] = UnifiedClip(name, proxy, "1", 2, 360, 640)
        sha, size = phone["_fingerprint"](file)
        bindings[name] = phone["PhoneSourceBinding"](
            media_id=name,
            proxy_path=proxy,
            generation="1",
            original=phone["OriginalMediaDescriptor"](
                sha256=sha,
                byte_count=size,
                duration_s=2,
                width=360,
                height=640,
                has_audio=False,
            ),
        )
    cases = {}
    inputs = dict(FIXTURE["cases"])
    inputs[CONTROL] = dict(inputs["archive_arrival"], creator_order=[0, 1])
    phone["settings"].phone_render_verified_features = phone["VERIFIED"]
    for key, case in inputs.items():
        names = case["attachments"]
        duration = 2 * len(names)
        plan = plan_unified_montage(
            [clips[n] for n in names],
            BriefView(wants_order=True, target_duration_s=duration),
            creator_order=case["creator_order"],
        )
        compiled = GuidedStoryExecutionPlan.model_validate(
            compile_execution_plan(plan.guided_edit(), track=None)
        )
        status, recipe = phone["_status"](
            compiled, tuple(bindings[n] for n in names), ()
        )
        (out / (key + "-plan.json")).write_text(compiled.model_dump_json(indent=2))
        status_file = key + ".json"
        (out / status_file).write_text(json.dumps(status))
        cases[key] = dict(
            status_file=status_file,
            duration_s=duration,
            clips=[dict(media_id=n, file=n + ".mp4") for n in names],
            required_capabilities=list(recipe.required_capabilities),
            drop_capability="basicComposition",
            samples=[],
        )
    (out / "e2e.json").write_text(
        json.dumps(dict(verified_features=phone["VERIFIED"], cases=cases))
    )


def picture(path, time, filters):
    # Compare the visible archival band only, ignoring black portrait padding.
    data = ffmpeg(
        "-ss",
        str(time),
        "-i",
        path,
        "-vframes",
        "1",
        "-vf",
        filters + ",scale=32:24,format=gray",
        "-f",
        "rawvideo",
        "-",
    )
    if len(data) != 32 * 24:
        raise ValueError(f"Missing frame in {path.name} at {time}")
    return data


def score(raw, out):
    times = (0.25, 1.0, 1.75)
    band = "crop=iw:iw*176/240:0:(ih-oh)/2"
    references = {
        t: {
            n: picture(raw_file(raw, n), t, PORTRAIT + "," + band)
            for n in FIXTURE["sources"]
        }
        for t in times
    }
    results = {}
    for key in [*FIXTURE["cases"], CONTROL]:
        expected = FIXTURE["cases"]["archive_arrival" if key == CONTROL else key][
            "expected"
        ]
        file = out / "frames" / (key + ".mp4")
        duration = float(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(file),
                ]
            )
        )
        samples = []
        for i, source in enumerate(expected):
            for t in times:
                timestamp = i * 2 + t
                frame = picture(file, timestamp, band)
                distances = {
                    n: sum(abs(a - b) for a, b in zip(frame, ref)) / len(frame)
                    for n, ref in references[t].items()
                }
                winner = min(distances, key=distances.get)
                # Absolute distance also rejects an unrelated image that happens to be nearest.
                samples.append(
                    dict(
                        time_s=timestamp,
                        expected=source,
                        nearest=winner,
                        error=round(distances[winner], 2),
                        passed=winner == source and distances[winner] <= 15,
                    )
                )
        results[key] = dict(
            duration_s=duration,
            duration_ok=abs(duration - 2 * len(expected)) <= 0.1,
            samples=samples,
            sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        )
    (out / "scores.json").write_text(json.dumps(results, indent=2) + "\n")
    for key, result in results.items():
        print(
            key,
            sum(s["passed"] for s in result["samples"]),
            "/",
            len(result["samples"]),
            "duration",
            result["duration_s"],
        )
    return (
        all(
            r["duration_ok"] and all(s["passed"] for s in r["samples"])
            for k, r in results.items()
            if k != CONTROL
        )
        and results[CONTROL]["duration_ok"]
        and all(
            s["nearest"] == ("parliament" if s["time_s"] < 2 else "anatolian")
            and s["error"] <= 15
            and not s["passed"]
            for s in results[CONTROL]["samples"]
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "score"])
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.raw, args.output, download_sources=args.raw == Path("-"))
    else:
        raise SystemExit(
            0
            if score(
                args.output / "sources" if args.raw == Path("-") else args.raw,
                args.output,
            )
            else 1
        )
