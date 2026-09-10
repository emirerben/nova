"""Exercise classification, real git diffs, and required-check failure behavior."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().with_name("select-tests.py")
spec = importlib.util.spec_from_file_location("select_tests", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
ALL = {"web", "api", "ios"}


class ClassificationTests(unittest.TestCase):
    def test_boundaries(self):
        examples = {
            "src/apps/web/src/app/page.tsx": {"web"},
            "src/apps/web/package.json": {"web"},
            "scripts/ci/web-tests.mjs": {"web"},
            "src/apps/ios/project.yml": {"ios"},
            "scripts/ios/verify.sh": {"ios"},
            ".github/workflows/ios.yml": {"ios"},
            "src/apps/api/app/pipeline/reframe.py": {"api"},
            "src/apps/api/tests/test_routes.py": {"api"},
            "src/apps/api/prompts/writer.md": {"api"},
            "src/apps/api/app/routes/me.py": ALL,
            "src/apps/api/app/schemas/jobs.py": ALL,
            "src/apps/api/app/kria/contracts.py": ALL,
            "src/apps/api/app/config.py": ALL,
            "src/apps/api/pyproject.toml": ALL,
            "src/packages/motion-runtime/motion-limits.json": ALL,
            ".github/workflows/ci.yml": ALL,
            ".github/workflows/ci-changes.yml": ALL,
            "scripts/ci/select-tests.py": ALL,
            "scripts/ci/test_select_tests.py": ALL,
            "Dockerfile": ALL,
            "Makefile": ALL,
            "assets/fonts/font.ttf": ALL,
            "new-runtime/README.md": ALL,
            "docs/runbooks/ci.md": set(),
            "CHANGELOG.md": set(),
            "VERSION": set(),
        }
        for path, expected in examples.items():
            with self.subTest(path=path):
                self.assertEqual(ci.affected(path), expected)

    def test_non_pr_always_full(self):
        for event in ("push", "workflow_dispatch", None, "merge_group"):
            self.assertEqual(ci.selection(event, "", "")[0], ALL)

    def test_diff_error_falls_back_to_full(self):
        with patch.object(
            ci, "git", side_effect=subprocess.CalledProcessError(1, "git")
        ):
            self.assertEqual(ci.selection("pull_request", "bad", "bad")[0], ALL)


class GateTests(unittest.TestCase):
    def needs(self, selected, result):
        return {
            "changes": {"result": "success", "outputs": {"web": selected}},
            "suite": {"result": result},
        }

    def test_only_explicit_unselected_skip_passes(self):
        for selected in ("true", "false", "", None, "TRUE"):
            for result in ("success", "skipped", "failure", "cancelled", None):
                with self.subTest(selected=selected, result=result):
                    needs = self.needs(selected, result)
                    if (selected, result) in (
                        ("true", "success"),
                        ("false", "skipped"),
                    ):
                        ci.gate(needs, "web", "suite")
                    else:
                        with self.assertRaises(ValueError):
                            ci.gate(needs, "web", "suite")

    def test_selector_failure_and_missing_jobs_never_pass(self):
        for result in ("failure", "cancelled", "skipped", None):
            needs = self.needs("false", "skipped")
            needs["changes"]["result"] = result
            with self.assertRaises(ValueError):
                ci.gate(needs, "web", "suite")
        for needs in (
            {},
            {"changes": {"result": "success"}},
            {"suite": {"result": "success"}},
        ):
            with self.assertRaises(ValueError):
                ci.gate(needs, "web", "suite")

    def test_lint_union(self):
        for web, api in (
            ("true", "false"),
            ("false", "true"),
            ("true", "true"),
            ("false", "false"),
        ):
            result = "skipped" if web == api == "false" else "success"
            needs = {
                "changes": {"result": "success", "outputs": {"web": web, "api": api}},
                "lint-suites": {"result": result},
            }
            ci.gate(needs, "lint", "lint-suites")
        with self.assertRaises(ValueError):
            ci.gate(self.needs("false", "skipped"), "lint", "suite")


class GitDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old)
        self.git("init", "-q")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "CI tests")
        self.write("README.md", "initial")
        self.write("src/apps/web/old.ts", "export default 1;")
        self.write(
            "package.json", json.dumps({"version": "1", "scripts": {"test": "test"}})
        )
        self.write(
            "package-lock.json",
            json.dumps(
                {
                    "version": "1",
                    "packages": {
                        "": {"version": "1"},
                        "node_modules/a": {"version": "1"},
                    },
                }
            ),
        )
        self.base = self.commit()

    def git(self, *args):
        return (
            subprocess.check_output(["git", *args], stderr=subprocess.PIPE)
            .decode()
            .strip()
        )

    def write(self, path, value):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "test")
        return self.git("rev-parse", "HEAD")

    def selected(self):
        return ci.selection("pull_request", self.base, self.commit())[0]

    def test_release_metadata_does_not_wake_suites(self):
        for path in ("package.json", "package-lock.json"):
            data = json.loads(Path(path).read_text())
            data["version"] = "2"
            if "packages" in data:
                data["packages"][""]["version"] = "2"
            self.write(path, json.dumps(data))
        self.write("VERSION", "2")
        self.assertEqual(self.selected(), set())

    def test_dependency_version_still_runs_all(self):
        data = json.loads(Path("package-lock.json").read_text())
        data["packages"]["node_modules/a"]["version"] = "2"
        self.write("package-lock.json", json.dumps(data))
        self.assertEqual(self.selected(), ALL)

    def test_deleted_and_malformed_manifests_run_all(self):
        Path("package.json").unlink()
        self.write("package-lock.json", "bad json")
        self.assertEqual(self.selected(), ALL)

    def test_rename_checks_both_sides(self):
        Path("docs").mkdir()
        self.git("mv", "src/apps/web/old.ts", "docs/old.ts")
        self.assertEqual(self.selected(), {"web"})

    def test_deletion_and_mixed_changes(self):
        Path("src/apps/web/old.ts").unlink()
        self.write("scripts/ios/new.sh", "true")
        self.assertEqual(self.selected(), {"web", "ios"})

    def test_names_with_newlines_are_not_split(self):
        self.write("src/apps/web/new\nfile.ts", "test")
        self.assertEqual(self.selected(), {"web"})

    def test_merge_base_excludes_unrelated_base_changes(self):
        self.git("checkout", "-qb", "pr")
        self.write("src/apps/web/new.ts", "test")
        head = self.commit()
        self.git("checkout", "-q", self.base)
        self.write("src/apps/api/app/pipeline/new.py", "test")
        moved_base = self.commit()
        self.assertEqual(ci.selection("pull_request", moved_base, head)[0], {"web"})

    def test_empty_diff_runs_all(self):
        self.assertEqual(ci.selection("pull_request", self.base, self.base)[0], ALL)

    def test_cli_outputs_and_summary(self):
        self.write("src/apps/ios/new.swift", "test")
        head = self.commit()
        output, summary = Path("output"), Path("summary")
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT.resolve()),
            ],
            check=True,
            env={
                **os.environ,
                "CI_EVENT": "pull_request",
                "CI_BASE": self.base,
                "CI_HEAD": head,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
            },
            capture_output=True,
        )
        self.assertEqual(output.read_text(), "web=false\napi=false\nios=true\n")
        self.assertIn("ios: run", summary.read_text())


if __name__ == "__main__":
    unittest.main()
