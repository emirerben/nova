"""Fixture-driven check of scripts/hooks/merge-guard.py (KRI-1396).

Runs the hook as the harness does (event JSON on stdin) with a stub `gh` first on
PATH. Red control: `MERGE_GUARD_SCRIPT=<copy of the pre-fix hook> python3 -m pytest
scripts/ci/test_merge_guard.py` must fail on the wrapped-merge and false-positive cases.
"""

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = Path(os.environ.get("MERGE_GUARD_SCRIPT") or ROOT / "scripts/hooks/merge-guard.py")

STUB = """#!/usr/bin/env python3
import os, sys
open(os.environ["STUB_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
print({"red": '[{"name":"api","bucket":"fail"},{"name":"lint","bucket":"pass"}]',
       "running": '[{"name":"api","bucket":"pending"}]',
       "green": '[{"name":"api","bucket":"pass"},{"name":"ios","bucket":"skipping"}]',
       "none": "[]"}[os.environ["STUB_STATE"]])
"""

MERGE = "gh pr merge 5 --squash"
BLOCKED = {
    "plain": MERGE,
    "env prefix": "FOO=1 " + MERGE,
    "after &&": "git push && " + MERGE,
    "subshell": "(gh pr merge 5)",
    "subshell with flags": "(" + MERGE + ")",
    "bash -c": 'bash -c "gh pr merge 5"',
    "sh -c single quotes": "sh -c '" + MERGE + "'",
    "bash -lc": 'bash -lc "gh pr merge 5"',
    "command substitution": "echo $(gh pr merge 5)",
    "substitution in quotes": 'echo "merged: $(gh pr merge 5)"',
    "backticks": "echo `gh pr merge 5`",
    "eval": 'eval "gh pr merge 5"',
    "heredoc to bash": "bash <<'EOF'\ngh pr merge 5\nEOF",
    "unquoted heredoc substitution": "cat <<EOF\n$(gh pr merge 5)\nEOF",
    "second command on a later line": "gh pr view 5\n" + MERGE,
    "api PUT": "gh api -X PUT repos/o/r/pulls/5/merge -f merge_method=squash",
    "api method flag": "gh api repos/o/r/pulls/5/merge --method PUT",
    "unbalanced quote falls back to text": "echo 'oops && gh pr merge 5",
}
ALLOWED = {
    "heredoc commit message": 'git commit -m "$(cat <<\'EOF\'\nfix\n\nthen gh pr merge 5\nEOF\n)"',
    "multi-line body": 'gh pr create --body "first\ngh pr merge 5\nlast"',
    "plain heredoc data": "cat > notes.md <<'EOF'\ngh pr merge 5\nEOF",
    "quoted mention": 'echo "run gh pr merge 5 later"',
    "single-quoted mention": "echo 'gh pr merge 5'",
    "other gh commands": "gh pr view 5 && gh pr checks 5 && gh api repos/o/r/pulls/5",
    "auto merge": "gh pr merge 5 --auto --squash",
    "disable auto": "gh pr merge 5 --disable-auto",
    "not a merge at all": "git status",
}


def run_hook(command, state="red", cwd=None, raw=None):
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp, "gh")
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        log = Path(tmp, "calls.log")
        env = {**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"}
        env.update(STUB_STATE=state, STUB_LOG=str(log))
        event = raw if raw is not None else json.dumps({"tool_input": {"command": command}, "cwd": cwd})
        done = subprocess.run(
            [sys.executable, str(HOOK)], input=event, capture_output=True, text=True, env=env, timeout=60
        )
        calls = log.read_text().splitlines() if log.exists() else []
    return done.returncode, done.stderr, calls


class MergeGuardTests(unittest.TestCase):
    def test_wrapped_and_plain_merges_of_a_red_pr_are_blocked(self):
        for name, command in BLOCKED.items():
            with self.subTest(name):
                code, err, calls = run_hook(command)
                self.assertEqual(code, 2, err)
                self.assertIn("api: fail", err)
                self.assertTrue(calls and calls[0].startswith("pr checks 5"), calls)

    def test_text_that_only_mentions_a_merge_is_not_blocked(self):
        for name, command in ALLOWED.items():
            with self.subTest(name):
                code, err, calls = run_hook(command)
                self.assertEqual((code, calls), (0, []), err)

    def test_running_and_missing_checks_block_and_green_passes(self):
        for state, code in (("running", 2), ("none", 2), ("green", 0)):
            with self.subTest(state):
                self.assertEqual(run_hook("bash -c 'gh pr merge 5'", state)[0], code)

    def test_repo_is_forwarded(self):
        self.assertIn("--repo o/r", run_hook("gh pr merge 5 -R o/r")[2][0])
        self.assertIn("--repo o/r", run_hook("gh api -X PUT repos/o/r/pulls/5/merge")[2][0])

    def test_unreadable_pr_blocks(self):
        for command in ("gh pr merge", "gh api graphql -f query='mutation{mergePullRequest(input:{})}'"):
            with self.subTest(command):
                self.assertEqual(run_hook(command)[0], 2)

    def test_internal_errors_exit_2_not_1(self):
        self.assertEqual(run_hook("", raw="not json")[0], 2)
        spec = importlib.util.spec_from_file_location("merge_guard", HOOK)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        if not hasattr(guard, "check"):  # pre-fix script: no fail-closed wrapper
            self.fail("hook has no fail-closed wrapper")
        guard.merges = lambda command: (_ for _ in ()).throw(RuntimeError("boom"))
        stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps({"tool_input": {"command": MERGE}}))
            self.assertEqual(guard.main(), 2)
        finally:
            sys.stdin = stdin

    def test_missing_gh_blocks(self):
        done = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_input": {"command": MERGE}}),
            capture_output=True, text=True, env={**os.environ, "PATH": "/nonexistent"},
        )
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_settings_registers_the_hook_with_a_timeout_it_cannot_outlive(self):
        spec = importlib.util.spec_from_file_location("merge_guard", HOOK)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        hooks = [
            hook
            for entry in settings["hooks"]["PreToolUse"]
            if entry["matcher"] == "Bash"
            for hook in entry["hooks"]
            if "merge-guard.py" in hook["command"]
        ]
        self.assertEqual(len(hooks), 1)
        self.assertGreater(hooks[0]["timeout"], getattr(guard, "BUDGET", 10**6))
        self.assertLess(hooks[0]["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
