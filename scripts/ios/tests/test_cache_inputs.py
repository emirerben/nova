"""Cache timestamp restoration must never hide a real source change."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "cache-inputs.py"
spec = importlib.util.spec_from_file_location("cache_inputs", SCRIPT)
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class CacheInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ios-cache-inputs-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".derived-data/\n*.xcodeproj/\n")
        self.source = self.write("src/apps/ios/Kria/View.swift", "let value = 1")
        self.asset = self.write("src/apps/web/public/fonts/Inter-Regular.ttf", "font")
        self.project = self.write(
            "src/apps/ios/Kria.xcodeproj/project.pbxproj", "project"
        )
        self.manifest = (
            self.root / "src/apps/ios/.derived-data/Build/ci-input-times.json"
        )
        self.original_time = 1_700_000_000_000_000_000
        for _, path in cache.inputs(self.root):
            os.utime(path, ns=(self.original_time, self.original_time))

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_unchanged_sources_assets_and_generated_project_reuse_timestamps(self):
        self.assertEqual(cache.save(self.root, self.manifest), 3)
        fresh = self.original_time + 100_000_000_000
        for path in (self.source, self.asset, self.project):
            os.utime(path, ns=(fresh, fresh))
        self.assertEqual(cache.restore(self.root, self.manifest), 3)
        for path in (self.source, self.asset, self.project):
            self.assertEqual(path.stat().st_mtime_ns, self.original_time)

    def test_same_size_changed_source_keeps_fresh_timestamp(self):
        cache.save(self.root, self.manifest)
        self.source.write_text("let value = 2")
        changed_time = self.source.stat().st_mtime_ns
        self.assertEqual(cache.restore(self.root, self.manifest), 2)
        self.assertEqual(self.source.stat().st_mtime_ns, changed_time)
        self.assertEqual(self.source.read_text(), "let value = 2")

    def test_added_and_deleted_inputs_do_not_restore_old_files(self):
        cache.save(self.root, self.manifest)
        self.source.unlink()
        new = self.write("src/apps/ios/Kria/New.swift", "new")
        new_time = new.stat().st_mtime_ns
        cache.restore(self.root, self.manifest)
        self.assertFalse(self.source.exists())
        self.assertEqual(new.stat().st_mtime_ns, new_time)

    def test_missing_or_corrupt_manifest_is_a_cache_miss(self):
        self.assertEqual(cache.restore(self.root, self.manifest), 0)
        self.manifest.parent.mkdir(parents=True)
        for value in (
            "bad json",
            "[]",
            "null",
            '{"src/apps/ios/Kria/View.swift":null}',
        ):
            self.manifest.write_text(value)
            self.assertEqual(cache.restore(self.root, self.manifest), 0)

    def test_manifest_cannot_supply_outside_paths_or_touch_symlinks(self):
        outside = self.write("outside.txt", "private")
        before = outside.stat().st_mtime_ns
        link = self.root / "src/apps/ios/Kria/Link.swift"
        link.symlink_to(outside)
        cache.save(self.root, self.manifest)
        state = json.loads(self.manifest.read_text())
        self.assertNotIn(str(link.relative_to(self.root)), state)
        entry = {"sha256": cache.digest(outside), "mtime_ns": self.original_time}
        state[str(outside)] = entry
        state["../../outside.txt"] = entry
        state[str(link.relative_to(self.root))] = entry
        self.manifest.write_text(json.dumps(state))
        cache.restore(self.root, self.manifest)
        self.assertEqual(outside.stat().st_mtime_ns, before)

    def test_invalid_timestamps_are_ignored(self):
        for timestamp in (True, -1, 2**64, "123", None):
            cache.save(self.root, self.manifest)
            state = json.loads(self.manifest.read_text())
            for entry in state.values():
                entry["mtime_ns"] = timestamp
            self.manifest.write_text(json.dumps(state))
            self.assertEqual(cache.restore(self.root, self.manifest), 0)

    def test_fingerprint_ignores_times_but_tracks_content_and_new_sources(self):
        before = cache.fingerprint(self.root)
        os.utime(self.source, None)
        cache.save(self.root, self.manifest)
        self.assertEqual(cache.fingerprint(self.root), before)
        self.source.write_text("let value = 2")
        changed = cache.fingerprint(self.root)
        self.assertNotEqual(changed, before)
        self.write("src/apps/ios/Kria/Untracked.swift", "new")
        self.assertNotEqual(cache.fingerprint(self.root), changed)


if __name__ == "__main__":
    unittest.main()
