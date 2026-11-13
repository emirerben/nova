# KRI-1380 evidence gate: real-script run

`scripts/ci/pr-evidence.py` (this branch's first commit) run against a clone of the
branch with a real receipt body. Each step is the public CLI with `--repo`, `--base`,
`--head` and `--body-file`; the receipt is recorded once for the original head and
reused unchanged. Commit SHAs differ between runs, patch ids do not.

```
patch_id of this PR: 5078a66d3e86a3e44e3549d627023a51ab2c1e78
$ validate (recorded head) base=560f1f37c head=8399fd287
    PR request evidence accepted for local_fix. This validates the receipt and references; it does not verify content, remote artifact availability, or deployment.
    exit=0
patch_id after rebase onto newer main: 5078a66d3e86a3e44e3549d627023a51ab2c1e78
$ validate (rebased, identical patch) base=1eddc98b2 head=cdb94fbb4
    PR request evidence accepted for local_fix. This validates the receipt and references; it does not verify content, remote artifact availability, or deployment.
    exit=0
$ validate (amended message, force-push) base=1eddc98b2 head=a23f2b78d
    PR request evidence accepted for local_fix. This validates the receipt and references; it does not verify content, remote artifact availability, or deployment.
    exit=0
$ validate (one more code commit) base=1eddc98b2 head=33bc452eb
    PR request evidence: patch_id does not match this PR's patch: the code changed after the evidence was recorded, or the evidence belongs to another change. Recompute with: python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD
    exit=1
$ validate (receipt copied onto another PR) base=1eddc98b2 head=3ef781cdc
    PR request evidence: patch_id does not match this PR's patch: the code changed after the evidence was recorded, or the evidence belongs to another change. Recompute with: python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD
    exit=1
```

Same patch (rebase onto a newer main, amend and force-push) stays valid. A further code
commit, or the same receipt on a different change, fails with `patch_id does not match`.
