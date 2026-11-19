"""Ways the CI baseline can report a wrong number, written before the script.

Each test names one way `ci-baseline.py` could mislead a before/after comparison:

- a run query capped at GitHub's 1,000-result limit silently undercounts;
- skipped, queued or still-running jobs counted as zero-minute work or crashing;
- a re-run attempt dropped (hides flake cost) or its minutes double-counted;
- post-merge checks (release metadata) inflating the PR's wait;
- an admin-merged PR with no finished required check reported as a 0-minute wait;
- a Linux job counted as a Mac job, or a Mac matrix leg missed;
- percentiles that interpolate, so two runs of the same data disagree;
- a window count that pages through its results one at a time and spends the
  hourly API budget before any measurement starts (it did, once);
- a measurement started with too little API allowance left, dying half way.
"""

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().with_name("ci-baseline.py")
spec = importlib.util.spec_from_file_location("ci_baseline", SCRIPT)
ci_baseline = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ci_baseline)


def job(name, labels, conclusion, started, completed, attempt=1):
    return {
        "name": name,
        "labels": labels,
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
        "run_attempt": attempt,
    }


def check(name, started, completed, conclusion="success"):
    return {
        "name": name,
        "started_at": started,
        "completed_at": completed,
        "conclusion": conclusion,
    }


class WindowTests(unittest.TestCase):
    def test_a_capped_window_is_split_instead_of_undercounted(self):
        counts = {
            ("2026-11-05T00:00:00Z", "2026-11-07T00:00:00Z"): 1400,
            ("2026-11-05T00:00:00Z", "2026-11-06T00:00:00Z"): 900,
            ("2026-11-06T00:00:00Z", "2026-11-07T00:00:00Z"): 500,
        }
        windows = ci_baseline.uncapped_windows(
            "2026-11-05T00:00:00Z", "2026-11-07T00:00:00Z", lambda a, b: counts[(a, b)]
        )
        self.assertEqual(
            windows,
            [
                ("2026-11-05T00:00:00Z", "2026-11-06T00:00:00Z"),
                ("2026-11-06T00:00:00Z", "2026-11-07T00:00:00Z"),
            ],
        )

    def test_a_window_that_cannot_be_split_further_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            ci_baseline.uncapped_windows(
                "2026-11-05T00:00:00Z", "2026-11-05T00:00:01Z", lambda a, b: 5000
            )


class ApiBudgetTests(unittest.TestCase):
    def run_recording(self, calls):
        def fake(command, **_):
            calls.append(command)
            out = "1400\n" if ".total_count" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

        return fake

    def test_counting_a_window_is_one_request_not_one_per_run(self):
        calls = []
        with patch.object(ci_baseline.subprocess, "run", self.run_recording(calls)):
            with self.assertRaises(RuntimeError):
                # One second wide and "over the cap": counted once, never listed.
                ci_baseline.list_runs("o/r", "2026-11-05T00:00:00Z", "2026-11-05T00:00:01Z")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--paginate", calls[0])
        self.assertIn("per_page=1&", calls[0][4])

    def test_listing_reads_every_page(self):
        calls = []
        with patch.object(ci_baseline.subprocess, "run", self.run_recording(calls)):
            ci_baseline.gh("repos/o/r/actions/runs/1/jobs", ".jobs[]")
        self.assertIn("--paginate", calls[0])

    def test_a_run_the_allowance_cannot_finish_stops_before_it_starts(self):
        def limits(remaining):
            def fake(command, **_):
                body = '{"remaining": %d, "reset": 1795087878}\n' % remaining
                return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

            return fake

        with patch.object(ci_baseline.subprocess, "run", limits(100)):
            with self.assertRaisesRegex(RuntimeError, "needs about 2000 .* 100 left"):
                ci_baseline.require_budget(2000)
        with patch.object(ci_baseline.subprocess, "run", limits(2000)):
            ci_baseline.require_budget(2000)

    def test_every_call_is_a_get(self):
        calls = []
        with patch.object(ci_baseline.subprocess, "run", self.run_recording(calls)):
            ci_baseline.gh("repos/o/r", ".x")
            ci_baseline.gh("repos/o/r", ".x", paginate=False)
        for command in calls:
            self.assertEqual(command[:4], ["gh", "api", "--method", "GET"])


