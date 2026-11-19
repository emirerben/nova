"""Exercise classification, real git diffs, and required-check failure behavior."""

import ast
import importlib.util
import json
import os
import re
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
journey_registry = ci.journey_registry
ALL = {"api", "ios", "ios_ui", "shared_motion"}


# Repo paths the iPhone tree names without reading them on the Mac leg. Every
# entry needs a reason; a path a test really reads belongs in
# select-tests.py IOS_NATIVE_INPUTS instead.
IOS_NAMED_NOT_READ = {
    "src/apps/api/": "AuthoredHandwritingLayoutTests joins tests/fixtures/ and "
    "assets/fonts/ onto this root; both are declared inputs",
    "src/apps/api/assets/": "an HTTP route fragment (plan-items/<id>/assets/...) "
    "that happens to resolve inside the API tree",
}
_SWIFT_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_PATH_LIKE = re.compile(r"(?:\.\./|\./|[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]*")


def repo_paths_named_by_ios(root):
    """Existing repo paths outside src/apps/ios that the iPhone tree names.

    A static scan: Swift string literals (comments dropped) and project/shell
    config lines, resolved against the repo root, the file's own directory and
    the roots tests join fixtures onto. It cannot see a path assembled at run
    time from pieces that are not path-like, which is why unknown paths still
    select a Mac and the rule was replayed against merged PRs before use.
    """
    ios = root / "src/apps/ios"
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "src/apps/ios"], cwd=root
    ).decode()
    named = {}
    for relative in filter(None, tracked.split("\0")):
        path = root / relative
        if path.suffix not in {".swift", ".yml", ".yaml", ".xcconfig", ".sh"}:
            continue
        # Generated API documents carry Python docstrings as prose, not reads.
        if not path.is_file() or "/Kria/Generated/" in f"/{relative}":
            continue
        text = path.read_text(errors="replace")
        if path.suffix == ".swift":
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            chunks = [
                literal
                for line in text.splitlines()
                if not line.lstrip().startswith("//")
                for literal in _SWIFT_LITERAL.findall(line)
            ]
        else:
            chunks = [line.split("#", 1)[0] for line in text.splitlines()]
        bases = (root, path.parent, ios, root / "src/apps/api", root / "src/apps/web")
        for chunk in chunks:
            for candidate in _PATH_LIKE.findall(chunk):
                for base in bases:
                    try:
                        resolved = (base / candidate).resolve()
                    except OSError:
                        continue
                    if resolved == root or root not in resolved.parents:
                        continue
                    # "originals/.." climbs back to where it started: a
                    # traversal string under test, not a repo path.
                    if resolved == base or resolved in base.parents:
                        continue
                    if not resolved.exists():
                        continue
                    found = resolved.relative_to(root).as_posix()
                    if resolved.is_dir():
                        found += "/"
                    # No early exit: a literal that exists under two roots
                    # (tests/fixtures/ and the API's) is credited to both.
                    if not found.startswith(("src/apps/ios/", ".git/")):
                        named.setdefault(found, relative)
    return named


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
        self.assertIn("journey_registry.py step kri-215-learning-loop", workflow)
        files = journey_registry.step_files("kri-215-learning-loop")
        self.assertTrue(files)
        for file in files:
            self.assertTrue(
                (journey_registry.ROOT / journey_registry.API / file).is_file(), file
            )

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
            "src/apps/ios/project.yml": {"api", "ios", "ios_ui"},
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
            self.assertEqual(ci.affected(path), {"api", "ios"})
        for path in (
            "src/apps/ios/Kria/Features/NativeEditorView.swift",
            "src/apps/ios/Tests/KriaUITests/KriaUITests.swift",
            "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Composition.swift",
            "src/apps/ios/Kria/Core/NativeEditorDocument.swift",
        ):
            self.assertEqual(ci.affected(path), {"api", "ios", "ios_ui"})
        for path in (
            "scripts/ios/cache-inputs.py",
            "src/apps/web/public/fonts/Inter-Regular.ttf",
            "src/apps/web/public/plan/type-posters/montage.mp4",
        ):
            self.assertEqual(ci.affected(path), {"ios", "ios_ui"})
        self.assertEqual(
            ci.affected("scripts/ios/kri-524-creation-e2e.py"),
            {"api", "ios", "ios_ui"},
        )

    @staticmethod
    def _ios_paths_built_with_path_joins(source):
        """Repo paths an API test builds as `base / "apps" / "ios" / ...`."""
        found = set()
        tree = ast.parse(source.read_text())
        inner = {
            id(node.left)
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            if id(node) in inner:
                continue  # only the outermost join of a chain
            parts, cursor = [], node
            while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
                if isinstance(cursor.right, ast.Constant) and isinstance(
                    cursor.right.value, str
                ):
                    parts.insert(0, cursor.right.value)
                cursor = cursor.left
            segments = [seg for part in parts for seg in part.split("/") if seg]
            if "ios" not in segments:
                continue
            at = segments.index("ios")
            root = "scripts" if at and segments[at - 1] == "scripts" else "src/apps"
            rest = segments[at + 1 :] or ["probe"]
            found.add("/".join([root, "ios", *rest]))
        return found

    def test_every_ios_path_an_api_test_reads_selects_the_api_suite(self):
        tests = SCRIPT.parents[2] / "src/apps/api/tests"
        read, joined = set(), set()
        for source in tests.rglob("*.py"):
            read.update(re.findall(r"src/apps/ios/[\w./-]+\.\w+", source.read_text()))
            joined.update(self._ios_paths_built_with_path_joins(source))
        self.assertIn("src/apps/ios/Kria/Core/SlidePost.swift", read)
        # Path joins the literal regex cannot see (KRI-1397).
        self.assertIn("src/apps/ios/Kria/Features", joined)
        self.assertIn("src/apps/ios/Tests/Fixtures/editor-commit-picker-contract.json", joined)
        self.assertIn("scripts/ios/kri-524-creation-e2e.py", joined)
        for path in sorted(read | joined):
            with self.subTest(path=path):
                self.assertIn("api", ci.affected(path))

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

    def test_only_declared_inputs_policy_and_unknown_paths_need_a_mac(self):
        for path in (
            "src/apps/ios/Kria/App.swift",
            "src/apps/ios/Kria/Generated/KriaContracts.swift",
            "scripts/ios/verify.sh",
            ".github/workflows/ios.yml",
            "src/apps/api/assets/fonts/Inter-Bold.ttf",
            "src/apps/api/tests/fixtures/split_full_screen.json",
            "tests/fixtures/drawing-parity/v1.json",
            "brand/social/dist/kria-mark.png",
            "src/packages/motion-runtime/creator-blocks.catalog.json",
            "src/apps/web/public/fonts/Inter-Bold.ttf",
            # CI policy and anything unclassified fail closed.
            ".github/workflows/ci.yml",
            "scripts/ci/select-tests.py",
            "scripts/ci/journey-manifest.json",
            "brand-new/tool.py",
        ):
            self.assertTrue(ci.native_input(path), path)
        for path in (
            "src/apps/api/app/kria/policy.py",
            "src/apps/api/app/config.py",
            "src/apps/api/app/routes/kria_runtime.py",
            "src/apps/api/app/pipeline/single_pass.py",
            "src/apps/api/tests/kria/test_policy.py",
            # Swift opens files directly in tests/fixtures/, never its folders.
            "src/apps/api/tests/fixtures/prompt_coverage/creation_composition.json",
            "src/apps/api/assets/models/face.tflite",
            "src/apps/api/prompts/main_creator.txt",
            "src/apps/web/src/app/plan/page.tsx",
            "docs/runbooks/change-based-ci.md",
            # API tests read it; nothing on the iPhone side does.
            ".env.example",
        ):
            self.assertFalse(ci.native_input(path), path)

    def test_api_files_swift_tests_open_also_run_the_iphone_unit_suite(self):
        """A parity fixture or font is read by both sides; changing it tests both."""
        for path in (
            "src/apps/api/assets/fonts/Inter-Bold.ttf",
            "src/apps/api/assets/fonts/handwriting-strokes.json",
            "src/apps/api/tests/fixtures/split_full_screen.json",
            "src/apps/api/tests/fixtures/phone_dissolve_timing.json",
        ):
            self.assertEqual(ci.affected(path), {"api", "ios"}, path)
        for path in (
            "src/apps/api/tests/fixtures/prompt_coverage/creation_composition.json",
            "src/apps/api/assets/models/face.tflite",
        ):
            self.assertEqual(ci.affected(path), {"api"}, path)

    def test_native_inputs_cover_everything_the_xcode_cache_hashes(self):
        source = (SCRIPT.parents[1] / "ios/cache-inputs.py").read_text()
        roots = ast.literal_eval(
            re.search(r"^INPUT_ROOTS = (\(.*?^\))", source, re.S | re.M).group(1)
        )
        self.assertTrue(roots)
        for root in roots:
            self.assertTrue(ci.native_input(root + "/file"), root)
            self.assertTrue((root + "/").startswith(ci.IOS_NATIVE_INPUTS), root)

    def test_every_repo_path_the_iphone_tree_names_is_a_declared_native_input(self):
        named = repo_paths_named_by_ios(SCRIPT.parents[2])
        # The scan must be finding real references, not passing on an empty set.
        self.assertIn("src/apps/api/assets/fonts/Inter-Bold.ttf", named)
        undeclared = {
            path: source
            for path, source in named.items()
            if not ci.declared_native_input(path)
            and path not in IOS_NAMED_NOT_READ
        }
        self.assertEqual(
            undeclared,
            {},
            "The iPhone tree names repo paths the Mac selector does not know. Add "
            "each to IOS_NATIVE_INPUTS in select-tests.py if a build or test reads "
            "it, or to IOS_NAMED_NOT_READ here with the reason it is not read.",
        )
        for path in IOS_NAMED_NOT_READ:
            self.assertIn(path, named, f"stale IOS_NAMED_NOT_READ entry: {path}")

    def test_non_pr_always_full(self):
        for event in ("push", "workflow_dispatch", None, "merge_group"):
            self.assertEqual(ci.selection(event, "", "")[0], ALL)

    def test_diff_error_falls_back_to_full(self):
        with patch.object(
            ci, "git", side_effect=subprocess.CalledProcessError(1, "git")
        ):
            self.assertEqual(ci.selection("pull_request", "bad", "bad")[0], ALL)


class JourneyApiTestsTests(unittest.TestCase):
    def test_every_journey_api_test_also_runs_in_the_api_shards(self):
        """Narrowing which journeys run (KRI-1541) may only drop a test's second
        execution. Each journey API test file must also be in the API shard set,
        which every PR that selects `api` runs in full."""
        spec = importlib.util.spec_from_file_location(
            "api_tests_for_journeys", SCRIPT.with_name("api-tests.py")
        )
        api_tests = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api_tests)
        api_root = SCRIPT.parents[2] / "src/apps/api"
        sharded = {str(path) for path in api_tests.test_files(api_root)}
        named = {
            test.split("::", 1)[0]
            for journey in ci.JOURNEYS
            for test in (journey.get("api_tests") or "").split()
        }
        self.assertTrue(named, "expected journeys to name API tests")

        def in_shards(entry):
            if entry in sharded:
                return True
            # A journey may name a directory; pytest then collects its files.
            directory = api_root / entry
            collected = [
                str(path.relative_to(api_root))
                for path in directory.rglob("*.py")
                if path.name.startswith("test_") or path.name.endswith("_test.py")
            ]
            return bool(collected) and set(collected) <= sharded

        self.assertEqual(sorted(entry for entry in named if not in_shards(entry)), [])


