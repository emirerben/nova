import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("journey_registry.py")
spec = importlib.util.spec_from_file_location("journey_registry", SCRIPT)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)

MARKED = 'import pytest\n\npytestmark = pytest.mark.journey("{id}"{step})\n\n\ndef test_it():\n    pass\n'
WORKFLOW = '      - run: |\n          tests="$(python3 scripts/ci/journey_registry.py step {step})"\n'


class Tree:
    """A throwaway repo shaped like the real one, committed so git can see renames."""

    def __init__(self, directory: str):
        self.root = Path(directory)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write(".github/workflows/ci.yml", WORKFLOW.format(step="alpha"))
        self.write(
            f"{registry.API}/tests/kria/test_alpha.py",
            MARKED.format(id="alpha", step=""),
        )
        self.write(
            f"{registry.API}/tests/kria/test_listed.py", "def test_it():\n    pass\n"
        )
        self.write(f"{registry.API}/app/alpha.py", "")
        self.manifest = self.root / "scripts/ci/journey-manifest.json"
        self.write(
            "scripts/ci/journey-manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "journeys": [
                        {
                            "id": "alpha",
                            "needs_native": False,
                            "api_tests": "tests/kria/test_listed.py",
                            "paths": [f"{registry.API}/app/alpha.py"],
                        }
                    ],
                }
            ),
        )
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout

    def write(self, relative: str, text: str):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def check(self):
        return registry.check(self.root, self.manifest)


class JourneyRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tree = Tree(self.directory.name)

    def test_checked_in_registries_have_no_drift(self):
        problems = registry.check()
        self.assertFalse(problems, "\n".join(problems))

    def test_a_new_marked_test_is_run_with_no_registry_edit(self):
        self.tree.write(
            f"{registry.API}/tests/kria/test_brand_new.py",
            MARKED.format(id="alpha", step=""),
        )
        self.assertEqual(
            registry.step_files("alpha", self.tree.root),
            ["tests/kria/test_alpha.py", "tests/kria/test_brand_new.py"],
        )
        self.assertEqual(self.tree.check(), [])

    def test_renaming_a_marked_test_keeps_it_in_its_step(self):
        self.tree.git(
            "mv",
            f"{registry.API}/tests/kria/test_alpha.py",
            f"{registry.API}/tests/kria/test_alpha_renamed.py",
        )
        self.assertEqual(
            registry.step_files("alpha", self.tree.root),
            ["tests/kria/test_alpha_renamed.py"],
        )
        self.assertEqual(self.tree.check(), [])

    def test_renaming_a_hand_listed_test_fails_check_then_sync_repairs_it(self):
        self.tree.git(
            "mv",
            f"{registry.API}/tests/kria/test_listed.py",
            f"{registry.API}/tests/kria/test_listed_renamed.py",
        )
        problems = self.tree.check()
        self.assertTrue(any("test_listed.py" in problem for problem in problems))
        self.assertIn("make sync-test-registries", problems[-1])
        changed = registry.sync(self.tree.root, self.tree.manifest, base="HEAD")
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.tree.check(), [])
        self.assertIn(
            "tests/kria/test_listed_renamed.py", self.tree.manifest.read_text()
        )

    def test_renaming_a_source_path_is_followed_into_paths(self):
        self.tree.git(
            "mv", f"{registry.API}/app/alpha.py", f"{registry.API}/app/alpha_renamed.py"
        )
        self.assertTrue(self.tree.check())
        registry.sync(self.tree.root, self.tree.manifest, base="HEAD")
        self.assertEqual(self.tree.check(), [])

    def test_a_marked_step_that_ci_never_runs_is_reported(self):
        self.tree.write(".github/workflows/ci.yml", "jobs: {}\n")
        problems = self.tree.check()
        self.assertTrue(any("never runs it" in problem for problem in problems))

    def test_a_workflow_step_with_no_declaring_test_is_reported(self):
        self.tree.write(".github/workflows/ci.yml", WORKFLOW.format(step="ghost"))
        problems = self.tree.check()
        self.assertTrue(any("'ghost'" in problem for problem in problems))
        with self.assertRaises(ValueError):
            registry.step_files("ghost", self.tree.root)

    def test_hand_listed_test_paths_in_the_workflow_are_reported(self):
        workflow = (
            WORKFLOW.format(step="alpha")
            + "          python3 -m pytest tests/kria/test_alpha.py\n"
        )
        self.tree.write(".github/workflows/ci.yml", workflow)
        self.assertTrue(any("by hand" in problem for problem in self.tree.check()))
        for path in ("tests/test_alpha.py", "src/apps/api/tests/test_alpha.py"):
            with self.subTest(path=path):
                self.tree.write(
                    ".github/workflows/ci.yml",
                    WORKFLOW.format(step="alpha")
                    + f"          python3 -m pytest {path}\n",
                )
                self.assertTrue(
                    any("by hand" in problem for problem in self.tree.check())
                )

    def test_unknown_journey_and_unreadable_markers_fail_closed(self):
        self.tree.write(
            f"{registry.API}/tests/kria/test_typo.py",
            MARKED.format(id="alhpa", step=""),
        )
        self.assertTrue(
            any("unknown journey" in problem for problem in self.tree.check())
        )
        self.tree.write(
            f"{registry.API}/tests/kria/test_typo.py",
            'import pytest\n\nNAME = "alpha"\npytestmark = pytest.mark.journey(NAME)\n',
        )
        problems = self.tree.check()
        self.assertTrue(any("string literals" in problem for problem in problems))

    def test_manifest_node_ids_must_name_real_tests(self):
        rows = json.loads(self.tree.manifest.read_text())
        rows["journeys"][0]["api_tests"] = "tests/kria/test_listed.py::test_missing"
        self.tree.manifest.write_text(json.dumps(rows))
        self.assertTrue(any("no such test" in problem for problem in self.tree.check()))

    def test_every_valid_spelling_of_a_marker_is_read_or_refused(self):
        spellings = {
            "annotated": 'import pytest\n\npytestmark: list = [pytest.mark.journey("alpha")]\n',
            "bare_mark": 'from pytest import mark\n\npytestmark = mark.journey("alpha")\n',
            "aliased_mark": 'from pytest import mark as m\n\npytestmark = m.journey("alpha")\n',
            "aliased_module": 'import pytest as pt\n\npytestmark = pt.mark.journey("alpha")\n',
            "combined": 'import pytest\n\npytestmark = [pytest.mark.slow, pytest.mark.journey("alpha")]\n',
        }
        for name, text in spellings.items():
            with self.subTest(name=name):
                self.tree.write(
                    f"{registry.API}/tests/kria/test_{name}.py",
                    text + "\n\ndef test_it():\n    pass\n",
                )
                self.assertIn(
                    f"tests/kria/test_{name}.py",
                    registry.step_files("alpha", self.tree.root),
                )
        refused = {
            "decorator": 'import pytest\n\n@pytest.mark.journey("alpha")\ndef test_it():\n    pass\n',
            "class_attribute": 'import pytest\n\nclass TestIt:\n    pytestmark = pytest.mark.journey("alpha")\n',
            "bare": "import pytest\n\npytestmark = pytest.mark.journey\n",
        }
        for name, text in refused.items():
            with self.subTest(name=name):
                path = f"{registry.API}/tests/kria/test_refused_{name}.py"
                self.tree.write(path, text)
                self.assertTrue(
                    any("module-level" in problem for problem in self.tree.check())
                )
                (self.tree.root / path).unlink()

    def test_a_marker_on_a_named_node_journey_is_refused(self):
        rows = json.loads(self.tree.manifest.read_text())
        rows["journeys"][0]["api_tests"] = "tests/kria/test_listed.py::test_it"
        self.tree.manifest.write_text(json.dumps(rows))
        problems = self.tree.check()
        self.assertTrue(any("api-cases" in problem for problem in problems), problems)

    def test_other_hand_lists_fail_check_on_a_rename_and_sync_follows_it(self):
        self.tree.write("Makefile", "t:\n\tpytest tests/kria/test_listed.py::test_it\n")
        self.tree.write(
            "scripts/ci/test_selection.py",
            'PATH = "src/apps/api/tests/kria/test_listed.py"\n',
        )
        self.tree.write(
            "src/apps/api/tests/gates/reliability-gates.json",
            '{"gates": [{"regression_tests": ["tests/kria/test_listed.py"]}]}\n',
        )
        self.tree.write(
            "scripts/request-following-regressions.py",
            'SUITES = ["tests/kria/test_listed.py"]\n',
        )
        self.tree.git("add", "-A")
        self.tree.git("commit", "-qm", "lists")
        self.assertEqual(self.tree.check(), [])
        self.tree.git(
            "mv",
            f"{registry.API}/tests/kria/test_listed.py",
            f"{registry.API}/tests/kria/test_listed_renamed.py",
        )
        problems = self.tree.check()
        self.assertTrue(any("Makefile names" in problem for problem in problems))
        stale = registry.check(self.tree.root, self.tree.manifest, base="HEAD")
        self.assertTrue(any("test_selection.py still names" in p for p in stale))
        registry.sync(self.tree.root, self.tree.manifest, base="HEAD")
        self.assertEqual(
            registry.check(self.tree.root, self.tree.manifest, base="HEAD"), []
        )
        self.assertIn(
            "test_listed_renamed.py::test_it", (self.tree.root / "Makefile").read_text()
        )
        self.assertIn(
            "src/apps/api/tests/kria/test_listed_renamed.py",
            (self.tree.root / "scripts/ci/test_selection.py").read_text(),
        )
        for sibling in (
            "src/apps/api/tests/gates/reliability-gates.json",
            "scripts/request-following-regressions.py",
        ):
            self.assertIn(
                "tests/kria/test_listed_renamed.py",
                (self.tree.root / sibling).read_text(),
                sibling,
            )

    def test_a_missing_hand_listed_test_node_is_reported(self):
        self.tree.write(
            "Makefile", "t:\n\tpytest tests/kria/test_listed.py::test_gone\n"
        )
        self.assertTrue(any("no such test" in problem for problem in self.tree.check()))

    def test_manifest_xctests_must_name_real_swift_tests(self):
        self.tree.write(
            "src/apps/ios/Tests/KriaTests/AlphaTests.swift",
            "final class AlphaTests: XCTestCase {\n    func testReal() {}\n}\n",
        )
        rows = json.loads(self.tree.manifest.read_text())
        rows["journeys"][0]["xctests"] = ["KriaTests/AlphaTests/testReal"]
        self.tree.manifest.write_text(json.dumps(rows))
        self.assertEqual(self.tree.check(), [])
        rows["journeys"][0]["xctests"] = ["KriaTests/AlphaTests/testRenamed"]
        self.tree.manifest.write_text(json.dumps(rows))
        self.assertTrue(
            any("names no test" in problem for problem in self.tree.check())
        )

    def test_a_journey_run_through_the_tests_command_can_carry_a_marker(self):
        self.tree.write(
            ".github/workflows/ci.yml",
            WORKFLOW.format(step="alpha")
            + '          tests="$(python3 scripts/ci/journey_registry.py tests beta)"\n',
        )
        rows = json.loads(self.tree.manifest.read_text())
        rows["journeys"].append(
            {
                "id": "beta",
                "needs_native": False,
                "paths": [f"{registry.API}/app/alpha.py"],
            }
        )
        self.tree.manifest.write_text(json.dumps(rows))
        self.tree.write(
            f"{registry.API}/tests/kria/test_beta.py", MARKED.format(id="beta", step="")
        )
        self.assertEqual(self.tree.check(), [])
        beta = next(
            row
            for row in registry.journeys(self.tree.root, self.tree.manifest)
            if row["id"] == "beta"
        )
        self.assertEqual(beta["api_tests"], "tests/kria/test_beta.py")

    def test_a_python_heredoc_that_calls_api_tests_runs_the_marked_journey(self):
        self.tree.write(
            ".github/workflows/ci.yml",
            WORKFLOW.format(step="alpha")
            + '          tests = journey_registry.api_tests("beta")\n',
        )
        rows = json.loads(self.tree.manifest.read_text())
        rows["journeys"].append(
            {
                "id": "beta",
                "needs_native": False,
                "paths": [f"{registry.API}/app/alpha.py"],
            }
        )
        self.tree.manifest.write_text(json.dumps(rows))
        self.tree.write(
            f"{registry.API}/tests/kria/test_beta.py", MARKED.format(id="beta", step="")
        )
        self.assertEqual(self.tree.check(), [])

    def test_workflow_native_test_names_must_name_real_tests_in_their_target(self):
        self.tree.write(
            "src/apps/ios/Tests/KriaUITests/AlphaUITests.swift",
            "final class AlphaUITests: XCTestCase {\n    func testReal() {}\n}\n",
        )
        steps = (
            "          -only-testing:KriaUITests/AlphaUITests/testReal \\\n"
            "          --xctest AlphaUITests/testReal\n"
        )
        self.tree.write(
            ".github/workflows/ci.yml", WORKFLOW.format(step="alpha") + steps
        )
        self.assertEqual(self.tree.check(), [])
        for text in (
            "          -only-testing:KriaTests/AlphaUITests/testReal\n",
            "          -only-testing:KriaUITests/AlphaUITests/testRenamed\n",
            "          --xctest AlphaUITests/testRenamed\n",
        ):
            self.tree.write(
                ".github/workflows/ci.yml", WORKFLOW.format(step="alpha") + text
            )
            self.assertTrue(
                any("names no native test" in p for p in self.tree.check()), text
            )

    def test_the_sync_hint_is_only_given_when_sync_can_fix_the_problem(self):
        self.tree.write(".github/workflows/ci.yml", "jobs: {}\n")
        problems = self.tree.check()
        self.assertTrue(problems)
        self.assertFalse(any("make sync-test-registries" in p for p in problems))

    def test_a_trigger_path_must_select_what_the_selector_would_match(self):
        self.tree.write(f"{registry.API}/app/alpha_two.py", "")
        rows = json.loads(self.tree.manifest.read_text())
        for dead in (f"{registry.API}/app/alpha_", f"{registry.API}/app"):
            rows["journeys"][0]["paths"] = [dead]
            self.tree.manifest.write_text(json.dumps(rows))
            self.assertTrue(
                any("matches no file" in p for p in self.tree.check()), dead
            )
        for live in (f"{registry.API}/app/alpha_two.py", f"{registry.API}/app/"):
            rows["journeys"][0]["paths"] = [live]
            self.tree.manifest.write_text(json.dumps(rows))
            self.assertEqual(self.tree.check(), [], live)


if __name__ == "__main__":
    unittest.main()
