"""Failure modes first, for the blocking third-fix rule (KRI-1383).

The gate must stay quiet unless a fix PR is the third on one ticket, and must
not be satisfiable by an empty gesture. Each case below is one way it can go
wrong:

- squash titles omit the ticket id: the prior fix is only known by its PR
  branch (pr-index) or the id sits later in the title
- a ticket with many feat/test/chore PRs is not a fix chain
- parent/child tickets: a child's fix naming its parent second must not count
  against the parent; KRI-12 must not match KRI-123
- the current PR itself is not a fix (feat/chore) -> never blocked
- the same PR seen in both git history and the PR index counts once
- template placeholders, one-row lists, rows without covered/uncovered, a block
  for a different ticket, and a block that is not changed in this PR do not pass
- skip marker: needs its own line and a real reason; inline or short or
  placeholder reasons do not pass
- CRLF bodies from the GitHub web UI
"""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("fix-chain-check.py").resolve()

GOOD_BLOCK = """Failure modes for KRI-7 (after fix #2)
- null role text -> saves | covered by tests/test_save.py::test_null_role
- clip at source end -> saves | uncovered - adding tests/test_save.py::test_end
"""


class FixChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@invalid.local")
        self.git("config", "user.name", "t")
        self.counter = 0

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True
        ).strip()

    def main_commit(self, subject):
        self.counter += 1
        (self.repo / "f.txt").write_text(f"{self.counter}\n")
        self.git("add", ".")
        self.git("commit", "-qm", subject)

    def run_check(self, *, title="", branch="", body="", index=None, files=None, pr=None):
        body_file = self.repo.parent / f"{self.repo.name}-body.md"
        body_file.write_text(body)
        args = ["python3", str(SCRIPT), "--repo", str(self.repo), "--main-ref", "main",
                "--branch", branch, "--title", title, "--body-file", str(body_file)]
        if index is not None:
            index_file = self.repo.parent / f"{self.repo.name}-index.json"
            index_file.write_text(json.dumps(index))
            args += ["--pr-index", str(index_file)]
        for path in files or []:
            args += ["--changed-file", path]
        if pr:
            args += ["--pr-number", str(pr)]
        done = subprocess.run(args, capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def two_fixes(self):
        self.main_commit("fix(editor): one (KRI-7) (#101)")
        self.main_commit("fix(editor): two (KRI-7) (#102)")

    def test_two_prior_fixes_block_third(self):
        self.two_fixes()
        code, out = self.run_check(title="fix(editor): three (KRI-7)", body="x")
        self.assertEqual(code, 1, out)
        self.assertIn("Failure modes for KRI-7", out)

    def test_one_prior_fix_passes(self):
        self.main_commit("fix(editor): one (KRI-7) (#101)")
        self.assertEqual(self.run_check(title="fix: two (KRI-7)")[0], 0)

    def test_ticket_only_in_branch_still_counts(self):
        self.two_fixes()
        code, out = self.run_check(title="fix(editor): three", branch="codex/kri-7-three")
        self.assertEqual(code, 1, out)

    def test_prior_without_id_in_title_is_found_by_pr_branch(self):
        self.main_commit("fix(editor): one (KRI-7) (#101)")
        self.main_commit("fix(editor): two without id (#102)")
        index = [{"number": 102, "title": "fix(editor): two without id", "headRefName": "codex/kri-7-two"}]
        code, out = self.run_check(title="fix: three (KRI-7)", index=index)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.run_check(title="fix: three (KRI-7)")[0], 0)

    def test_pr_only_in_index_counts_and_dedupes_with_git(self):
        self.main_commit("fix(editor): one (KRI-7) (#101)")
        index = [
            {"number": 101, "title": "fix(editor): one", "headRefName": "codex/kri-7-one"},
            {"number": 55, "title": "fix(editor): older", "headRefName": "kri-7-older"},
        ]
        code, out = self.run_check(title="fix: three (KRI-7)", index=index)
        self.assertEqual(code, 1, out)
        self.assertIn("2 prior fix", out)

    def test_many_non_fix_prs_do_not_trigger(self):
        for n, kind in enumerate(["feat", "test", "chore", "docs", "refactor"]):
            self.main_commit(f"{kind}(editor): step (KRI-7) (#{200 + n})")
        self.assertEqual(self.run_check(title="fix: first fix (KRI-7)")[0], 0)

    def test_current_pr_not_a_fix_is_never_blocked(self):
        self.two_fixes()
        self.assertEqual(self.run_check(title="feat(editor): new thing (KRI-7)")[0], 0)

    def test_parent_ticket_not_charged_for_child_fixes(self):
        self.main_commit("fix(a): x (KRI-12, parent KRI-1) (#301)")
        self.main_commit("fix(a): y (KRI-13, parent KRI-1) (#302)")
        self.assertEqual(self.run_check(title="fix(a): z (KRI-1)")[0], 0)

    def test_ticket_id_digit_boundary(self):
        self.main_commit("fix: a (KRI-123) (#401)")
        self.main_commit("fix: b (KRI-1234) (#402)")
        self.assertEqual(self.run_check(title="fix: c (KRI-12)")[0], 0)

    def test_current_pr_excluded_from_its_own_count(self):
        self.two_fixes()
        code, _ = self.run_check(title="fix: again (KRI-7)", pr=102)
        self.assertEqual(code, 0)

    def test_valid_block_in_body_passes(self):
        self.two_fixes()
        self.assertEqual(self.run_check(title="fix: three (KRI-7)", body=GOOD_BLOCK)[0], 0)

    def test_crlf_body_passes(self):
        self.two_fixes()
        body = GOOD_BLOCK.replace("\n", "\r\n")
        self.assertEqual(self.run_check(title="fix: three (KRI-7)", body=body)[0], 0)

    def test_block_for_other_ticket_fails(self):
        self.two_fixes()
        body = GOOD_BLOCK.replace("KRI-7", "KRI-70")
        self.assertEqual(self.run_check(title="fix: three (KRI-7)", body=body)[0], 1)

    def test_weak_blocks_fail(self):
        self.two_fixes()
        weak = {
            "template placeholders": "Failure modes for KRI-7\n- <shape> -> <outcome> | covered by <test id>\n- <a> -> <b> | uncovered - adding <test id>\n",
            "single row": "Failure modes for KRI-7\n- a -> b | covered by tests/test_x.py::t\n",
            "no verdict": "Failure modes for KRI-7\n- a -> b\n- c -> d\n",
            "empty test token": "Failure modes for KRI-7\n- a -> b | covered by todo\n- c -> d | covered by tbd\n",
            "heading only": "Failure modes for KRI-7\n",
        }
        for name, body in weak.items():
            with self.subTest(name):
                self.assertEqual(self.run_check(title="fix: three (KRI-7)", body=body)[0], 1)

    def test_doc_block_counts_only_when_changed_in_this_pr(self):
        self.two_fixes()
        doc = self.repo / "docs" / "reviews"
        doc.mkdir(parents=True)
        (doc / "kri-7-failure-modes.md").write_text("# Notes\n\n" + GOOD_BLOCK)
        path = "docs/reviews/kri-7-failure-modes.md"
        self.assertEqual(self.run_check(title="fix: three (KRI-7)")[0], 1)
        self.assertEqual(self.run_check(title="fix: three (KRI-7)", files=[path])[0], 0)
        self.assertEqual(self.run_check(title="fix: three (KRI-7)", files=["../outside.md"])[0], 1)

    def test_skip_marker_needs_own_line_and_real_reason(self):
        self.two_fixes()
        ok = "[skip-fix-chain] umbrella ticket, this is an unrelated typo in copy\n"
        self.assertEqual(self.run_check(title="fix: three (KRI-7)", body=ok)[0], 0)
        bad = {
            "short": "[skip-fix-chain] not needed\n",
            "inline": "see [skip-fix-chain] umbrella ticket, unrelated typo in copy\n",
            "placeholder": "[skip-fix-chain] <one-line reason why this is not a repeat>\n",
            "filler": "[skip-fix-chain] n/a n/a n/a n/a n/a n/a n/a n/a\n",
            "no reason": "[skip-fix-chain]\n",
        }
        for name, body in bad.items():
            with self.subTest(name):
                self.assertEqual(self.run_check(title="fix: three (KRI-7)", body=body)[0], 1)

    def test_no_ticket_anywhere_is_skipped(self):
        self.two_fixes()
        code, out = self.run_check(title="fix: something", branch="feature/x")
        self.assertEqual(code, 0, out)
        self.assertIn("no KRI", out)


if __name__ == "__main__":
    unittest.main()
