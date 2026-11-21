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
  "formatted" quietly comes to mean less;
- the commit hook hands over paths from the repository root
  (`src/apps/api/app/x.py`); taken as they are they do not exist from the API
  directory, and a path outside the API would be linted with the API's rules;
- the editor's format-on-save hook formats with whatever `ruff` is first on
  PATH (Homebrew's was 0.15.20 on 2026-11-21), so a saved file fails CI's check;
- the hooks are defined but never installed, or installed without the checks;
- a worktree made with `git worktree add` has no environment of its own, so
  the lookup lands on Homebrew's older ruff and every push from it is refused,
  although the primary checkout has the pinned one;
- deleting a remote branch runs the push hook with an all-zero commit; a check
  that reads that commit fails and the deletion is refused (seen with hk
  2.13.0 in a scratch repository), and a check of the working tree would judge
  files that are not being pushed.
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
            version = self.version.get(argv[0]) if isinstance(self.version, dict) else self.version
            return SimpleNamespace(returncode=0, stdout=version + "\n")
        return SimpleNamespace(returncode=self.codes[argv[1]], stdout="")


def run(argv=(), missing=(), ruff=("/venv/bin/ruff",), **fake):
    calls = FakeRun(**fake)
    out = []
    code = api_lint.main(
        list(argv),
        run=calls,
        ruff=list(ruff),
        lock=LOCK,
        say=out.append,
        exists=lambda path: path not in missing,
    )
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
        # The pinned ruff by its path: a bare `ruff` may be another version.
        self.assertIn("/venv/bin/ruff format", out)

    def test_another_ruff_version_refuses_to_run_and_names_the_install(self):
        code, calls, out = run(version="ruff 0.15.20")
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1, "nothing may be checked with the wrong formatter")
        self.assertIn("0.15.20", out)
        self.assertIn("ruff==0.17.6", out)

    def test_a_missing_ruff_fails_instead_of_skipping(self):
        code, calls, out = run(ruff=())
        self.assertEqual((code, calls), (2, []))
        self.assertIn("ruff==0.17.6", out)

    def test_the_first_ruff_of_the_pinned_version_is_used_wherever_it_is(self):
        versions = {"/brew/ruff": "ruff 0.15.20", "/primary/venv/ruff": "ruff 0.17.6"}
        code, calls, _ = run(ruff=("/brew/ruff", "/primary/venv/ruff"), version=versions)
        self.assertEqual(code, 0)
        self.assertEqual(calls[-2][0], ["/primary/venv/ruff", "check", "."])
        self.assertEqual(calls[-1][0], ["/primary/venv/ruff", "format", "--check", "."])

    def test_no_ruff_of_the_pinned_version_names_every_one_it_found(self):
        versions = {"/brew/ruff": "ruff 0.15.20", "/other/ruff": "ruff 0.16.1"}
        code, calls, out = run(ruff=("/brew/ruff", "/other/ruff"), version=versions)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 2, "only the two version questions")
        self.assertIn("/brew/ruff is ruff 0.15.20", out)
        self.assertIn("/other/ruff is ruff 0.16.1", out)

    def test_paths_from_the_repository_root_are_read_as_api_paths(self):
        _, calls, _ = run(["src/apps/api/app/a.py", "tests/b.py"])
        self.assertEqual(calls[1][0], ["/venv/bin/ruff", "check", "app/a.py", "tests/b.py"])

    def test_a_path_outside_the_api_is_refused_not_linted(self):
        for path in ("scripts/ci/api-lint.py", "src/apps/web/x.py", "../x.py"):
            with self.subTest(path=path):
                # The first is a real file, but not under src/apps/api.
                code, calls, out = run([path], missing=("scripts/ci/api-lint.py",))
                self.assertEqual(code, 2)
                self.assertEqual(len(calls), 1)
                self.assertIn(path, out)

    def test_ruff_path_prints_the_pinned_ruff_and_nothing_else(self):
        code, calls, out = run(["--ruff-path"])
        self.assertEqual((code, out), (0, "/venv/bin/ruff"))
        self.assertEqual(len(calls), 1, "asking for the path must not lint anything")

    def test_ruff_path_gives_no_path_for_another_version(self):
        code, _, out = run(["--ruff-path"], version="ruff 0.15.20")
        self.assertEqual(code, 2)
        self.assertNotIn("/venv/bin/ruff\n", out + "\n")

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

    def test_format_on_save_asks_for_the_pinned_ruff(self):
        text = (REPO / "scripts/hooks/format-on-save.sh").read_text()
        self.assertIn("scripts/ci/api-lint.py --ruff-path", text)
        self.assertNotIn("command -v ruff", text)

    def test_the_commit_hook_runs_this_command_on_staged_api_files(self):
        text = (REPO / "hk.pkl").read_text()
        commit = text.split('["pre-commit"]', 1)[1].split('["pre-push"]', 1)[0]
        self.assertIn('glob = "src/apps/api/**/*.py"', commit)
        self.assertIn('check = "python3 scripts/ci/api-lint.py {{files}}"', commit)

    def test_the_push_hook_lints_the_whole_api_tree(self):
        push = (REPO / "hk.pkl").read_text().split('["pre-push"]', 1)[1]
        self.assertIn("python3 scripts/ci/api-lint.py\n", push)

    def test_every_push_check_lets_a_branch_deletion_through(self):
        push = (REPO / "hk.pkl").read_text().split('["pre-push"]', 1)[1]
        checks = re.findall(r'check = """\n(.*?)"""', push, re.S)
        self.assertEqual(len(checks), push.count("check = "), "write each check as a block")
        self.assertGreaterEqual(len(checks), 4)
        for check in checks:
            self.assertTrue(check.lstrip().startswith("\(notADeletion)"), check)
        self.assertIn(
            'case "${HK_TO_REF:-}" in 0000000*) exit 0 ;; esac', (REPO / "hk.pkl").read_text()
        )

    def test_a_new_worktree_installs_the_hooks(self):
        text = (REPO / "scripts/worktree-setup.sh").read_text()
        self.assertIn("hk install", text)

    def test_preship_uses_the_same_command(self):
        text = (REPO / "scripts/preship-check.sh").read_text()
        self.assertIn("scripts/ci/api-lint.py", text)
        self.assertNotRegex(text, r"\"\$RUFF\" (check|format)")


if __name__ == "__main__":
    unittest.main()
