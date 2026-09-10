"""Regression coverage for KRI-31's deterministic release owner."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "release_metadata.py"
spec = importlib.util.spec_from_file_location("release_metadata", SCRIPT)
release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = release
spec.loader.exec_module(release)


CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [0.75.10.0] - 2026-09-10

### Fixed
- First release note

## [0.76.0.0] - 2026-09-10

### Added
- New slide editor

## [0.75.10.0] - 2026-09-10

### Fixed
- Second release note
"""


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "VERSION").write_text("0.76.0.0\n")
        (self.repo / "CHANGELOG.md").write_text(CHANGELOG)
        (self.repo / "package.json").write_text(json.dumps({"version": "0.75.10", "dependencies": {"x": "1"}}))
        (self.repo / "package-lock.json").write_text(
            json.dumps({"version": "0.75.10", "packages": {"": {"version": "0.75.10"}}})
        )

    def test_prepare_repairs_drift_and_is_idempotent(self):
        version = release.prepare_release(
            self.repo, 31, "Automate changelog metadata", ["release:added"], "2026-09-10"
        )
        self.assertEqual(str(version), "0.76.1.0")
        self.assertEqual((self.repo / "VERSION").read_text(), "0.76.1.0\n")
        self.assertEqual(json.loads((self.repo / "package.json").read_text())["version"], "0.76.1")
        lock = json.loads((self.repo / "package-lock.json").read_text())
        self.assertEqual(lock["version"], "0.76.1")
        self.assertEqual(lock["packages"][""]["version"], "0.76.1")
        changelog = (self.repo / "CHANGELOG.md").read_text()
        self.assertLess(changelog.index("[0.76.1.0]"), changelog.index("[0.76.0.0]"))
        self.assertEqual(changelog.count("## [0.75.10.0]"), 1)
        self.assertIn("First release note", changelog)
        self.assertIn("Second release note", changelog)
        self.assertIn("<!-- release-pr: 31 -->", changelog)
        snapshot = {name: (self.repo / name).read_text() for name in release.ROOT_FILES}
        self.assertIsNone(release.prepare_release(self.repo, 31, "ignored", [], "2026-09-10"))
        self.assertEqual(snapshot, {name: (self.repo / name).read_text() for name in release.ROOT_FILES})

    def test_labels_default_for_absent_and_conflicting_values(self):
        self.assertEqual(release.release_labels([]), ("patch", "Changed"))
        self.assertEqual(release.release_labels(["release:minor", "release:fixed"]), ("minor", "Fixed"))
        self.assertEqual(
            release.release_labels(["release:major", "release:minor", "release:fixed"]),
            ("patch", "Fixed"),
        )
        self.assertEqual(
            release.release_labels(["release:major", "release:added", "release:fixed"]),
            ("major", "Changed"),
        )

    def test_validation_rejects_unsynchronized_package_versions(self):
        with self.assertRaises(release.ReleaseMetadataError):
            release.validate_release_metadata(self.repo)

    def test_validation_requires_the_changelog_head_to_match_version(self):
        (self.repo / "CHANGELOG.md").write_text(
            release.normalize_changelog((self.repo / "CHANGELOG.md").read_text())
        )
        (self.repo / "VERSION").write_text("0.76.1.0\n")
        for name in ("package.json", "package-lock.json"):
            value = json.loads((self.repo / name).read_text())
            value["version"] = "0.76.1"
            if name == "package-lock.json":
                value["packages"][""]["version"] = "0.76.1"
            (self.repo / name).write_text(json.dumps(value))
        with self.assertRaisesRegex(release.ReleaseMetadataError, "newest CHANGELOG"):
            release.validate_release_metadata(self.repo)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def test_guard_allows_dependencies_but_rejects_manual_version_edits(self):
        self.git("init", "-q")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "CI")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        package = json.loads((self.repo / "package.json").read_text())
        package["dependencies"]["x"] = "2"
        (self.repo / "package.json").write_text(json.dumps(package))
        self.git("add", "package.json")
        self.git("commit", "-qm", "dependency update")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.assertEqual(release.guard_pr_metadata(self.repo, base, head), [])
        (self.repo / "VERSION").write_text("0.76.1.0\n")
        self.assertTrue(release.guard_pr_metadata(self.repo, "HEAD", working=True))

    def test_workflows_and_preship_use_the_release_owner(self):
        root = SCRIPT.parents[1]
        workflow = (root / ".github/workflows/release-metadata.yml").read_text()
        ownership = (root / ".github/workflows/release-metadata-ownership.yml").read_text()
        preship = (root / "scripts/preship-check.sh").read_text()
        self.assertIn("types: [closed]", workflow)
        self.assertIn("actions/create-github-app-token@v1", workflow)
        self.assertIn("RELEASE_METADATA_APP_ID", workflow)
        self.assertIn("RELEASE_METADATA_APP_PRIVATE_KEY", workflow)
        self.assertIn("--pr-number", workflow)
        self.assertIn("for attempt in 1 2 3 4 5", workflow)
        self.assertIn("--head", ownership)
        self.assertIn("release_metadata.py guard", preship)


if __name__ == "__main__":
    unittest.main()
