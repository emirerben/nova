"""Synthetic, privacy-safe tests for bounded KRI-499 log collection."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from app.services import length_check_observation

ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "scripts/ci/collect-length-checks.py"
CANDIDATE = "b" * 40
START = 1_000
END = START + 24 * 60 * 60
NOW = END + 60


def _proof(**overrides: object) -> dict[str, object]:
    proof: dict[str, object] = {
        "expected_candidate_sha": CANDIDATE,
        "api_revision": CANDIDATE,
        "worker_revision": CANDIDATE,
        "timestamp_epoch_s": NOW - 1,
        "deployment_started_epoch_s": START,
        "log_source_provenance": {"operator_attestation": "synthetic"},
    }
    proof.update(overrides)
    return proof


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event": "creator_length_check",
        "timestamp_epoch_s": START + 1,
        "check_mode": "log_only",
        "check_observations": 2,
        "check_declines": 0,
        "api_revision": CANDIDATE,
        "worker_revision": CANDIDATE,
    }
    event.update(overrides)
    return event


def _run(tmp_path: Path, proof: dict[str, object], lines: list[object], *extra: str):
    proof_path = tmp_path / "fleet-proof.json"
    journal = tmp_path / "journal.json"
    proof_path.write_text(json.dumps(proof))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--fleet-proof",
            str(proof_path),
            "--journal",
            str(journal),
            "--interval-start",
            str(START),
            "--interval-end",
            str(END),
            "--stream-complete",
            "--now-epoch-s",
            str(NOW),
            *extra,
        ],
        input="\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines),
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(journal.read_text())


def test_collects_only_sanitized_aggregate_evidence(tmp_path: Path) -> None:
    result, journal = _run(
        tmp_path, _proof(), [{"event": "unrelated", "creator_id": "do-not-save"}, _event()]
    )
    assert result.returncode == 0
    assert journal == json.loads(result.stdout)
    assert journal["status"] == "collected"
    assert journal["clean_observations"][0]["check_observations"] == 2
    assert "creator_id" not in result.stdout


def test_collector_accepts_the_actual_emitter_event_inside_a_fly_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    stream = io.StringIO()
    handler = length_check_observation._logger.handlers[0]
    monkeypatch.setattr(handler, "stream", stream)
    monkeypatch.setattr(length_check_observation.time, "time", lambda: START + 1)
    monkeypatch.setenv("CREATOR_DURATION_CHECK_REVISION", CANDIDATE)
    length_check_observation.emit_length_check_observation(check_mode="log_only", outcome="passed")

    result, journal = _run(tmp_path, _proof(), [{"message": stream.getvalue().strip()}])

    assert result.returncode == 0
    assert journal["status"] == "collected"
    assert journal["clean_observations"][0]["check_observations"] == 1


def test_collector_resets_coverage_for_empty_mismatched_or_malformed_events(tmp_path: Path) -> None:
    for lines in (
        [],
        [_event(worker_revision="a" * 40)],
        ["not json"],
        [{"message": '{"event":"creator_length_check"'}],
    ):
        result, journal = _run(tmp_path, _proof(), list(lines))
        assert result.returncode == 0
        assert journal["status"] == "pending"
        assert journal["clean_observations"] == []


def test_collector_rejects_stale_proof_and_short_or_incomplete_stream(tmp_path: Path) -> None:
    result, journal = _run(tmp_path, _proof(timestamp_epoch_s=NOW - 301), [_event()])
    assert result.returncode == 0
    assert journal["status"] == "pending"

    proof_path = tmp_path / "fleet-proof.json"
    journal_path = tmp_path / "journal.json"
    proof_path.write_text(json.dumps(_proof()))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--fleet-proof",
            str(proof_path),
            "--journal",
            str(journal_path),
            "--interval-start",
            str(START),
            "--interval-end",
            str(END - 1),
            "--now-epoch-s",
            str(NOW),
        ],
        input=json.dumps(_event()),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(journal_path.read_text())["clean_observations"] == []
