#!/usr/bin/env python3
"""Export a saved prepared story through the existing native fixture harness.

prepare --prepared private-draft.json --bindings private-phone-bindings.json
        --originals private-media-id-paths.json --output private-output-directory
compare --output private-output-directory

This offline tool never uploads, publishes, calls a model or authenticates its
operator-supplied inputs. Keep real source material and detailed reports private.
Use AudioParityFixtureTests for host exports, or the DEBUG camera-roll export
proof screen for hardware. Both use the production AVFoundation exporter.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))
os.environ.setdefault("STORAGE_BUCKET", "offline-camera-roll-proof")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def synthetic(args):
    """Distinct long-take picture/audio signatures, generated without user media."""
    import subprocess

    args.output.mkdir(parents=True, exist_ok=True)
    paths = [args.output / f"source-{index}.mp4" for index in range(2)]
    if any(path.exists() for path in paths):
        raise ValueError(
            "Choose a fresh output directory; existing inputs are preserved"
        )
    for index, path in enumerate(paths):
        colour, frequency, step = ("red", 330, 7) if index == 0 else ("blue", 440, 11)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-n",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s=640x360:r=30:d=180,negate=enable='gte(t,100)'",
                "-f",
                "lavfi",
                "-i",
                f"aevalsrc=0.1*sin(2*PI*({frequency}+floor(t)*{step})*t):s=48000:d=180",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(path),
            ],
            check=True,
        )
    print("Two synthetic 180-second inputs generated; provider calls: 0")


def prepare(args):
    import uuid

    from app.kria.device_render import DeviceRenderStatus, make_device_request
    from app.pipeline.guided_story import (
        GuidedStoryExecutionPlan,
        compile_proposal_execution_plan,
    )
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan
    from app.services.media_collection_preparation import (
        PreparedCollectionStory,
        execution_digest,
    )
    from app.services.phone_sources import PhoneSourceBinding

    raw = json.loads(args.prepared.read_text())
    prepared = PreparedCollectionStory.model_validate(
        raw.get("prepared_collection_story", raw)
    )
    compiled = compile_proposal_execution_plan(prepared.snapshot)
    if execution_digest(compiled) != prepared.compiled_digest:
        raise ValueError("Prepared choices or compiler changed")
    bindings = tuple(
        PhoneSourceBinding.model_validate(value)
        for value in json.loads(args.bindings.read_text())
    )
    originals = {
        key: Path(value).resolve()
        for key, value in json.loads(args.originals.read_text()).items()
    }
    paths = {}
    device_paths = {}
    import shutil

    for binding in bindings:
        path = originals[binding.media_id]
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            checksum != binding.original.sha256
            or path.stat().st_size != binding.original.byte_count
        ):
            raise ValueError("Original fingerprint differs from its pinned binding")
        filename = hashlib.sha256(binding.media_id.encode()).hexdigest()[:24] + ".mp4"
        target = args.output / "inputs" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        paths[binding.media_id] = str(target.resolve())
        device_paths[binding.media_id] = str(target.relative_to(args.output))
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(compiled), bindings
    )
    request = make_device_request(
        job_id=uuid.uuid4(), variant_id="camera-roll-proof", revision=1, recipe=recipe
    )
    status = DeviceRenderStatus(phase="awaiting_device", request=request)
    expected = recipe.model_dump(mode="json")
    cases = {"correct": expected}
    wrong_window = copy.deepcopy(expected)
    first = wrong_window["tracks"][0]["clips"][0]
    if first["source_start"] < first["source_duration"]:
        raise ValueError(
            "The late-window negative control needs a first source start beyond its cut duration"
        )
    first["source_start"] = 0
    cases["wrong_window"] = wrong_window
    wrong_audio = copy.deepcopy(expected)
    wrong_audio["audio"]["original_volume"] = 0
    cases["wrong_audio"] = wrong_audio
    if len(expected["tracks"][0]["clips"]) > 1:
        wrong_order = copy.deepcopy(expected)
        clips = wrong_order["tracks"][0]["clips"]
        clips[0]["source_asset_id"], clips[1]["source_asset_id"] = (
            clips[1]["source_asset_id"],
            clips[0]["source_asset_id"],
        )
        cases["wrong_order"] = wrong_order
    for name, candidate in cases.items():
        directory = args.output / "cases" / name
        write(directory / "recipe.json", candidate)
        write(directory / "assets.json", paths)
        write(directory / "device-assets.json", device_paths)
        write(directory / "expected-status.json", status.model_dump(mode="json"))
    write(
        args.output / "proof.json",
        {
            "schema_version": 1,
            "story_id": str(prepared.story_id),
            "story_revision": prepared.story_revision,
            "compiled_digest": prepared.compiled_digest,
            "cases": list(cases),
            "provider_calls": 0,
            "publication": "none",
            "evidence_level": "operator_supplied_inputs_require_independent_review",
        },
    )
    print(json.dumps({"output": str(args.output), "cases": list(cases)}))


def compare(args):
    from app.kria.device_render import DeviceRenderStatus
    from app.services.actual_export_verification import verify_actual_export

    manifest = json.loads((args.output / "proof.json").read_text())
    results = []
    for name in manifest["cases"]:
        directory = args.output / "cases" / name
        status = DeviceRenderStatus.model_validate_json(
            (directory / "expected-status.json").read_text()
        )
        sources = {
            key: Path(value)
            for key, value in json.loads(
                (directory / "assets.json").read_text()
            ).items()
        }
        report = verify_actual_export(
            directory / "phone.mp4", status, sources, brand_tail="none"
        )
        write(directory / "report.json", report)
        expected = "checked" if name == "correct" else "rejected"
        results.append(
            {
                "case": name,
                "status": report["status"],
                "expected": expected,
                "pass": report["status"] == expected,
                "checks": len(report["checks"]),
                "sha256": report.get("sha256"),
            }
        )
    aggregate = {
        **manifest,
        "results": results,
        "pass": all(value["pass"] for value in results),
    }
    write(args.output / "report.json", aggregate)
    print(json.dumps(aggregate, indent=2))
    return 0 if aggregate["pass"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inputs = commands.add_parser("synthetic")
    inputs.add_argument("--output", type=Path, required=True)
    build = commands.add_parser("prepare")
    for name in ("prepared", "bindings", "originals", "output"):
        build.add_argument(f"--{name}", type=Path, required=True)
    check = commands.add_parser("compare")
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "synthetic":
        synthetic(args)
        return 0
    if args.command == "prepare":
        prepare(args)
        return 0
    return compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
