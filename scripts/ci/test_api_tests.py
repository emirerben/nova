"""Tests for deterministic API test-suite sharding."""

import importlib.util
from pathlib import Path
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

    def test_shards_are_complete_disjoint_and_deterministic(self):
        first = api_tests.shard_files(self.api_root, 0)
        second = api_tests.shard_files(self.api_root, 1)

        self.assertEqual(first, api_tests.shard_files(self.api_root, 0))
        self.assertIn(Path("tests/nested/suffix_test.py"), first + second)
        self.assertIn(Path("tests/nested/quality/test_still_included.py"), first + second)
        self.assertFalse(set(first) & set(second))
        self.assertEqual(
            set(first) | set(second), set(api_tests.test_files(self.api_root))
        )
        self.assertEqual(len(first) + len(second), len(set(first) | set(second)))

    def test_new_test_file_is_included_without_a_manifest_update(self):
        new_file = self.api_root / "tests/new_area/test_new_coverage.py"
        new_file.parent.mkdir(parents=True)
        new_file.write_text("# newly added\n")

        selected = set(api_tests.shard_files(self.api_root, 0)) | set(
            api_tests.shard_files(self.api_root, 1)
        )
        self.assertIn(new_file.relative_to(self.api_root), selected)
        self.assertNotIn(Path("tests/quality/test_expensive.py"), selected)

    def test_invalid_or_empty_shards_fail_closed(self):
        for shard in (-1, 2):
            with self.subTest(shard=shard), self.assertRaises(ValueError):
                api_tests.shard_files(self.api_root, shard)
        with self.assertRaises(ValueError):
            api_tests.shard_files(self.api_root, 0, shard_count=3)

        empty = Path(self.tmp.name) / "empty"
        (empty / "tests/quality").mkdir(parents=True)
        (empty / "tests/quality/test_only_quality.py").write_text("# excluded\n")
        with self.assertRaisesRegex(ValueError, "selected no test files"):
            api_tests.shard_files(empty, 0)

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
