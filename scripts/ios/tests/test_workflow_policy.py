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

    def matrices(self):
        """(pull_request, main/manual) legs from the matrix fromJSON expression."""
        import json
        import re

        line = next(
            line for line in self.workflow.splitlines() if "matrix: ${{ fromJSON(" in line
        )
        self.assertIn("github.event_name == 'pull_request' &&", line)
        literals = re.findall(r"'(\{.*?\})'", line)
        self.assertEqual(len(literals), 2)
        return [json.loads(literal)["include"] for literal in literals]

    def test_required_job_aggregates_native_and_portable_contract_legs(self):
        self.assertIn("ios-tests:", self.workflow)
        self.assertIn("fail-fast: false", self.workflow)
        for legs in self.matrices():
            self.assertIn({"lane": "contracts", "runner": "ubuntu-latest"}, legs)
            natives = [leg for leg in legs if leg["lane"] == "native"]
            self.assertTrue(natives)
            self.assertTrue(all(leg["runner"] == "macos-15" for leg in natives))
        self.assertIn("runs-on: ${{ matrix.runner }}", self.workflow)
        self.assertIn("needs: [changes, ios-tests]", self.workflow)
        self.assertIn("gate ios ios-tests", self.workflow)

    def test_prs_keep_one_native_leg_and_main_shards_the_full_suite(self):
        pull_request, main = self.matrices()
        # PR check names stay "ios-tests (native, macos-15)".
        self.assertEqual(
            [leg for leg in pull_request if leg["lane"] == "native"],
            [{"lane": "native", "runner": "macos-15"}],
        )
        shards = [leg["shard"] for leg in main if leg["lane"] == "native"]
        count = len(shards)
        self.assertGreater(count, 1)
        self.assertEqual(shards, [f"{i}/{count}" for i in range(1, count + 1)])
        ui_step = self.workflow.split("- name: Native UI tests (reuse compiled build)", 1)[1]
        self.assertIn("KRIA_IOS_UI_SHARD: ${{ matrix.shard }}", ui_step.split("run:", 1)[0])
        # Exactly one leg per run executes unit tests and saves the shared cache.
        first = "(!matrix.shard || startsWith(matrix.shard, '1/'))"
        self.assertIn(f"KRIA_IOS_UNIT_TESTS: ${{{{ {first} && '1' || '0' }}}}", self.workflow)
        save = self.workflow.split("- name: Save Xcode build data", 1)[1].split("uses:", 1)[0]
        self.assertIn(first, save)

    def test_contract_checks_are_portable_and_native_work_stays_on_macos(self):
        contracts = self.workflow.split(
            "- name: Install contract generator dependencies", 1
        )[1].split("- name: Set up Xcode", 1)[0]
        self.assertIn("if: matrix.lane == 'contracts'", contracts)
        self.assertIn("python -m app.cli.kria_contracts --check", contracts)

        native_xcode = self.workflow.split("- name: Set up Xcode", 1)[1]
        self.assertIn("if: matrix.lane == 'native'", native_xcode)
        # PR runs keep the plain artifact name; shard legs add a suffix.
        self.assertIn("|| 'ios-native-test-diagnostics' }}", native_xcode)

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
