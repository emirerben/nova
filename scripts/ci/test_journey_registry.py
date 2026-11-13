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
        self.assertEqual(registry.check(), [])

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


if __name__ == "__main__":
    unittest.main()
