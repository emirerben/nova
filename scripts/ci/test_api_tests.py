"""Tests for deterministic API test-suite sharding.

Ways the weight-packed shards can go wrong, written before the packing (KRI-1544):

1. A test file lands in no shard, or in two.
2. The timings file is missing, unreadable or malformed, and files are lost or
   the runner crashes.
3. A weight is not a positive finite number (a bad refresh) and skews or breaks
   the packing.
4. The timings name a file that no longer exists.
5. A new test file has no timing.
6. Two shard jobs of one run compute different partitions, so a file runs in
   neither. Each job packs on its own runner.
7. The heavy files end up together and one shard is much slower than the rest.
8. pytest gets the files heaviest first, so xdist starts every worker on the
   heaviest renders at once (what pushed a test into its timeout in #2389).
9. The workflow matrix and SHARD_COUNT disagree.
10. A failed API job's existing artifact blocks a retry, or optional timing
    upload failure turns passing tests red.
11. The per-test recorder (tests/_ci_times.py) is never switched on, is not
    loaded by the suite, or writes where the workflow does not keep it, so
    every later measurement is silently empty.
12. A test that used to run starts skipping in CI (a missing package, a
    service that is not there, a shallow clone) and the shard stays green.
13. An expected-skip entry is so broad that a new reason in the same file is
    excused with it.
14. The recorder wrote no rows, and "no rows" is read as "no skips".
15. The expected-skip list is unreadable and every skip is waved through.
16. Rows left by an earlier run in the same directory are judged as this run's.
17. A test that needs git history is added, skips in the shards as allowed, and
    is never added to the job that has the history, so it runs nowhere.
18. Per-worker databases (tests/_worker_database.py): the switch is off and the
    run changes anyway; or it is on and pytest starts more workers than there
    are databases, starts although creating them failed, or never receives the
    switch because the workflow or conftest dropped its half.
19. The switch is on and a worker ran on the shared database anyway (a later
    edit moves the selection, a plugin imports the app first), and the shard is
    green: the isolation is claimed and absent. Or the proof itself is hollow:
    a worker wrote no row, the rows carry no database name, the name comes from
    the environment while the already-imported app is on another database
    (tests/test_ci_times_recorder.py), or pytest's own exit code is lost when
    the proof passes.
"""

import contextlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().with_name("api-tests.py")
REAL_API_ROOT = SCRIPT.parents[2] / "src/apps/api"
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

    def shards(self, api_root=None, timings=None):
        timings = timings or self.api_root / "no-timings.json"
        return [
            api_tests.shard_files(api_root or self.api_root, shard, timings=timings)
            for shard in range(api_tests.SHARD_COUNT)
        ]

    def timings(self, payload):
        path = self.api_root / "timings.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def assert_complete(self, shards, api_root=None):
        everything = [path for shard in shards for path in shard]
        self.assertEqual(len(everything), len(set(everything)))
        self.assertEqual(
            set(everything), set(api_tests.test_files(api_root or self.api_root))
        )

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

    def test_bad_timings_lose_no_file(self):
        """Modes 2 and 3: whatever the timings file holds, every file runs once."""
        heavy = "tests/nested/test_bravo.py"
        for payload in (
            None,
            "not json",
            "[]",
            {"files": []},
            {"files": {heavy: "12"}},
            {"files": {heavy: -5}},
            {"files": {heavy: 0}},
            {"files": {heavy: True}},
            {"files": {heavy: float("nan")}},
            {"files": {heavy: float("inf")}},
            {"files": {heavy: 1e300}},
            {"files": {heavy: 10**400}},
            {"files": {7: 3}},
        ):
            with self.subTest(payload=payload):
                path = (
                    self.api_root / "absent.json"
                    if payload is None
                    else self.timings(payload)
                )
                shards = self.shards(timings=path)
                self.assert_complete(shards)
                self.assertEqual(shards, self.shards(timings=path))

    def test_timings_for_deleted_files_are_ignored(self):
        shards = self.shards(
            timings=self.timings({"files": {"tests/gone/test_removed.py": 500}})
        )
        self.assert_complete(shards)
        self.assertEqual(shards, self.shards())

    def test_a_heavy_file_is_weighed_against_the_light_ones(self):
        """Mode 7: packed by seconds, not by how many files a shard holds."""
        for index in range(20):
            (self.api_root / f"tests/test_light_{index:02}.py").write_text("# light\n")
        heavy = [Path("tests/test_alpha.py"), Path("tests/test_delta.py")]
        timings = self.timings({"files": {str(path): 40 for path in heavy}})
        shards = self.shards(timings=timings)
        self.assert_complete(shards)
        # The two heavy files never share a shard, and a shard that holds one
        # holds fewer files than a shard that holds none.
        holders = [shard for shard in shards if set(heavy) & set(shard)]
        self.assertEqual(len(holders), 2)
        for shard in holders:
            self.assertEqual(len(set(heavy) & set(shard)), 1)
        if len(shards) > 2:
            lightest = min(len(shard) for shard in holders)
            others = [shard for shard in shards if not set(heavy) & set(shard)]
            self.assertTrue(all(len(shard) > lightest for shard in others))

    def test_each_shard_lists_its_files_in_path_order(self):
        """Mode 8: pytest collects in argument order and xdist hands tests out in
        collection order, so the heaviest files must not lead the list."""
        timings = self.timings(
            {"files": {"tests/test_delta.py": 90, "tests/nested/test_charlie.py": 60}}
        )
        for shard in self.shards(timings=timings):
            self.assertEqual(shard, sorted(shard))

    def test_the_workflow_runs_exactly_the_shards_the_runner_partitions_into(self):
        """A matrix with fewer legs than SHARD_COUNT silently never runs a
        quarter of the suite; one with more fails a leg on "selected no files"."""
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        job = workflow[workflow.index("\n  test-api-suite:") :]
        job = job[: job.index("\n    steps:")]
        matrix = re.search(r"^\s+shard: \[([0-9, ]+)\]$", job, re.MULTILINE)
        self.assertIsNotNone(matrix, "test-api-suite must list its shards")
        self.assertEqual(
            [int(value) for value in matrix.group(1).split(",")],
            list(range(api_tests.SHARD_COUNT)),
        )
        self.assertIn("fail-fast: false", job)

    def test_real_suite_shards_carry_about_the_same_measured_time(self):
        """Mode 7 on the real suite and the checked-in timings."""
        api_root = SCRIPT.parents[2] / "src/apps/api"
        seconds = api_tests.file_seconds(api_tests.TIMINGS)
        self.assertGreater(len(seconds), 100, "the timings file did not load")
        shards = self.shards(api_root, timings=api_tests.TIMINGS)
        self.assert_complete(shards, api_root)
        loads = [
            sum(
                seconds.get(path.as_posix(), api_tests.DEFAULT_SECONDS)
                for path in shard
            )
            for shard in shards
        ]
        self.assertLessEqual(max(loads), 1.03 * sum(loads) / len(loads), loads)
        # Stale timings cannot balance anything: most of them must name a file
        # that still exists (refresh with scripts/ci/api-test-timings.py).
        existing = {path.as_posix() for path in api_tests.test_files(api_root)}
        self.assertGreater(len(set(seconds) & existing), 0.8 * len(seconds))

    def test_junit_upload_is_attempt_scoped_and_does_not_gate_api_tests(self):
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        upload = re.search(
            r"^      - name: Keep per-test times\n(?:^ {8,}[^\n]*\n)+",
            workflow,
            re.MULTILINE,
        )
        self.assertIsNotNone(upload, "test-api-suite must keep the timing report")
        self.assertIn("        if: always()\n", upload.group())
        self.assertIn("        continue-on-error: true\n", upload.group())
        self.assertIn(
            "          name: api-junit-${{ github.run_id }}-${{ matrix.shard }}-${{ github.run_attempt }}\n",
            upload.group(),
        )
        suite_step = workflow.split(
            "      - name: Run API test shard with timing reports\n", 1
        )[1].split(upload.group(), 1)[0]
        self.assertNotIn("continue-on-error", suite_step)

    def test_the_shard_run_records_per_test_rows_and_the_workflow_keeps_them(self):
        env = api_tests.pytest_env({"PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin", "NOVA_CI_TIMES_DIR": api_tests.TIMES})
        repo = SCRIPT.parents[2]
        conftest = (repo / "src/apps/api/tests/conftest.py").read_text()
        self.assertIn('pytest_plugins = ("tests._ci_times",)\n', conftest)
        workflow = (repo / ".github/workflows/ci.yml").read_text()
        upload = re.search(
            r"^      - name: Keep per-test times by phase\n(?:^ {8,}[^\n]*\n)+",
            workflow,
            re.MULTILINE,
        )
        self.assertIsNotNone(upload, "test-api-suite must keep the recorder's rows")
        self.assertIn("        if: always()\n", upload.group())
        self.assertIn(f"          path: src/apps/api/{api_tests.TIMES}\n", upload.group())
        self.assertIn(
            "          name: api-times-${{ github.run_id }}-${{ matrix.shard }}-${{ github.run_attempt }}\n",
            upload.group(),
        )

    def skip_rows(self, *skips):
        rows = [{"kind": "test", "nodeid": "tests/test_alpha.py::test_ok",
                 "file": "tests/test_alpha.py", "outcome": "passed", "reason": ""}]
        for nodeid, reason in skips:
            rows.append({"kind": "test", "nodeid": nodeid, "file": nodeid.split("::")[0],
                         "outcome": "skipped", "reason": reason})
        directory = self.api_root / api_tests.TIMES
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gw0.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def expected(self, entries):
        path = self.api_root / "expected-skips.json"
        path.write_text(entries if isinstance(entries, str) else json.dumps({"skips": entries}))
        return path

    def test_a_skip_that_is_not_on_the_list_fails_the_shard(self):
        self.skip_rows(("tests/test_alpha.py::test_new", "Skipped: nova_test Postgres not reachable"))
        expected = self.expected(
            [{"file": "tests/test_delta.py", "reason": "live only", "why": "paid"}]
        )
        self.assertEqual(
            api_tests.unexpected_skips(self.api_root, expected),
            [("tests/test_alpha.py::test_new", "Skipped: nova_test Postgres not reachable")],
        )
        self.assertEqual(api_tests.check_skips(self.api_root, expected), 1)

    def test_a_listed_skip_passes_but_a_new_reason_in_the_same_file_does_not(self):
        expected = self.expected(
            [{"file": "tests/test_alpha.py", "reason": "needs --eval-mode=live", "why": "paid"}]
        )
        self.skip_rows(("tests/test_alpha.py::test_live", "Skipped: needs --eval-mode=live"))
        self.assertEqual(api_tests.check_skips(self.api_root, expected), 0)
        self.skip_rows(
            ("tests/test_alpha.py::test_live", "Skipped: needs --eval-mode=live"),
            ("tests/test_alpha.py::test_other", "Skipped: ffmpeg not installed"),
        )
        self.assertEqual(
            api_tests.unexpected_skips(self.api_root, expected),
            [("tests/test_alpha.py::test_other", "Skipped: ffmpeg not installed")],
        )

    def test_no_recorded_rows_is_a_failure_not_a_clean_run(self):
        expected = self.expected([])
        with self.assertRaisesRegex(ValueError, "no per-test rows"):
            api_tests.unexpected_skips(self.api_root, expected)
        self.assertEqual(api_tests.check_skips(self.api_root, expected), 1)

    def test_an_unreadable_or_malformed_list_fails_closed(self):
        self.skip_rows()
        for payload in ("{not json", json.dumps({"skips": [{"file": "tests/a.py"}]}),
                        json.dumps({"skips": [{"file": "tests/a.py", "reason": "(", "why": "x"}]}),
                        json.dumps({"skips": [{"file": "tests/a.py", "reason": "", "why": "x"}]})):
            with self.assertRaises(ValueError, msg=payload):
                api_tests.unexpected_skips(self.api_root, self.expected(payload))
        self.assertEqual(
            api_tests.check_skips(self.api_root, self.api_root / "missing.json"), 1
        )

    def test_rows_from_an_earlier_run_are_cleared_before_the_shard_starts(self):
        self.skip_rows(("tests/test_alpha.py::test_old", "Skipped: stale"))
        api_tests.clear_rows(self.api_root)
        self.assertFalse((self.api_root / api_tests.TIMES).exists())

    def test_the_committed_list_is_well_formed_and_names_real_files(self):
        entries = api_tests.expected_skips()
        self.assertTrue(entries)
        api_root = SCRIPT.parents[2] / "src/apps/api"
        for file, _ in entries:
            self.assertTrue((api_root / file).is_file(), file)

    def test_every_file_that_asks_for_git_history_runs_in_the_history_job(self):
        (self.api_root / "tests/nested/test_bravo.py").write_text(
            "import pytest\n\n@pytest.mark.needs_git_history\ndef test_x(): ...\n"
        )
        self.assertEqual(
            api_tests.history_files(self.api_root), [Path("tests/nested/test_bravo.py")]
        )
        command, env = api_tests.history_command(self.api_root, {"PATH": "/bin"})
        self.assertIn("tests/nested/test_bravo.py", command)
        self.assertEqual(command[command.index("-m") + 1], "needs_git_history")
        self.assertEqual(env["NOVA_REQUIRE_GIT_HISTORY"], "1")
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        self.assertIn("python3 scripts/ci/api-tests.py history\n", workflow)

    def test_a_history_job_with_nothing_to_run_fails(self):
        with self.assertRaisesRegex(ValueError, "no test file asks for git history"):
            api_tests.history_command(self.api_root, {})

    def test_the_real_suite_has_history_tests(self):
        api_root = SCRIPT.parents[2] / "src/apps/api"
        names = {path.name for path in api_tests.history_files(api_root)}
        self.assertLessEqual({"test_reliability_gates.py", "test_copy_catalog.py"}, names)

    def test_pytest_command_executes_each_selected_file_once(self):
        selected = api_tests.shard_files(self.api_root, 0)
        command = api_tests.pytest_command(selected)

        paths = [argument for argument in command if argument.endswith(".py")]
        self.assertEqual(paths, [str(path) for path in selected])
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("--ignore=tests/quality", command)
        self.assertIn("--durations=20", command)
        # The report the timings are refreshed from, and the workflow keeps it.
        self.assertIn(f"--junitxml={api_tests.JUNIT}", command)
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        self.assertIn(f"path: src/apps/api/{api_tests.JUNIT}\n", workflow)
        self.assertEqual(
            command[command.index("-n") : command.index("-n") + 3],
            ["-n", "auto", "--timeout=60"],
        )

    def test_without_the_switch_the_run_is_the_one_it_was(self):
        for value in (None, "0", "on"):
            environ = {} if value is None else {api_tests.WORKER_DB_SWITCH: value}
            self.assertIsNone(api_tests.worker_databases(environ, cpu_count=lambda: 4))
        selected = api_tests.shard_files(self.api_root, 0)
        command = api_tests.pytest_command(selected)
        self.assertEqual(command, api_tests.pytest_command(selected, None))
        self.assertEqual(command[command.index("-n") + 1], "auto")

    def test_with_the_switch_pytest_gets_exactly_as_many_workers_as_databases(self):
        environ = {api_tests.WORKER_DB_SWITCH: "1"}
        self.assertEqual(api_tests.worker_databases(environ, cpu_count=lambda: 4), 4)
        self.assertEqual(api_tests.worker_databases(environ, cpu_count=lambda: None), 1)
        self.assertEqual(
            api_tests.worker_database_command(4)[1:], ["-m", "tests._worker_database", "4"]
        )
        command = api_tests.pytest_command(api_tests.shard_files(self.api_root, 0), 4)
        self.assertEqual(
            command[command.index("-n") : command.index("-n") + 3], ["-n", "4", "--timeout=60"]
        )

    def run_main(self, environ, statuses, problems=()):
        calls = []
        proof = problems if callable(problems) else (lambda *arguments: list(problems))

        def fake_run(command, **kwargs):
            calls.append((command, dict(kwargs["env"])))
            return mock.Mock(returncode=statuses[len(calls) - 1])

        with (
            mock.patch.object(api_tests.subprocess, "run", fake_run),
            mock.patch.object(api_tests.os, "cpu_count", lambda: 4),
            mock.patch.dict(api_tests.os.environ, environ, clear=True),
            mock.patch.object(api_tests, "check_skips", lambda api_root: 0),
            mock.patch.object(api_tests, "worker_database_problems", proof),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return api_tests.main(["0"]), calls

    def test_the_databases_are_made_before_pytest_and_a_failure_stops_the_shard(self):
        url = "postgresql://postgres:postgres@localhost:5432/nova_test"
        on = {api_tests.WORKER_DB_SWITCH: "1", "DATABASE_URL": url}
        status, calls = self.run_main(on, [0, 0])
        self.assertEqual(status, 0)
        self.assertEqual(calls[0][0][1:4], ["-m", "tests._worker_database", "4"])
        self.assertEqual(calls[1][0][0], "pytest")
        self.assertIn("4", calls[1][0][calls[1][0].index("-n") + 1])
        # Only pytest is told the copies exist, and only after they were made.
        self.assertNotIn(api_tests.WORKER_DB_COPIED_FROM, calls[0][1])
        self.assertEqual(calls[1][1][api_tests.WORKER_DB_COPIED_FROM], url)

        status, calls = self.run_main(on, [3])
        self.assertEqual(status, 3)
        self.assertEqual(len(calls), 1, "pytest must not start without its databases")

        status, calls = self.run_main({}, [0])
        self.assertEqual(status, 0)
        self.assertEqual([call[0][0] for call in calls], ["pytest"])
        self.assertNotIn(api_tests.WORKER_DB_COPIED_FROM, calls[0][1])
        self.assertEqual(calls[0][0][calls[0][0].index("-n") + 1], "auto")

    def test_the_workflow_and_conftest_carry_their_half_of_the_switch(self):
        repo = SCRIPT.parents[2]
        workflow = (repo / ".github/workflows/ci.yml").read_text()
        suite_step = workflow.split(
            "      - name: Run API test shard with timing reports\n", 1
        )[1].split("        run: python3 scripts/ci/api-tests.py", 1)[0]
        self.assertIn(
            f"          {api_tests.WORKER_DB_SWITCH}: "
            "${{ vars.CI_API_DB_PER_WORKER == 'on' && '1' || '0' }}\n",
            suite_step,
        )
        conftest = (repo / "src/apps/api/tests/conftest.py").read_text()
        select = "_worker_database.select(os.environ)\n"
        self.assertIn(select, conftest)
        # Before the first fixture, so before any test or fixture imports the app.
        self.assertLess(conftest.index(select), conftest.index("@pytest.fixture"))
        self.assertNotIn("\nfrom app", conftest.split(select, 1)[0])
        self.assertNotIn("\nimport app", conftest.split(select, 1)[0])
        module = (repo / "src/apps/api/tests/_worker_database.py").read_text()
        self.assertIn(f'SWITCH = "{api_tests.WORKER_DB_SWITCH}"\n', module)
        self.assertIn(f'COPIED_FROM = "{api_tests.WORKER_DB_COPIED_FROM}"\n', module)

    URL = "postgresql://postgres:postgres@localhost:5432/nova_test"

    def startup_rows(self, databases):
        directory = self.api_root / api_tests.TIMES
        directory.mkdir(parents=True, exist_ok=True)
        for worker, database in databases.items():
            row = {"kind": "startup", "worker": worker, "seconds": 1.0, "items": 3}
            if database is not None:
                row["database"] = database
            (directory / f"{worker}.jsonl").write_text(json.dumps(row) + "\n")

    def problems(self, databases, workers=2):
        self.startup_rows(databases)
        expected = api_tests.worker_database_names(REAL_API_ROOT, self.URL, workers)
        return api_tests.worker_database_problems(self.api_root, expected)

    def test_the_expected_names_come_from_the_module_that_makes_the_copies(self):
        self.assertEqual(
            api_tests.worker_database_names(REAL_API_ROOT, self.URL, 3),
            {"gw0": "nova_gw0_test", "gw1": "nova_gw1_test", "gw2": "nova_gw2_test"},
        )

    def test_every_worker_on_its_own_copy_is_no_problem(self):
        self.assertEqual(self.problems({"gw0": "nova_gw0_test", "gw1": "nova_gw1_test"}), [])

    def test_a_worker_on_the_shared_database_or_on_anothers_copy_is_named(self):
        found = self.problems({"gw0": "nova_gw0_test", "gw1": "nova_test"})
        self.assertEqual(len(found), 1)
        self.assertIn("gw1", found[0])
        self.assertIn("nova_test", found[0])
        found = self.problems({"gw0": "nova_gw1_test", "gw1": "nova_gw1_test"})
        self.assertEqual(len(found), 1)
        self.assertIn("gw0", found[0])

    def test_a_worker_that_left_no_row_or_no_database_name_is_a_problem(self):
        self.assertTrue(self.problems({"gw0": "nova_gw0_test"}))
        self.assertTrue(self.problems({"gw0": "nova_gw0_test", "gw1": None}))
        shutil.rmtree(self.api_root / api_tests.TIMES)
        expected = api_tests.worker_database_names(REAL_API_ROOT, self.URL, 2)
        self.assertTrue(api_tests.worker_database_problems(self.api_root, expected))

    def test_a_green_pytest_with_a_worker_on_the_shared_database_fails_the_shard(self):
        on = {api_tests.WORKER_DB_SWITCH: "1", "DATABASE_URL": self.URL}
        status, calls = self.run_main(on, [0, 0], problems=["gw1 ran on 'nova_test'"])
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 2)
        status, _ = self.run_main(on, [0, 5])
        self.assertEqual(status, 5, "pytest's own failure must survive a passing proof")
        status, _ = self.run_main(on, [0, 5], problems=["gw1 ran on 'nova_test'"])
        self.assertEqual(status, 5)

    def test_without_the_switch_the_proof_is_not_asked_for(self):
        def never(*arguments):
            raise AssertionError("no proof without per-worker databases")

        status, _ = self.run_main({}, [0], problems=never)
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
