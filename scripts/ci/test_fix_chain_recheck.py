"""Failure modes first, for the merge-time recheck of open fix PRs (KRI-1383).

- a sibling fix PR on the same ticket merging must select every other open fix PR
  on that ticket (the stale-green case: #2041 / #2024 history)
- open PRs on other tickets, non-fix PRs, a KRI-12 vs KRI-123 id prefix and a
  child ticket named second are never selected
- a merged PR that is not a fix selects nothing
- the merged ticket is found by subject, title or branch, and the open PR by
  title or branch, the same rule the check uses
- a merged commit with no PR (direct push) selects nothing
"""

import importlib.util
from pathlib import Path
import unittest

_path = Path(__file__).with_name("fix-chain-recheck.py")
_spec = importlib.util.spec_from_file_location("fix_chain_recheck", _path)
recheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recheck)


def open_pr(number, title, branch=""):
    return {
        "number": number,
        "title": title,
        "headRefName": branch,
        "headRefOid": f"sha{number}",
    }


def merged(title, branch=""):
    return [{"title": title, "head": {"ref": branch}}]


class RecheckTests(unittest.TestCase):
    def test_sibling_merge_selects_other_open_fix_prs_on_the_ticket(self):
        open_prs = [
            open_pr(1, "fix: a (KRI-7)"),
            open_pr(2, "fix: b", "codex/kri-7-b"),
            open_pr(3, "fix: c (KRI-8)"),
            open_pr(4, "feat: d (KRI-7)"),
            open_pr(5, "fix: e (KRI-70)"),
            open_pr(6, "fix: f (KRI-8)", "codex/kri-7-parent-named"),
        ]
        got = recheck.stale_prs(merged("fix: z (KRI-7)"), "fix: z (KRI-7) (#9)", open_prs)
        self.assertEqual(got, [(1, "sha1"), (2, "sha2")])

    def test_non_fix_merge_selects_nothing(self):
        open_prs = [open_pr(1, "fix: a (KRI-7)")]
        self.assertEqual(
            recheck.stale_prs(merged("feat: z (KRI-7)"), "feat: z (KRI-7) (#9)", open_prs),
            [],
        )

    def test_merged_ticket_from_branch_when_title_has_no_id(self):
        open_prs = [open_pr(1, "fix: a (KRI-7)")]
        got = recheck.stale_prs(
            merged("fix: no id", "codex/kri-7-z"), "fix: no id (#9)", open_prs
        )
        self.assertEqual(got, [(1, "sha1")])

    def test_commit_without_a_pr_selects_nothing(self):
        self.assertEqual(
            recheck.stale_prs([], "fix: direct push (KRI-7)", [open_pr(1, "fix: a (KRI-7)")]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