class MacMinuteTests(unittest.TestCase):
    def test_only_macos_labels_are_mac_jobs(self):
        self.assertTrue(ci_baseline.is_mac(job("x", ["macos-15"], "success", None, None)))
        self.assertFalse(
            ci_baseline.is_mac(job("x", ["ubuntu-latest"], "success", None, None))
        )
        self.assertFalse(ci_baseline.is_mac(job("x", [], "success", None, None)))

    def test_skipped_and_unfinished_jobs_carry_no_minutes(self):
        jobs = [
            job("a", ["macos-15"], "skipped", None, None),
            job("b", ["macos-15"], None, "2026-11-05T10:00:00Z", None),
            job("c", ["macos-15"], "success", "2026-11-05T10:00:00Z", "2026-11-05T10:30:00Z"),
        ]
        summary = ci_baseline.mac_minutes(jobs)
        self.assertEqual(summary["minutes"], {"success": 30})
        self.assertEqual(summary["unfinished"], 1)

    def test_every_attempt_counts_and_reruns_are_reported_separately(self):
        jobs = [
            job("u", ["macos-15"], "failure", "2026-11-05T10:00:00Z", "2026-11-05T10:20:00Z", 1),
            job("u", ["macos-15"], "success", "2026-11-05T11:00:00Z", "2026-11-05T11:25:00Z", 2),
        ]
        summary = ci_baseline.mac_minutes(jobs)
        self.assertEqual(summary["minutes"], {"failure": 20, "success": 25})
        self.assertEqual(summary["rerun_minutes"], 25)

    def test_part_minutes_round_up_like_billing(self):
        jobs = [
            job("u", ["macos-15"], "cancelled", "2026-11-05T10:00:00Z", "2026-11-05T10:00:01Z"),
        ]
        self.assertEqual(ci_baseline.mac_minutes(jobs)["minutes"], {"cancelled": 1})

    def test_cancelled_jobs_report_how_long_they_ran(self):
        jobs = [
            job("u", ["macos-15"], "cancelled", "2026-11-05T10:00:00Z", "2026-11-05T10:03:00Z"),
            job("u", ["macos-15"], "cancelled", "2026-11-05T10:00:00Z", "2026-11-05T10:40:00Z"),
            job("u", ["ubuntu-latest"], "cancelled", "2026-11-05T10:00:00Z", "2026-11-05T10:50:00Z"),
        ]
        self.assertEqual(ci_baseline.mac_minutes(jobs)["cancelled_after"], [3.0, 40.0])


class FeedbackTests(unittest.TestCase):
    MERGED = "2026-11-05T12:00:00Z"

    def test_wait_is_first_start_to_last_required_finish(self):
        checks = [
            check("guard", "2026-11-05T10:00:00Z", "2026-11-05T10:01:00Z"),
            check("lint", "2026-11-05T10:02:00Z", "2026-11-05T10:05:00Z"),
            check("test-api", "2026-11-05T10:02:00Z", "2026-11-05T11:00:00Z"),
            check("build-and-test", "2026-11-05T10:02:00Z", "2026-11-05T10:40:00Z"),
        ]
        self.assertEqual(ci_baseline.feedback_minutes(checks, self.MERGED), 60.0)

    def test_post_merge_checks_do_not_extend_the_wait(self):
        checks = [
            check("lint", "2026-11-05T10:00:00Z", "2026-11-05T10:05:00Z"),
            check("test-api", "2026-11-05T10:00:00Z", "2026-11-05T10:30:00Z"),
            check("build-and-test", "2026-11-05T10:00:00Z", "2026-11-05T10:20:00Z"),
            check("publish", "2026-11-05T12:00:30Z", "2026-11-05T20:00:00Z"),
        ]
        self.assertEqual(ci_baseline.feedback_minutes(checks, self.MERGED), 30.0)

    def test_a_rerun_counts_from_the_first_attempt_to_the_last_finish(self):
        checks = [
            check("lint", "2026-11-05T10:00:00Z", "2026-11-05T10:05:00Z"),
            check("build-and-test", "2026-11-05T10:00:00Z", "2026-11-05T10:20:00Z"),
            check("test-api", "2026-11-05T10:00:00Z", "2026-11-05T10:30:00Z", "failure"),
            check("test-api", "2026-11-05T11:00:00Z", "2026-11-05T11:30:00Z"),
        ]
        self.assertEqual(ci_baseline.feedback_minutes(checks, self.MERGED), 90.0)

    def test_a_merge_without_every_required_check_has_no_wait(self):
        checks = [
            check("lint", "2026-11-05T10:00:00Z", "2026-11-05T10:05:00Z"),
            check("test-api", "2026-11-05T10:00:00Z", None, None),
        ]
        self.assertIsNone(ci_baseline.feedback_minutes(checks, self.MERGED))

    def test_a_mac_leg_is_found_by_its_check_name_and_a_skip_is_not_a_mac(self):
        ran = [check("iPhone validation (unit)", "2026-11-05T10:00:00Z", "2026-11-05T10:30:00Z")]
        skipped = [check("iPhone validation (unit)", None, None, "skipped")]
        self.assertTrue(ci_baseline.pr_used_mac(ran))
        self.assertFalse(ci_baseline.pr_used_mac(skipped))
        self.assertFalse(ci_baseline.pr_used_mac([check("api (0)", "a", "b")]))


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_returns_an_observed_value(self):
        values = [10, 20, 30, 40]
        self.assertEqual(ci_baseline.percentile(values, 50), 20)
        self.assertEqual(ci_baseline.percentile(values, 90), 40)
        self.assertIsNone(ci_baseline.percentile([], 50))


if __name__ == "__main__":
    unittest.main()
