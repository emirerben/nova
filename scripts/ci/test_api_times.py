"""Ways the per-test timing report can mislead, written before the script.

`api-times.py` reads the rows tests/_ci_times.py wrote in API shard jobs:

- an older attempt of the same run and shard is added to the newer one, so
  every total doubles after a re-run;
- a line cut short by a killed worker crashes the report, or is dropped
  without a word;
- a test that ran twice in one run is summed as if it were one slow test;
- a file's time is summed across runs instead of taking the middle run, so
  three downloaded runs make every file look three times slower;
- skips are counted but not named, so a new silent skip cannot be seen, or an
  expected failure is listed as a skip;
- the time before the first test is averaged across workers, hiding that every
  worker pays it.

Ways `api-times.py compare OLD NEW` can wave through a CI change that should
have been stopped (a new install, shard layout or runner):

- a test that no longer runs in the new arm goes unnoticed;
- a test that passed before and now skips (a package the new environment
  lacks) is counted as "still green";
- a test that never reached the 60 s timeout before reaches it now, or more
  samples sit above 45 s than before, and the layout is adopted anyway;
- a re-run of one shard (a second attempt) is read as a run that lost every
  other shard's tests;
- arms with different numbers of runs are compared count for count.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("api-times.py")
spec = importlib.util.spec_from_file_location("api_times", SCRIPT)
api_times = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api_times)


def test_row(nodeid, call, outcome="passed", reason="", setup=0.0, teardown=0.0, cpu=None):
    return {
        "kind": "test",
        "worker": "gw0",
        "nodeid": nodeid,
        "file": nodeid.split("::", 1)[0],
        "outcome": outcome,
        "reason": reason,
        "setup": setup,
        "call": call,
        "teardown": teardown,
        "cpu": call if cpu is None else cpu,
        "wall": setup + call + teardown,
    }


class ApiTimesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, rows, tail=""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows) + tail)

    def test_only_the_latest_attempt_of_a_run_and_shard_is_read(self):
        self.write("api-times-10-0-1/gw0.jsonl", [test_row("tests/test_a.py::test_x", 100)])
        self.write("api-times-10-0-2/gw0.jsonl", [test_row("tests/test_a.py::test_x", 7)])
        self.write("api-times-10-1-1/gw0.jsonl", [test_row("tests/test_b.py::test_y", 3)])
        report = api_times.summarize(api_times.load(self.root))
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["files"]["tests/test_a.py"], 7)
        self.assertEqual(report["files"]["tests/test_b.py"], 3)

    def test_a_line_cut_short_is_counted_not_fatal(self):
        self.write(
            "api-times-10-0-1/gw0.jsonl",
            [test_row("tests/test_a.py::test_x", 2)],
            tail='{"kind": "test", "nodeid": "tests/test_a.py::te',
        )
        report = api_times.summarize(api_times.load(self.root))
        self.assertEqual(report["unreadable_lines"], 1)
        self.assertEqual(report["files"]["tests/test_a.py"], 2)

    def test_a_test_that_ran_twice_in_one_run_is_named(self):
        self.write("api-times-10-0-1/gw0.jsonl", [test_row("tests/test_a.py::test_x", 2)])
        self.write("api-times-10-1-1/gw0.jsonl", [test_row("tests/test_a.py::test_x", 2)])
        report = api_times.summarize(api_times.load(self.root))
        self.assertEqual(report["ran_twice"], ["tests/test_a.py::test_x"])

    def test_a_files_time_is_the_middle_run_not_the_sum_of_runs(self):
        for run, seconds in ((10, 30), (11, 10), (12, 12)):
            self.write(
                f"api-times-{run}-0-1/gw0.jsonl",
                [
                    test_row("tests/test_a.py::test_x", seconds, setup=1, teardown=1),
                    test_row("tests/test_a.py::test_y", 1),
                ],
            )
        report = api_times.summarize(api_times.load(self.root))
        self.assertEqual(report["runs"], 3)
        # Per run: 33, 13, 15 seconds including setup and teardown.
        self.assertEqual(report["files"]["tests/test_a.py"], 15)
        self.assertEqual(report["tests"]["tests/test_a.py::test_x"], 14)
        self.assertEqual(report["phase_seconds"], {"setup": 1, "call": 13, "teardown": 1})

    def test_skips_are_named_with_reasons_and_xfails_are_separate(self):
        self.write(
            "api-times-10-0-1/gw0.jsonl",
            [
                test_row("tests/test_a.py::test_s", 0, "skipped", "ffmpeg not installed"),
                test_row("tests/test_a.py::test_x", 0, "xfailed", "known gap"),
                test_row("tests/test_a.py::test_p", 1),
            ],
        )
        report = api_times.summarize(api_times.load(self.root))
        self.assertEqual(report["outcomes"], {"passed": 1, "skipped": 1, "xfailed": 1})
        self.assertEqual(
            report["skipped"], {"tests/test_a.py::test_s": "ffmpeg not installed"}
        )
        self.assertEqual(report["xfailed"], {"tests/test_a.py::test_x": "known gap"})

    def test_startup_is_reported_per_worker(self):
        rows = [
            {"kind": "startup", "worker": "gw0", "seconds": 70.0, "items": 100},
            {"kind": "startup", "worker": "gw1", "seconds": 80.0, "items": 100},
        ]
        self.write("api-times-10-0-1/gw0.jsonl", rows[:1])
        self.write("api-times-10-0-1/gw1.jsonl", rows[1:])
        report = api_times.summarize(api_times.load(self.root))
        self.assertEqual(
            report["startup"], {"10/0": {"gw0": 70.0, "gw1": 80.0, "items": 100}}
        )

    def test_the_report_names_slow_files_and_waiting_tests(self):
        self.write(
            "api-times-10-0-1/gw0.jsonl",
            [
                test_row("tests/test_slow.py::test_wait", 25, cpu=1),
                test_row("tests/test_fast.py::test_cpu", 2),
            ],
        )
        text = api_times.render(api_times.summarize(api_times.load(self.root)))
        self.assertIn("Files over 20 s: 1", text)
        self.assertIn("tests/test_slow.py", text)
        self.assertIn("CPU 4%", text)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def arm(self, name, runs):
        """runs: {"<run>-<shard>-<attempt>": [rows]}"""
        for artifact, rows in runs.items():
            path = self.root / name / f"api-times-{artifact}" / "gw0.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return self.root / name

    def compare(self, old, new):
        return api_times.compare(
            api_times.load(self.arm("old", old), attempts_as_runs=True),
            api_times.load(self.arm("new", new), attempts_as_runs=True),
        )

    def test_identical_arms_pass(self):
        rows = [test_row("tests/test_a.py::test_x", 2), test_row("tests/test_a.py::test_s", 0, "skipped", "live only")]
        result = self.compare({"1-0-1": rows}, {"2-0-1": rows})
        self.assertTrue(result["ok"], result)

    def test_a_test_that_stopped_running_fails_the_comparison(self):
        result = self.compare(
            {"1-0-1": [test_row("tests/test_a.py::test_x", 2), test_row("tests/test_a.py::test_y", 2)]},
            {"2-0-1": [test_row("tests/test_a.py::test_x", 2)]},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["only_old"], ["tests/test_a.py::test_y"])

    def test_a_pass_that_became_a_skip_fails_the_comparison(self):
        result = self.compare(
            {"1-0-1": [test_row("tests/test_a.py::test_x", 2)]},
            {"2-0-1": [test_row("tests/test_a.py::test_x", 0, "skipped", "No module named six")]},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["outcome_changes"],
            {"tests/test_a.py::test_x": {"old": ["passed"], "new": ["skipped"]}},
        )

    def test_a_rerun_of_one_shard_is_another_sample_not_a_missing_shard(self):
        shard0 = [test_row("tests/test_a.py::test_x", 2)]
        shard1 = [test_row("tests/test_b.py::test_y", 2)]
        result = self.compare(
            {"1-0-1": shard0, "1-1-1": shard1},
            {"2-0-1": shard0, "2-1-1": shard1, "2-1-2": shard1},
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["samples"], {"old": 2, "new": 3})

    def test_a_test_newly_at_the_timeout_fails_the_comparison(self):
        result = self.compare(
            {"1-0-1": [test_row("tests/test_a.py::test_x", 40), test_row("tests/test_a.py::test_long", 90)]},
            {"2-0-1": [test_row("tests/test_a.py::test_x", 61), test_row("tests/test_a.py::test_long", 95)]},
        )
        self.assertFalse(result["ok"])
        # test_long already ran past 60 s before (it sets its own timeout).
        self.assertEqual(result["newly_at_timeout"], ["tests/test_a.py::test_x"])

    def test_more_samples_above_45_seconds_fail_the_comparison(self):
        result = self.compare(
            {"1-0-1": [test_row("tests/test_a.py::test_x", 44), test_row("tests/test_a.py::test_y", 46)]},
            {"2-0-1": [test_row("tests/test_a.py::test_x", 47), test_row("tests/test_a.py::test_y", 46)]},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["above_45_per_run"], {"old": 1.0, "new": 2.0})

    def test_slow_samples_are_compared_per_run_not_count_for_count(self):
        rows = [test_row("tests/test_a.py::test_y", 46)]
        result = self.compare({"1-0-1": rows}, {"2-0-1": rows, "3-0-1": rows})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["above_45_per_run"], {"old": 1.0, "new": 1.0})


if __name__ == "__main__":
    unittest.main()
