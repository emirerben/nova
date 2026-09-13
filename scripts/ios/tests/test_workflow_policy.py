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

    def test_required_job_aggregates_native_and_portable_contract_legs(self):
        self.assertIn("ios-tests:", self.workflow)
        self.assertIn("fail-fast: false", self.workflow)
        self.assertIn("lane: native", self.workflow)
        self.assertIn("runner: macos-15", self.workflow)
        self.assertIn("lane: contracts", self.workflow)
        self.assertIn("runner: ubuntu-latest", self.workflow)
        self.assertIn("runs-on: ${{ matrix.runner }}", self.workflow)
        self.assertIn("needs: [changes, ios-tests]", self.workflow)
        self.assertIn("gate ios ios-tests", self.workflow)

    def test_contract_checks_are_portable_and_native_work_stays_on_macos(self):
        contracts = self.workflow.split(
            "- name: Install contract generator dependencies", 1
        )[1].split("- name: Set up Xcode", 1)[0]
        self.assertIn("if: matrix.lane == 'contracts'", contracts)
        self.assertIn("python -m app.cli.kria_contracts --check", contracts)

        native_xcode = self.workflow.split("- name: Set up Xcode", 1)[1]
        self.assertIn("if: matrix.lane == 'native'", native_xcode)
        self.assertIn("name: ios-native-test-diagnostics", native_xcode)

    def test_full_ui_regression_is_deferred_but_fail_closed(self):
        ui_step = self.workflow.split("- name: Native UI tests (reuse compiled build)", 1)[1]
        self.assertIn(
            "if: matrix.lane == 'native' && github.event_name != 'pull_request'",
            ui_step,
        )
        self.assertIn("KRIA_IOS_UI_GROUPS: full", ui_step)
        self.assertIn("- name: Report deferred PR UI regression", self.workflow)
        self.assertIn("runs fail-closed on the subsequent `main` push", self.workflow)


if __name__ == "__main__":
    unittest.main()
