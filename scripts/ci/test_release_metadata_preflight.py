"""Release ownership failures must stop selection before expensive CI queues."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
STEP = "Validate PR release metadata before selecting suites"


class ReleaseMetadataPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/release_metadata.py", self.repo / "scripts")
        (self.repo / "VERSION").write_text("1.0.0.0\n")
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n")
        self.write_package("1.0.0")
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Preflight tests")
        self.git("config", "user.email", "preflight@example.invalid")
        self.base = self.commit("base")
        self.git("remote", "add", "origin", str(self.repo))
        self.git("checkout", "-qb", "feature")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def write_package(self, version, dependency="1"):
        (self.repo / "package.json").write_text(
            json.dumps({"version": version, "dependencies": {"library": dependency}})
        )
        (self.repo / "package-lock.json").write_text(
            json.dumps({"version": version, "packages": {"": {"version": version}}})
        )

    def run_preflight(self, head, base_ref="main"):
        workflow = (ROOT / ".github/workflows/ci-changes.yml").read_text()
        step = workflow.split(f"      - name: {STEP}\n", 1)[1]
        script = step.split("        run: |\n", 1)[1].split("      - name:", 1)[0]
        script = "\n".join(line[10:] for line in script.splitlines())
        marker = self.repo / "selected"
        marker.unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script + "\ntouch selected\n"],
            cwd=self.repo,
            env={**os.environ, "BASE_REF": base_ref, "HEAD_SHA": head},
            capture_output=True,
            text=True,
        )
        return result, marker.exists()

    def test_manual_metadata_edits_block_selection(self):
        for path, value in (
            ("VERSION", "2.0.0.0\n"),
            ("CHANGELOG.md", "# Manual release note\n"),
            ("package.json", '{"version":"2.0.0"}'),
            ("package-lock.json", '{"version":"2.0.0"}'),
            (
                "package-lock.json",
                '{"version":"1.0.0","packages":{"":{"version":"2.0.0"}}}',
            ),
        ):
            with self.subTest(path=path, value=value):
                self.git("checkout", "-q", self.base, "--", path)
                (self.repo / path).write_text(value)
                head = self.commit("manual metadata")
                result, selected = self.run_preflight(head)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("post-merge automation-owned", result.stderr)
                self.assertFalse(selected)
                self.git("checkout", "-q", self.base, "--", path)

    def test_inherited_metadata_and_dependency_updates_pass(self):
        self.git("checkout", "-q", "main")
        (self.repo / "VERSION").write_text("1.1.0.0\n")
        self.write_package("1.1.0")
        self.commit("automated release")
        self.git("checkout", "-q", "feature")
        self.git("merge", "--no-edit", "main")
        self.write_package("1.1.0", dependency="2")
        result, selected = self.run_preflight(self.commit("dependency update"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(selected)

    def test_event_head_is_checked_instead_of_merge_checkout(self):
        (self.repo / "VERSION").write_text("2.0.0.0\n")
        invalid_head = self.commit("manual metadata")
        self.git("checkout", "-q", "main")
        result, selected = self.run_preflight(invalid_head)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(selected)

    def test_missing_history_and_failed_fetch_stop_selection(self):
        for head, base in (("f" * 40, "main"), (self.base, "missing-branch")):
            with self.subTest(head=head, base=base):
                result, selected = self.run_preflight(head, base)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(selected)

    def test_corrected_commit_recovers_without_branch_update(self):
        (self.repo / "VERSION").write_text("2.0.0.0\n")
        result, selected = self.run_preflight(self.commit("manual metadata"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(selected)
        (self.repo / "VERSION").write_text("1.0.0.0\n")
        (self.repo / "feature.txt").write_text("feature\n")
        result, selected = self.run_preflight(self.commit("restore release ownership"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(selected)

    def test_preflight_precedes_selection_and_matches_existing_scope(self):
        workflow = (ROOT / ".github/workflows/ci-changes.yml").read_text()
        step = workflow.split(f"      - name: {STEP}\n", 1)[1].split(
            "      - name:", 1
        )[0]
        self.assertIn(
            "if: github.event_name == 'pull_request' && github.event.pull_request.base.ref == 'main'",
            step,
        )
        self.assertIn("HEAD_SHA: ${{ github.event.pull_request.head.sha }}", step)
        self.assertIn("BASE_REF: ${{ github.event.pull_request.base.ref }}", step)
        self.assertNotIn("continue-on-error", step)
        self.assertLess(
            workflow.index(STEP), workflow.index("- name: Select affected suites")
        )


if __name__ == "__main__":
    unittest.main()
