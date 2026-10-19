"""Profile actual-export checks on local, already-staged evidence files.

No publication, database, model or storage calls. Fixture preparation and network
transfer are outside these measurements. Reports omit request IDs and paths.
Exit zero means profiling completed, not that every candidate passed its checks.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def _cpu() -> float:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    os.environ.setdefault("STORAGE_BUCKET", "offline-export-check")
    os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
    from app.kria.device_render import DeviceRenderStatus  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("bench_verifier", args.implementation)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    invoke = module._run
    calls = []

    def profiled(arguments, **kwargs):
        stage = (
            "ocr"
            if arguments[0] == "tesseract"
            else "probe"
            if arguments[0] == "ffprobe"
            else "audio_decode"
            if "-vn" in arguments
            else "picture_decode"
        )
        started = time.perf_counter()
        try:
            return invoke(arguments, **kwargs)
        finally:
            calls.append((stage, time.perf_counter() - started))

    module._run = profiled
    result = {
        "captured_at": datetime.now(UTC).isoformat(),
        "implementation_sha256": hashlib.sha256(args.implementation.read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "system": platform.system(),
        "measurement": "verification_only_staged_local_files_no_transfer_or_fixture_creation",
        "cases": [],
    }
    result["tools"] = {
        tool: subprocess.run(
            [tool, flag], check=True, capture_output=True, text=True, timeout=20
        ).stdout.splitlines()[0]
        for tool, flag in [("ffmpeg", "-version"), ("tesseract", "--version")]
    }
    quota = Path("/sys/fs/cgroup/cpu.max")
    if quota.exists():
        result["cpu_quota"] = quota.read_text().strip()
    for fixture in json.loads(args.fixtures.read_text()):
        status = DeviceRenderStatus.model_validate_json(Path(fixture["status"]).read_text())
        sources = {key: Path(path) for key, path in fixture["sources"].items()}
        runs = []
        for repeat in range(args.repeat):
            calls.clear()
            cpu_before, started = _cpu(), time.perf_counter()
            report = module.verify_actual_export(
                Path(fixture["video"]),
                status,
                sources,
                brand_tail=fixture.get("brand_tail", "none"),
            )
            elapsed, cpu = time.perf_counter() - started, _cpu() - cpu_before
            stages = defaultdict(lambda: {"calls": 0, "wall_s": 0.0})
            for stage, seconds in calls:
                stages[stage]["calls"] += 1
                stages[stage]["wall_s"] += seconds
            counts = defaultdict(int)
            for check in report["checks"]:
                counts[f"{check['kind']}:{check['status']}"] += 1
            run = {
                "wall_s": round(elapsed, 6),
                "cpu_s": round(cpu, 6),
                "status": report["status"],
                "checks": dict(counts),
                "picture_samples": sum(c.get("samples", 0) for c in report["checks"]),
                "stages": {
                    key: {**value, "wall_s": round(value["wall_s"], 6)}
                    for key, value in stages.items()
                },
            }
            runs.append(run)
            print(json.dumps({"case": fixture["name"], "repeat": repeat, **run}), flush=True)
        elapsed = [run["wall_s"] for run in runs]
        result["cases"].append(
            {
                "name": fixture["name"],
                "kind": fixture["kind"],
                "duration_s": status.request.recipe.duration,
                "canvas": status.request.recipe.canvas.model_dump(mode="json"),
                "median_s": statistics.median(elapsed),
                "min_s": min(elapsed),
                "max_s": max(elapsed),
                "runs": runs,
            }
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