class ManifestRowTests(unittest.TestCase):
    """A manifest edit selects the rows it changed, or everything (KRI-1541).

    The manifest is CI policy: every row whose definition changed must still
    execute, and any edit that is not a plain row change keeps the old
    behaviour of selecting every suite and every journey.
    """

    BASE = {
        "schema_version": 1,
        "journeys": [
            {"id": "linux-a", "paths": ["src/apps/api/app/a.py"], "needs_native": False},
            {"id": "linux-b", "paths": ["src/apps/api/app/b.py"], "needs_native": False},
            {"id": "native-c", "paths": ["src/apps/ios/Kria/C.swift"], "needs_native": True},
            {"id": "named-d", "paths": ["src/apps/api/app/d.py"], "xctests": ["KriaTests/D/testD"]},
        ],
    }

    def edited(self, change):
        after = json.loads(json.dumps(self.BASE))
        change(after)
        return ci.manifest_row_changes(self.BASE, after)

    def test_a_changed_or_added_linux_row_selects_only_itself(self):
        self.assertEqual(
            self.edited(lambda m: m["journeys"][0]["paths"].append("src/apps/api/x.py")),
            (["linux-a"], False),
        )
        self.assertEqual(
            self.edited(
                lambda m: m["journeys"].append({"id": "linux-new", "paths": ["x/"]})
            ),
            (["linux-new"], False),
        )

    def test_a_native_row_on_either_side_needs_the_mac(self):
        self.assertEqual(
            self.edited(lambda m: m["journeys"][2]["paths"].append("src/apps/ios/Z.swift")),
            (["native-c"], True),
        )
        self.assertEqual(
            self.edited(lambda m: m["journeys"][3]["xctests"].append("KriaTests/D/testE")),
            (["named-d"], True),
        )
        # Native before, Linux after: the row lost its iPhone proof in this PR.
        self.assertEqual(
            self.edited(lambda m: m["journeys"][2].update(needs_native=False)),
            (["native-c"], True),
        )
        self.assertEqual(
            self.edited(lambda m: m["journeys"][0].update(needs_native=True)),
            (["linux-a"], True),
        )

    def test_reordering_or_reformatting_changes_no_row(self):
        self.assertEqual(self.edited(lambda m: m["journeys"].reverse()), ([], False))
        self.assertEqual(self.edited(lambda m: None), ([], False))

    def test_anything_that_is_not_a_plain_row_change_selects_everything(self):
        for change in (
            lambda m: m["journeys"].pop(0),  # a removed journey deletes coverage
            lambda m: m.update(schema_version=2),
            lambda m: m.update(extra={"x": 1}),
            lambda m: m["journeys"].append(dict(m["journeys"][0])),  # duplicate id
            lambda m: m["journeys"].append({"paths": ["no-id/"]}),
            lambda m: m.update(journeys={"not": "a list"}),
            lambda m: m.pop("journeys"),
        ):
            self.assertIsNone(self.edited(change))
        self.assertIsNone(ci.manifest_row_changes([], self.BASE))
        self.assertIsNone(ci.manifest_row_changes(self.BASE, None))


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
        # A PR's short selections stay on the unit leg's Mac. PRs run `full` as
        # the smoke tripwire, so it never splits either.
        self.assertEqual(ci.ui_shards("pull_request", "full"), 1)
        self.assertEqual(ci.ui_shards("pull_request", "none"), 1)
        self.assertEqual(ci.ui_shards("pull_request", "smoke"), 1)
        self.assertEqual(ci.ui_shards("pull_request", "smoke,projects"), 1)
        # An editor PR's group ran 89 min serially (#2225): it splits across
        # Macs, sized by the PR leg target and capped like nightly.
        editor = ci.ui_shards("pull_request", "smoke,editor")
        self.assertGreater(editor, 1)
        self.assertLessEqual(editor, ci.ui_tests.PR_MAX_SHARDS)
        self.assertEqual(
            editor,
            ci.ui_tests.shard_count(
                "smoke,editor",
                target=ci.ui_tests.PR_SHARD_TARGET_SECONDS,
                max_shards=ci.ui_tests.PR_MAX_SHARDS,
            ),
        )
        # The creation group records ~158 min: uncapped it would take five legs
        # (six Macs with the unit leg) and queue behind the account's five
        # runners. PRs stay at three; nightly/manual may use more.
        self.assertEqual(ci.ui_tests.PR_MAX_SHARDS, 3)
        self.assertEqual(ci.ui_shards("pull_request", "smoke,creation"), 3)
        self.assertEqual(
            ci.native_legs(
                [], "pull_request", ci.ui_shards("pull_request", "smoke,creation")
            ),
            ["unit", "ui-1", "ui-2", "ui-3"],
        )
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


