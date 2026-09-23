"""Guard selection against uncovered tests and validate actual XCTest results."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ui_tests.py"
spec = importlib.util.spec_from_file_location("ui_tests", SCRIPT)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)

RETRIED_FIXTURE = json.loads(
    (Path(__file__).with_name("fixtures") / "retried-ui-results.json").read_text()
)


def _report_without_test_case(report, method_name):
    """Deep-copy report with the named Test Case node ("name" == method_name())
    pruned from the tree, simulating a run that only selected the other tests."""
    trimmed = copy.deepcopy(report)

    def prune(node):
        node["children"] = [
            child
            for child in node.get("children", [])
            if not (
                child.get("nodeType") == "Test Case"
                and child.get("name") == f"{method_name}()"
            )
        ]
        for child in node["children"]:
            prune(child)

    for node in trimmed["testNodes"]:
        prune(node)
    return trimmed


class UISelectionTests(unittest.TestCase):
    def test_every_current_test_is_classified(self):
        self.assertTrue(ui.inventory_matches())
        data = ui.manifest()
        features = [
            test
            for group in ("creation", "projects", "editor")
            for test in data["groups"][group]
        ]
        self.assertEqual(len(features), len(set(features)))
        self.assertEqual(set(features), ui.discovered())

    def test_each_exact_mapping_selects_smoke_and_one_feature(self):
        for path, group in ui.manifest()["sources"].items():
            with self.subTest(path=path):
                self.assertTrue((ui.ROOT / path).is_file(), "Mapping is stale")
                self.assertEqual(ui.select_groups([path])[0], f"smoke,{group}")

    def test_mixed_shared_and_unknown_inputs_require_full(self):
        editor = "src/apps/ios/Kria/Features/NativeEditorView.swift"
        creation = "src/apps/ios/Kria/Features/CreationAttachments.swift"
        for paths in (
            [],
            [editor, creation],
            [editor, "src/apps/ios/Kria/Core/AppState.swift"],
            ["src/apps/ios/Kria/Features/ChatWorkspaceView.swift"],
            ["src/apps/ios/Kria/Features/CanonicalChatComponents.swift"],
            ["src/apps/ios/Kria/Features/ChatUITestTransport.swift"],
            ["src/apps/ios/Kria/Features/EditorViews.swift"],
            ["src/apps/ios/Kria/Core/Services.swift"],
            ["src/apps/ios/Kria/Core/Models.swift"],
            ["src/apps/ios/Kria/DesignSystem/BrandComponents.swift"],
            ["src/apps/ios/Kria/Resources/new.png"],
            ["src/apps/ios/project.yml"],
            ["scripts/ios/verify.sh"],
            ["src/apps/ios/Kria/new.swift"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(ui.select_groups(paths)[0], "full")

    def test_new_removed_and_renamed_tests_force_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "src/apps/ios/Tests/KriaUITests"
            shutil.copytree(ui.ROOT / "src/apps/ios/Tests/KriaUITests", tests)
            path = "src/apps/ios/Tests/KriaUITests/ProjectsUITests.swift"
            original = (root / path).read_text()
            for source in (
                original.replace(
                    "    func testProject", "    func testRenamedProject", 1
                ),
                original.replace("    func testProject", "    func checkProject", 1),
                original.replace(
                    "final class ProjectsUITests: XCTestCase {",
                    "final class ProjectsUITests: XCTestCase {\n    func testNewCase() {}",
                ),
            ):
                (root / path).write_text(source)
                self.assertEqual(ui.select_groups([path], root)[0], "full")
            (root / path).write_text(original)
            (tests / "NewTests.swift").write_text(
                "class NewTests: XCTestCase { func testNew() {} }"
            )
            self.assertEqual(ui.select_groups([path], root)[0], "full")

    def test_deleted_source_mapping_requires_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(
                ui.ROOT / "src/apps/ios/Tests/KriaUITests",
                root / "src/apps/ios/Tests/KriaUITests",
            )
            self.assertEqual(
                ui.select_groups(
                    ["src/apps/ios/Kria/Features/NativeEditorView.swift"], root
                )[0],
                "full",
            )

    def test_invalid_manifest_falls_back_but_execution_fails(self):
        with patch.object(ui, "manifest", side_effect=ValueError("bad")):
            self.assertEqual(ui.select_groups(["any"])[0], "full")
            with self.assertRaises(ValueError):
                ui.expected_tests("smoke,editor")
        data = ui.manifest()
        data["groups"]["creation"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(data))
            with patch.object(ui, "MANIFEST", path), self.assertRaises(ValueError):
                ui.expected_tests("smoke,creation")

    def test_focused_filters_preserve_smoke_and_deduplicate(self):
        data = ui.manifest()
        for group in ("creation", "projects", "editor"):
            self.assertEqual(
                ui.expected_tests(f"smoke,{group}"),
                set(data["groups"]["smoke"] + data["groups"][group]),
            )
        # Smoke alone is the PR tripwire when the selector wants full coverage.
        self.assertEqual(ui.expected_tests("smoke"), set(data["groups"]["smoke"]))
        with self.assertRaises(ValueError):
            ui.validate_groups("smoke")
        for invalid in (
            None,
            "",
            "none",
            "editor,smoke",
            "smoke,editor,creation",
            # Changed-test ids must be canonical (sorted, unique) and real.
            "smoke,SignInUITests/testSignInShowsEveryProviderAndLegalLink,"
            "ProjectsUITests/testProjectActionsCanBeCancelledWithoutChangingProject",
            "smoke,ProjectsUITests/testNope",
            "smoke,ProjectsUITests/helper",
            "smoke,editor,editor",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ui.expected_tests(invalid)

    def test_changed_test_methods_add_only_those_tests_to_smoke(self):
        data = ui.manifest()
        rename = "ProjectsUITests/testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry"
        self.assertEqual(
            ui.expected_tests(f"smoke,{rename}"),
            set(data["groups"]["smoke"]) | {f"KriaUITests/{rename}"},
        )
        self.assertEqual(
            ui.expected_tests(f"smoke,editor,{rename}"),
            set(data["groups"]["smoke"] + data["groups"]["editor"])
            | {f"KriaUITests/{rename}"},
        )

    def test_shards_partition_the_full_suite_and_balance_recorded_time(self):
        inventory = ui.discovered()
        durations = json.loads(ui.DURATIONS.read_text())
        self.assertTrue(all(float(value) > 0 for value in durations.values()))
        for count in range(1, 5):
            parts = [
                ui.shard_tests(inventory, f"{i}/{count}") for i in range(1, count + 1)
            ]
            with self.subTest(count=count):
                self.assertEqual(set().union(*parts), inventory)
                self.assertEqual(sum(len(part) for part in parts), len(inventory))
                loads = [sum(durations.get(test, 0) for test in part) for part in parts]
                self.assertLessEqual(max(loads) - min(loads), max(durations.values()))
        # Unknown (new) tests still land in exactly one shard.
        extra = inventory | {"KriaUITests/NewUITests/testBrandNew"}
        parts = [ui.shard_tests(extra, f"{i}/3") for i in (1, 2, 3)]
        self.assertEqual(
            sum("KriaUITests/NewUITests/testBrandNew" in p for p in parts), 1
        )
        for invalid in ("", "0/3", "4/3", "1/0", "a/b", "1/10", "1-3"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ui.shard_tests(inventory, invalid)

    def test_changed_tests_maps_lines_inside_test_methods_only(self):
        path = "src/apps/ios/Tests/KriaUITests/ProjectsUITests.swift"
        source = (ui.ROOT / path).read_text()
        lines = source.splitlines()

        def line_of(fragment, after=0):
            return next(
                n for n, text in enumerate(lines, 1) if n > after and fragment in text
            )

        rename = line_of(
            "func testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry"
        )
        drawer = line_of("func testPartialDrawerDragsAlwaysSettleAtTheNearestEndpoint")
        helper = line_of("private func createFreshChat")
        rename_id = "ProjectsUITests/testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry"
        drawer_id = (
            "ProjectsUITests/testPartialDrawerDragsAlwaysSettleAtTheNearestEndpoint"
        )
        self.assertEqual(ui.changed_tests(source, {rename + 2}), {rename_id})
        self.assertEqual(
            ui.changed_tests(source, {rename, drawer - 1, drawer + 3}),
            {rename_id, drawer_id},
        )
        for outside in (
            {0},
            {1},  # import
            {line_of("final class ProjectsUITests")},
            {helper + 1},  # private helper body
            {rename + 2, helper + 1},
            {len(lines) + 1},
            set(),
        ):
            with self.subTest(outside=outside):
                self.assertIsNone(ui.changed_tests(source, outside))
        # Unfamiliar layouts (tests not at member indentation) never focus.
        self.assertIsNone(
            ui.changed_tests(
                source.replace("    func testRename", "func testRename"), {rename + 2}
            )
        )

    def test_selector_focuses_changed_methods_and_keeps_groups_conservative(self):
        projects = "src/apps/ios/Tests/KriaUITests/ProjectsUITests.swift"
        editor_source = "src/apps/ios/Kria/Features/NativeEditorView.swift"
        creation_source = "src/apps/ios/Kria/Features/CreationAttachments.swift"
        rename = "ProjectsUITests/testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry"
        focused = {projects: {rename}}
        self.assertEqual(
            ui.select_groups([projects], focused=focused)[0], f"smoke,{rename}"
        )
        # A group already covering the method keeps the plain group value.
        self.assertEqual(
            ui.select_groups(
                [projects, "src/apps/ios/Tests/KriaUITests/SignInUITests.swift"],
                focused=focused,
            )[0],
            "smoke,projects",
        )
        self.assertEqual(
            ui.select_groups([projects, editor_source], focused=focused)[0],
            f"smoke,editor,{rename}",
        )
        self.assertEqual(
            ui.select_groups([editor_source, creation_source], focused=focused)[0],
            "full",
        )
        for fallback in (
            None,
            {projects: None},
            {projects: {"ProjectsUITests/testGone"}},
        ):
            with self.subTest(fallback=fallback):
                self.assertEqual(
                    ui.select_groups([projects], focused=fallback)[0], "smoke,projects"
                )
        # An unmapped test file used to select full (smoke alone on PRs), so the
        # edited test never ran before merge; a focused edit now runs it.
        visual = "src/apps/ios/Tests/KriaUITests/NativeCaptionVisualUITests.swift"
        caption = "NativeCaptionVisualUITests/testCaptionAndVisualPaperScreens"
        self.assertNotIn(visual, ui.manifest()["sources"])
        self.assertEqual(ui.select_groups([visual])[0], "full")
        self.assertEqual(
            ui.select_groups([visual], focused={visual: {caption}})[0],
            f"smoke,{caption}",
        )
        for path, group in ui.manifest()["sources"].items():
            value = ui.select_groups([path], focused=focused)[0]
            self.assertEqual(value, ui.validate_groups(value))


class ResultCoverageTests(unittest.TestCase):
    def report(self, result="Passed", identifier="Example/testWorks()"):
        return {
            "testNodes": [
                {
                    "nodeType": "UI test bundle",
                    "name": "KriaUITests",
                    "children": [
                        {
                            "nodeType": "Test Suite",
                            "name": "Example",
                            "children": [
                                {
                                    "nodeType": "Test Case",
                                    "nodeIdentifier": identifier,
                                    "result": result,
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    def test_passed_expected_tests_are_required(self):
        expected = {"KriaUITests/Example/testWorks"}
        self.assertIn("Verified 1", ui.verify_results(self.report(), expected))
        self.assertIn(
            "Verified 1",
            ui.verify_results(
                self.report(identifier="KriaUITests/Example/testWorks()"), expected
            ),
        )
        for result in ("Skipped", "Failed", "Expected Failure", None):
            with self.subTest(result=result), self.assertRaises(ValueError):
                ui.verify_results(self.report(result), expected)
        for report in (
            {"testNodes": []},
            self.report(identifier="Example/testOther()"),
        ):
            with self.assertRaises(ValueError):
                ui.verify_results(report, expected)
        with self.assertRaises(ValueError):
            ui.verify_results(
                self.report(), expected | {"KriaUITests/Example/testMissing"}
            )


class FlakyDetectionTests(unittest.TestCase):
    """Real xcresulttool output (fixtures/retried-ui-results.json) from a
    -retry-tests-on-failure -test-iterations 3 run: one permanent failure, one
    pass-on-retry, one first-try pass."""

    PASS_ON_RETRY = "KriaUITests/KRI117RetryProbeUITests/testFailsOnceThenPasses"
    FIRST_TRY_PASS = "KriaUITests/KRI117RetryProbeUITests/testPassesFirstTry"
    ALWAYS_FAILS = "KriaUITests/KRI117RetryProbeUITests/testAlwaysFails"

    def test_pass_on_retry_is_flagged_flaky_with_attempts_and_failure_messages(self):
        flaky = ui.flaky_tests(RETRIED_FIXTURE)
        self.assertEqual([test["identifier"] for test in flaky], [self.PASS_ON_RETRY])
        entry = flaky[0]
        self.assertEqual(entry["attempts"], 2)
        self.assertEqual(
            entry["failure_messages"],
            [
                "KRI117RetryProbeUITests.swift:17: failed - "
                "KRI-117 probe: deliberate first-attempt failure"
            ],
        )

    def test_pass_on_retry_does_not_fail_verification_when_expected(self):
        # Only the two passing tests were selected for this run (verify_results
        # treats a KriaUITests test outside `expected` as an unexpected extra,
        # so a genuinely-selected-and-still-failing test belongs in a separate
        # scenario, covered by test_fail_all_iterations_still_raises below).
        report = _report_without_test_case(RETRIED_FIXTURE, "testAlwaysFails")
        expected = {self.PASS_ON_RETRY, self.FIRST_TRY_PASS}
        self.assertIn("Verified 2", ui.verify_results(report, expected))

    def test_fail_all_iterations_still_raises(self):
        with self.assertRaises(ValueError):
            ui.verify_results(RETRIED_FIXTURE, {self.ALWAYS_FAILS})

    def test_first_try_pass_is_not_flaky(self):
        identifiers = {test["identifier"] for test in ui.flaky_tests(RETRIED_FIXTURE)}
        self.assertNotIn(self.FIRST_TRY_PASS, identifiers)

    def test_report_with_no_repetitions_has_no_flaky_tests(self):
        self.assertEqual(ui.flaky_tests(ResultCoverageTests().report()), [])

    def test_missing_expected_test_still_raises(self):
        with self.assertRaises(ValueError):
            ui.verify_results(
                RETRIED_FIXTURE,
                {"KriaUITests/KRI117RetryProbeUITests/testDoesNotExist"},
            )


class VerifyCommandFlakyReportingTests(unittest.TestCase):
    """Exercise the `verify` CLI path end to end: flaky-tests.json, the
    ::warning annotation, and the GITHUB_STEP_SUMMARY section. Patches
    subprocess.check_output (the real call shells out to xcrun) and
    expected_tests (expected_tests("full") walks real Tests/KriaUITests
    sources, which do not contain the fixture's probe class)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / "ui.xcresult"
        self.bundle.mkdir()
        self.summary = Path(self.temp.name) / "summary.md"
        # Only the two passing tests were selected; the permanently-failing
        # probe test belongs to the raise-on-genuine-failure scenario instead.
        report = _report_without_test_case(RETRIED_FIXTURE, "testAlwaysFails")
        self.fixture_bytes = json.dumps(report).encode()

    def run_verify(self, expected):
        buffer = io.StringIO()
        argv = ["ui_tests.py", "verify", "smoke,editor", str(self.bundle)]
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(self.summary))
        with (
            patch.object(ui, "expected_tests", return_value=expected),
            patch.object(
                ui.subprocess, "check_output", return_value=self.fixture_bytes
            ),
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, env),
            contextlib.redirect_stdout(buffer),
        ):
            ui.main()
        return buffer.getvalue()

    def test_flaky_report_warning_and_summary_are_emitted(self):
        expected = {
            "KriaUITests/KRI117RetryProbeUITests/testFailsOnceThenPasses",
            "KriaUITests/KRI117RetryProbeUITests/testPassesFirstTry",
        }
        stdout = self.run_verify(expected)
        self.assertIn("Flaky (passed on retry):", stdout)
        self.assertIn(
            "::warning title=Flaky UI test::"
            "KriaUITests/KRI117RetryProbeUITests/testFailsOnceThenPasses",
            stdout,
        )
        self.assertIn("Verified 2", stdout)

        report = json.loads((self.bundle.parent / "flaky-tests.json").read_text())
        self.assertEqual(len(report), 1)
        self.assertEqual(
            report[0]["identifier"],
            "KriaUITests/KRI117RetryProbeUITests/testFailsOnceThenPasses",
        )
        self.assertEqual(report[0]["attempts"], 2)

        summary = self.summary.read_text()
        self.assertIn("## Flaky UI tests (passed on retry)", summary)
        self.assertIn("testFailsOnceThenPasses", summary)
        self.assertIn("Linear issue", summary)

    def test_no_flaky_tests_writes_an_empty_list_and_no_summary_section(self):
        report = _report_without_test_case(
            _report_without_test_case(RETRIED_FIXTURE, "testAlwaysFails"),
            "testFailsOnceThenPasses",
        )
        self.fixture_bytes = json.dumps(report).encode()
        expected = {"KriaUITests/KRI117RetryProbeUITests/testPassesFirstTry"}
        self.run_verify(expected)
        report = json.loads((self.bundle.parent / "flaky-tests.json").read_text())
        self.assertEqual(report, [])
        self.assertFalse(self.summary.exists())


if __name__ == "__main__":
    unittest.main()
