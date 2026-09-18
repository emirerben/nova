"""Pin verify.sh's UI-phase retry flags so a future edit cannot silently drop them.

The unit phase and the build-for-testing step must stay retry-free: only the
serial UI suite is flaky, and retrying a compile step would just hide a real
build break for three times as long.
"""

from pathlib import Path
import re
import unittest


VERIFY = Path(__file__).resolve().parents[1] / "verify.sh"


class UIRetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.script = VERIFY.read_text()
        match = re.search(r"\nrun_ui\(\) \{\n(.*?)\n\}\n", self.script, re.DOTALL)
        self.assertIsNotNone(match, "Could not locate run_ui() in verify.sh")
        self.run_ui_body = match.group(1)
        self.before, self.after = (
            self.script[: match.start()],
            self.script[match.end() :],
        )

    def test_run_ui_retries_the_ui_phase_only(self):
        self.assertIn("-retry-tests-on-failure", self.run_ui_body)
        self.assertIn("-test-iterations 3", self.run_ui_body)
        self.assertNotIn("-retry-tests-on-failure", self.before)
        self.assertNotIn("-retry-tests-on-failure", self.after)
        self.assertNotIn("-test-iterations", self.before)
        self.assertNotIn("-test-iterations", self.after)

    def test_run_ui_always_runs_verify_and_stays_fail_closed(self):
        # The control-flow fix: capture xcodebuild's status without tripping
        # `set -e`, run verify whenever the result bundle exists, and only
        # succeed when both signals are green.
        self.assertIn("xcodebuild_status=$?", self.run_ui_body)
        self.assertIn("verify_status=$?", self.run_ui_body)
        self.assertIn('[[ -e "$RESULT_DIR/ui.xcresult" ]]', self.run_ui_body)


if __name__ == "__main__":
    unittest.main()