class AutomaticUpdateMappingTests(unittest.TestCase):
    def test_actual_117_path_ci_diff_selects_real_native_recovery_incidents(self):
        fixture = json.loads(
            (
                SCRIPT.parent / "fixtures/automatic-result-convergence-diff.json"
            ).read_text()
        )
        self.assertEqual(len(fixture["paths"]), 117)
        self.assertEqual(
            ci.unmapped_journey_paths(
                fixture["base"], fixture["head"], paths=fixture["paths"]
            ),
            [],
        )
        expected = {
            "saved-song-render-recovery",
            "slide-post-generation-receipt-recovery",
        }
        with patch.object(ci, "journey_diff_paths", return_value=fixture["paths"]):
            selected, coverage, ids = ci.journey_details(
                "pull_request", fixture["base"], fixture["head"]
            )
        self.assertTrue(selected)
        self.assertEqual(coverage, "covered")
        self.assertTrue(expected <= set(ids.split(",")))
        self.assertTrue(ci.journey_native_for_ids(sorted(expected)))
        for path, incident in zip(fixture["original_unmapped"], sorted(expected)):
            self.assertEqual(ci.journey_ids_for_path(path), {incident})

    def test_recovery_incidents_require_named_api_and_native_execution(self):
        rows = {row["id"]: row for row in ci.JOURNEYS}
        workflow = (SCRIPT.parents[2] / ".github/workflows/ci.yml").read_text()
        start = workflow.index(
            "      - name: Verify original generation receipts and saved-song recovery"
        )
        step = workflow[start : workflow.index("      - name:", start + 15)]
        for key in (
            "saved-song-render-recovery",
            "slide-post-generation-receipt-recovery",
        ):
            row = rows[key]
            self.assertIs(row["needs_native"], True)
            self.assertTrue(row["xctests"])
            self.assertIn(key, step)
            for node in row["api_tests"].split():
                self.assertIn("::test_", node)
                file, name = node.split("::")
                self.assertIn(
                    "def " + name + "(",
                    (SCRIPT.parents[2] / "src/apps/api" / file).read_text(),
                )
        self.assertIn("python3 -m pytest $tests", step)
        self.assertIn("--junitxml=", step)
        self.assertIn("journey-gate.py api-cases", step)

    def test_missing_recovery_mapping_still_fails_closed(self):
        fixture = json.loads(
            (
                SCRIPT.parent / "fixtures/automatic-result-convergence-diff.json"
            ).read_text()
        )
        incidents = (
            "saved-song-render-recovery",
            "slide-post-generation-receipt-recovery",
        )
        for removed in ({incidents[0]}, {incidents[1]}, set(incidents)):
            original = [row for row in ci.JOURNEYS if row["id"] not in removed]
            expected = [
                path
                for path, key in zip(fixture["original_unmapped"], incidents)
                if key in removed
            ]
            with (
                self.subTest(removed=removed),
                patch.object(ci, "JOURNEYS", original),
                patch.object(ci, "journey_diff_paths", return_value=fixture["paths"]),
            ):
                self.assertEqual(
                    ci.journey_details(
                        "pull_request", fixture["base"], fixture["head"]
                    ),
                    (True, "gap", ""),
                )
                self.assertEqual(
                    ci.unmapped_journey_paths(
                        fixture["base"], fixture["head"], paths=fixture["paths"]
                    ),
                    expected,
                )


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
        self.assertEqual(suites, {"api", "ios"})
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
                    "kri-492-length-ownership,kri-524-creation,kri-577-reliability-gates,kri-1456-composer-question-reaches-the-creator,kri-1124-withdrawn-labels,kri-760-whole-line-ask,kri-648-phone-text,kri-29ekim-post-approval-text-sequence",
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
                (True, "covered", "kri-726-background-recovery,native-activity-inbox"),
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
            (
                "src/apps/api/app/services/native_activity.py",
                (True, "covered", "native-activity-inbox"),
            ),
            (
                "src/apps/ios/Kria/Core/NativeActivity.swift",
                (
                    True,
                    "covered",
                    "native-activity-inbox,native-activity-account-boundary,native-notification-runtime",
                ),
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
                "kri-524-creation,kri-557-thinking,kri-648-phone-text,kri-551-consented-retakes,rendered-editor-original-clock,native-activity-account-boundary,native-notification-runtime,saved-song-render-recovery",
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

    def run_selector(self, head, **env):
        output, summary = Path("output"), Path("summary")
        for stale in (output, summary):
            stale.unlink(missing_ok=True)
        subprocess.run(
            [sys.executable, str(SCRIPT.resolve())],
            check=True,
            env={
                **{k: v for k, v in os.environ.items() if k != "CI_MAC_SELECTION"},
                "CI_EVENT": "pull_request",
                "CI_BASE": self.base,
                "CI_HEAD": head,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                **env,
            },
            capture_output=True,
        )
        return output.read_text().splitlines(), summary.read_text()

    def test_an_api_contract_change_keeps_linux_contracts_and_starts_no_mac(self):
        self.write("src/apps/api/app/config.py", "FLAG = True")
        lines, summary = self.run_selector(self.commit())
        self.assertIn("ios=true", lines)  # ios-contracts still runs on Linux
        self.assertIn("ios_native=false", lines)
        self.assertIn("native_validation=false", lines)
        self.assertIn("iPhone inputs: unchanged (no Mac", summary)

    def test_required_gates_accept_the_skipped_mac_and_reject_a_lost_one(self):
        """The selector's own outputs, taken through each PR gate."""
        self.write("src/apps/api/app/config.py", "FLAG = True")
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual(
            (outputs["ios"], outputs["ios_native"], outputs["native_validation"]),
            ("true", "false", "false"),
        )
        changes = {"result": "success", "outputs": outputs}
        # Linux journey jobs run exactly as the selection says; only the Mac
        # is skipped.
        linux = "success" if outputs["journey"] == "true" else "skipped"
        library = (
            "success"
            if "kri-384-library-ingest" in outputs["journey_ids"].split(",")
            else "skipped"
        )
        journey = {
            "changes": changes,
            "journey-mapping": {"result": linux},
            "journey-offline": {"result": linux},
            "journey-library": {"result": library},
            "journey-native": {"result": "skipped"},
        }
        ci.gate_journey(journey, "pull_request")
        ios = {
            "changes": changes,
            "journey": {"result": "success"},
            "ios-contracts": {"result": "success"},
        }
        ci.gate(ios, "ios", "journey")
        # The Linux contract job is what still proves this PR: it may not skip.
        with self.assertRaises(ValueError):
            ci.gate({**ios, "ios-contracts": {"result": "skipped"}}, "ios", "journey")
        # A Mac that ran anyway, or a selector claiming unchanged inputs after
        # the fact, is an inconsistent run, never a pass.
        with self.assertRaises(ValueError):
            ci.gate_journey(
                {**journey, "journey-native": {"result": "success"}}, "pull_request"
            )
        lost = {**changes, "outputs": {**outputs, "ios_native": "true"}}
        with self.assertRaises(ValueError):
            ci.gate_journey({**journey, "changes": lost}, "pull_request")

    def test_the_rollback_variable_restores_a_mac_for_every_selected_pr(self):
        self.write("src/apps/api/app/config.py", "FLAG = True")
        lines, summary = self.run_selector(self.commit(), CI_MAC_SELECTION="full")
        self.assertIn("ios=true", lines)
        self.assertIn("ios_native=true", lines)
        self.assertIn("native_validation=true", lines)
        self.assertIn("CI_MAC_SELECTION=full", summary)

    def test_an_iphone_input_change_still_starts_a_mac(self):
        self.write("src/apps/api/tests/fixtures/split_full_screen.json", "{}")
        lines, _ = self.run_selector(self.commit())
        self.assertIn("ios=true", lines)
        self.assertIn("ios_native=true", lines)
        self.assertIn("native_validation=true", lines)

    def with_real_manifest(self, edit=None):
        """Commit the repo's own manifest as the base, then one edit of it."""
        manifest = json.loads((SCRIPT.with_name("journey-manifest.json")).read_text())
        path = "scripts/ci/journey-manifest.json"
        self.write(path, json.dumps(manifest, indent=2))
        self.base = self.commit()
        if edit:
            edit(manifest)
            self.write(path, json.dumps(manifest, indent=2))
        return manifest

    def row(self, manifest, journey_id):
        return next(r for r in manifest["journeys"] if r["id"] == journey_id)

    def test_a_linux_journey_row_edit_runs_that_journey_the_api_suite_and_no_mac(self):
        self.with_real_manifest(
            lambda m: self.row(m, "kri-577-reliability-gates")["paths"].append(
                "src/apps/api/app/services/brand_new.py"
            )
        )
        lines, summary = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual(outputs["journey_ids"], "kri-577-reliability-gates")
        self.assertEqual(outputs["journey_coverage"], "covered")
        self.assertEqual(
            {suite: outputs[suite] for suite in ALL},
            {"api": "true", "ios": "false", "ios_ui": "false", "shared_motion": "false"},
        )
        self.assertEqual(outputs["ios_native"], "false")
        self.assertEqual(outputs["native_validation"], "false")
        self.assertIn("iPhone inputs: unchanged (no Mac", summary)

    def test_a_native_journey_row_edit_keeps_its_mac_and_its_unit_tests(self):
        self.with_real_manifest(
            lambda m: self.row(m, "kri-524-creation")["paths"].append(
                "src/apps/ios/Kria/BrandNew.swift"
            )
        )
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual(outputs["journey_ids"], "kri-524-creation")
        self.assertEqual(outputs["ios"], "true")
        self.assertEqual(outputs["ios_native"], "true")
        self.assertEqual(outputs["journey_needs_native"], "true")
        self.assertEqual(outputs["native_validation"], "true")

    def test_removing_a_journey_row_still_selects_everything(self):
        self.with_real_manifest(lambda m: m["journeys"].pop())
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual({outputs[suite] for suite in ALL}, {"true"})
        self.assertEqual(outputs["ios_native"], "true")
        self.assertEqual(
            outputs["journey_ids"].split(","), [j["id"] for j in ci.JOURNEYS]
        )

    def test_an_unreadable_manifest_still_selects_everything(self):
        self.with_real_manifest()
        self.write("scripts/ci/journey-manifest.json", "{ not json")
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual({outputs[suite] for suite in ALL}, {"true"})
        self.assertEqual(outputs["native_validation"], "true")

    def test_a_manifest_reformat_selects_the_api_suite_and_no_journey(self):
        manifest = self.with_real_manifest()
        self.write("scripts/ci/journey-manifest.json", json.dumps(manifest))
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual(outputs["api"], "true")
        self.assertEqual(outputs["ios"], "false")
        self.assertEqual(outputs["journey"], "false")
        self.assertEqual(outputs["journey_coverage"], "not_applicable")

    def test_a_row_edit_next_to_other_changes_adds_to_their_selection(self):
        self.with_real_manifest(
            lambda m: self.row(m, "kri-577-reliability-gates")["paths"].append(
                "src/apps/api/app/services/brand_new.py"
            )
        )
        self.write("src/apps/ios/Kria/App.swift", "// changed")
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual(outputs["ios"], "true")
        self.assertEqual(outputs["ios_native"], "true")
        self.assertIn("kri-577-reliability-gates", outputs["journey_ids"].split(","))

    def test_the_rollback_variable_also_undoes_manifest_and_env_example_narrowing(self):
        """CI_MAC_SELECTION=full must restore everything KRI-1534 narrowed on PRs."""
        self.with_real_manifest(
            lambda m: self.row(m, "kri-577-reliability-gates")["paths"].append(
                "src/apps/api/app/services/brand_new.py"
            )
        )
        lines, _ = self.run_selector(self.commit(), CI_MAC_SELECTION="full")
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual({outputs[suite] for suite in ALL}, {"true"})
        self.assertEqual(outputs["native_validation"], "true")
        self.assertEqual(
            outputs["journey_ids"].split(","), [j["id"] for j in ci.JOURNEYS]
        )

        self.write(".env.example", "NEW_FLAG=false")
        self.base = self.git("rev-parse", "HEAD")
        lines, _ = self.run_selector(self.commit(), CI_MAC_SELECTION="full")
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual({outputs[suite] for suite in ALL}, {"true"})
        self.assertEqual(outputs["native_validation"], "true")

    def test_env_example_selects_the_api_suite_that_reads_it(self):
        self.write(".env.example", "NEW_FLAG=false")
        lines, _ = self.run_selector(self.commit())
        outputs = dict(line.split("=", 1) for line in lines)
        self.assertEqual(
            {suite: outputs[suite] for suite in ALL},
            {"api": "true", "ios": "false", "ios_ui": "false", "shared_motion": "false"},
        )
        self.assertEqual(outputs["native_validation"], "false")

    def test_native_inputs_fail_closed_without_a_readable_pr_diff(self):
        self.assertTrue(ci.native_inputs_changed("push", "", ""))
        self.assertTrue(ci.native_inputs_changed("schedule", "", ""))
        self.assertTrue(ci.native_inputs_changed("pull_request", "missing", "missing"))
        head = self.git("rev-parse", "HEAD")
        self.assertTrue(ci.native_inputs_changed("pull_request", head, head))

    def test_native_inputs_ignore_release_metadata_and_see_both_rename_sides(self):
        self.write("package.json", json.dumps({"version": "2", "scripts": {"test": "test"}}))
        self.write("src/apps/api/app/pipeline/x.py", "x = 1")
        head = self.commit()
        self.assertFalse(ci.native_inputs_changed("pull_request", self.base, head))
        Path("src/apps/ios/Kria").mkdir(parents=True)
        self.git("mv", "src/apps/api/app/pipeline/x.py", "src/apps/ios/Kria/x.py")
        moved = self.commit()
        self.assertTrue(ci.native_inputs_changed("pull_request", head, moved))

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
            "api=true\nios=true\nios_ui=true\nshared_motion=false\nnative_validation=true\nios_ui_groups=full\n"
            "ios_ui_shards=1\njourney=false\njourney_coverage=not_applicable\n"
            'journey_ids=\njourney_needs_native=false\nnative_legs=["unit"]\n'
            "journey_mapped=true\nios_render_evidence=false\nios_native=true\n",
        )
        self.assertIn("iPhone inputs: changed", summary.read_text())
        self.assertIn("ios: run", summary.read_text())
        self.assertIn("Export/OCR unit proofs: skipped", summary.read_text())
        self.assertIn("iOS UI groups: full (1 native leg)", summary.read_text())


if __name__ == "__main__":
    unittest.main()
