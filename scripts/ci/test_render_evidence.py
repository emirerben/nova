"""PR unit runs skip the slow export/OCR proofs only when no render code changed."""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/ios"))
import render_evidence

spec = importlib.util.spec_from_file_location("select_tests", ROOT / "scripts/ci/select-tests.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def fake_git(paths):
    def run(*args):
        if args[0] == "merge-base":
            return b"base\n"
        return b"\0".join(p.encode() for p in paths)
    return run


class RenderEvidenceTests(unittest.TestCase):
    def test_every_listed_test_still_exists(self):
        self.assertEqual(render_evidence.missing(), [])

    def test_skip_args_name_the_kria_unit_bundle(self):
        args = render_evidence.skip_args()
        self.assertEqual(len(args), len(render_evidence.TESTS))
        self.assertTrue(all(a.startswith("-skip-testing:KriaTests/") for a in args))

    def test_selected_journeys_keep_every_test_they_name(self):
        # journey-gate.py native-units fails when a named journey test is
        # missing from the unit result bundle, so the skip must leave them in.
        manifest = render_evidence.JOURNEY_MANIFEST.read_text()
        import json

        journeys = json.loads(manifest)["journeys"]
        for row in journeys:
            skipped = {a.removeprefix("-skip-testing:") for a in render_evidence.skip_args(row["id"])}
            for test in row.get("xctests", []):
                self.assertFalse(
                    any(test == s or test.startswith(s + "/") for s in skipped),
                    f"{row['id']} names {test}, which would be skipped",
                )
        everything = ",".join(row["id"] for row in journeys)
        named = render_evidence.named_by_journeys(everything)
        self.assertEqual(
            render_evidence.skip_args(everything),
            [f"-skip-testing:KriaTests/{t}" for t in render_evidence.TESTS if t not in named],
        )
        self.assertIn("DurationTextPreservationTests", named)
        self.assertEqual(len(render_evidence.skip_args("")), len(render_evidence.TESTS))

    def test_skip_args_cli_reads_the_selected_journeys(self):
        script = ROOT / "scripts/ios/render_evidence.py"
        env = {"KRIA_JOURNEY_IDS": "editor-durable-save,reference-style-save", "PATH": "/usr/bin:/bin"}
        out = subprocess.run([sys.executable, str(script), "skip-args"], env=env,
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertNotIn("-skip-testing:KriaTests/SavedDraftPreviewRangeTests", out)
        self.assertNotIn("-skip-testing:KriaTests/ReferenceStyleSaveTests", out)
        self.assertIn("-skip-testing:KriaTests/DurationTextPreservationTests", out)

    def test_render_paths_trigger_and_others_do_not(self):
        for path in (
            "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/LivePreviewComposition.swift",
            "src/apps/ios/Kria/Core/NativeEditorRenderCompiler.swift",
            "src/apps/ios/Kria/Features/NativeTextEditing.swift",
            "src/apps/ios/Kria/Core/DeviceRenderSessions.swift",
            "src/apps/ios/Tests/KriaTests/DurationTextPreservationTests.swift",
            "src/apps/ios/Tests/Fixtures/ReferenceStyleSave.json",
            "src/apps/ios/project.yml",
            "src/apps/web/public/fonts/Inter.ttf",
        ):
            self.assertTrue(render_evidence.triggered([path]), path)
        for path in (
            "src/apps/ios/Kria/Features/ChatView.swift",
            "src/apps/ios/Tests/KriaTests/ChatSendReceiptTests.swift",
            "scripts/ios/render_evidence.py",
            "src/apps/api/app/main.py",
        ):
            self.assertFalse(render_evidence.triggered([path]), path)

    def test_only_pull_requests_without_render_changes_skip(self):
        for event in ("push", "schedule", "workflow_dispatch", None):
            self.assertTrue(ci.render_evidence_selection(event, "a", "b"))
        with patch.object(ci, "git", fake_git(["src/apps/ios/Kria/Features/ChatView.swift"])):
            self.assertFalse(ci.render_evidence_selection("pull_request", "a", "b"))
        with patch.object(ci, "git", fake_git(["src/apps/ios/Kria/Core/NativeEditorDocument.swift"])):
            self.assertTrue(ci.render_evidence_selection("pull_request", "a", "b"))
        with patch.object(ci, "git", fake_git([])):
            self.assertTrue(ci.render_evidence_selection("pull_request", "a", "b"))

    def test_unreadable_diff_runs_the_proofs(self):
        def broken(*args):
            raise subprocess.CalledProcessError(128, "git")
        with patch.object(ci, "git", broken):
            self.assertTrue(ci.render_evidence_selection("pull_request", "a", "b"))

    def test_workflow_skips_only_on_pull_requests_with_an_explicit_false(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn(
            "KRIA_IOS_SKIP_RENDER_EVIDENCE: ${{ github.event_name == 'pull_request' && "
            "needs.changes.outputs.ios_render_evidence == 'false' && '1' || '0' }}",
            workflow,
        )
        changes = (ROOT / ".github/workflows/ci-changes.yml").read_text()
        self.assertIn("value: ${{ jobs.select.outputs.ios_render_evidence }}", changes)
        self.assertIn("ios_render_evidence: ${{ steps.select.outputs.ios_render_evidence }}", changes)
        verify = (ROOT / "scripts/ios/verify.sh").read_text()
        self.assertIn('if [[ "${KRIA_IOS_SKIP_RENDER_EVIDENCE:-0}" == "1" ]]', verify)
        self.assertIn("render_evidence.py\" skip-args", verify)
        self.assertIn("KRIA_JOURNEY_IDS: ${{ needs.changes.outputs.journey_ids }}", workflow)
        # The main-push release workflow never sets the skip.
        self.assertNotIn("KRIA_IOS_SKIP_RENDER_EVIDENCE", (ROOT / ".github/workflows/ios.yml").read_text())


if __name__ == "__main__":
    unittest.main()
