"""Lifecycle E2E: real Git hooks/queue, with the costly simulator boundary stubbed.

Failure cases fixed before implementation: duplicate events, overlapping runs,
dirty source checkouts, pulls/rebases, failed drives, lost cleanup/evidence,
missing prerequisites, existing hooks, and hooks recursively firing in snapshots.
Real XCUITest proof is recorded separately; these fixtures prove orchestration.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]


class AutomaticVerificationE2E(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "test-results/verify-kria-ios/auto"
        self.env = dict(
            os.environ,
            KRIA_VERIFY_AUTO_ROOT=str(self.state),
            KRIA_VERIFY_AUTO_NOTIFICATIONS="0",
        )
        self.env.pop("CI", None)
        self.env.pop("KRIA_VERIFY_AUTO_DISABLED", None)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        for name in [
            "scripts/ios/auto-verify.py",
            "scripts/ios/feature_map.py",
            "scripts/ios/ui_tests.py",
            "scripts/hooks/kria-verification-dispatch.py",
            ".cursor/skills/verify-kria-ios/features/journeys.json",
        ]:
            source = ROOT / name
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        data = json.loads((self.repo / name).read_text())
        tests = sorted({test for entry in data["features"] for test in entry["tests"]})
        for test in tests:
            case, method = test.split("/")
            source = self.repo / f"src/apps/ios/Tests/KriaUITests/{case}.swift"
            source.parent.mkdir(parents=True, exist_ok=True)
            if not source.exists():
                source.write_text(f"class {case}: XCTestCase {{\n")
            with source.open("a") as stream:
                stream.write(f"    func {method}() {{}}\n")
        control = self.repo / ".cursor/skills/verify-kria-ios/control-kria-ios"
        control.write_text("""#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[3]
command=sys.argv[1]; run=sys.argv[sys.argv.index("--run")+1]
directory=root/"test-results/verify-kria-ios"/run
directory.mkdir(parents=True,exist_ok=True)
state=directory/"run.json"
value=json.loads(state.read_text()) if state.exists() else {"cleaned":False}
with (directory/"calls.txt").open("a") as log: log.write(command+"\\n")
if command=="launch":
    import subprocess
    value["head"]=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
    state.write_text(json.dumps(value))
    if (root/"SLOW_LAUNCH").exists(): time.sleep(10)
    time.sleep(0.3)
if command=="drive":
    if (root/"FAIL_DRIVE").exists(): sys.exit(1)
    (directory/"proof.json").write_text(json.dumps({"head":value["head"],"status":"passed"}))
