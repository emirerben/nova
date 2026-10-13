"""Guard the offline feature map and Debug build identity."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/ios"))
import feature_map as verification  # noqa: E402
from app_identity import app_code_sha256  # noqa: E402


class FeatureVerificationTests(unittest.TestCase):
    def test_app_identity_includes_debug_code_not_only_the_thin_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app / "Kria").write_bytes(b"unchanged launcher")
            code = app / "Kria.debug.dylib"
            code.write_bytes(b"first build")
            first = app_code_sha256(app)
            code.write_bytes(b"second build")
            self.assertNotEqual(first, app_code_sha256(app))

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
            "src/apps/ios/NewNative/unknown.swift",
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

    def test_selection_remains_bounded_and_docs_or_unit_only_changes_skip_ui(self):
        entries = verification.load_map()
        tests = verification.tests_for_features([entry["id"] for entry in entries])
        self.assertEqual(len(tests), 16)
        self.assertEqual(verification.features_for_paths(["docs/new.md"]), [])
        self.assertEqual(
            verification.features_for_paths(
                ["src/apps/ios/Tests/KriaTests/NewTests.swift"]
            ),
            [],
        )
        with self.assertRaises(ValueError):
            verification.tests_for_features(["typo"])

    def test_feature_docs_and_executable_map_agree_on_test_ids(self):
        for feature in verification.load_map():
            text = (
                (ROOT / verification.MAP)
                .parent.joinpath(f"{feature['id']}.md")
                .read_text()
            )
            for test in feature["tests"]:
                self.assertIn(test, text)


if __name__ == "__main__":
    unittest.main()
