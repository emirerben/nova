"""The KPI report is deterministic and the committed baseline stays in sync with the code."""

from __future__ import annotations

from pathlib import Path

from .report import main, render_report

BASELINE = (
    Path(__file__).resolve().parents[6]
    / "docs"
    / "reviews"
    / "kri-185"
    / "request-following-baseline.md"
)
REGENERATE = (
    "cd src/apps/api && python -m tests.evals.request_following.report --phase baseline "
    "--out ../../../docs/reviews/kri-185/request-following-baseline.md"
)


def test_report_is_deterministic():
    assert render_report("baseline") == render_report("baseline")


def test_report_names_every_request_type_and_the_wrong_landmark_stub():
    text = render_report("baseline")
    for row in ("title_exact", "order_route", "readability", "restructure", "style"):
        assert f"| {row} |" in text
    assert "Wrong-landmark rate" in text and "| trip | n/a | 0 |" in text
    assert "Small-sample warning" in text


def test_committed_baseline_report_matches_the_replay():
    """Fails when a fixture or a real code path moves a number; regenerate on purpose
    (the assertion message carries the command) after reviewing the movement."""
    assert BASELINE.read_text(encoding="utf-8") == render_report("baseline"), REGENERATE


def test_cli_writes_the_report(tmp_path, monkeypatch):
    out = tmp_path / "nested" / "report.md"
    monkeypatch.setattr("sys.argv", ["report", "--phase", "P0", "--out", str(out)])
    main()
    assert out.read_text(encoding="utf-8").startswith("# Request-following KPI: P0")
