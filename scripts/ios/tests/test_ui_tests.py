"""Guard selection against uncovered tests and validate actual XCTest results."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ui_tests.py"
spec = importlib.util.spec_from_file_location("ui_tests", SCRIPT)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


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


if __name__ == "__main__":
    unittest.main()
