"""Tests for deterministic API test-suite sharding."""

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("api-tests.py")
spec = importlib.util.spec_from_file_location("api_tests", SCRIPT)
api_tests = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api_tests)


class ApiTestShardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api_root = Path(self.tmp.name)
        tests = self.api_root / "tests"
        for name in (
            "test_alpha.py",
            "nested/test_bravo.py",
            "nested/test_charlie.py",
            "test_delta.py",
            "nested/suffix_test.py",
            "nested/quality/test_still_included.py",
            "quality/test_expensive.py",
        ):
            path = tests / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n")

    def shards(self, api_root=None):
        return [
            api_tests.shard_files(api_root or self.api_root, shard)
            for shard in range(api_tests.SHARD_COUNT)
        ]

    def test_shards_are_complete_disjoint_and_deterministic(self):
        shards = self.shards()
        everything = [path for shard in shards for path in shard]

        self.assertEqual(shards, self.shards())
        self.assertIn(Path("tests/nested/suffix_test.py"), everything)
        self.assertIn(Path("tests/nested/quality/test_still_included.py"), everything)
        self.assertEqual(len(everything), len(set(everything)))
        self.assertEqual(set(everything), set(api_tests.test_files(self.api_root)))

    def test_new_test_file_is_included_without_a_manifest_update(self):
        new_file = self.api_root / "tests/new_area/test_new_coverage.py"
        new_file.parent.mkdir(parents=True)
        new_file.write_text("# newly added\n")

        selected = {path for shard in self.shards() for path in shard}
        self.assertIn(new_file.relative_to(self.api_root), selected)
        self.assertNotIn(Path("tests/quality/test_expensive.py"), selected)

    def test_invalid_or_empty_shards_fail_closed(self):
        for shard in (-1, api_tests.SHARD_COUNT):
            with self.subTest(shard=shard), self.assertRaises(ValueError):
                api_tests.shard_files(self.api_root, shard)
        with self.assertRaises(ValueError):
            api_tests.shard_files(
                self.api_root, 0, shard_count=api_tests.SHARD_COUNT + 1
            )

        empty = Path(self.tmp.name) / "empty"
        (empty / "tests/quality").mkdir(parents=True)
        (empty / "tests/quality/test_only_quality.py").write_text("# excluded\n")
        with self.assertRaisesRegex(ValueError, "selected no test files"):
            api_tests.shard_files(empty, 0)

    def test_the_workflow_runs_exactly_the_shards_the_runner_partitions_into(self):
        """A matrix with fewer legs than SHARD_COUNT silently never runs a
        quarter of the suite; one with more fails a leg on "selected no files"."""
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        job = workflow[workflow.index("\n  test-api-suite:") :]
        job = job[: job.index("\n    steps:")]
        matrix = re.search(r"^\s+shard: \[([0-9, ]+)\]$", job, re.M)
        self.assertIsNotNone(matrix, "test-api-suite must list its shards")
        self.assertEqual(
            [int(value) for value in matrix.group(1).split(",")],
            list(range(api_tests.SHARD_COUNT)),
        )
        self.assertIn("fail-fast: false", job)

    def test_real_suite_shards_are_even_enough_to_be_worth_their_setup(self):
        """Round-robin over sorted files: no shard may be starved or bloated."""
        api_root = SCRIPT.parents[2] / "src/apps/api"
        sizes = [len(shard) for shard in self.shards(api_root)]
        self.assertEqual(sum(sizes), len(api_tests.test_files(api_root)))
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_pytest_command_executes_each_selected_file_once(self):
        selected = api_tests.shard_files(self.api_root, 0)
        command = api_tests.pytest_command(selected)

        paths = [argument for argument in command if argument.endswith(".py")]
        self.assertEqual(paths, [str(path) for path in selected])
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("--ignore=tests/quality", command)
        self.assertIn("--durations=20", command)
        self.assertEqual(
            command[command.index("-n") : command.index("-n") + 3],
            ["-n", "auto", "--timeout=60"],
        )


if __name__ == "__main__":
    unittest.main()
