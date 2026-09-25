"""The KPI report is deterministic and the committed baseline stays in sync with the code."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_report_names_every_request_type_and_the_wrong_landmark_rate():
    text = render_report("baseline")
    for row in ("title_exact", "order_route", "readability", "restructure", "style"):
        assert f"| {row} |" in text
    assert "Wrong-landmark rate" in text
    assert "| harbor_run | 14% | 7 |" in text and "| trip | 25% | 4 |" in text
    assert "| sport | n/a | 0 |" in text
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


def test_replay_is_the_default_and_needs_no_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = tmp_path / "r.md"
    monkeypatch.setattr("sys.argv", ["report", "--out", str(out)])
    main()
    assert "replay mode, offline" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "extra",
    [
        [],  # nothing approved
        ["--usage-purpose", "live_eval", "--test-run-id", "x", "--approve-reservation"],  # no cap
        [
            "--usage-purpose",
            "live_eval",
            "--test-run-id",
            "x",
            "--max-cost-usd",
            "5",
            "--approve-reservation",
        ],  # over the $2 ceiling
        [
            "--usage-purpose",
            "live_eval",
            "--test-run-id",
            "x",
            "--max-cost-usd",
            "1",
        ],  # reservation not acknowledged
    ],
)
def test_live_mode_refuses_to_start_without_the_paid_run_guards(extra, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr("sys.argv", ["report", "--eval-mode", "live", *extra])
    with pytest.raises(SystemExit):
        main()


def test_live_mode_needs_an_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    args = [
        "report",
        "--eval-mode",
        "live",
        "--usage-purpose",
        "live_eval",
        "--test-run-id",
        "x",
        "--max-cost-usd",
        "1",
        "--approve-reservation",
    ]
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(SystemExit):
        main()


def test_live_mode_hands_the_guards_to_the_runner_and_labels_the_report(tmp_path, monkeypatch):
    """No network: `render_report` is stubbed; we only check the wiring."""
    seen: dict[str, str] = {}

    def fake_render(phase, mode):
        import os

        seen.update(mode=mode, purpose=os.environ["NOVA_EVAL_USAGE_PURPOSE"])
        return f"# Request-following KPI: {phase}\nLIVE\n"

    for key in (
        "NOVA_EVAL_MODE",
        "NOVA_EVAL_USAGE_PURPOSE",
        "NOVA_EVAL_TEST_RUN_ID",
        "NOVA_EVAL_MAX_COST_USD",
        "NOVA_EVAL_RESERVATION_APPROVED",
    ):
        monkeypatch.setenv(key, "")  # restored by monkeypatch after the test
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr("tests.evals.request_following.report.render_report", fake_render)
    out = tmp_path / "live.md"
    monkeypatch.setattr(
        "sys.argv",
        [
            "report", "--phase", "P5", "--eval-mode", "live", "--usage-purpose", "live_eval",
            "--test-run-id", "rf-1", "--max-cost-usd", "1", "--approve-reservation",
            "--out", str(out),
        ],
    )  # fmt: skip
    main()
    assert seen == {"mode": "live", "purpose": "live_eval"}
    assert out.read_text(encoding="utf-8").startswith("# Request-following KPI: P5")


def test_live_report_labels_itself_as_live(monkeypatch):
    """The header must never let a live run pass for a recording."""
    monkeypatch.setattr(
        "tests.evals.request_following.report.collect", lambda mode="replay": ([], [])
    )
    assert "LIVE mode" in render_report("P5", "live")
    assert "replay mode, offline" in render_report("P5", "replay")
