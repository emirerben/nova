"""Tests for app.cli.gate — the gate tick's CLI over build_gate (pure stdlib)."""

from __future__ import annotations

import io
import json

from app.cli import gate as gate_cli


def test_render_needed_true_for_pipeline_path(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("+++ b/src/apps/api/app/pipeline/reframe.py\n")
    )
    assert gate_cli.main(["render-needed"]) == 0  # 0 = verify-overlays must run


def test_render_needed_false_for_frontend(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("+++ b/src/apps/web/src/app/page.tsx\n"))
    assert gate_cli.main(["render-needed"]) == 1  # 1 = skip render gate


def test_report_prints_json_and_writes_pr_body(monkeypatch, tmp_path, capsys):
    results = json.dumps(
        [
            {"name": "pytest", "blocking": True, "passed": True, "detail": ""},
            {"name": "qa", "blocking": False, "passed": False, "detail": "1 err"},
        ]
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(results))
    out = tmp_path / "body.md"
    rc = gate_cli.main(
        [
            "report",
            "--task-id",
            "t1",
            "--task-title",
            "Fix wrap",
            "--head",
            "abc123def456",
            "--pr-body-out",
            str(out),
        ]
    )
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["passed"] is True  # advisory qa fail does not block
    body = out.read_text()
    assert "Fix wrap" in body
    assert "All blocking gates green" in body
    assert "fail (advisory)" in body


def test_report_blocking_fail_marks_not_passed(monkeypatch, tmp_path, capsys):
    results = json.dumps(
        [{"name": "tsc", "blocking": True, "passed": False, "detail": "type error"}]
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(results))
    rc = gate_cli.main(
        ["report", "--task-id", "x", "--task-title", "t", "--pr-body-out", str(tmp_path / "b")]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["passed"] is False
