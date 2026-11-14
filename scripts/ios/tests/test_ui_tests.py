"""Guard selection against uncovered tests and validate actual XCTest results."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
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
OVERRUN_FIXTURE = json.loads(
    (Path(__file__).with_name("fixtures") / "overrun-ui-results.json").read_text()
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
    def test_measured_durations_preserve_new_feature_timings(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "feature.json"
            measured = Path(directory) / "measured.json"
            legacy.write_text(json.dumps({"existing": 300, "new": 90}))
            measured.write_text(json.dumps({"existing": 60}))
            with (
                patch.object(ui, "DURATIONS", measured),
                patch.object(ui, "LEGACY_DURATIONS", legacy, create=True),
            ):
                self.assertEqual(
                    ui.weights({"existing", "new"}), {"existing": 60, "new": 90}
                )

    def test_feature_durations_work_before_any_bulk_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "feature.json"
            legacy.write_text(json.dumps({"new": 90}))
            with (
                patch.object(ui, "DURATIONS", Path(directory) / "missing.json"),
                patch.object(ui, "LEGACY_DURATIONS", legacy, create=True),
            ):
                self.assertEqual(ui.weights({"new"}), {"new": 90})

    def test_registry_follows_the_tests_and_every_test_is_in_one_group(self):
        problems = ui.check()
        self.assertFalse(problems, "\n".join(problems))
        data = ui.manifest()
        features = [
            test
            for group in ("creation", "projects", "editor")
            for test in data["groups"][group]
        ]
        self.assertEqual(len(features), len(set(features)))
        unclassified = {
            test
            for test in ui.discovered()
            if test.split("/")[1] in data["unclassified"]
        }
        self.assertEqual(set(features) | unclassified, ui.discovered())
        self.assertLessEqual(unclassified, set(data["groups"]["smoke"]))

    def test_each_exact_mapping_selects_smoke_and_one_feature(self):
        for path, group in ui.manifest()["sources"].items():
            with self.subTest(path=path):
                self.assertTrue((ui.ROOT / path).is_file(), "Mapping is stale")
                self.assertEqual(ui.select_groups([path])[0], f"smoke,{group}")

    def test_a_ui_test_file_selects_the_group_that_owns_its_tests(self):
        # #2243 edited NativeCaptionAnimationUITests.swift, an editor test file
        # with no `sources` entry: selection fell back to `full`, which PRs run
        # as smoke, so the edited tests never ran before merge. A test file's
        # group comes from the groups' own inventory, never a hand mapping.
        data = ui.manifest()
        owners = {}
        for group in ui.FEATURES:
            for test in data["groups"][group]:
                owners.setdefault(test, set()).add(group)
        checked = 0
        for path in sorted((ui.ROOT / ui.UI_TEST_ROOT).rglob("*.swift")):
            relative = str(path.relative_to(ui.ROOT))
            source = path.read_text()
            classes = re.findall(r"\bclass\s+(\w+)\s*:\s*XCTestCase\b", source)
            methods = re.findall(r"\bfunc\s+(test\w+)\s*\(", source)
            if not methods or relative in data["sources"]:
                continue
            groups = set().union(
                *(
                    owners.get(f"KriaUITests/{classes[0]}/{name}", set())
                    for name in methods
                )
            )
            if len(groups) != 1:
                continue
            with self.subTest(path=relative):
                self.assertEqual(
                    ui.select_groups([relative])[0], f"smoke,{groups.pop()}"
                )
                checked += 1
        self.assertGreater(checked, 0)
        self.assertEqual(
            ui.select_groups(
                ["src/apps/ios/Tests/KriaUITests/NativeCaptionAnimationUITests.swift"]
            )[0],
            "smoke,editor",
        )

    def test_test_file_groups_stay_conservative(self):
        # Failure modes: a file whose tests span groups runs only one of them; a
        # file-scope helper other files call changes their tests unseen; an
        # explicit mapping is overridden.
        files = {
            "AUITests.swift": "import XCTest\n\n@MainActor\nfinal class AUITests: XCTestCase {\n    func testA() {}\n}\n\nextension AUITests {\n    func helper() {}\n}\n",
            "BUITests.swift": "import XCTest\n\nfinal class BUITests: XCTestCase {\n    func testB() {}\n}\n\nstruct Harness {}\n",
            "EUITests.swift": "import XCTest\n\nfinal class EUITests: XCTestCase {\n    func testF() {}\n}\n\nprivate extension XCUIElement {}\n",
            "CUITests.swift": "import XCTest\n\nfinal class CUITests: XCTestCase {\n    func testC() {}\n    func testD() {}\n}\n",
            "DUITests.swift": "import XCTest\n\nfinal class DUITests: XCTestCase {\n    func testE() {}\n}\n",
        }
        data = {
            "groups": {
                "smoke": ["KriaUITests/AUITests/testA"],
                "editor": [
                    "KriaUITests/AUITests/testA",
                    "KriaUITests/BUITests/testB",
                    "KriaUITests/CUITests/testC",
                    "KriaUITests/EUITests/testF",
                ],
                "creation": ["KriaUITests/CUITests/testD"],
                "projects": ["KriaUITests/DUITests/testE"],
            },
            "sources": {f"{ui.UI_TEST_ROOT}DUITests.swift": "creation"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ui.UI_TEST_ROOT).mkdir(parents=True)
            for name, source in files.items():
                (root / ui.UI_TEST_ROOT / name).write_text(source)
            with patch.object(ui, "manifest", return_value=data):
                for name, expected in (
                    ("AUITests.swift", "smoke,editor"),
                    ("BUITests.swift", "full"),
                    ("CUITests.swift", "full"),
                    ("EUITests.swift", "smoke,editor"),
                    ("DUITests.swift", "smoke,creation"),
                ):
                    with self.subTest(name=name):
                        self.assertEqual(
                            ui.select_groups([ui.UI_TEST_ROOT + name], root)[0],
                            expected,
                        )

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

    def test_new_removed_and_renamed_tests_need_no_registry_edit(self):
        # A method added, renamed or removed inside a classified class stays in
        # that class's group; a brand-new class runs in every selection until it
        # is classified, so no test is skipped for want of a registry line.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "src/apps/ios/Tests/KriaUITests"
            shutil.copytree(ui.ROOT / "src/apps/ios/Tests/KriaUITests", tests)
            path = "src/apps/ios/Tests/KriaUITests/ProjectsUITests.swift"
            original = (root / path).read_text()
            for source, test in (
                (
                    original.replace(
                        "    func testProject", "    func testRenamedProject", 1
                    ),
                    "KriaUITests/ProjectsUITests/testRenamedProjectActionsCanBeCancelledWithoutChangingProject",
                ),
                (
                    original.replace(
                        "final class ProjectsUITests: XCTestCase {",
                        "final class ProjectsUITests: XCTestCase {\n    func testNewCase() {}",
                    ),
                    "KriaUITests/ProjectsUITests/testNewCase",
                ),
            ):
                (root / path).write_text(source)
                value = ui.select_groups([path], root)[0]
                self.assertEqual(value, "smoke,projects")
                self.assertIn(test, ui.manifest(root)["groups"]["projects"])
            (root / path).write_text(original)
            (tests / "NewTests.swift").write_text(
                "class NewTests: XCTestCase { func testNew() {} }"
            )
            data = ui.manifest(root)
            self.assertEqual(
                set(data["unclassified"]) - set(ui.manifest()["unclassified"]),
                {"NewTests"},
            )
            self.assertIn("KriaUITests/NewTests/testNew", data["groups"]["smoke"])
            self.assertEqual(ui.select_groups([path], root)[0], "smoke,projects")
            self.assertIn(
                "KriaUITests/NewTests/testNew",
                ui.expected_tests("smoke,projects", root),
            )

    def test_stale_entries_are_reported_with_the_fix_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "src/apps/ios/Tests/KriaUITests"
            shutil.copytree(ui.ROOT / "src/apps/ios/Tests/KriaUITests", tests)
            (tests / "ProjectsUITests.swift").unlink()
            problems = ui.check(root)
            self.assertTrue(any("ProjectsUITests" in problem for problem in problems))
            self.assertIn("make sync-test-registries", problems[-1])

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
        data = json.loads(ui.MANIFEST.read_text())
        data["overrides"]["SignInUITests/testSignInShowsEveryProviderAndLegalLink"] = (
            "nope"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(data))
            with patch.object(ui, "MANIFEST", path):
                self.assertEqual(ui.select_groups(["any"])[0], "full")
                with self.assertRaises(ValueError):
                    ui.expected_tests("smoke,creation")

    def test_an_in_place_rename_is_named_by_check_and_followed_by_sync(self):
        real = json.loads(ui.MANIFEST.read_text())
        smoke = real["smoke"][0]
        smoke_class, smoke_method = smoke.removeprefix("KriaUITests/").split("/")
        override = next(
            name for name in real["overrides"] if name.split("/")[0] != smoke_class
        )
        override_class, override_method = override.split("/")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "src/apps/ios/Tests/KriaUITests"
            shutil.copytree(ui.ROOT / "src/apps/ios/Tests/KriaUITests", tests)
            groups = root / "ui-test-groups.json"
            groups.write_text(json.dumps({**real, "sources": {}}))
            journeys = root / "journey-manifest.json"
            journeys.write_text(json.dumps({"xctests": [smoke]}))
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                f"-only-testing:{smoke} \\\n--xctest {smoke_class}/{smoke_method}\n"
            )

            def git(*args):
                subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True
                )

            def source(name):
                return next(
                    path
                    for path in tests.rglob("*.swift")
                    if re.search(rf"class {name}\b", path.read_text())
                )

            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            git("add", "-A")
            git("commit", "-qm", "base")
            for klass, old, new in (
                (smoke_class, smoke_method, "testRenamedSmokeSubject"),
                (override_class, override_method, "testRenamedOverrideSubject"),
            ):
                file = source(klass)
                file.write_text(
                    file.read_text().replace(f"func {old}(", f"func {new}(")
                )
            with patch.object(ui, "MANIFEST", groups):
                problems = ui.check(root)
                self.assertTrue(any(smoke_method in p for p in problems))
                self.assertTrue(any(override_method in p for p in problems))
                self.assertFalse(any("ci.yml" in p for p in problems))
                self.assertIn("make sync-test-registries", problems[-1])
                problems = ui.check(root, base="HEAD", journeys=journeys)
                self.assertTrue(
                    any(f"ci.yml still names '{smoke_class}/" in p for p in problems)
                )
                value, _ = ui.select_groups(
                    [source(smoke_class).relative_to(root).as_posix()], root
                )
                self.assertNotEqual(value, "full")
                self.assertNotIn(smoke, ui.expected_tests("smoke", root))
                changed = ui.sync(root, base="HEAD", journeys=journeys)
                self.assertTrue(any(line.startswith("smoke ") for line in changed))
                self.assertEqual(ui.check(root, base="HEAD", journeys=journeys), [])
                renamed = smoke.replace(smoke_method, "testRenamedSmokeSubject")
                self.assertIn(renamed, ui.expected_tests("smoke", root))
                self.assertIn(renamed, journeys.read_text())
                self.assertNotIn(smoke_method, workflow.read_text())
                self.assertIn("testRenamedSmokeSubject", workflow.read_text())
                overrides = json.loads(groups.read_text())["overrides"]
                self.assertIn(f"{override_class}/testRenamedOverrideSubject", overrides)
                self.assertNotIn(override, overrides)

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
        durations = ui.load_durations()
        self.assertTrue(all(float(value) > 0 for value in durations.values()))
        for count in range(1, 5):
            parts = [
                ui.shard_tests(inventory, f"{i}/{count}") for i in range(1, count + 1)
            ]
            with self.subTest(count=count):
                self.assertEqual(set().union(*parts), inventory)
                self.assertEqual(sum(len(part) for part in parts), len(inventory))
                loads = [
                    sum(ui.weights(part, durations).values())
                    + (420 if i == 0 and count > 1 else 0)
                    for i, part in enumerate(parts)
                ]
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
        # An empty part would verify vacuously, so it is an error instead.
        with self.assertRaises(ValueError):
            ui.shard_tests(set(sorted(inventory)[:2]), "3/3")

    def test_focused_selections_partition_exactly_like_the_full_suite(self):
        rename = "ProjectsUITests/testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry"
        for value in ("smoke", *ui.FOCUSED, f"smoke,editor,{rename}"):
            expected = ui.expected_tests(value)
            for count in range(1, ui.MAX_SHARDS + 1):
                parts = [
                    ui.shard_tests(expected, f"{i}/{count}")
                    for i in range(1, count + 1)
                ]
                with self.subTest(value=value, count=count):
                    self.assertEqual(set().union(*parts), expected)
                    self.assertEqual(sum(len(part) for part in parts), len(expected))

    def test_shard_count_splits_long_selections_only(self):
        # The recorded durations size today's selections: the smoke tripwire
        # and the short feature groups keep one leg, an editor PR splits, and
        # main's full suite uses every leg the workflow defines.
        self.assertEqual(ui.shard_count("smoke"), 1)
        self.assertEqual(ui.shard_count("smoke,projects"), 1)
        self.assertGreater(ui.shard_count("smoke,editor"), 1)
        self.assertEqual(ui.shard_count("full"), ui.MAX_SHARDS)
        # One leg per SHARD_TARGET_SECONDS of recorded time, clamped.
        smoke = sorted(ui.expected_tests("smoke"))
        target = ui.SHARD_TARGET_SECONDS
        for seconds, legs in (
            (target, 1),
            (target + 1, 2),
            (2 * target + 1, min(3, ui.MAX_SHARDS)),
            (100 * target, ui.MAX_SHARDS),
        ):
            durations = {test: seconds / len(smoke) for test in smoke}
            with self.subTest(seconds=seconds):
                self.assertEqual(ui.shard_count("smoke", durations=durations), legs)
        # Never more legs than tests: every leg must have a non-empty part.
        pair = set(smoke[:2])
        durations = {test: 100.0 * target for test in pair}
        with patch.object(ui, "expected_tests", return_value=pair):
            self.assertEqual(ui.shard_count("smoke", durations=durations), 2)
        with self.assertRaises(ValueError):
            ui.shard_count("none")

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
        # An unmapped test file selects the group owning its tests; a focused
        # edit still narrows to just the changed methods.
        visual = "src/apps/ios/Tests/KriaUITests/NativeCaptionVisualUITests.swift"
        caption = "NativeCaptionVisualUITests/testCaptionAndVisualPaperScreens"
        self.assertNotIn(visual, ui.manifest()["sources"])
        self.assertEqual(ui.select_groups([visual])[0], "smoke,editor")
        self.assertEqual(
            ui.select_groups([visual], focused={visual: {caption}})[0],
            f"smoke,{caption}",
        )
        for path, group in ui.manifest()["sources"].items():
            value = ui.select_groups([path], focused=focused)[0]
            self.assertEqual(value, ui.validate_groups(value))


class ShardOverheadTests(unittest.TestCase):
    def test_unit_overhead_reduces_first_shard_without_changing_coverage(self):
        tests = {f"test{i}" for i in range(24)}
        durations = dict.fromkeys(tests, 120)
        parts = [ui.shard_tests(tests, f"{i}/3", durations) for i in range(1, 4)]
        self.assertEqual(set.union(*parts), tests)
        self.assertEqual(sum(map(len, parts)), len(tests))
        loads = [
            len(part) * 120 + (420 if i == 0 else 0) for i, part in enumerate(parts)
        ]
        self.assertLessEqual(max(loads) - min(loads), 120)
        self.assertLess(len(parts[0]), len(parts[1]))
        self.assertEqual(ui.shard_tests(tests, "1/1", durations), tests)

    def test_shards_without_unit_work_balance_every_leg(self):
        # PR UI legs run no unit tests, so no leg reserves time for them.
        tests = {f"test{i}" for i in range(24)}
        durations = dict.fromkeys(tests, 120)
        parts = [
            ui.shard_tests(tests, f"{i}/3", durations, overhead=0) for i in range(1, 4)
        ]
        self.assertEqual(set.union(*parts), tests)
        self.assertEqual([len(part) for part in parts], [8, 8, 8])

    def test_pr_editor_selection_fits_its_legs(self):
        # Each PR UI leg targets PR_SHARD_TARGET_SECONDS of recorded time.
        tests = ui.expected_tests("smoke,editor")
        shards = ui.shard_count("smoke,editor", target=ui.PR_SHARD_TARGET_SECONDS)
        self.assertGreater(shards, 1)
        loads = [
            sum(ui.weights(ui.shard_tests(tests, f"{i}/{shards}", overhead=0)).values())
            for i in range(1, shards + 1)
        ]
        self.assertLessEqual(max(loads), ui.PR_SHARD_TARGET_SECONDS)


class ResultCoverageTests(unittest.TestCase):
    def test_unit_class_named_like_its_bundle_keeps_both_components(self):
        expected = {"KriaTests/KriaTests/testRefresh"}
        for identifier in (
            "KriaTests/testRefresh()",
            "KriaTests/KriaTests/testRefresh()",
        ):
            report = self.report(identifier=identifier)
            report["testNodes"][0].update(nodeType="Unit test bundle", name="KriaTests")
            self.assertEqual(ui.result_tests(report), {next(iter(expected)): "Passed"})
            self.assertIn("Verified 1", ui.verify_results(report, expected))
        for identifier, result in (
            ("Other/testRefresh()", "Passed"),
            ("Foreign/KriaTests/testRefresh()", "Passed"),
            ("KriaTests/testRefresh()", "Skipped"),
            ("KriaTests/testRefresh()", "Failed"),
        ):
            report = self.report(result=result, identifier=identifier)
            report["testNodes"][0].update(nodeType="Unit test bundle", name="KriaTests")
            with (
                self.subTest(identifier=identifier, result=result),
                self.assertRaises(ValueError),
            ):
                ui.verify_results(report, expected)

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


class OverrunRerunTests(unittest.TestCase):
    """Real xcresulttool output (fixtures/overrun-ui-results.json) from nightly
    43415393573: one test ran past its 3 minute allowance, which xcodebuild never
    retries, and verify.sh reruns it once (KRI-1379)."""

    OVERRAN = "KriaUITests/NativeEditorInspectorUITests/testSoundEffectsHomeLibraryEditTrimFlow"
    SIBLING = (
        "KriaUITests/NativeEditorInspectorUITests/"
        "testFourToolRailIsAPlainRowNotAScrollViewSoSwipesNeverStartOnIt"
    )
    MESSAGE = "Test exceeded execution time allowance of 3 minutes"

    def rerun(self, result="Passed", identifier=None, children=None):
        case = {
            "nodeType": "Test Case",
            "nodeIdentifier": (identifier or self.OVERRAN).removeprefix("KriaUITests/")
            + "()",
            "result": result,
        }
        if children:
            case["children"] = children
        return {
            "testNodes": [
                {
                    "nodeType": "UI test bundle",
                    "name": "KriaUITests",
                    "children": [case],
                }
            ]
        }

    def test_only_a_test_past_its_allowance_is_an_overrun(self):
        self.assertEqual(ui.overrun_tests(OVERRUN_FIXTURE), [self.OVERRAN])
        self.assertEqual(ui.overrun_tests(RETRIED_FIXTURE), [])

    def test_a_passing_rerun_completes_coverage_and_is_reported_flaky(self):
        expected = {self.OVERRAN, self.SIBLING}
        with self.assertRaises(ValueError):
            ui.verify_results(OVERRUN_FIXTURE, expected)
        self.assertIn(
            "Verified 2",
            ui.verify_results(OVERRUN_FIXTURE, expected, rerun=self.rerun()),
        )
        flaky = ui.flaky_tests(OVERRUN_FIXTURE, rerun=self.rerun())
        self.assertEqual(
            flaky,
            [
                {
                    "identifier": self.OVERRAN,
                    "attempts": 2,
                    "failure_messages": [self.MESSAGE],
                }
            ],
        )

    def test_a_rerun_cannot_excuse_anything_but_an_overrun(self):
        expected = {self.OVERRAN, self.SIBLING}
        overran_again = self.rerun(
            "Failed", children=[{"nodeType": "Failure Message", "name": self.MESSAGE}]
        )
        for rerun in (
            overran_again,
            self.rerun(identifier=self.SIBLING),
            {"testNodes": []},
        ):
            with self.subTest(rerun=rerun), self.assertRaises(ValueError):
                ui.verify_results(OVERRUN_FIXTURE, expected, rerun=rerun)
        self.assertEqual(ui.flaky_tests(OVERRUN_FIXTURE, rerun=overran_again), [])


if __name__ == "__main__":
    unittest.main()
