# KRI-1380 evidence gate: real-script run

`scripts/ci/pr-evidence.py` at commit 5e903674a run against a clone of the branch with a
real receipt body. Each step is the public CLI with `--repo`, `--base`, `--head` and
`--body-file`; the receipt is recorded once for the original head and reused unchanged.
Commit SHAs differ between runs, patch ids do not. The clone sets
`diff.algorithm=histogram` and `color.ui=always` locally to show the id ignores them.

```
patch_id of this PR: 4eabb5877df12554fb0cf88e2d8054020d76c224
$ validate (recorded head, local diff.algorithm=histogram and color.ui=always) base=560f1f37c head=5e903674a
    PR request evidence accepted for local_fix.
    exit=0
patch_id after rebase onto newer main: 4eabb5877df12554fb0cf88e2d8054020d76c224
$ validate (rebased, identical patch) base=4a592a1d4 head=8e666302f
    PR request evidence accepted for local_fix.
    exit=0
$ validate (amended message, force-push) base=4a592a1d4 head=7c2370346
    PR request evidence accepted for local_fix.
    exit=0
$ validate (one more code commit) base=4a592a1d4 head=651990f9c
    PR request evidence: patch_id does not match this PR's patch: the code changed after the evidence was recorded, or the evidence belongs to another change. Recompute with: python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD
    exit=1
$ validate (re-indented one added line) base=4a592a1d4 head=0560028b3
    PR request evidence: patch_id does not match this PR's patch: the code changed after the evidence was recorded, or the evidence belongs to another change. Recompute with: python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD
    exit=1
$ validate (receipt copied onto another PR) base=4a592a1d4 head=50fab1f2e
    PR request evidence: patch_id does not match this PR's patch: the code changed after the evidence was recorded, or the evidence belongs to another change. Recompute with: python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD
    exit=1
```

Same patch (rebase onto a newer main, amend and force-push) stays valid, even with a
hostile local git diff config. A further code commit, a re-indented line, or the same
receipt on a different change fails with `patch_id does not match`. Relocated lines and
base-edit adjacency are covered by `scripts/ci/test_pr_evidence.py`.
