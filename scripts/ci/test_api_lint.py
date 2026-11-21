"""Ways the API lint and format check can pass something CI then fails, written first.

`scripts/ci/api-lint.py` is the one command behind `lint-suites` in CI and the
ruff step of `scripts/preship-check.sh`:

- a laptop's ruff is another version than the one `uv.lock` gives CI, so the
  two format differently and a clean local run fails in CI (0.15 and 0.17 were
  both in use on 2026-11-21);
- `pyproject.toml` asks for a range, so the next `uv lock` moves CI to a new
  formatter without anyone deciding to;
- lint fails first and the format check never runs, so the author fixes one,
  pushes, and fails again on the other;
- no ruff is installed and the step reports "skipped" instead of failing;
- the check runs from the wrong directory or on a narrowed path and passes on
  nothing;
- CI stops running the format half (it never ran before this file existed and
  92 files had drifted);
- the list of files left unformatted grows, or keeps a file that is gone, so
  "formatted" quietly comes to mean less.
"""

import importlib.util
import re
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"

spec = importlib.util.spec_from_file_location("api_lint", REPO / "scripts/ci/api-lint.py")
api_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_lint)

# The pipeline files no journey claimed when the format check was added.
NOT_FORMATTED_ON_2026_11_21 = {
    "app/pipeline/audio_layout.py",
    "app/pipeline/intro_voiceover_mix.py",
    "app/pipeline/lyric_support.py",
    "app/pipeline/orientation.py",
    "app/pipeline/prompt_loader.py",
    "app/pipeline/scene_detect.py",
    "app/pipeline/text_overlay_v2/tokenize.py",
    "app/pipeline/text_wrap.py",
}

LOCK = '[[package]]\nname = "ruff"\nversion = "0.17.6"\nsource = { registry = "x" }\n'


class FakeRun:
    """Stands in for subprocess.run: records each call and answers from a table."""

    def __init__(self, version="ruff 0.17.6", check=0, fmt=0):
        self.version, self.codes, self.calls = version, {"check": check, "format": fmt}, []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("cwd")))
        if argv[1] == "--version":
            return SimpleNamespace(returncode=0, stdout=self.version + "\n")
        return SimpleNamespace(returncode=self.codes[argv[1]], stdout="")


def run(argv=(), **fake):
    calls = FakeRun(**fake)
    out = []
    code = api_lint.main(list(argv), run=calls, ruff="/venv/bin/ruff", lock=LOCK, say=out.append)
    return code, calls.calls, "\n".join(out)


class ApiLintTests(unittest.TestCase):
    def test_the_whole_api_tree_is_linted_and_format_checked_from_the_api_directory(self):
        code, calls, _ = run()
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[1:],
            [
                (["/venv/bin/ruff", "check", "."], API),
                (["/venv/bin/ruff", "format", "--check", "."], API),
            ],
        )

    def test_named_files_narrow_both_checks_and_nothing_else(self):
        _, calls, _ = run(["app/a.py", "tests/b.py"])
        self.assertEqual(calls[1][0], ["/venv/bin/ruff", "check", "app/a.py", "tests/b.py"])
        self.assertEqual(
            calls[2][0], ["/venv/bin/ruff", "format", "--check", "app/a.py", "tests/b.py"]
        )

    def test_a_lint_failure_still_runs_the_format_check_and_fails(self):
        code, calls, _ = run(check=1)
        self.assertEqual(code, 1)
        self.assertEqual(calls[2][0][:3], ["/venv/bin/ruff", "format", "--check"])

    def test_a_format_failure_alone_fails_and_names_the_fix(self):
        code, _, out = run(fmt=1)
        self.assertEqual(code, 1)
        self.assertIn("ruff format", out)

    def test_another_ruff_version_refuses_to_run_and_names_the_install(self):
        code, calls, out = run(version="ruff 0.15.20")
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1, "nothing may be checked with the wrong formatter")
        self.assertIn("0.15.20", out)
        self.assertIn("ruff==0.17.6", out)

    def test_a_missing_ruff_fails_instead_of_skipping(self):
        out = []
        code = api_lint.main([], run=FakeRun(), ruff=None, lock=LOCK, say=out.append)
        self.assertEqual(code, 2)
        self.assertIn("ruff==0.17.6", "\n".join(out))

    def test_a_lock_without_ruff_is_an_error_not_a_pass(self):
        with self.assertRaises(SystemExit):
            api_lint.locked_version('[[package]]\nname = "rich"\nversion = "1"\n')


class WiringTests(unittest.TestCase):
    def test_pyproject_pins_exactly_the_version_the_lock_installs(self):
        locked = api_lint.locked_version((API / "uv.lock").read_text())
        dev = tomllib.loads((API / "pyproject.toml").read_text())["project"][
            "optional-dependencies"
        ]["dev"]
        self.assertIn(f"ruff=={locked}", dev)

    def test_ci_runs_the_shared_command_on_the_whole_tree(self):
        text = (REPO / ".github/workflows/ci.yml").read_text()
        job = text.split("\n  lint-suites:\n", 1)[1].split("\n  lint:\n", 1)[0]
        self.assertIn("        run: python3 scripts/ci/api-lint.py\n", job + "\n")
        # No second, narrower or forgiving ruff call in the workflow.
        self.assertEqual(re.findall(r"^\s*run: .*\bruff\b.*$", text, re.M), [])
        self.assertNotIn("api-lint.py ||", text)

    def test_the_unformatted_list_only_shrinks(self):
        listed = tomllib.loads((API / "pyproject.toml").read_text())["tool"]["ruff"]["format"][
            "exclude"
        ]
        self.assertEqual(sorted(set(listed)), sorted(listed))
        self.assertLessEqual(set(listed), NOT_FORMATTED_ON_2026_11_21)
        for path in listed:
            self.assertTrue((API / path).is_file(), f"{path} is gone: delete its line")

    def test_preship_uses_the_same_command(self):
        text = (REPO / "scripts/preship-check.sh").read_text()
        self.assertIn("scripts/ci/api-lint.py", text)
        self.assertNotRegex(text, r"\"\$RUFF\" (check|format)")


if __name__ == "__main__":
    unittest.main()
