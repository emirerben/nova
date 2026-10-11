"""Tests for the non-mutating KRI-499 promotion command preparer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/prepare-length-promotion.py"


def _head_sha() -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()


def _passing_evidence(candidate_sha: str) -> dict[str, object]:
    now = int(time.time())
    deployed = now - 24 * 60 * 60
    return {
        "schema_version": 1,
        "base_sha": "a" * 40,
        "candidate_sha": candidate_sha,
        "replay": {
            "base_sha": "a" * 40,
            "candidate_sha": candidate_sha,
            "complete": True,
            "successful": True,
            "available_count": 23,
            "input_count": 23,
            "replayed_count": 23,
        },
        "newly_blocking_declines": 0,
        "affected_supported_paths": ["device"],
        "deployment_started_epoch_s": deployed,
        "clean_since_epoch_s": deployed,
        "clean_observations": [
            {
                "timestamp_epoch_s": deployed,
                "check_mode": "log_only",
                "check_observations": 1,
                "check_declines": 0,
                "api_revision": candidate_sha,
                "worker_revision": candidate_sha,
            },
            {
                "timestamp_epoch_s": now,
                "check_mode": "log_only",
                "check_observations": 2,
                "check_declines": 0,
                "api_revision": candidate_sha,
                "worker_revision": candidate_sha,
            },
        ],
        "canary_evidence": [
            {
                "path": "device",
                "measured_export_success": True,
                "api_revision": candidate_sha,
                "worker_revision": candidate_sha,
                "export_duration_s": 24.0,
                "frame_count": 720,
                "command": "xcodebuild test KRI492",
                "app_build": "Kria 1.0 (1)",
                "source_fixture_sha256": "b" * 64,
                "source_provenance": {"fixture": "kri492_length_24"},
                "timestamp_epoch_s": now,
            }
        ],
    }


def _run(evidence: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src/apps/api")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence", str(evidence), "--repo-root", str(ROOT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def test_prepare_prints_exact_enforcement_dispatch_for_valid_evidence(tmp_path: Path) -> None:
    evidence_path = tmp_path / "proof with spaces.json"
    candidate = _head_sha()
    evidence_path.write_text(json.dumps(_passing_evidence(candidate)))

    result = _run(evidence_path)

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["status"] == "ready_for_human_dispatch"
    assert plan["setting"] == {"key": "CREATOR_DURATION_CHECK_MODE", "value": "enforce"}
    assert f"-f expected_sha={candidate}" in plan["next_command"]
    assert "-f creator_duration_check_mode=enforce" in plan["next_command"]
    assert "length_promotion_evidence=$(cat '" in plan["next_command"]


def test_prepare_refuses_pending_evidence_without_printing_dispatch(tmp_path: Path) -> None:
    evidence_path = tmp_path / "pending.json"
    evidence_path.write_text(json.dumps({"schema_version": 1}))

    result = _run(evidence_path)

    assert result.returncode != 0
    plan = json.loads(result.stdout)
    assert plan["status"] == "pending"
    assert plan["next_command"] is None
