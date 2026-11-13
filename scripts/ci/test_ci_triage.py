"""Guard tests for the ci-triage nudge hook and the triage helper.

Written before the code, per CLAUDE.md "Testing rules". Ways this can fail:

Nudge hook (scripts/hooks/ci-triage-nudge.py)
  F1  It blocks or slows a tool call: any crash, bad stdin, or missing `gh`
      must exit 0 with no output. That includes SystemExit raised by the
      triage helper's REST fallback at session start (seen live: exit 1), and
      a session-start check slower than its 15s hook timeout (seen live: 59s,
      one REST call per check run).
  F2  It never fires: misses red output from `gh pr checks` (tab text, JSON
      `bucket`), `gh run watch/view` ("completed with 'failure'", X lines), or
      `gh pr view --json statusCheckRollup` (`conclusion: FAILURE`).
  F3  It fires on green / pending-only output, or on commands that are not CI
      reads (a `grep fail` over a log file, a pytest run).
  F4  It loops: the triage helper's own `gh` calls re-trigger the nudge.
  F5  It nags: the same red state in the same session nudges every poll of
      `gh run watch`.
  F6  Its output is not valid PostToolUse JSON, so Claude never sees it.
  F7  The tool_response shape changes (dict vs str) and detection silently
      stops.

Triage helper (.claude/skills/ci-triage/scripts/triage.py)
  F8  A check name is mapped to the wrong local command (the reproduction
      runs something unrelated and passes, giving false confidence).
  F9  Infra flakes (runner shutdown, no space, 5xx) are reported as test
      failures, so the agent edits code that was never broken.
  F10 Failing pytest node ids in the log are lost, so the repro reruns a whole
      40-minute shard instead of the failing tests.
  F11 A job link that is not an Actions job URL crashes the helper.
  F12 A gate job is mapped by its name alone. CI's `lint` gate also validates
      PR evidence, and `lint`/`test-api`/`build-and-test` fail whenever an
      upstream job failed, so "lint -> ruff" sends the agent to the wrong fix.
      Seen live on PR #2289: `lint` was red for a stale evidence head_sha.
  F14 The REST fallback reports runs a newer push superseded (auto-cancelled
      by `concurrency: cancel-in-progress`) as red. Seen live on PR #2220.
  F13 REST job logs (no `job<TAB>step<TAB>` prefix) keep their timestamps.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/hooks/ci-triage-nudge.py"
TRIAGE = ROOT / ".claude/skills/ci-triage/scripts/triage.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_hook(event, *, session: str = "s1", tmp: str | None = None):
    env = dict(os.environ)
    env["TMPDIR"] = tmp or tempfile.mkdtemp()
    payload = (
        event
        if isinstance(event, str)
        else json.dumps({"session_id": session, **event})
    )
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def bash_event(command: str, stdout: str, stderr: str = ""):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": False},
    }


CHECKS_RED = "lint\tpass\t31s\thttps://x\napi (1)\tfail\t6m2s\thttps://github.com/o/r/actions/runs/1/job/2\n"
CHECKS_GREEN = "lint\tpass\t31s\thttps://x\napi (1)\tpass\t6m2s\thttps://x\n"
CHECKS_PENDING = "lint\tpass\t31s\thttps://x\napi (1)\tpending\t0\thttps://x\n"


class NudgeHook(unittest.TestCase):
    def nudged(self, result) -> str:
        self.assertEqual(result.returncode, 0, result.stderr)  # F1
        if not result.stdout.strip():
            return ""
        out = json.loads(result.stdout)  # F6
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return out["hookSpecificOutput"]["additionalContext"]

    def test_bad_stdin_is_silent(self):  # F1
        result = run_hook("not json")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_session_start_without_pr_is_silent(self):  # F1
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "feature", repo], check=True)
        env = dict(os.environ, GH_TOKEN="invalid", GH_HOST="github.invalid")
        result = subprocess.run(
            [sys.executable, str(HOOK), "--session-start"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_red_pr_checks_text_nudges(self):  # F2
        self.assertIn(
            "ci-triage",
            self.nudged(run_hook(bash_event("gh pr checks 812", CHECKS_RED))),
        )

    def test_red_json_bucket_nudges(self):  # F2
        out = json.dumps([{"name": "api (0)", "bucket": "fail"}])
        self.assertTrue(
            self.nudged(run_hook(bash_event("gh pr checks --json name,bucket", out)))
        )

    def test_red_run_watch_nudges(self):  # F2
        out = "X main CI · 123\nRun CI (123) completed with 'failure'\n"
        self.assertTrue(self.nudged(run_hook(bash_event("gh run watch 123", out))))

    def test_red_rollup_conclusion_nudges(self):  # F2
        out = json.dumps(
            {"statusCheckRollup": [{"name": "lint", "conclusion": "FAILURE"}]}
        )
        self.assertTrue(
            self.nudged(
                run_hook(bash_event("gh pr view 9 --json statusCheckRollup", out))
            )
        )

    def test_failure_reported_on_stderr_nudges(self):  # F7
        result = run_hook(
            bash_event("gh pr checks", "", "Some checks were not successful")
        )
        self.assertTrue(self.nudged(result))

    def test_string_tool_response_nudges(self):  # F7
        event = bash_event("gh pr checks", "")
        event["tool_response"] = CHECKS_RED
        self.assertTrue(self.nudged(run_hook(event)))

    def test_green_and_pending_are_silent(self):  # F3
        self.assertFalse(
            self.nudged(run_hook(bash_event("gh pr checks", CHECKS_GREEN)))
        )
        self.assertFalse(
            self.nudged(run_hook(bash_event("gh pr checks", CHECKS_PENDING)))
        )

    def test_non_ci_commands_are_silent(self):  # F3
        self.assertFalse(
            self.nudged(run_hook(bash_event("grep fail ci.log", CHECKS_RED)))
        )
        self.assertFalse(self.nudged(run_hook(bash_event("pytest -x", "1 failed"))))

    def test_triage_helper_does_not_retrigger(self):  # F4
        cmd = "python3 .claude/skills/ci-triage/scripts/triage.py 812"
        self.assertFalse(self.nudged(run_hook(bash_event(cmd, CHECKS_RED))))

    def test_same_red_state_nudges_once_per_session(self):  # F5
        tmp = tempfile.mkdtemp()
        event = bash_event("gh run watch 5", "X job\ncompleted with 'failure'")
        self.assertTrue(self.nudged(run_hook(event, tmp=tmp)))
        self.assertFalse(self.nudged(run_hook(event, tmp=tmp)))
        self.assertTrue(self.nudged(run_hook(event, session="s2", tmp=tmp)))


class TriageHelper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = load(TRIAGE, "ci_triage")

    def repro(self, name: str, workflow: str = "CI", log: str = "") -> str:
        return self.t.reproduce(name, workflow, log)

    def test_mapping(self):  # F8
        self.assertIn("ruff check", self.repro("lint"))
        self.assertIn("api-tests.py 1", self.repro("api (1)"))
        self.assertIn("make ios-verify", self.repro("ios-tests (native, 2/5)", "iOS"))
        self.assertIn("kria_contracts --check", self.repro("ios-contracts"))
        self.assertIn(
            "pytest tests/evals/",
            self.repro("Structural evals (PR guardrail)", "Agent evals"),
        )
        self.assertIn(
            "check_claude_md_size.sh", self.repro("guard", "CLAUDE.md size guard")
        )
        self.assertIn(
            "check_layer2_cache_bump.sh",
            self.repro("guard", "Layer-2 cache-version guard"),
        )
        self.assertIn(
            "make verify-overlays", self.repro("verify-overlays (render paths only)")
        )
        self.assertIn("PR body", self.repro("PR evidence", "PR evidence"))

    def test_unknown_check_points_at_workflow_not_a_guess(self):  # F8
        self.assertIn(
            "workflow", self.repro("brand-new-check", "Something new").lower()
        )

    def test_infra_flakes_are_not_test_failures(self):  # F9
        for log in (
            "The runner has received a shutdown signal.",
            "No space left on device",
            "error: RPC failed; HTTP 502",
            "The hosted runner lost communication with the server.",
        ):
            self.assertEqual(self.t.classify("fail", log), "infra-flake", log)
        self.assertEqual(self.t.classify("cancel", ""), "cancelled")
        self.assertEqual(
            self.t.classify("fail", "FAILED tests/x.py::test_a - AssertionError"),
            "failure",
        )

    def test_failing_pytest_ids_are_targeted(self):  # F10
        log = (
            "api (0)\tRun\t2026-11-13T10:00:00.0Z FAILED tests/routes/test_me.py::test_a - assert 1\n"
            "api (0)\tRun\t2026-11-13T10:00:00.0Z FAILED tests/x/test_b.py::T::test_c[p1] - E\n"
        )
        cmd = self.repro("api (0)", log=log)
        self.assertIn("tests/routes/test_me.py::test_a", cmd)
        self.assertIn("'tests/x/test_b.py::T::test_c[p1]'", cmd)
        self.assertNotIn("api-tests.py", cmd)

    def test_log_wins_over_check_name(self):  # F12
        log = "2026-11-13T12:35:48.08Z PR request evidence: head_sha must match the exact current PR head SHA"
        self.assertIn("PR body", self.repro("lint", log=log))

    def test_gate_points_at_upstream_failures(self):  # F12
        failing = [
            {"name": "lint"},
            {"name": "lint-suites"},
            {"name": "api (1)"},
            {"name": "test-api"},
        ]
        self.assertEqual(self.t.upstream("lint", failing), ["lint-suites"])
        self.assertEqual(self.t.upstream("test-api", failing), ["api (1)"])
        self.assertEqual(self.t.upstream("lint", [{"name": "lint"}]), [])
        self.assertEqual(self.t.upstream("api (1)", failing), [])

    def test_rest_log_timestamps_are_stripped(self):  # F13
        self.assertEqual(
            self.t.clean("\ufeff2026-11-13T09:55:41.0227067Z hello"), ["hello"]
        )
        self.assertEqual(self.t.clean("lint\tRun\t2026-11-13T09:55:41.02Z hi"), ["hi"])

    def test_superseded_runs_are_dropped(self):  # F14
        runs = [
            {
                "id": 1,
                "name": "PR evidence",
                "workflow": "PR evidence",
                "bucket": "cancel",
            },
            {
                "id": 3,
                "name": "PR evidence",
                "workflow": "PR evidence",
                "bucket": "pass",
            },
            {
                "id": 2,
                "name": "guard",
                "workflow": "CLAUDE.md size guard",
                "bucket": "pass",
            },
            {
                "id": 4,
                "name": "guard",
                "workflow": "Layer-2 cache-version guard",
                "bucket": "fail",
            },
        ]
        latest = self.t.latest(runs)
        self.assertEqual(
            sorted((c["name"], c["bucket"]) for c in latest),
            [("PR evidence", "pass"), ("guard", "fail"), ("guard", "pass")],
        )

    def test_odd_links_do_not_crash(self):  # F11
        self.assertEqual(self.t.job_ref("https://example.com/whatever"), (None, None))
        self.assertEqual(self.t.job_ref(""), (None, None))
        self.assertEqual(
            self.t.job_ref("https://github.com/o/r/actions/runs/11/job/22"),
            ("11", "22"),
        )


if __name__ == "__main__":
    unittest.main()
