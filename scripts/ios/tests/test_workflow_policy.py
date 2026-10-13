"""Offline contract for the iOS PR runtime budget and regression coverage."""

from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/ios.yml"
CI_WORKFLOW = WORKFLOW.with_name("ci.yml")
TESTFLIGHT_WORKFLOW = WORKFLOW.with_name("testflight.yml")


class IOSWorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text()

    def test_pr_builds_the_ui_bundle_only_when_native_ui_inputs_changed(self):
        ci = CI_WORKFLOW.read_text()
        self.assertIn("- name: Build iPhone app and run selected unit tests", ci)
        self.assertIn("needs.changes.outputs.ios_ui == 'false'", ci)
        self.assertIn("needs.changes.outputs.journey_needs_native == 'false'", ci)
        self.assertIn("&& 'unit' || 'prepare-ui'", ci)

    def test_pr_native_checks_use_one_ci_job_and_one_compilation(self):
        ci = CI_WORKFLOW.read_text()
        self.assertNotIn("  pull_request:", self.workflow)
        self.assertIn("  pull_request:", ci)
        self.assertIn("- name: Build iPhone app and run selected unit tests", ci)
        self.assertIn("- name: Run selected iPhone UI scenarios", ci)
        self.assertIn(
            "- name: Reuse the same simulator for selected native journeys", ci
        )
        self.assertNotIn("- name: Build the app and test bundles once", ci)
        self.assertIn("run: python3 scripts/ci/select-tests.py gate ios journey", ci)
        self.assertIn("  ios-contracts:", ci)
        self.assertIn("- name: Verify mobile OpenAPI and shared contracts", ci)
        self.assertIn("needs: [changes, journey, ios-contracts]", ci)
        self.assertIn("  journey-offline:", ci)
        self.assertIn("  journey-native:", ci)
        self.assertIn("gate-journey", ci)
        self.assertIn(
            "fixture-bundle-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}",
            ci,
        )

    def test_portable_only_journeys_do_not_require_a_native_fixture_artifact(self):
        ci = CI_WORKFLOW.read_text()
        for name in (
            "Seal portable fixture inputs for this exact workflow attempt",
            "Transfer fixture inputs and offline evidence",
            "Download this attempt's portable fixture inputs",
            "Check fixture provenance and transferred bytes",
        ):
            step = ci.split(f"- name: {name}", 1)[1].split("- name:", 1)[0]
            with self.subTest(step=name):
                self.assertIn(
                    "if: needs.changes.outputs.journey_needs_native == 'true'", step
                )

    def matrices(self):
        """{leg count: legs} from the matrix fromJSON expression, keyed on the
        selector's ios_ui_shards output; the final fallback literal is count 1."""
        import json
        import re

        line = next(
            line
            for line in self.workflow.splitlines()
            if "matrix: ${{ fromJSON(" in line
        )
        counts = re.findall(
            r"needs\.changes\.outputs\.ios_ui_shards == '(\d)' &&", line
        )
        literals = re.findall(r"'(\{.*?\})'", line)
        self.assertEqual(len(literals), len(counts) + 1)
        keys = [int(count) for count in counts] + [1]
        self.assertEqual(len(set(keys)), len(keys))
        return {
            count: json.loads(literal)["include"]
            for count, literal in zip(keys, literals)
        }

    def test_required_job_aggregates_native_and_portable_contract_legs(self):
        self.assertIn("ios-tests:", self.workflow)
        self.assertIn("fail-fast: false", self.workflow)
        for legs in self.matrices().values():
            self.assertIn({"lane": "contracts", "runner": "ubuntu-latest"}, legs)
            natives = [leg for leg in legs if leg["lane"] == "native"]
            self.assertTrue(natives)
            self.assertTrue(all(leg["runner"] == "macos-15" for leg in natives))
            # strategy.job-index names the shard artifacts: natives come first.
            self.assertEqual(legs[: len(natives)], natives)
        self.assertIn("runs-on: ${{ matrix.runner }}", self.workflow)
        self.assertIn("needs: [changes, ios-tests]", self.workflow)
        self.assertIn("gate ios ios-tests", self.workflow)

    def test_every_selectable_shard_count_runs_exactly_its_shards(self):
        import sys

        sys.path.insert(0, str(WORKFLOW.parents[2] / "scripts/ios"))
        import ui_tests

        matrices = self.matrices()
        # shard_count can return any count up to MAX_SHARDS; each needs a matrix.
        self.assertEqual(set(matrices), set(range(1, ui_tests.MAX_SHARDS + 1)))
        # One leg keeps the unsharded check name "ios-tests (native, macos-15)".
        self.assertEqual(
            [leg for leg in matrices[1] if leg["lane"] == "native"],
            [{"lane": "native", "runner": "macos-15"}],
        )
        # n legs are exactly shards 1/n..n/n, so their parts cover the selection.
        for count, legs in matrices.items():
            if count == 1:
                continue
            with self.subTest(count=count):
                shards = [leg["shard"] for leg in legs if leg["lane"] == "native"]
                self.assertEqual(shards, [f"{i}/{count}" for i in range(1, count + 1)])
        # The selector's count reaches this workflow through the reusable job.
        changes = (WORKFLOW.parent / "ci-changes.yml").read_text()
        self.assertIn("value: ${{ jobs.select.outputs.ios_ui_shards }}", changes)
        self.assertIn(
            "ios_ui_shards: ${{ steps.select.outputs.ios_ui_shards }}", changes
        )
        ui_step = self.workflow.split(
            "- name: Native UI tests (reuse compiled build)", 1
        )[1]
        self.assertIn(
            "KRIA_IOS_UI_SHARD: ${{ matrix.shard }}", ui_step.split("run:", 1)[0]
        )
        # Exactly one leg per run executes unit tests, saves the shared cache,
        # and writes the PR summary.
        first = "(!matrix.shard || startsWith(matrix.shard, '1/'))"
        self.assertIn(
            f"KRIA_IOS_UNIT_TESTS: ${{{{ {first} && '1' || '0' }}}}", self.workflow
        )
        for step in ("Save Xcode build data", "Report deferred PR UI regression"):
            with self.subTest(step=step):
                condition = self.workflow.split(f"- name: {step}", 1)[1]
                self.assertIn(first, condition.split("\n", 2)[1])

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
        ci = CI_WORKFLOW.read_text()
        ui_step = ci.split("- name: Run selected iPhone UI scenarios", 1)[1]
        ui_step = ui_step.split("- name: Reuse the same simulator", 1)[0]
        self.assertIn(
            "if: github.event_name == 'pull_request' && needs.changes.outputs.ios_ui == 'true'",
            ui_step,
        )
        self.assertIn(
            "KRIA_IOS_UI_GROUPS: ${{ needs.changes.outputs.ios_ui_groups == 'full'"
            " && 'smoke' || needs.changes.outputs.ios_ui_groups }}",
            ui_step,
        )
        main_ui = self.workflow.split(
            "- name: Native UI tests (reuse compiled build)", 1
        )[1]
        self.assertIn("KRIA_IOS_UI_GROUPS: full", main_ui)

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

    def test_main_never_cancels_a_running_ios_build(self):
        # A cancel-on-push policy starved main of finished runs, so TestFlight
        # never received a release.
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertNotIn("cancel-in-progress: ${{", self.workflow)

    def test_main_release_and_full_regression_are_separate(self):
        smoke_name = "- name: Smoke UI tests before release"
        release_name = "- name: Release to TestFlight after build, unit and smoke tests"
        before_smoke, rest = self.workflow.split(smoke_name, 1)
        smoke, rest = rest.split(release_name, 1)
        release, full = rest.split("- name: Native UI tests", 1)
        self.assertIn("- name: Build and unit tests", before_smoke)
        self.assertIn("- name: Require named native unit journey tests", before_smoke)
        self.assertIn("KRIA_IOS_UI_GROUPS: smoke", smoke)
        self.assertIn("github.event_name == 'push'", smoke)
        self.assertIn("github.event_name == 'push'", release)
        self.assertIn("github.ref == 'refs/heads/main'", release)
        self.assertIn("github.event_name != 'push'", full)
        self.assertIn("KRIA_IOS_UI_GROUPS: full", full)
        self.assertIn('cron: "23 3 * * *"', self.workflow)
        self.assertNotIn("Renew the UI build receipt", self.workflow)
        self.assertEqual(self.workflow.count("gh workflow run testflight.yml"), 1)

    def test_testflight_skips_a_commit_an_earlier_upload_already_contains(self):
        testflight = TESTFLIGHT_WORKFLOW.read_text()
        # An out-of-order or duplicate dispatch must not downgrade the upload.
        self.assertIn('git merge-base --is-ancestor "$HEAD_SHA" "$sha"', testflight)
        self.assertIn("superseded", testflight)


if __name__ == "__main__":
    unittest.main()
