"""Guard automatic feature coverage before implementing the CI integration."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ios/verify-feature-map.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("feature_verification", SCRIPT)
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class FeatureVerificationTests(unittest.TestCase):
    def test_feature_paths_select_their_user_journeys(self):
        cases = {
            "src/apps/ios/Kria/Features/TeleprompterScreen.swift": ["teleprompter"],
            "src/apps/ios/Kria/Features/NativeEditorView.swift": ["editor"],
            "src/apps/ios/Kria/Features/SlidePostWorkspace.swift": ["slides"],
            "src/apps/ios/Kria/Features/CreationAttachments.swift": ["creation"],
            "src/apps/ios/Tests/KriaUITests/ProjectsUITests.swift": ["projects"],
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(verification.features_for_paths([path]), expected)

    def test_shared_unknown_and_mixed_changes_do_not_fall_back_to_smoke_alone(self):
        all_features = [f["id"] for f in verification.load_map()]
        for path in [
            "src/apps/ios/Kria/Features/ChatWorkspaceView.swift",
            "src/apps/ios/Kria/Core/Services.swift",
            "new-native/unknown.swift",
        ]:
            self.assertEqual(verification.features_for_paths([path]), all_features)
        self.assertEqual(
            verification.features_for_paths(
                [
                    "src/apps/ios/Kria/Features/NativeEditorView.swift",
                    "src/apps/ios/Kria/Features/TeleprompterScreen.swift",
                ]
            ),
            ["teleprompter", "editor"],
        )

    def test_existing_selected_group_and_smoke_are_preserved_without_duplicate_tests(
        self,
    ):
        merged = verification.merge_groups("smoke,creation", "teleprompter,editor")
        self.assertTrue(merged.startswith("smoke,creation,"))
        tests = verification.ui_tests.expected_tests(merged)
        self.assertIn(
            "KriaUITests/TeleprompterUITests/testScriptIsSavedAndCameraFailureKeepsItEditable",
            tests,
        )
        self.assertIn(
            "KriaUITests/EditorUITests/testCapturedCreationWordsPlayReopenAndExport",
            tests,
        )
        self.assertEqual(len(merged.split(",")), len(set(merged.split(","))))

    def test_missing_or_renamed_tests_and_unknown_feature_ids_fail_closed(self):
        with self.assertRaises(ValueError):
            verification.merge_groups("smoke", "typo")
        data = {
            "version": 1,
            "features": [
                {
                    "id": "projects",
                    "paths": ["x"],
                    "tests": ["ProjectsUITests/testRemoved"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                verification.load_map(path)

    def test_persistence_requires_the_complete_script_and_copies_the_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            container = root / "container"
            output = root / "evidence"
            draft = (
                container
                / "Library/Application Support/KriaTeleprompter/account/project/draft.json"
            )
            draft.parent.mkdir(parents=True)
            output.mkdir()
            draft.write_text(json.dumps({"text": "Today I want"}))
            with self.assertRaises(ValueError):
                verification.copy_script_draft(container, output)
            draft.write_text(json.dumps({"text": verification.SCRIPT_TEXT}))
            self.assertEqual(verification.copy_script_draft(container, output), 1)
            draft.unlink()
            self.assertEqual(
                json.loads((output / "persistence/0-draft.json").read_text())["text"],
                verification.SCRIPT_TEXT,
            )

    def test_feature_docs_and_executable_map_agree_on_test_ids(self):
        for feature in verification.load_map():
            text = (verification.MAP_PATH.parent / f"{feature['id']}.md").read_text()
            for test in feature["tests"]:
                self.assertIn(test, text)


if __name__ == "__main__":
    unittest.main()
