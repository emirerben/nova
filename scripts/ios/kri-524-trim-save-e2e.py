#!/usr/bin/env python3
"""Generate a synthetic clip/text trim → Save → native export regression.

Uses the route regression's recorded operation shape and independent assertions.
Source bytes and initial editor state are authored fixtures; no provider calls
or production writes occur. Run with the API test environment's Python, then
set TEST_RUNNER_KRIA_E2E_DIR to the output directory for the Swift export test.
"""

# ruff: noqa: E402
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))
os.environ.setdefault("STORAGE_BUCKET", "nova-test")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost/nova_test"
)

import pytest

from app.kria.device_render import DeviceRenderStatus
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.routes import test_compound_word_trim_save as replay


def main() -> None:
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    source = output / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=1920x1080:r=30:d=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    original_fixture = replay._guided_shape_job
    jobs = []

    def fixture(monkeypatch):
        job, revision = original_fixture(monkeypatch)
        for binding in job.assembly_plan[PHONE_SOURCES_FIELD]:
            binding["original"].update(
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                byte_count=source.stat().st_size,
            )
        jobs.append(job)
        return job, revision

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(replay, "_guided_shape_job", fixture)
        replay.test_twelve_explicit_words_survive_compound_trim_save_and_later_save(
            monkeypatch
        )
        request = device_status(jobs[0], "guided_story").request
        status = DeviceRenderStatus(phase="awaiting_device", request=request)
        (output / "status.json").write_text(status.model_dump_json(indent=2))
        features = sorted(request.recipe.required_capabilities)
        case = {
            "status_file": "status.json",
            "duration_s": 4,
            "required_capabilities": features,
            "drop_capability": "positionedText",
            "clips": [{"media_id": "source", "file": source.name}],
            "expected_words": "Join us for our favorite bakery and tea shop near the harbor".split(),
            "expected_word_end_s": 2,
            "model_transport_provenance": {
                "creation": "authored_editor_fixture",
                "retime": "recorded_operation_shape",
            },
            "samples": [
                {
                    "name": "surviving_footage",
                    "t": 3.5,
                    "x": 80,
                    "y": 1800,
                    "rgb": [255, 0, 0],
                }
            ],
            "caption_samples": [
                {
                    "name": "word_visible",
                    "t": 1.4,
                    "region": [0, 900, 1080, 1900],
                    "expect_text": True,
                },
                {
                    "name": "words_finished",
                    "t": 2.3,
                    "region": [0, 900, 1080, 1900],
                    "expect_text": False,
                },
            ],
            "audio_samples": [
                {"name": f"clip_{index}", "t": t, "speaker_hz": 440, "muted_hz": 880}
                for index, t in enumerate([1, 2.5, 3.5])
            ],
        }
        (output / "e2e.json").write_text(
            json.dumps(
                {
                    "verified_features": features,
                    "cases": {"compound_words_trim_save": case},
                },
                indent=2,
            )
        )
    print(f"Verified compound Save; wrote native export fixture to {output}")


if __name__ == "__main__":
    main()