if command=="cleanup": value["cleaned"]=True
state.write_text(json.dumps(value))
""")
        control.chmod(0o755)
        (self.repo / ".gitignore").write_text("test-results/\n")
        self.commit("seed")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def cli(self, *args, check=True):
        return subprocess.run(
            ["python3", "scripts/ios/auto-verify.py", *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    def wait_result(self, count=1):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            paths = list((self.state / "requests").glob("*.json"))
            rows = [json.loads(path.read_text()) for path in paths]
            if len(rows) == count and all(
                row["status"] not in {"queued", "running"} for row in rows
            ):
                return rows
            time.sleep(0.05)
        self.fail(f"Background requests did not finish: {rows}")

    def change(self, name="src/apps/ios/Kria/Features/TeleprompterScreen.swift"):
        self.git("checkout", "-b", "codex/fixture")
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("native change\n")
        return self.commit("native")

    def test_duplicate_events_verify_frozen_commit_and_keep_dirty_files(self):
        head = self.change()
        (self.repo / "src/apps/ios/Kria/Features/TeleprompterScreen.swift").write_text(
            "unsaved changes\n"
        )
        for reason in ["session", "pr", "agent-finish"]:
            self.cli("enqueue", "--reason", reason)
        row = self.wait_result()[0]
        self.assertEqual(
            (row["head"], row["features"], row["status"]),
            (head, ["teleprompter"], "passed"),
        )
        evidence = Path(row["evidence"])
        self.assertEqual(
            (evidence / "calls.txt").read_text(), "launch\ndoctor\ndrive\ncleanup\n"
        )
        self.assertTrue(json.loads((evidence / "run.json").read_text())["cleaned"])
        self.assertEqual(
            (
                self.repo / "src/apps/ios/Kria/Features/TeleprompterScreen.swift"
            ).read_text(),
            "unsaved changes\n",
        )
        self.assertFalse(Path(row["snapshot"]).exists())
        self.assertEqual(len(json.loads(self.cli("status", "--json").stdout)), 1)

    def test_failed_drive_cleans_and_remains_failed_until_explicit_retry(self):
        self.change()
        (self.repo / "FAIL_DRIVE").touch()
        self.commit("failing boundary")
        self.cli("enqueue", "--reason", "pr")
        row = self.wait_result()[0]
        self.assertEqual(row["status"], "failed")
        self.assertTrue(
            json.loads((Path(row["evidence"]) / "run.json").read_text())["cleaned"]
        )
        self.assertIn("failed", self.cli("status").stdout)
        self.cli("enqueue", "--reason", "pr")
        self.assertEqual(len(self.wait_result()), 1)

    def test_docs_only_creates_no_worker_or_verification(self):
        self.change("docs/new.md")
        self.cli("enqueue", "--reason", "pr")
        self.assertEqual(json.loads(self.cli("status", "--json").stdout), [])

    def test_post_merge_and_rewrite_hooks_preserve_existing_hooks(self):
        hooks = self.repo / ".git/hooks"
        old = hooks / "post-merge"
        old.write_text('#!/bin/sh\nprintf "previous\\n" >> previous-hook.txt\n')
        old.chmod(0o755)
        self.cli("install")
        self.cli("install")
        self.change()
        self.git("checkout", "main")
        self.git("merge", "--ff-only", "codex/fixture")
        row = self.wait_result()[0]
        self.assertEqual(row["status"], "passed")
        self.assertEqual((self.repo / "previous-hook.txt").read_text(), "previous\n")
        dispatcher = Path(self.git("config", "core.hooksPath")) / "post-rewrite"
        subprocess.run(
            [str(dispatcher), "rebase"],
            cwd=self.repo,
            env=self.env,
            input="",
            text=True,
            check=True,
        )
        self.assertEqual(len(self.wait_result()), 1)

    def test_pre_push_queues_without_waiting_and_preserves_rejection(self):
        self.change()
        self.cli("install")
        hook = Path(self.git("config", "core.hooksPath")) / "pre-push"
        start = time.monotonic()
        subprocess.run(
            [str(hook), "origin", "fixture"],
            cwd=self.repo,
            env=self.env,
            input="",
            text=True,
            check=True,
        )
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.wait_result()[0]["status"], "passed")
        old = self.repo / ".git/hooks/pre-push"
        old.write_text("#!/bin/sh\nexit 17\n")
        old.chmod(0o755)
        result = subprocess.run(
            [str(hook), "origin", "fixture"],
            cwd=self.repo,
            env=self.env,
            input="",
            text=True,
        )
        self.assertEqual(result.returncode, 17)

    def test_relative_hooks_path_and_uninstall_restore_original_configuration(self):
        hooks = self.repo / "local-hooks"
        hooks.mkdir()
        (hooks / "post-merge").write_text("#!/bin/sh\ntouch relative-hook-ran\n")
        (hooks / "post-merge").chmod(0o755)
        self.git("config", "core.hooksPath", "local-hooks")
        self.cli("install")
        self.change()
        self.git("checkout", "main")
        self.git("merge", "--ff-only", "codex/fixture")
        self.assertEqual(self.wait_result()[0]["status"], "passed")
        self.assertTrue((self.repo / "relative-hook-ran").exists())
        self.cli("uninstall")
        self.assertEqual(self.git("config", "core.hooksPath"), "local-hooks")

    def test_real_pull_queues_the_updated_commit(self):
        remote = self.root / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(self.repo), str(remote)],
            check=True,
            capture_output=True,
        )
        self.git("remote", "add", "origin", str(remote))
        self.cli("install")
        head = self.change()
        self.env["KRIA_VERIFY_AUTO_DISABLED"] = "1"
        self.git("push", "origin", "HEAD:refs/heads/update")
        self.env.pop("KRIA_VERIFY_AUTO_DISABLED")
        self.git("checkout", "main")
        self.git("pull", "--ff-only", "origin", "update")
        row = self.wait_result()[0]
        self.assertEqual(
            (row["reason"], row["head"], row["status"]), ("post-merge", head, "passed")
        )

    def test_real_rebase_verifies_rewritten_native_commit_even_if_tree_is_unchanged(
        self,
    ):
        before = self.change()
        self.git("checkout", "main")
        (self.repo / "upstream.txt").write_text("unrelated upstream change")
        self.commit("upstream")
        self.git("checkout", "codex/fixture")
        self.cli("install")
        self.git("rebase", "main")
        after = self.git("rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        row = self.wait_result()[0]
        self.assertEqual(
            (row["reason"], row["head"], row["status"]),
            ("post-rewrite", after, "passed"),
        )

    def test_restart_recovers_abandoned_run_and_keeps_its_evidence(self):
        self.change()
        self.cli("enqueue", "--reason", "pr")
        row = self.wait_result()[0]
        row["status"] = "running"
        # Simulate a worker which died after cleanup/evidence, before saving status.
        (self.state / "requests" / f"{row['key']}.json").write_text(json.dumps(row))
        self.cli("worker", "--root", str(self.state))
        recovered = self.wait_result()[0]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertTrue((Path(recovered["evidence"]) / "proof.json").exists())
        self.cli("retry", row["key"])
        self.assertEqual(
            {r["status"] for r in self.wait_result(2)}, {"interrupted", "passed"}
        )

    def test_push_of_non_current_branch_verifies_that_pushed_commit(self):
        native = self.change()
        self.git("checkout", "main")
        self.cli("install")
        hook = Path(self.git("config", "core.hooksPath")) / "pre-push"
        payload = (
            f"refs/heads/codex/fixture {native} refs/heads/codex/fixture {'0' * 40}\n"
        )
        subprocess.run(
            [str(hook), "origin", "fixture"],
            cwd=self.repo,
            env=self.env,
            input=payload,
            text=True,
            check=True,
        )
        self.assertEqual(self.wait_result()[0]["head"], native)

    def test_killed_worker_recovers_owned_child_and_preserves_unrelated_process(self):
        self.change()
        (self.repo / "SLOW_LAUNCH").touch()
        self.commit("slow external boundary")
        unrelated = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"])
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.terminate)
        self.cli("enqueue", "--reason", "pr")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = json.loads(self.cli("status", "--json").stdout)
            if rows and rows[0].get("active_child"):
                row = rows[0]
                break
            time.sleep(0.05)
        else:
            self.fail("Owned helper child did not start")
        # Ensure launch wrote its cleanup record before killing the worker alone.
        state = (
            Path(row["snapshot"])
            / "test-results/verify-kria-ios"
            / row["key"]
            / "run.json"
        )
        while not state.exists():
            time.sleep(0.02)
        os.kill(row["worker_pid"], signal.SIGKILL)
        self.cli("worker", "--root", str(self.state))
        result = self.wait_result()[0]
        self.assertEqual(result["status"], "interrupted", result)
        self.assertTrue(
            json.loads((Path(result["evidence"]) / "run.json").read_text())["cleaned"]
        )
        self.assertIsNone(unrelated.poll())


if __name__ == "__main__":
    unittest.main()
