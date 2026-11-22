"""Replaying stored export reports under the current late-window rule (KRI-831).

Failure modes, written before the command:
- a stored `checked` report that the current rule would reject is not listed;
- a report from before the grid was stored is called exact, though the rule can only
  be an upper bound there (the stored evidence cannot say the window was on the grid);
- a window stored as off the grid is flagged anyway;
- a report with no per-sample evidence (before v4) is counted as replayed and clean;
- a picture window that passed inside a report rejected for its text or audio is
  skipped, though it is real evidence of what the rule does to a correct window;
- a window that already failed is counted as a flip, or a repair's report is skipped
  or counted twice;
- the fetch stops early, keeps jobs that never rendered on a phone, or swallows a
  failed request and reports a clean replay of half the jobs.
"""

from __future__ import annotations

import json

import pytest

from app.cli import replay_export_verdicts as cli

MATCH, MISS = (0.01, 0.99), (0.04, 0.80)


def _sample(displayed, following):
    return {
        "error_displayed": displayed[0],
        "ssim_displayed": displayed[1],
        "error_next": following[0],
        "ssim_next": following[1],
    }


ON_DISPLAYED, ON_NEXT, ON_EITHER = _sample(MATCH, MISS), _sample(MISS, MATCH), _sample(MATCH, MATCH)


def _report(
    samples,
    *,
    status="checked",
    picture="passed",
    version="phone-simple-cut-v5",
    lead_next=-0.2,
    **more,
):
    picture = {
        "kind": "picture",
        "clip_id": "c0",
        "status": picture,
        "samples": len(samples),
        "neighbour_samples": sum(sample is ON_NEXT for sample in samples),
        "lead_next": lead_next,
        "sample_evidence": samples,
        **more,
    }
    return {"status": status, "verifier_version": version, "checks": [picture]}


def _job(*reports, repairs=()):
    return {
        "job_id": "job",
        "variants": [
            {
                "variant_id": "v",
                "attempts": [
                    {"attempt_id": f"a{index}", "actual_export_verification": report}
                    for index, report in enumerate(reports)
                ],
                "repair_history": [{"report": report} for report in repairs],
            }
        ],
    }


LATE = [ON_EITHER, ON_NEXT, ON_NEXT, ON_NEXT, ON_NEXT]
CORRECT = [ON_EITHER, ON_DISPLAYED, ON_DISPLAYED, ON_NEXT, ON_DISPLAYED]


def test_a_checked_report_the_current_rule_rejects_is_listed_as_an_upper_bound():
    (row,) = cli.replay(_job(_report(LATE)))
    assert (row["verdict"], row["reason"]) == ("flips", "no_sample_holds_displayed_frame")
    assert row["grid"] == "unknown", "v5 did not store whether the window was on the grid"
    assert (row["job_id"], row["clip_id"], row["verifier_version"]) == (
        "job",
        "c0",
        "phone-simple-cut-v5",
    )


def test_a_stored_grid_makes_the_replay_exact():
    (on_grid,) = cli.replay(_job(_report(LATE, on_source_grid=True)))
    assert (on_grid["verdict"], on_grid["grid"]) == ("flips", "on")
    (off_grid,) = cli.replay(_job(_report(LATE, on_source_grid=False)))
    assert (off_grid["verdict"], off_grid["grid"]) == ("holds", "off")


def test_a_correct_window_holds_and_a_window_that_already_failed_is_not_a_flip():
    rows = cli.replay(_job(_report(CORRECT), _report(LATE, status="rejected", picture="failed")))
    assert [row["verdict"] for row in rows] == ["holds", "already_failed"]


def test_a_passed_window_inside_a_report_rejected_for_other_checks_is_replayed():
    (row,) = cli.replay(_job(_report(LATE, status="rejected")))
    assert (row["verdict"], row["report_status"]) == ("flips", "rejected")


def test_a_report_without_sample_evidence_is_not_replayable():
    old = _report(LATE, version="phone-simple-cut-v3")
    del old["checks"][0]["sample_evidence"]
    (row,) = cli.replay(_job(old))
    assert row["verdict"] == "not_replayable"


def test_repair_reports_are_replayed_once_each():
    rows = cli.replay(_job(_report(CORRECT), repairs=[_report(LATE)]))
    assert sorted(row["verdict"] for row in rows) == ["flips", "holds"]


def _admin(pages, reports):
    calls = []

    def get(path):
        calls.append(path)
        if path.startswith("jobs?"):
            offset = int(path.split("offset=")[1])
            return {"items": pages.get(offset, [])}
        return reports[path.split("/")[1]]

    return get, calls


def test_fetch_reads_every_job_since_the_date_and_keeps_only_phone_renders(tmp_path):
    def item(job, day):
        return {"job_id": job, "created_at": f"2026-11-{day:02d}T10:00:00Z"}

    pages = {0: [item("new", 20), item("plain", 19)], 2: [item("edge", 10), item("old", 9)]}
    reports = {
        "new": _job(_report(LATE)),
        "plain": {"job_id": "plain", "variants": []},
        "edge": _job(_report(CORRECT)),
    }
    get, calls = _admin(pages, reports)
    kept = cli.fetch(get, since="2026-11-10", out=tmp_path, page=2)
    assert kept == 2
    assert sorted(path.name for path in tmp_path.iterdir()) == ["edge.json", "new.json"]
    assert json.loads((tmp_path / "new.json").read_text()) == reports["new"]
    assert "jobs/old/export-verification" not in calls, "older than --since"


def test_fetch_does_not_swallow_a_failed_request(tmp_path):
    def get(path):
        if path.startswith("jobs?"):
            return {"items": [{"job_id": "a", "created_at": "2026-11-20T10:00:00Z"}]}
        raise RuntimeError("admin request failed: jobs/a/export-verification")

    with pytest.raises(RuntimeError, match="jobs/a/export-verification"):
        cli.fetch(get, since="2026-11-01", out=tmp_path)


def test_command_exits_non_zero_when_a_checked_report_flips(tmp_path, capsys):
    (tmp_path / "late.json").write_text(json.dumps(_job(_report(LATE))))
    (tmp_path / "fine.json").write_text(json.dumps(_job(_report(CORRECT))))
    assert cli.main([str(tmp_path)]) == 1
    printed = capsys.readouterr().out
    assert "flips: 1" in printed and "holds: 1" in printed
    assert "job c0 no_sample_holds_displayed_frame (report checked" in printed

    (tmp_path / "late.json").unlink()
    assert cli.main([str(tmp_path)]) == 0
