"""Ways the Mac-rule backtest could wrongly say "safe to skip", written first.

- a diff GitHub truncated (300-file cap) read as "no iPhone input changed";
- a renamed file judged by its new path only;
- a Mac failure that a re-run turned green counted as a missed regression, or a
  failure that stayed red dropped because a later attempt was cancelled;
- a run whose Mac was skipped or cancelled counted as a Mac pass;
- a manifest edit the replay could not read (or that is not a plain row
  change) treated as "no native row changed".

Whether `main` was already red is ci-baseline.py `main_ios_before`, tested there.
"""

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().with_name("native-inputs-backtest.py")
spec = importlib.util.spec_from_file_location("native_inputs_backtest", SCRIPT)
backtest = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(backtest)


def job(name, conclusion, attempt=1, steps=()):
    return {
        "name": name,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "failed_steps": list(steps),
    }


class WouldSkipTests(unittest.TestCase):
    def test_api_only_diff_would_skip_and_an_iphone_input_would_not(self):
        self.assertTrue(backtest.would_skip(["src/apps/api/app/kria/policy.py"], False))
        self.assertFalse(
            backtest.would_skip(
                ["src/apps/api/app/kria/policy.py", "src/apps/ios/Kria/App.swift"], False
            )
        )

    def test_a_truncated_or_empty_diff_never_skips(self):
        self.assertFalse(backtest.would_skip(["src/apps/api/app/kria/policy.py"], True))
        self.assertFalse(backtest.would_skip([], False))

    MANIFEST = "scripts/ci/journey-manifest.json"

    def test_a_manifest_edit_skips_only_when_its_changed_rows_are_linux_only(self):
        paths = ["src/apps/api/app/kria/policy.py", self.MANIFEST]
        self.assertTrue(backtest.would_skip(paths, False, (["linux-a"], False)))
        self.assertFalse(backtest.would_skip(paths, False, (["native-c"], True)))
        # Unread or not a plain row change: the manifest still selects a Mac.
        self.assertFalse(backtest.would_skip(paths, False, None))
        self.assertFalse(backtest.would_skip(paths, False))
        # A Linux-only row edit never hides another iPhone input in the diff.
        self.assertFalse(
            backtest.would_skip(
                [self.MANIFEST, "src/apps/ios/Kria/App.swift"], False, (["linux-a"], False)
            )
        )

    def test_a_manifest_is_decoded_from_the_contents_api_or_not_trusted(self):
        import base64

        body = base64.b64encode(b'{"schema_version": 1, "journeys": []}').decode()
        self.assertEqual(
            backtest.decode_manifest(body[:20] + "\n" + body[20:]),
            {"schema_version": 1, "journeys": []},
        )
        self.assertIsNone(backtest.decode_manifest(""))
        self.assertIsNone(backtest.decode_manifest(base64.b64encode(b"{ not json").decode()))
        self.assertIsNone(backtest.decode_manifest("%%% not base64 %%%"))

    def test_a_manifest_is_fetched_as_json_and_a_missing_one_is_not_trusted(self):
        import base64
        import subprocess
        from unittest.mock import patch

        encoded = base64.b64encode(b'{"schema_version": 1, "journeys": []}').decode()
        calls = []

        def api(body, code=0):
            def fake(command, **_):
                calls.append(command)
                return subprocess.CompletedProcess(command, code, stdout=body, stderr="404")

            return fake

        run = backtest.baseline.subprocess
        with patch.object(run, "run", api('{"content":"%s"}\n' % encoded)):
            self.assertEqual(
                backtest.fetch_manifest("o/r", "abc"),
                {"schema_version": 1, "journeys": []},
            )
        self.assertNotIn("--paginate", calls[0])
        self.assertIn("ref=abc", calls[0][4])
        with patch.object(run, "run", api('{"content":null}\n')):
            self.assertIsNone(backtest.fetch_manifest("o/r", "abc"))
        with patch.object(run, "run", api("", code=1)), patch.object(
            backtest.baseline.time, "sleep", lambda _: None
        ):
            self.assertIsNone(backtest.fetch_manifest("o/r", "abc"))

    def test_both_sides_of_a_rename_are_judged(self):
        files = [
            {
                "filename": "src/apps/api/app/kria/moved.py",
                "previous_filename": "src/apps/ios/Kria/Moved.swift",
            }
        ]
        paths = backtest.changed_paths(files)
        self.assertEqual(
            paths, ["src/apps/api/app/kria/moved.py", "src/apps/ios/Kria/Moved.swift"]
        )
        self.assertFalse(backtest.would_skip(paths, False))


class MacOutcomeTests(unittest.TestCase):
    def test_a_failure_a_rerun_turned_green_is_a_flake(self):
        jobs = [
            job("iPhone validation (unit)", "failure", 1, ["Build iPhone app"]),
            job("iPhone validation (unit)", "success", 2),
        ]
        self.assertEqual(backtest.mac_outcome(jobs)["verdict"], "flake")

    def test_a_failure_that_never_went_green_stays_a_failure(self):
        jobs = [
            job("iPhone validation (unit)", "failure", 1, ["Build iPhone app"]),
            job("iPhone validation (unit)", "cancelled", 2),
        ]
        outcome = backtest.mac_outcome(jobs)
        self.assertEqual(outcome["verdict"], "failed")
        self.assertEqual(outcome["failed_steps"], ["Build iPhone app"])

    def test_one_red_leg_is_not_hidden_by_a_green_one(self):
        jobs = [
            job("iPhone validation (unit)", "success", 1),
            job("iPhone validation (ui-1)", "failure", 1, ["Run selected iPhone UI scenarios"]),
        ]
        self.assertEqual(backtest.mac_outcome(jobs)["verdict"], "failed")

    def test_skipped_and_cancelled_macs_are_not_passes(self):
        self.assertEqual(
            backtest.mac_outcome([job("iPhone validation (unit)", "skipped")])["verdict"],
            "not_run",
        )
        self.assertEqual(
            backtest.mac_outcome([job("iPhone validation (unit)", "cancelled")])["verdict"],
            "cancelled",
        )
        self.assertEqual(backtest.mac_outcome([job("api (0)", "failure")])["verdict"], "not_run")

    def test_a_green_mac_is_a_pass(self):
        self.assertEqual(
            backtest.mac_outcome([job("iPhone validation (unit)", "success")])["verdict"],
            "passed",
        )


if __name__ == "__main__":
    unittest.main()
