"""Offline protocol controls; these invented labels are never retrieval evidence."""

import hashlib
import json
import subprocess
import sys


def evaluate(tmp_path, *, semantic_wrong=False, late=False):
    labels = [
        {
            "query_key": "answer",
            "categories": ["lit_house", "ferry", "long_take_ending", "late_speech", "occlusion"],
            "answerable": True,
            "accepted_ids": ["a"],
            "unusable_ids": ["b"],
            "irrelevant_ids": ["c"],
            "evidence_ref": "private/prelabelled-intervals",
        },
        {
            "query_key": "none",
            "categories": ["no_match"],
            "answerable": False,
            "accepted_ids": [],
            "unusable_ids": [],
            "irrelevant_ids": ["c"],
            "evidence_ref": "private/no-match-review",
        },
    ]
    encoded = json.dumps(labels).encode()
    protocol = {
        "registered_at": "2026-10-01T00:00:00Z",
        "labels_sha256": hashlib.sha256(encoded).hexdigest(),
        "recall_floor": 0.9,
        "max_latency_s": 20,
        "total_budget_usd": 0,
        "app_revision": "test",
        "server_revision": "test",
        "scope": "single_creator",
    }
    rows = [
        {
            "query_key": label["query_key"],
            "method": method,
            "ranked_ids": (["b"] if semantic_wrong and method == "semantic" else ["a"])
            if label["answerable"]
            else [],
            "completed_at": "2026-09-01T00:00:00Z" if late else "2026-10-02T00:00:00Z",
            "latency_s": 1,
            "settled_spend_usd": 0,
            "model_calls": 0,
            "source_evidence_ref": "private/current-source-receipts",
        }
        for label in labels
        for method in ("text", "semantic")
    ]
    (tmp_path / "labels.json").write_bytes(encoded)
    (tmp_path / "protocol.json").write_text(json.dumps(protocol))
    (tmp_path / "runs.json").write_text(json.dumps(rows))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.camera_roll_retrieval_eval",
            "--protocol",
            str(tmp_path / "protocol.json"),
            "--labels",
            str(tmp_path / "labels.json"),
            "--runs",
            str(tmp_path / "runs.json"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        capture_output=True,
        text=True,
    )
    return result, json.loads((tmp_path / "report.json").read_text()) if (
        tmp_path / "report.json"
    ).exists() else None


def test_report_prefers_smallest_passing_method_and_reports_unusable_returns(tmp_path):
    result, report = evaluate(tmp_path, semantic_wrong=True)
    assert result.returncode == 0, result.stderr
    assert report["recommended_method_for_review"] == "text"
    assert report["methods"]["text"]["recall_at_10"] == 1
    assert report["methods"]["semantic"]["unusable_returns"] == 1
    assert report["methods"]["semantic"]["recall_at_10"] == 0
    assert report["methods"]["text"]["no_match_precision"] == 1
    assert "private/" not in json.dumps(report)
    assert report["supports_wider_rollout"] is False


def test_runs_before_registration_are_rejected(tmp_path):
    result, _ = evaluate(tmp_path, late=True)
    assert result.returncode != 0
