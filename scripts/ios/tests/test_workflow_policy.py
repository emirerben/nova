"""Offline contract for the iOS PR runtime budget and regression coverage."""

from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/ios.yml"


class IOSWorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text()

    def test_pr_required_gate_runs_unit_mode(self):
        self.assertIn(
            "github.event_name == 'pull_request' && 'unit' || 'prepare-ui'",
            self.workflow,
        )
        self.assertIn("- name: Build and unit tests", self.workflow)

    def test_full_ui_regression_is_deferred_but_fail_closed(self):
        ui_step = self.workflow.split("- name: Native UI tests (reuse compiled build)", 1)[1]
        self.assertIn("if: github.event_name != 'pull_request'", ui_step)
        self.assertIn("KRIA_IOS_UI_GROUPS: full", ui_step)
        self.assertIn("- name: Report deferred PR UI regression", self.workflow)
        self.assertIn("runs fail-closed on the subsequent `main` push", self.workflow)


if __name__ == "__main__":
    unittest.main()
