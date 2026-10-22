"""Offline release-history regressions; no Apple or GitHub calls.

Failure modes: workflow SHA differs from binary; successful skipped upload;
failed/partial upload; lost/ambiguous checkout evidence; out-of-order runs;
unknown or divergent commits; empty/truncated history silently permits upload.
"""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "upload_provenance.py"
spec = importlib.util.spec_from_file_location("upload_provenance", PATH)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

OLD = "c58b89bbdf54afd8db8bf6dca15bc6eee5e59559"
HEAD = "90067edbc3fc7fd1efb5c591e6f0084045028176"
WORKFLOW = "110e8719" + "0" * 32


def log(sha=OLD, uploaded=True):
    lines = [
        "release\tRun actions/checkout@v4\t2026-10-21T19:58:25Z [command]/opt/homebrew/bin/git log -1 --format=%H",
        f"release\tRun actions/checkout@v4\t2026-10-21T19:58:26Z {sha}",
    ]
    if uploaded:
        lines.append(
            "release\tUpload and distribute to external TestFlight\t2026-10-21T20:10:24Z [20:10:24]: Successfully uploaded the new binary to App Store Connect"
        )
    return "\n".join(lines)


def attempt(id, end, upload="success", archive="success"):
    return {
        "id": id,
        "completedAt": end,
        "steps": [
            {"name": p.ARCHIVE, "conclusion": archive},
            {"name": p.UPLOAD, "conclusion": upload},
        ],
    }


class ProvenanceTests(unittest.TestCase):
    def test_reruns_preserve_previous_attempts_including_current_run(self):
        runs = [
            {"databaseId": 1, "status": "completed", "attempt": 2},
            {"databaseId": 2, "status": "in_progress", "attempt": 2},
            {"databaseId": 3, "status": "queued", "attempt": 1},
        ]
        self.assertEqual(p.history_attempts(runs, "2"), [(1, 1), (1, 2), (2, 1)])
        with self.assertRaises(p.ProvenanceError):
            p.history_attempts(runs, "99")

    def test_legacy_unknown_step_requires_runner_command_boundaries(self):
        evidence = (
            log()
            .replace("Run actions/checkout@v4", "UNKNOWN STEP")
            .replace(p.UPLOAD, "UNKNOWN STEP")
        )
        with self.assertRaises(p.ProvenanceError):
            p.latest_source([attempt(1, "a")], lambda _: evidence)
        lines = evidence.splitlines()
        lines.insert(
            0,
            "release\tUNKNOWN STEP\t2026-10-21T19:58:24Z ##[group]Run actions/checkout@v4",
        )
        lines.insert(
            3,
            "release\tUNKNOWN STEP\t2026-10-21T20:00:00Z ##[group]Run BUNDLE_GEMFILE=fastlane/Gemfile bundle _2.7.2_ exec fastlane ios testflight",
        )
        self.assertEqual(
            p.latest_source([attempt(1, "a")], lambda _: "\n".join(lines)), OLD
        )

    def test_unallocated_runner_requires_complete_raw_assignment_metadata(self):
        # Raw Actions metadata for 39443830702/1. GitHub's check annotation
        # independently confirms no hosted runner ever acquired this job.
        job = {
            "status": "completed",
            "conclusion": "cancelled",
            "runner_id": 0,
            "runner_name": "",
            "runner_group_id": 0,
            "runner_group_name": "",
            "steps": [],
        }
        self.assertTrue(p.never_executed(job))
        for key, value in [
            ("runner_id", 42),
            ("runner_name", "runner"),
            ("runner_group_id", 1),
            ("runner_group_name", "hosted"),
            ("steps", [{"name": p.UPLOAD}]),
            ("conclusion", "success"),
            ("status", "in_progress"),
        ]:
            with self.subTest(key=key):
                self.assertFalse(p.never_executed(dict(job, **{key: value})))
        for key in job:
            with self.subTest(missing=key):
                self.assertFalse(
                    p.never_executed({k: v for k, v in job.items() if k != key})
                )

    def test_incident_uses_checkout_source_not_workflow_head(self):
        a = attempt(39666180034, "2026-10-21T20:14:00Z")
        a["headSha"] = WORKFLOW
        self.assertEqual(p.latest_source([a], lambda _: log()), OLD)

    def test_successful_workflow_with_skipped_upload_is_not_evidence(self):
        attempts = [attempt(2, "b", "skipped", "skipped"), attempt(1, "a")]
        self.assertEqual(p.latest_source(attempts, lambda _: log()), OLD)

    def test_fastlane_duplicate_success_does_not_claim_new_source(self):
        logs = {
            1: log(),
            2: log(HEAD, False)
            + "\nrelease\t"
            + p.UPLOAD
            + "\t2026-10-21T20:20:00Z TestFlight already has build 1671 for 0.1.0; skipping duplicate upload 1670.",
        }
        self.assertEqual(
            p.latest_source([attempt(2, "b"), attempt(1, "a")], logs.__getitem__), OLD
        )

    def test_execution_order_wins_over_run_id(self):
        self.assertEqual(
            p.latest_source(
                [attempt(99, "a"), attempt(10, "b")],
                lambda id: log(HEAD if id == 10 else OLD),
            ),
            HEAD,
        )

    def test_missing_or_ambiguous_provenance_blocks(self):
        for evidence in ["", log(uploaded=False), log() + "\n" + log(HEAD)]:
            with self.subTest(evidence=evidence), self.assertRaises(p.ProvenanceError):
                p.latest_source([attempt(1, "a")], lambda _: evidence)

    def test_partial_failed_upload_with_confirmed_transfer_still_protects_source(self):
        self.assertEqual(
            p.latest_source([attempt(1, "a", "failure")], lambda _: log()), OLD
        )

    def test_failed_upload_without_transfer_evidence_blocks(self):
        with self.assertRaises(p.ProvenanceError):
            p.latest_source([attempt(2, "b", "failure"), attempt(1, "a")], lambda _: "")

    def test_no_verified_upload_blocks_including_history_limit(self):
        with self.assertRaises(p.ProvenanceError):
            p.latest_source([], lambda _: "")

    def test_archive_must_have_succeeded(self):
        with self.assertRaises(p.ProvenanceError):
            p.latest_source([attempt(1, "a", archive="failure")], lambda _: log())

    def test_real_git_ancestry_duplicate_older_newer_and_unverifiable(self):
        with tempfile.TemporaryDirectory() as repo:

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", repo, *args], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            git("commit", "--allow-empty", "-qm", "old")
            old = git("rev-parse", "HEAD")
            git("commit", "--allow-empty", "-qm", "new")
            new = git("rev-parse", "HEAD")
            self.assertEqual(p.compare_source(new, old, new, repo), "base")
            self.assertEqual(p.compare_source(old, new, new, repo), "superseded")
            self.assertEqual(p.compare_source(new, new, new, repo), "superseded")
            for source in ["f" * 40, "main", ""]:
                with self.subTest(source=source), self.assertRaises(p.ProvenanceError):
                    p.compare_source(new, source, new, repo)
            git("checkout", "--orphan", "other")
            git("commit", "--allow-empty", "-qm", "divergent")
            with self.assertRaises(p.ProvenanceError):
                p.compare_source(new, git("rev-parse", "HEAD"), new, repo)


if __name__ == "__main__":
    unittest.main()
