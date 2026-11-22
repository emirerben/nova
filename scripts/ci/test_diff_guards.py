"""Ways the diff-based PR guards can pass without checking, written before the fix.

`src/apps/api/scripts/check_layer2_cache_bump.sh` and
`scripts/check_extension_zip_fresh.sh` each read the PR's diff and fail when a
watched file changed without its companion change. Each could report green
without having looked:

- the diff itself fails (a base commit the checkout does not have, a shallow
  clone) and the guard reads the empty output as "nothing changed";
- the fix for that turns an honest empty or unrelated diff into a failure;
- the guard stops catching the change it exists for.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARDS = {
    "layer2": REPO / "src/apps/api/scripts/check_layer2_cache_bump.sh",
    "extension": REPO / "scripts/check_extension_zip_fresh.sh",
}
MISSING_COMMIT = "0123456789abcdef0123456789abcdef01234567"


class DiffGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.repo = Path(cls.tmp.name)
        cls.git("init", "-q", "-b", "main")
        cls.write("src/apps/api/app/pipeline/template_cache.py",
                  "def compute_text_overlay_version():\n    return 'x'\n")
        cls.write("src/apps/api/app/agents/text_alignment.py", "prompt_version = '1'\n")
        cls.write("src/apps/extension/src/popup.ts", "export const a = 1;\n")
        cls.write("README.md", "one\n")
        cls.base = cls.commit("base")
        cls.write("README.md", "two\n")
        cls.unrelated = cls.commit("docs only")
        cls.write("src/apps/api/app/agents/text_alignment.py",
                  "prompt_version = '1'\nBEHAVIOUR = 2\n")
        cls.write("src/apps/extension/src/popup.ts", "export const a = 2;\n")
        cls.watched = cls.commit("watched files, no companion change")

    @classmethod
    def git(cls, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=cls.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    @classmethod
    def write(cls, name, text):
        path = cls.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    @classmethod
    def commit(cls, message):
        cls.git("add", "-A")
        cls.git("commit", "-q", "-m", message)
        return cls.git("rev-parse", "HEAD")

    def run_guard(self, name, base, head):
        env = {**os.environ, "BASE_SHA": base, "HEAD_SHA": head}
        return subprocess.run(
            ["bash", str(GUARDS[name])], cwd=self.repo, env=env,
            capture_output=True, text=True, check=False,
        )

    def test_a_diff_that_cannot_be_read_fails_instead_of_passing(self):
        for name in GUARDS:
            result = self.run_guard(name, MISSING_COMMIT, self.watched)
            self.assertNotEqual(result.returncode, 0, f"{name}: {result.stdout}")
            self.assertNotIn("nothing to check", result.stdout, name)

    def test_an_unrelated_or_empty_diff_still_passes(self):
        for name in GUARDS:
            for head in (self.unrelated, self.base):
                result = self.run_guard(name, self.base, head)
                self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_a_watched_change_without_its_companion_still_fails(self):
        for name in GUARDS:
            result = self.run_guard(name, self.unrelated, self.watched)
            self.assertEqual(result.returncode, 1, f"{name}: {result.stdout}{result.stderr}")
            self.assertIn("FAIL", result.stderr, name)


if __name__ == "__main__":
    unittest.main()
