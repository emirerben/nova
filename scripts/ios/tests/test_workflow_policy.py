"""Offline contract for the iOS PR runtime budget and regression coverage."""

from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/ios.yml"


class IOSWorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text()

    def test_pr_builds_the_ui_bundle_only_when_native_ui_inputs_changed(self):
        self.assertIn(
            "(github.event_name != 'pull_request' || needs.changes.outputs.ios_ui == 'true')"
            " && 'prepare-ui' || 'unit'",
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

    def test_prs_run_a_bounded_ui_subset_and_main_runs_the_full_regression(self):
        ui_step = self.workflow.split("- name: Native UI tests (reuse compiled build)", 1)[1]
        ui_step = ui_step.split("- name: Compare focused UI coverage", 1)[0]
        self.assertIn(
            "if: matrix.lane == 'native' && (github.event_name != 'pull_request'"
            " || needs.changes.outputs.ios_ui == 'true')",
            ui_step,
        )
        # Non-PR events always run full. A PR runs the selector's focused group,
        # and only smoke when the selector wants full: never the full suite.
        self.assertIn(
            "KRIA_IOS_UI_GROUPS: ${{ github.event_name != 'pull_request' && 'full'"
            " || (needs.changes.outputs.ios_ui_groups == 'full' && 'smoke'"
            " || needs.changes.outputs.ios_ui_groups) }}",
            ui_step,
        )
        self.assertIn("- name: Report deferred PR UI regression", self.workflow)
        self.assertIn("runs fail-closed on the", self.workflow)
        self.assertIn("subsequent `main` push", self.workflow)

    def test_smoke_tripwire_covers_creation_to_ready_and_the_editor(self):
        import json

        manifest = json.loads(
            (WORKFLOW.parents[2] / "scripts/ios/ui-test-groups.json").read_text()
        )
        smoke = manifest["groups"]["smoke"]
        self.assertIn(
            "KriaUITests/CreationUITests/"
            "testCreationWithAttachedFootageReachesConfirmationAndReadyForBothRuntimes",
            smoke,
        )
        self.assertTrue(any("/EditorUITests/" in test for test in smoke))
        # The PR subset must stay bounded.
        self.assertLessEqual(len(smoke), 6)


if __name__ == "__main__":
    unittest.main()
