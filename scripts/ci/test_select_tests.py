"""Exercise classification, real git diffs, and required-check failure behavior."""

import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().with_name("select-tests.py")
spec = importlib.util.spec_from_file_location("select_tests", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
ALL = {"api", "ios", "ios_ui", "shared_motion"}


class ClassificationTests(unittest.TestCase):
    def test_learning_loop_docs_map_to_an_executed_journey(self):
        path = "src/apps/api/tests/evals/request_following/README.md"
        matches = [
            j
            for j in ci.JOURNEYS
            if any(ci.matches_journey_path(path, prefix) for prefix in j["paths"])
        ]
        self.assertEqual([j["id"] for j in matches], ["kri-215-learning-loop"])
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        self.assertIn(
            "contains(needs.changes.outputs.journey_ids, 'kri-215-learning-loop')",
            workflow,
        )
        self.assertIn("pytest tests/incidents/test_request_following_loop.py", workflow)

    def test_manifest_native_routing_is_conservative_and_mixed_safe(self):
        self.assertFalse(ci.journey_native_for_ids(["kri-577-reliability-gates"]))
        self.assertFalse(ci.journey_native_for_ids(["kri-384-library-ingest"]))
        self.assertFalse(
            ci.journey_native_for_ids(
                ["kri-384-library-ingest", "kri-577-reliability-gates"]
            )
        )
        self.assertTrue(
            ci.journey_native_for_ids(["kri-577-reliability-gates", "kri-524-creation"])
        )
        with self.assertRaises(ValueError):
            with patch.object(ci, "JOURNEYS", [{"id": "unknown"}]):
                ci.journey_native_for_ids(["unknown"])
        with self.assertRaises(ValueError):
            with patch.object(
                ci, "JOURNEYS", [{"id": "python-only", "needs_native": "false"}]
            ):
                ci.journey_native_for_ids(["python-only"])

    def test_boundaries(self):
        examples = {
            "src/apps/web/src/app/page.tsx": set(),
            "src/apps/web/package.json": {"shared_motion"},
            "scripts/ci/web-tests.mjs": set(),
            "src/apps/ios/project.yml": {"ios", "ios_ui"},
            "scripts/ios/verify.sh": {"ios", "ios_ui"},
            ".github/workflows/ios.yml": {"ios", "ios_ui"},
            "src/apps/api/app/pipeline/reframe.py": {"api"},
            "src/apps/api/app/pipeline/motion_scene.py": {"api", "shared_motion"},
            "src/packages/motion-runtime/creator-blocks.catalog.json": ALL,
            "src/apps/web/src/__tests__/lib/motion-runtime.test.ts": {"shared_motion"},
            "src/apps/web/package-lock.json": {"shared_motion"},
            "src/apps/api/tests/test_routes.py": {"api"},
            "src/apps/api/prompts/writer.md": {"api"},
            "src/apps/api/app/routes/me.py": {"api", "ios"},
            "src/apps/api/app/schemas/jobs.py": {"api", "ios"},
            "src/apps/api/app/kria/contracts.py": {"api", "ios"},
            "src/apps/api/app/kria/duplicate_uploads.py": {"api"},
            "src/apps/api/app/kria/planner.py": {"api", "ios"},
            "src/apps/api/app/config.py": {"api", "ios"},
            "src/apps/api/pyproject.toml": {"api", "ios"},
            "src/packages/motion-runtime/motion-limits.json": ALL,
            ".github/workflows/ci.yml": ALL,
            ".github/workflows/ci-changes.yml": ALL,
            "scripts/ci/select-tests.py": ALL,
            "scripts/ci/test_select_tests.py": set(),
            "Dockerfile": ALL,
            "Makefile": ALL,
            "assets/fonts/font.ttf": ALL,
            "new-runtime/README.md": ALL,
            "docs/runbooks/ci.md": set(),
            "CHANGELOG.md": set(),
            "VERSION": set(),
        }
        for path, expected in examples.items():
            with self.subTest(path=path):
                self.assertEqual(ci.affected(path), expected)

    def test_ios_ui_selection(self):
        for path in (
            "src/apps/ios/Kria/Generated/openapi.yaml",
            "src/apps/ios/Tests/KriaTests/EditorDocumentTests.swift",
            "src/apps/ios/Tests/Fixtures/editor-commit-picker-contract.json",
            "src/apps/ios/Tests/Fixtures/guided_label_rebase_vectors.json",
        ):
            self.assertEqual(ci.affected(path), {"ios"})
        for path in (
            "src/apps/ios/Kria/Features/NativeEditorView.swift",
            "src/apps/ios/Tests/KriaUITests/KriaUITests.swift",
            "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Composition.swift",
            "scripts/ios/cache-inputs.py",
        ):
            self.assertEqual(ci.affected(path), {"ios", "ios_ui"})
        for path in (
            "src/apps/web/public/fonts/Inter-Regular.ttf",
            "src/apps/web/public/plan/type-posters/montage.mp4",
        ):
            self.assertEqual(ci.affected(path), {"ios", "ios_ui"})

    def test_swift_server_mirrors_also_run_api_parity_tests(self):
        for path in (
            "src/apps/ios/Kria/Core/NativeEditorDocument.swift",
            "src/apps/ios/Kria/Core/SlidePost.swift",
        ):
            with self.subTest(path=path):
                self.assertEqual(ci.affected(path), {"api", "ios", "ios_ui"})

    def test_xcode_bundled_web_resources_select_native_coverage(self):
        project = SCRIPT.parents[2] / "src/apps/ios/project.yml"
        bundled = []
        for line in project.read_text().splitlines():
            if line.strip().startswith("- path:"):
                path = shlex.split(line.split("path:", 1)[1], comments=True)[0]
                if path.startswith("../web/"):
                    bundled.append("src/apps/web/" + path.removeprefix("../web/"))
        self.assertTrue(bundled, "Expected native-bundled web resources")
        for path in bundled:
            with self.subTest(path=path):
                self.assertTrue({"ios", "ios_ui"}.issubset(ci.affected(path)))

    def test_non_pr_always_full(self):
        for event in ("push", "workflow_dispatch", None, "merge_group"):
            self.assertEqual(ci.selection(event, "", "")[0], ALL)

    def test_diff_error_falls_back_to_full(self):
        with patch.object(
            ci, "git", side_effect=subprocess.CalledProcessError(1, "git")
        ):
            self.assertEqual(ci.selection("pull_request", "bad", "bad")[0], ALL)


class GateTests(unittest.TestCase):
    def needs(self, selected, result):
        return {
            "changes": {"result": "success", "outputs": {"web": selected}},
            "suite": {"result": result},
        }

    def test_only_explicit_unselected_skip_passes(self):
        for selected in ("true", "false", "", None, "TRUE"):
            for result in ("success", "skipped", "failure", "cancelled", None):
                with self.subTest(selected=selected, result=result):
                    needs = self.needs(selected, result)
                    if (selected, result) in (
                        ("true", "success"),
                        ("false", "skipped"),
                    ):
                        ci.gate(needs, "web", "suite")
                    else:
                        with self.assertRaises(ValueError):
                            ci.gate(needs, "web", "suite")

    def test_selector_failure_and_missing_jobs_never_pass(self):
        for result in ("failure", "cancelled", "skipped", None):
            needs = self.needs("false", "skipped")
            needs["changes"]["result"] = result
            with self.assertRaises(ValueError):
                ci.gate(needs, "web", "suite")

    def test_required_journey_gate_accepts_only_selected_success_or_explicit_skip(self):
        for selected, ios, result in (
            ("true", "false", "success"),
            ("false", "true", "success"),
            ("false", "false", "skipped"),
        ):
            needs = {
                "changes": {
                    "result": "success",
                    "outputs": {
                        "journey": selected,
                        "ios": ios,
                        "shared_motion": "false",
                    },
                },
                "journey": {"result": result},
                "journey-library": {"result": "skipped"},
                # test-api has other dependencies; they must not weaken this gate.
                "test-api-suite": {"result": "success"},
                "creator-contract-replay": {"result": "success"},
                "shared-motion-runtime": {"result": "skipped"},
            }
            ci.gate(needs, "journey", "journey")
        for selected, ios, result in (
            ("true", "false", "skipped"),
            ("false", "true", "skipped"),
            ("false", "false", "success"),
            (None, "false", "skipped"),
            ("false", None, "skipped"),
        ):
            needs = {
                "changes": {
                    "result": "success",
                    "outputs": {"journey": selected, "ios": ios},
                },
                "journey": {"result": result},
            }
            with self.assertRaises(ValueError):
                ci.gate(needs, "journey", "journey")
        for ids in (
            "kri-384-library-ingest",
            "kri-384-library-ingest,kri-577-reliability-gates",
            "kri-384-library-ingest,kri-524-creation",
        ):
            with self.subTest(ids=ids):
                library_needs = {
                    "changes": {
                        "result": "success",
                        "outputs": {
                            "journey": "true",
                            "ios": "false",
                            "journey_ids": ids,
                            "shared_motion": "false",
                        },
                    },
                    "journey": {"result": "success"},
                    "journey-library": {"result": "success"},
                    "test-api-suite": {"result": "success"},
                    "creator-contract-replay": {"result": "success"},
                    "shared-motion-runtime": {"result": "skipped"},
                }
                ci.gate(library_needs, "journey", "journey")
                library_needs["journey-library"]["result"] = "skipped"
                with self.assertRaises(ValueError):
                    ci.gate(library_needs, "journey", "journey")
        ci.gate(
            {
                "changes": {
                    "result": "success",
                    "outputs": {"api": "true", "shared_motion": "false"},
                },
                "test-api-suite": {"result": "success"},
                "journey": {"result": "skipped"},
                "journey-library": {"result": "skipped"},
                "creator-contract-replay": {"result": "success"},
                "shared-motion-runtime": {"result": "skipped"},
            },
            "api",
            "test-api-suite",
        )
        for needs in (
            {},
            {"changes": {"result": "success"}},
            {"suite": {"result": "success"}},
        ):
            with self.assertRaises(ValueError):
                ci.gate(needs, "web", "suite")

    def test_pr_ios_gate_shares_the_journey_job(self):
        for ios, journey, result in (
            ("true", "false", "success"),
            ("false", "true", "success"),
            ("false", "false", "skipped"),
        ):
            needs = {
                "changes": {
                    "result": "success",
                    "outputs": {
                        "ios": ios,
                        "journey": journey,
                        "ios_ui": "false",
                        "ios_ui_groups": "none",
                        "ios_ui_shards": "1",
                    },
                },
                "journey": {"result": result},
                "ios-contracts": {"result": "success" if ios == "true" else "skipped"},
            }
            ci.gate(needs, "ios", "journey")
            needs["journey"]["result"] = "failure"
            with self.assertRaises(ValueError):
                ci.gate(needs, "ios", "journey")
            needs["journey"]["result"] = result
            needs["ios-contracts"]["result"] = "failure"
            with self.assertRaises(ValueError):
                ci.gate(needs, "ios", "journey")

    def test_ios_gate_rejects_missing_or_inconsistent_ui_selection(self):
        for ios, ui in (("true", None), ("true", ""), ("false", "true")):
            needs = {
                "changes": {"result": "success", "outputs": {"ios": ios, "ios_ui": ui}},
                "ios-tests": {"result": "success"},
            }
            with self.assertRaises(ValueError):
                ci.gate(needs, "ios", "ios-tests")
        for ui in ("true", "false"):
            ci.gate(
                {
                    "changes": {
                        "result": "success",
                        "outputs": {
                            "ios": "true",
                            "ios_ui": ui,
                            "ios_ui_groups": "full" if ui == "true" else "none",
                            "ios_ui_shards": "1",
                        },
                    },
                    "ios-tests": {"result": "success"},
                },
                "ios",
                "ios-tests",
            )

    def test_ios_gate_requires_consistent_group_contract(self):
        for groups in (
            None,
            "",
            "none",
            "typo",
            "editor",
            "smoke",
            "smoke,editor,creation",
            "smoke,B/testB,A/testA",
            "smoke,A/testA,A/testA",
            "smoke,A/notATest",
        ):
            needs = {
                "changes": {
                    "result": "success",
                    "outputs": {
                        "ios": "true",
                        "ios_ui": "true",
                        "ios_ui_groups": groups,
                        "ios_ui_shards": "1",
                    },
                },
                "ios-tests": {"result": "success"},
            }
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                ci.gate(needs, "ios", "ios-tests")
        for groups in (
            "full",
            *ci.ui_tests.FOCUSED,
            "smoke,A/testA,B/testB",
            "smoke,editor,A/testA",
        ):
            needs = {
                "changes": {
                    "result": "success",
                    "outputs": {
                        "ios": "true",
                        "ios_ui": "true",
                        "ios_ui_groups": groups,
                        "ios_ui_shards": "1",
                    },
                },
                "ios-tests": {"result": "success"},
            }
            ci.gate(needs, "ios", "ios-tests")

    def test_ios_gate_requires_a_valid_shard_count(self):
        def needs(ui, shards):
            outputs = {
                "ios": "true",
                "ios_ui": ui,
                "ios_ui_groups": "smoke,editor" if ui == "true" else "none",
            }
            if shards is not None:
                outputs["ios_ui_shards"] = shards
            return {
                "changes": {"result": "success", "outputs": outputs},
                "ios-tests": {"result": "success"},
            }

        maximum = str(ci.ui_tests.MAX_SHARDS)
        invalid = [
            ("true", s) for s in (None, "", "0", "x", "1/2", str(int(maximum) + 1))
        ]
        # A UI-less run is one build/unit leg; extra legs would only repeat it.
        invalid += [("false", "2"), ("false", maximum)]
        for ui, shards in invalid:
            with self.subTest(ui=ui, shards=shards), self.assertRaises(ValueError):
                ci.gate(needs(ui, shards), "ios", "ios-tests")
        for ui, shards in [("true", str(n)) for n in range(1, int(maximum) + 1)] + [
            ("false", "1")
        ]:
            with self.subTest(ui=ui, shards=shards):
                ci.gate(needs(ui, shards), "ios", "ios-tests")

    def test_ui_shards_follow_the_executed_selection(self):
        # Main and manual runs shard the full suite: serial no longer fits the cap.
        self.assertEqual(ci.ui_shards("push", "full"), 1)
        self.assertEqual(
            ci.ui_shards("workflow_dispatch", "full"), ci.ui_tests.MAX_SHARDS
        )
        # One PR Mac job owns the selected UI and journey scenarios.
        self.assertEqual(ci.ui_shards("pull_request", "full"), 1)
        self.assertEqual(ci.ui_shards("pull_request", "none"), 1)
        self.assertEqual(ci.ui_shards("pull_request", "smoke,projects"), 1)
        self.assertEqual(ci.ui_shards("pull_request", "smoke,editor"), 1)
        # An unusable selection keeps one leg; its UI step fails closed.
        with patch.object(ci.ui_tests, "shard_count", side_effect=ValueError):
            self.assertEqual(ci.ui_shards("pull_request", "smoke,editor"), 1)

    def test_lint_union(self):
        for web, api in (
            ("true", "false"),
            ("false", "true"),
            ("true", "true"),
            ("false", "false"),
        ):
            result = "skipped" if api == "false" else "success"
            needs = {
                "changes": {"result": "success", "outputs": {"web": web, "api": api}},
                "lint-suites": {"result": result},
            }
            ci.gate(needs, "lint", "lint-suites")
        with self.assertRaises(ValueError):
            ci.gate(self.needs("false", "skipped"), "lint", "suite")


class GitDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old)
        self.git("init", "-q")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "CI tests")
        self.write("README.md", "initial")
        self.write("src/apps/web/old.ts", "export default 1;")
        self.write(
            "package.json", json.dumps({"version": "1", "scripts": {"test": "test"}})
        )
        self.write(
            "package-lock.json",
            json.dumps(
                {
                    "version": "1",
                    "packages": {
                        "": {"version": "1"},
                        "node_modules/a": {"version": "1"},
                    },
                }
            ),
        )
        self.base = self.commit()

    def git(self, *args):
        return (
            subprocess.check_output(["git", *args], stderr=subprocess.PIPE)
            .decode()
            .strip()
        )

    def write(self, path, value):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "test")
        return self.git("rev-parse", "HEAD")

    def selected(self):
        return ci.selection("pull_request", self.base, self.commit())[0]

    def test_selector_contract_test_changes_stay_on_unconditional_linux_gate(self):
        self.write("scripts/ci/test_select_tests.py", "changed selector assertions")
        head = self.commit()
        suites, _, groups = ci.selection_details("pull_request", self.base, head)
        self.assertEqual(suites, set())
        self.assertEqual(groups, "none")
        self.assertEqual(
            ci.journey_details("pull_request", self.base, head),
            (False, "not_applicable", ""),
        )
        self.assertFalse(ci.native_validation("pull_request", suites, False))
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci-changes.yml").read_text()
        self.assertIn(
            "python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v", workflow
        )
        self.assertLess(
            workflow.index("Test CI selection"),
            workflow.index("Select affected suites"),
        )

    def test_audited_chat_workspace_tests_keep_full_native_units_without_exports(self):
        path = "src/apps/ios/Tests/KriaTests/ChatWorkspaceTests.swift"
        self.write(path, "changed unit assertions")
        head = self.commit()
        suites, _, groups = ci.selection_details("pull_request", self.base, head)
        self.assertEqual(suites, {"ios"})
        self.assertEqual(groups, "none")
        self.assertEqual(
            ci.journey_details("pull_request", self.base, head),
            (False, "not_applicable", ""),
        )
        self.assertTrue(ci.native_validation("pull_request", suites, False))
        self.assertEqual(ci.native_legs([]), ["unit"])
        named = {test for row in ci.JOURNEYS for test in row.get("xctests", [])}
        self.assertFalse(
            any(test.startswith("KriaTests/ChatWorkspaceTests/") for test in named)
        )

    def test_isolated_tests_never_override_runtime_or_policy_dependencies(self):
        isolated = (
            "scripts/ci/test_select_tests.py",
            "src/apps/ios/Tests/KriaTests/ChatWorkspaceTests.swift",
        )
        for path in isolated:
            self.write(path, "changed unit assertions")
        base = self.base
        all_journeys = {row["id"] for row in ci.JOURNEYS}
        cases = {
            "scripts/ci/select-tests.py": (ALL, all_journeys),
            ".github/workflows/ci.yml": (ALL, all_journeys),
            "scripts/ci/journey-manifest.json": (ALL, all_journeys),
            "src/apps/api/app/routes/generative_jobs.py": (
                {"api", "ios"},
                {"kri-648-phone-text", "kri-852-speech-editor"},
            ),
            "src/apps/api/app/schemas/guided_edit_revision.py": (
                {"api", "ios"},
                {
                    "kri-1032-montage-copy-parity",
                    "kri-561-song-order",
                    "kri-689-clip-speed",
                },
            ),
            "src/apps/ios/Kria/Features/NativeEditorSession.swift": (
                {"ios", "ios_ui"},
                {"kri-648-phone-text", "kri-669-gameday", "kri-852-speech-editor"},
            ),
            "src/apps/ios/Tests/KriaTests/DeviceMontageRenderE2ETests.swift": (
                {"ios"},
                {"kri-492-length-ownership", "kri-524-creation"},
            ),
            "src/apps/ios/Tests/KriaTests/NativeEditorInteractionTests.swift": (
                {"ios"},
                set(),
            ),
            "scripts/ios/kri-492-length-e2e.py": (
                {"ios", "ios_ui"},
                {"kri-492-length-ownership"},
            ),
            "new-runtime/unknown.py": (ALL, set()),
        }
        for path, (required_suites, required_journeys) in cases.items():
            with self.subTest(path=path):
                self.write(path, "changed dependency")
                head = self.commit()
                actual = set(
                    ci.journey_details("pull_request", base, head)[2].split(",")
                ) - {""}
                self.assertTrue(required_journeys <= actual)
                suites = ci.selection("pull_request", base, head)[0]
                self.assertTrue(required_suites <= suites)
                self.assertTrue(
                    ci.native_validation(
                        "pull_request", suites, bool(required_journeys)
                    )
                )
                legs = ci.native_legs(actual)
                if required_journeys & {
                    "kri-492-length-ownership",
                    "kri-689-clip-speed",
                    "kri-669-gameday",
                    "kri-524-creation",
                }:
                    self.assertIn("creation", legs)
                if "kri-648-phone-text" in required_journeys:
                    self.assertIn("screens", legs)
                Path(path).unlink()

    def test_full_regression_cadences_ignore_isolated_test_exemptions(self):
        for event in ("push", "schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual(ci.selection(event, self.base, self.base)[0], ALL)
                selected, coverage, ids = ci.journey_details(
                    event, self.base, self.base
                )
                self.assertTrue(selected)
                self.assertEqual(coverage, "covered")
                self.assertEqual(
                    set(ids.split(",")), {row["id"] for row in ci.JOURNEYS}
                )

    def test_focused_and_unit_only_group_outputs(self):
        self.write("src/apps/ios/Kria/Features/CreationAttachments.swift", "changed")
        head = self.commit()
        self.assertEqual(
            ci.selection_details("pull_request", self.base, head)[2], "smoke,creation"
        )
        self.write("src/apps/ios/Kria/Features/NativeEditorView.swift", "changed")
        head = self.commit()
        self.assertEqual(
            ci.selection_details("pull_request", self.base, head)[2], "full"
        )
        self.assertEqual(ci.selection_details("push", self.base, head)[2], "smoke")
        self.assertEqual(ci.selection_details("pull_request", head, head)[2], "full")

    def test_ui_test_method_edits_select_only_those_methods(self):
        # #1162 fixed one editor test and paid for the whole ~25-minute group.
        path = "src/apps/ios/Tests/KriaUITests/NativeEditorInspectorUITests.swift"
        original = (SCRIPT.parents[2] / path).read_text()
        self.write(path, original)
        self.base = self.commit()
        method = "func testTextReturnAndDeleteKeepCanvasLinesAligned() {"
        body = original.split(method, 1)[1]
        statement = "XCTAssertTrue(input.waitForExistence(timeout: 3))"
        self.assertIn(statement, body.split("    func ", 1)[0])
        edited = original.replace(
            method + body.split(statement, 1)[0] + statement,
            method + body.split(statement, 1)[0] + statement.replace("3", "4"),
        )
        self.write(path, edited)
        head = self.commit()
        self.assertEqual(
            ci.selection_details("pull_request", self.base, head)[2],
            "smoke,NativeEditorInspectorUITests/"
            "testTextReturnAndDeleteKeepCanvasLinesAligned",
        )
        # Deleting a line inside the method is attributed to it as well.
        self.base = head
        self.write(path, edited.replace(statement.replace("3", "4") + "\n", "", 1))
        self.assertEqual(
            ci.selection_details("pull_request", self.base, self.commit())[2],
            "smoke,NativeEditorInspectorUITests/"
            "testTextReturnAndDeleteKeepCanvasLinesAligned",
        )

    def test_ui_test_helper_or_header_edits_select_the_whole_group(self):
        path = "src/apps/ios/Tests/KriaUITests/NativeEditorInspectorUITests.swift"
        original = (SCRIPT.parents[2] / path).read_text()
        self.write(path, original)
        self.base = self.commit()
        helper = 'XCTAssertTrue(element.waitForExistence(timeout: 3), "Missing timeline'
        self.assertEqual(original.count(helper), 1)
        for edited in (
            original.replace(helper, helper.replace("3", "4"), 1),
            original.replace("import UIKit", "import UIKit\nimport Foundation", 1),
        ):
            with self.subTest(edited=edited[:40]):
                self.write(path, edited)
                head = self.commit()
                self.assertEqual(
                    ci.selection_details("pull_request", self.base, head)[2],
                    "smoke,editor",
                )
                self.base = head

    def test_unit_and_contract_only_keep_ui_omitted(self):
        for path in (
            "src/apps/ios/Tests/KriaTests/new.swift",
            "src/apps/ios/Kria/Generated/openapi.yaml",
            "src/apps/api/app/schemas/new.py",
        ):
            self.write(path, "changed")
        self.assertEqual(
            ci.selection_details("pull_request", self.base, self.commit())[2], "none"
        )

    def test_ui_rename_to_unknown_path_requires_full(self):
        self.write("src/apps/ios/Kria/Features/NativeEditorView.swift", "changed")
        self.base = self.commit()
        self.git(
            "mv",
            "src/apps/ios/Kria/Features/NativeEditorView.swift",
            "src/apps/ios/Kria/Features/Unknown.swift",
        )
        self.assertEqual(
            ci.selection_details("pull_request", self.base, self.commit())[2], "full"
        )

    def test_release_metadata_does_not_wake_suites(self):
        for path in ("package.json", "package-lock.json"):
            data = json.loads(Path(path).read_text())
            data["version"] = "2"
            if "packages" in data:
                data["packages"][""]["version"] = "2"
            self.write(path, json.dumps(data))
        self.write("VERSION", "2")
        self.assertEqual(self.selected(), set())

    def test_dependency_version_still_runs_all(self):
        data = json.loads(Path("package-lock.json").read_text())
        data["packages"]["node_modules/a"]["version"] = "2"
        self.write("package-lock.json", json.dumps(data))
        self.assertEqual(self.selected(), ALL)

    def test_deleted_and_malformed_manifests_run_all(self):
        Path("package.json").unlink()
        self.write("package-lock.json", "bad json")
        self.assertEqual(self.selected(), ALL)

    def test_rename_checks_both_sides(self):
        Path("docs").mkdir()
        self.git("mv", "src/apps/web/old.ts", "docs/old.ts")
        self.assertEqual(self.selected(), set())

    def test_deletion_and_mixed_changes(self):
        Path("src/apps/web/old.ts").unlink()
        self.write("scripts/ios/new.sh", "true")
        self.assertEqual(self.selected(), {"ios", "ios_ui"})

    def test_names_with_newlines_are_not_split(self):
        self.write("src/apps/web/new\nfile.ts", "test")
        self.assertEqual(self.selected(), set())

    def test_merge_base_excludes_unrelated_base_changes(self):
        self.git("checkout", "-qb", "pr")
        self.write("src/apps/web/new.ts", "test")
        head = self.commit()
        self.git("checkout", "-q", self.base)
        self.write("src/apps/api/app/pipeline/new.py", "test")
        moved_base = self.commit()
        self.assertEqual(ci.selection("pull_request", moved_base, head)[0], set())

    def test_empty_diff_runs_all(self):
        self.assertEqual(ci.selection("pull_request", self.base, self.base)[0], ALL)

    def test_journey_manifest_coverage_is_explicit_and_fail_closed(self):
        cases = (
            (
                "src/apps/api/app/pipeline/guided_story.py",
                (True, "covered", "kri-524-creation"),
            ),
            (
                "src/apps/api/app/services/cloud_render_contract.py",
                (True, "covered", "kri-492-length-ownership,kri-524-creation"),
            ),
            (
                "src/apps/api/app/kria/planner.py",
                (
                    True,
                    "covered",
                    "kri-524-creation,kri-557-thinking,kri-787-merope-t1-replay,kri-577-reliability-gates,kri-760-whole-line-ask,kri-988-pool-photo-attached,kri-721-approval-readiness,kri-966-take-window-length,duration-text-preservation",
                ),
            ),
            (
                "src/apps/api/app/kria/editor_sequence_timing.py",
                (True, "covered", "duration-text-preservation"),
            ),
            (
                "src/apps/api/app/kria/approval_readiness.py",
                (
                    True,
                    "covered",
                    "kri-721-approval-readiness,kri-29ekim-live-approval-gate",
                ),
            ),
            (
                "src/apps/api/app/tasks/content_plan_build.py",
                (
                    True,
                    "covered",
                    "kri-492-length-ownership,kri-524-creation,kri-787-merope-t1-replay,kri-577-reliability-gates,kri-788-order-answer-dispatch,kri-648-phone-text",
                ),
            ),
            (
                "src/apps/api/tests/tasks/test_kri788_order_answer_dispatch.py",
                (True, "covered", "kri-788-order-answer-dispatch"),
            ),
            (
                "src/apps/api/app/kria/duplicate_uploads.py",
                (True, "covered", "kri-577-reliability-gates"),
            ),
            (
                "src/apps/ios/Kria/Features/ThoughtSummaryDisclosure.swift",
                (True, "covered", "kri-557-thinking"),
            ),
            (
                "src/apps/api/app/schemas/slide_post.py",
                (True, "covered", "slide-post-binding-recovery"),
            ),
            (
                "src/apps/api/app/routes/plan_items.py",
                (
                    True,
                    "covered",
                    "kri-557-thinking,slide-post-binding-recovery,rendered-editor-original-clock",
                ),
            ),
            (
                "src/apps/api/app/schemas/user_song.py",
                (True, "covered", "kri-561-song-order"),
            ),
            (
                "src/apps/api/app/pipeline/lipsync_montage.py",
                (True, "covered", "kri-561-song-order"),
            ),
            (
                "src/apps/api/app/schemas/guided_edit_revision.py",
                (
                    True,
                    "covered",
                    "kri-561-song-order,kri-1032-montage-copy-parity,kri-689-clip-speed,rendered-editor-original-clock",
                ),
            ),
            (
                "src/apps/api/tests/routes/test_kri1032_editor_matches_render.py",
                (True, "covered", "kri-1032-montage-copy-parity"),
            ),
            (
                "src/apps/ios/Tests/Fixtures/KRI1032PlazaM1Editor.json",
                (True, "covered", "kri-1032-montage-copy-parity"),
            ),
            (
                "src/apps/api/app/kria/speaker_framing_ask.py",
                (True, "covered", "kri-547-speaker-framing"),
            ),
            (
                "src/apps/api/app/kria/text_shape_ask.py",
                (True, "covered", "kri-1029-1030-plaza-title"),
            ),
            (
                "src/apps/api/app/pipeline/phone_recipe_shared.py",
                (True, "covered", "kri-547-speaker-framing"),
            ),
            (
                "src/apps/api/app/pipeline/phone_speaker_framing.py",
                (True, "covered", "kri-547-speaker-framing"),
            ),
            (
                "src/apps/api/app/pipeline/phone_subtitled_plan.py",
                (
                    True,
                    "covered",
                    "kri-547-speaker-framing,kri-577-reliability-gates,kri-648-phone-text",
                ),
            ),
            (
                "src/apps/api/app/pipeline/phone_subtitled_title.py",
                (True, "covered", "kri-547-speaker-framing,kri-577-reliability-gates"),
            ),
            (
                "src/apps/api/app/pipeline/render_geometry.py",
                (True, "covered", "kri-547-speaker-framing"),
            ),
            (
                "src/apps/ios/Tests/Fixtures/KRI547SpeakerFaceFill.json",
                (True, "covered", "kri-547-speaker-framing"),
            ),
            (
                "src/apps/api/prompts/main_creator.txt",
                (True, "covered", "kri-586-hook-copy,kri-815-voiceover-script"),
            ),
            (
                "src/apps/api/tests/fixtures/agent_evals/main_creator/kri586_generate_after_choice.json",
                (True, "covered", "kri-586-hook-copy"),
            ),
            (
                "src/apps/api/tests/fixtures/agent_evals/narrated_clip_alignment/golden/tigre_spoken_cues.json",
                (True, "covered", "kri-590-voiceover-cues"),
            ),
            (
                "src/apps/api/app/kria/voiceover_scope.py",
                (True, "covered", "kri-598-voiceover-planner"),
            ),
            (
                "src/apps/api/tests/fixtures/agent_evals/clip_intent_planner/golden/kri598_tigre_spoken_cues.json",
                (True, "covered", "kri-598-voiceover-planner"),
            ),
            (
                "src/apps/api/tests/kria/test_voiceover_downgrade_losses.py",
                (True, "covered", "kri-598-voiceover-planner"),
            ),
            (
                "src/apps/api/app/kria/voiceover_clips.py",
                (True, "covered", "kri-593-voiceover-clips"),
            ),
            (
                "src/apps/api/app/pipeline/silence_cut.py",
                (True, "covered", "kri-653-silent-take"),
            ),
            (
                "src/apps/api/app/services/clip_speech_check.py",
                (True, "covered", "kri-808-clip-speech-language"),
            ),
            (
                "src/apps/api/tests/fixtures/kri808_don_julio_clip_speech.json",
                (True, "covered", "kri-808-clip-speech-language"),
            ),
            (
                "src/apps/api/app/kria/clip_scope.py",
                (
                    True,
                    "covered",
                    "kri-598-voiceover-planner,kri-623-ruled-out-clips,kri-761-look-alike-exclusions",
                ),
            ),
            (
                "src/apps/api/app/pipeline/narrated_photo_sequence.py",
                (
                    True,
                    "covered",
                    "kri-577-reliability-gates,kri-598-voiceover-planner",
                ),
            ),
            (
                "src/apps/api/tests/fixtures/kri623_tigre_m2_montage_turn.json",
                (True, "covered", "kri-623-ruled-out-clips"),
            ),
            (
                "src/apps/api/tests/fixtures/agent_evals/clip_request_resolver/golden/kri766_label_echo_vs_described_action.json",
                (True, "covered", "kri-766-label-bound"),
            ),
            (
                "src/apps/api/tests/kria/test_kri766_label_stays_bound.py",
                (True, "covered", "kri-766-label-bound"),
            ),
            (
                "src/apps/api/app/kria/narration_notes.py",
                (True, "covered", "kri-585-spoken-order"),
            ),
            (
                "src/apps/api/app/services/spoken_roles.py",
                (True, "covered", "kri-585-spoken-order"),
            ),
            (
                "src/apps/api/tests/fixtures/kri585_tigre_m1_clips.json",
                (True, "covered", "kri-585-spoken-order"),
            ),
            (
                "src/apps/api/app/pipeline/agents/gemini_analyzer.py",
                (
                    True,
                    "covered",
                    "kri-845-clip-detail-evidence,kri-625-moment-quality",
                ),
            ),
            (
                "src/apps/api/prompts/analyze_clip.txt",
                (
                    True,
                    "covered",
                    "kri-845-clip-detail-evidence,kri-625-moment-quality",
                ),
            ),
            (
                "src/apps/api/prompts/edit_copilot.txt",
                (True, "covered", "kri-667-turkish-chat"),
            ),
            (
                "src/apps/api/app/kria/opening_cut_ask.py",
                (True, "covered", "kri-787-merope-t1-replay,kri-628-opening-cut"),
            ),
            (
                "src/apps/api/app/pipeline/phone_opening_cut.py",
                (True, "covered", "kri-787-merope-t1-replay,kri-628-opening-cut"),
            ),
            (
                "src/apps/api/app/services/spoken_quotes.py",
                (
                    True,
                    "covered",
                    "kri-628-opening-cut,kri-789-take-trim-not-order,kri-681-named-line-windows,kri-760-whole-line-ask",
                ),
            ),
            (
                "src/apps/api/app/pipeline/caption_correct.py",
                (True, "covered", "kri-708-caption-correction"),
            ),
            (
                "src/apps/api/app/pipeline/phone_spoken_text.py",
                (True, "covered", "kri-707-talking-spoken-text"),
            ),
            (
                "src/apps/api/app/kria/spoken_text_ask.py",
                (True, "covered", "kri-707-talking-spoken-text"),
            ),
            (
                "src/apps/api/app/pipeline/phone_talking_cutaways.py",
                (True, "covered", "kri-719-talking-multi-clip"),
            ),
            (
                "src/apps/api/app/kria/duplicate_send.py",
                (True, "covered", "kri-552-duplicate-resend"),
            ),
            (
                "src/apps/api/app/kria/text_trim.py",
                (True, "covered", "kri-552-duplicate-resend"),
            ),
            (
                "src/apps/api/tests/pipeline/test_phone_talking_cutaways.py",
                (True, "covered", "kri-719-talking-multi-clip"),
            ),
            (
                "src/apps/api/app/pipeline/phone_subtitled_lanes.py",
                (True, "covered", "kri-841-talking-ending-clip,speaker-background"),
            ),
            (
                "src/apps/api/tests/tasks/test_kri841_talking_ending_clip.py",
                (True, "covered", "kri-841-talking-ending-clip"),
            ),
            (
                "src/apps/api/app/services/length_planning.py",
                (
                    True,
                    "covered",
                    "kri-681-named-line-windows,kri-988-pool-photo-attached",
                ),
            ),
            (
                "src/apps/api/tests/fixtures/kri681_tigre_night_m1_clips.json",
                (True, "covered", "kri-681-named-line-windows"),
            ),
            (
                "src/apps/api/tests/fixtures/kri803_don_julio_m1_clips.json",
                (True, "covered", "kri-760-whole-line-ask"),
            ),
            (
                "src/apps/api/app/pipeline/unified_montage.py",
                (
                    True,
                    "covered",
                    "kri-492-length-ownership,kri-524-creation,kri-577-reliability-gates,kri-760-whole-line-ask,kri-648-phone-text,kri-29ekim-post-approval-text-sequence",
                ),
            ),
            (
                "src/apps/api/app/pipeline/captions.py",
                (True, "covered", "kri-632-phone-captions"),
            ),
            (
                "src/apps/api/app/pipeline/caption_spelling.py",
                (True, "covered", "kri-632-phone-captions"),
            ),
            (
                "src/apps/api/tests/fixtures/kri632_t1_talking_words.json",
                (True, "covered", "kri-632-phone-captions"),
            ),
            (
                "src/apps/api/app/pipeline/phone_captions.py",
                (True, "covered", "kri-548-caption-watermark,kri-648-phone-text"),
            ),
            (
                "src/apps/api/app/pipeline/portable_text_layout.py",
                (True, "covered", "kri-548-caption-watermark"),
            ),
            (
                "src/apps/ios/Tests/Fixtures/KRI548CaptionWatermark.json",
                (True, "covered", "kri-548-caption-watermark"),
            ),
            (
                "src/apps/api/app/kria/planner.py.backup",
                (True, "gap", ""),
            ),
            (
                "src/apps/api/app/kria/device_render.py",
                (
                    True,
                    "covered",
                    "kri-492-length-ownership,kri-669-gameday,kri-726-background-recovery",
                ),
            ),
            (
                "src/apps/api/app/kria/automatic_export_repair.py",
                (True, "covered", "kri-726-background-recovery"),
            ),
            (
                "src/apps/api/app/tasks/export_verification.py",
                (True, "covered", "kri-726-background-recovery"),
            ),
            (
                "src/apps/api/tests/integration/test_background_export_completion.py",
                (True, "covered", "kri-726-background-recovery"),
            ),
            (
                "src/apps/api/app/pipeline/pinned_text.py",
                (True, "covered", "kri-669-gameday"),
            ),
            (
                "src/apps/api/app/pipeline/text_stack_layout.py",
                (True, "covered", "kri-669-gameday"),
            ),
            (
                "src/apps/api/prompts/edit_copilot_ops/text.txt",
                (True, "covered", "kri-669-gameday,duration-text-preservation"),
            ),
            # This has journey impact but no incident-corpus repro mapping.
            (
                "src/apps/api/app/pipeline/unmapped_future_compiler.py",
                (True, "gap", ""),
            ),
            ("docs/runbooks/ci.md", (False, "not_applicable", "")),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                self.write(path, "changed")
                self.assertEqual(
                    ci.journey_details("pull_request", self.base, self.commit()),
                    expected,
                )
                self.base = self.git("rev-parse", "HEAD")

    def test_journey_selects_each_affected_incident_without_borrowing_another(self):
        self.write("src/apps/api/app/pipeline/guided_story.py", "changed")
        self.write("src/apps/ios/Kria/Core/Services.swift", "changed")
        self.assertEqual(
            ci.journey_details("pull_request", self.base, self.commit()),
            (
                True,
                "covered",
                "kri-524-creation,kri-557-thinking,kri-648-phone-text,kri-551-consented-retakes,rendered-editor-original-clock",
            ),
        )

    def test_rendered_clock_paths_select_the_recovery_journey(self):
        for path in (
            "src/apps/api/app/routes/generative_jobs.py",
            "src/apps/api/app/services/guided_timeline.py",
            "src/apps/ios/Kria/Core/NativeEditorInteraction.swift",
            "src/apps/ios/Tests/KriaTests/RenderedOriginalClockTests.swift",
        ):
            with self.subTest(path=path):
                self.write(path, "changed")
                selected, coverage, ids = ci.journey_details(
                    "pull_request", self.base, self.commit()
                )
                self.assertTrue(selected)
                self.assertEqual(coverage, "covered")
                self.assertIn("rendered-editor-original-clock", ids.split(","))
                self.assertTrue(
                    ci.journey_needs_native(
                        "pull_request", self.base, self.git("rev-parse", "HEAD")
                    )
                )
                self.base = self.git("rev-parse", "HEAD")

    def test_journey_native_runner_is_linux_for_python_only_and_mac_for_mixed(self):
        self.write("src/apps/api/app/kria/duplicate_uploads.py", "changed")
        self.write("src/apps/api/app/agents/narrated_clip_alignment.py", "changed")
        head = self.commit()
        self.assertEqual(
            ci.journey_details("pull_request", self.base, head)[2],
            "kri-577-reliability-gates,kri-590-voiceover-cues,kri-215-phone-editorial-skills",
        )
        self.assertFalse(ci.journey_needs_native("pull_request", self.base, head))
        self.base = head
        self.write("src/apps/api/app/pipeline/guided_story.py", "changed")
        self.write(
            "src/apps/api/app/agents/narrated_clip_alignment.py", "changed again"
        )
        head = self.commit()
        self.assertTrue(ci.journey_needs_native("pull_request", self.base, head))

    def test_journey_diff_errors_and_empty_diff_are_coverage_gaps(self):
        self.assertEqual(
            ci.journey_details("pull_request", self.base, self.base), (True, "gap", "")
        )
        with patch.object(
            ci, "git", side_effect=subprocess.CalledProcessError(1, "git")
        ):
            self.assertEqual(
                ci.journey_details("pull_request", "bad", "bad"), (True, "gap", "")
            )

    def unmapped_cli(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, str(SCRIPT.resolve()), "unmapped-paths", *args],
            input=stdin,
            capture_output=True,
            text=True,
        )

    def test_unmapped_paths_names_only_unmapped_journey_paths(self):
        # Failure modes (KRI-749): unmapped path unnamed; mapped path falsely
        # named; non-journey path failing preship; git error reading as clean.
        unmapped = "src/apps/api/app/kria/zz_unmapped.py"
        mapped = next(
            prefix
            for journey in ci.JOURNEYS
            for prefix in journey["paths"]
            if not prefix.endswith("/")
        )
        self.write(unmapped, "x = 1")
        self.write(mapped, "x = 1")
        self.write("src/apps/web/unrelated.ts", "export {};")
        head = self.commit()
        result = self.unmapped_cli("--base", self.base, "--head", head)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.split(), [unmapped])

    def test_unmapped_paths_is_quiet_for_mapped_and_non_journey_changes(self):
        self.write("src/apps/web/unrelated.ts", "export {};")
        head = self.commit()
        result = self.unmapped_cli("--base", self.base, "--head", head)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, ""))

    def test_unmapped_paths_git_error_and_missing_base_are_nonzero(self):
        self.assertEqual(
            self.unmapped_cli("--base", "no-such-ref", "--head", "HEAD").returncode, 1
        )
        self.assertEqual(self.unmapped_cli().returncode, 2)

    def test_unmapped_paths_agrees_with_the_ci_gap_verdict(self):
        self.write("src/apps/api/app/kria/zz_unmapped.py", "x = 1")
        head = self.commit()
        self.assertEqual(ci.journey_details("pull_request", self.base, head)[1], "gap")
        self.assertEqual(
            ci.unmapped_journey_paths(self.base, head),
            ["src/apps/api/app/kria/zz_unmapped.py"],
        )

    def test_unmapped_paths_stdin_classifies_uncommitted_files(self):
        result = self.unmapped_cli(
            "--stdin", stdin="src/apps/api/app/kria/zz_unmapped.py\nREADME.md\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.split(), ["src/apps/api/app/kria/zz_unmapped.py"]
        )

    def test_preship_runs_the_journey_mapping_check(self):
        preship = (SCRIPT.parents[2] / "scripts/preship-check.sh").read_text()
        self.assertIn("validate-mapping", preship)
        self.assertIn("unmapped-paths", preship)
        self.assertIn("-u GITHUB_OUTPUT", preship)

    def test_cli_outputs_and_summary(self):
        self.write("src/apps/ios/new.swift", "test")
        head = self.commit()
        output, summary = Path("output"), Path("summary")
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT.resolve()),
            ],
            check=True,
            env={
                **os.environ,
                "CI_EVENT": "pull_request",
                "CI_BASE": self.base,
                "CI_HEAD": head,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
            },
            capture_output=True,
        )
        self.assertEqual(
            output.read_text(),
            "api=false\nios=true\nios_ui=true\nshared_motion=false\nnative_validation=true\nios_ui_groups=full\n"
            "ios_ui_shards=1\njourney=false\njourney_coverage=not_applicable\n"
            'journey_ids=\njourney_needs_native=false\nnative_legs=["unit"]\n'
            "ios_render_evidence=false\n",
        )
        self.assertIn("ios: run", summary.read_text())
        self.assertIn("Export/OCR unit proofs: skipped", summary.read_text())
        self.assertIn("iOS UI groups: full (1 native leg)", summary.read_text())


if __name__ == "__main__":
    unittest.main()
