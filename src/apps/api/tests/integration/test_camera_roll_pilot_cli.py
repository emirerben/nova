"""Exercise the offline pilot report boundary; synthetic rows are never pilot evidence."""

import json
import subprocess
import sys


def run_report(tmp_path, sessions):
    protocol = {
        "registered_at": "2026-10-01T00:00:00Z",
        "creator_count": 10,
        "collections_per_creator": 2,
        "selection_floor": 0.5,
        "export_floor": 0.8,
        "max_corrective_turns": 2,
        "max_latency_s": 300,
        "max_settled_spend_usd": 2,
        "reliability_evidence": "private/reliability-reviewed",
        "native_output_evidence": "private/native-reviewed",
        "app_revision": "test-app",
        "server_revision": "test-server",
        "capability_cohort": "test-only",
    }
    (tmp_path / "protocol.json").write_text(json.dumps(protocol))
    (tmp_path / "sessions.json").write_text(json.dumps(sessions))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.camera_roll_pilot",
            "--protocol",
            str(tmp_path / "protocol.json"),
            "--sessions",
            str(tmp_path / "sessions.json"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        capture_output=True,
        text=True,
    )
    return result, json.loads((tmp_path / "report.json").read_text()) if (
        tmp_path / "report.json"
    ).exists() else None


def session(index):
    return {
        "session_key": f"session-{index}",
        "creator_key": f"creator-{index // 2}",
        "collection_key": f"collection-{index}",
        "started_at": "2026-10-02T00:00:00Z",
        "consented": True,
        "invited": True,
        "eligible": True,
        "exposed": True,
        "selected": True,
        "approved": True,
        "verified_export": True,
        "human_fulfillment": "met",
        "export_artifact_sha256": "b" * 64,
        "approved_snapshot_sha256": "a" * 64,
        "export_snapshot_sha256": "a" * 64,
        "review_evidence": "private/synthetic-review",
        "corrective_turns": 1,
        "latency_s": 60,
        "model_calls": 2,
        "settled_spend_usd": 0.1,
    }


def test_report_keeps_selection_approval_and_verified_export_separate(tmp_path):
    rows = [session(index) for index in range(20)]
    rows[0].update(
        approved=False,
        verified_export=False,
        human_fulfillment="unknown",
        export_artifact_sha256=None,
        export_snapshot_sha256=None,
        review_evidence=None,
    )
    result, report = run_report(tmp_path, rows)
    assert result.returncode == 0, result.stderr
    assert report["counts"]["selected"] == 20
    assert report["counts"]["approved"] == 19
    assert report["counts"]["verified_exports"] == 19
    assert report["fulfilled_export_rate"] == 0.95
    assert report["decision"] == "eligible_for_review"
    assert "creator-" not in json.dumps(report), "Aggregate output contains no participant keys"


def test_missing_sample_unknown_spend_and_stress_runs_cannot_pass(tmp_path):
    row = session(0)
    row["settled_spend_usd"] = None
    stress = session(1)
    stress["internal_stress_test"] = True
    result, report = run_report(tmp_path, [row, stress])
    assert result.returncode == 0, result.stderr
    assert report["decision"] == "hold"
    assert report["counts"]["eligible_sessions"] == 1
    assert report["internal_stress_sessions"] == 1
    assert "resource_evidence" in report["failed_bars"]
    assert "sample" in report["failed_bars"]


def test_late_registration_duplicate_sessions_and_unapproved_export_rejected(tmp_path):
    row = session(0)
    row["started_at"] = "2026-09-30T00:00:00Z"
    result, _ = run_report(tmp_path, [row])
    assert result.returncode != 0
    row = session(0)
    result, _ = run_report(tmp_path, [row, row])
    assert result.returncode != 0
    row = session(0)
    row["approved"] = False
    result, _ = run_report(tmp_path, [row])
    assert result.returncode != 0
