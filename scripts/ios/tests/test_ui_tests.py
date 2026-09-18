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
        for invalid in (
            None,
            "",
            "none",
            "smoke",
            "editor,smoke",
            "smoke,editor,creation",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ui.expected_tests(invalid)


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
