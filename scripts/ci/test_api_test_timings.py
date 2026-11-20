"""Tests for the script that turns CI junit reports into the shard timings file.

Ways it can go wrong, written before the script (KRI-1544):

1. A test in a class (or a nested class) is credited to no file, or to the
   wrong one, because junit only gives a dotted classname.
2. One slow runner sets a file's weight: the middle of several reports must.
3. A file that a report does not mention gets a made-up weight.
4. Light files flood the timings file.
5. A report from another suite (no matching file) is counted.
6. Failed or copied earlier attempts bias the median, or rerunning one shard
   drops the other shards' earlier reports.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("api-test-timings.py")
spec = importlib.util.spec_from_file_location("api_test_timings", SCRIPT)
timings = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(timings)


def report(cases):
    rows = "".join(
        f'<testcase classname="{classname}" name="{name}" time="{seconds}"/>'
        for classname, name, seconds in cases
    )
    return f'<testsuites><testsuite name="pytest">{rows}</testsuite></testsuites>'


class TimingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = [
            Path("tests/test_top.py"),
            Path("tests/pipeline/test_render.py"),
            Path("tests/pipeline/test_light.py"),
            Path("tests/pipeline/test_unreported.py"),
        ]

    def write(self, name, cases):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report(cases))

    def test_cases_are_credited_to_their_file_and_the_middle_report_wins(self):
        render = "tests.pipeline.test_render"
        self.write(
            "run1/a.xml", [(render, "test_a", 4), (f"{render}.TestX", "test_b", 6)]
        )
        self.write(
            "run2/a.xml",
            [(render, "test_a", 30), (f"{render}.TestX.Inner", "test_b", 30)],
        )
        self.write(
            "run3/a.xml", [(render, "test_a", 5), (f"{render}.TestX", "test_b", 7)]
        )
        self.write(
            "run1/b.xml",
            [
                ("tests.test_top", "test_c", 8),
                ("tests.pipeline.test_light", "test_d", 0.2),
            ],
        )
        self.write("run1/other.xml", [("web.test_elsewhere", "test_e", 500)])
        seconds, unowned = timings.measure(self.root, self.files)
        self.assertEqual(seconds["tests/pipeline/test_render.py"], 12)
        self.assertEqual(seconds["tests/test_top.py"], 8)
        self.assertEqual(seconds["tests/pipeline/test_light.py"], 0.2)
        self.assertNotIn("tests/pipeline/test_unreported.py", seconds)
        self.assertEqual(unowned, 1)

    def test_uses_latest_available_report_per_run_and_shard(self):
        render = "tests.pipeline.test_render"
        self.write("nested/api-junit-10-0-1/api-junit.xml", [(render, "test_a", 100)])
        self.write("api-junit-10-0-2/api-junit.xml", [(render, "test_a", 12)])
        self.write("api-junit-10-0-10/api-junit.xml", [(render, "test_a", 20)])
        self.write("copy/api-junit-10-0-10/api-junit.xml", [(render, "test_a", 20)])
        self.write("api-junit-10-1-1/api-junit.xml", [("tests.test_top", "test_b", 8)])
        (self.root / "api-junit-10-1-2").mkdir()
        self.write("api-junit-11-0-1/api-junit.xml", [(render, "test_a", 30)])
        self.write("legacy/junit.xml", [(render, "test_a", 40)])

        seconds, unowned = timings.measure(self.root, self.files)
        self.assertEqual(seconds["tests/pipeline/test_render.py"], 30)
        self.assertEqual(seconds["tests/test_top.py"], 8)
        self.assertEqual(unowned, 0)

    def test_only_heavy_files_are_written_and_the_file_reads_back(self):
        payload = timings.payload(
            {"tests/a.py": 41.26, "tests/b.py": timings.MIN_LISTED_SECONDS - 0.01},
            "run 1",
        )
        self.assertEqual(payload["files"], {"tests/a.py": 41.3})
        self.assertEqual(payload["measured"], "run 1")
        out = self.root / "timings.json"
        out.write_text(json.dumps(payload))
        api_tests = timings.api_tests
        self.assertEqual(api_tests.file_seconds(out), {"tests/a.py": 41.3})


if __name__ == "__main__":
    unittest.main()
