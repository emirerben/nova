# TestFlight cancellation before job creation (KRI-577)

The automatic release at run 40567486670 failed before archiving:
`Run 40545760778 attempt 1: missing or ambiguous release job`.
The older run was cancelled with no Actions jobs. The provenance guard required
one release job even when no job had ever been created.

## Boundary and acceptance

An empty `gh run view` summary is insufficient to skip history. The guard now
reads both the exact raw run/attempt record and its raw job inventory. It requires
matching run and attempt IDs, repository, main branch and TestFlight workflow,
completed cancellation, and an explicit zero count with an empty job list.
Missing or inconsistent records still stop release. Existing allocated-runner
checks, prior attempts, checkout/transfer receipts and Git ancestry checks remain.

The real CLI replay failed before the repair and passes after it. Its Git graph
separates workflow revision from binary source. It rejects incorrect identity,
active/successful attempts, missing/inconsistent inventories, ambiguous release
jobs and unknown transfer evidence. A cancelled attempt with a completed upload,
and an upload in an earlier attempt before a jobless rerun, still protect the
uploaded source. A jobless attempt without any verified upload still blocks.

Reproduce without network or Apple credentials:

```bash
python3 -m unittest discover -s scripts/ios/tests -v
```

The retained-history audit captured 155 read-only GitHub responses. Replaying
those responses with the real repository and the workflow's `git fetch origin
main` prerequisite reproduces the old failure and returns
`dbb9c09f443dd6775b46584fd2d1291e5eb5d0ed false` with the repair. The uploaded
base is inferred from checkout plus binary-transfer evidence, not workflow head.
The first local ancestry check failed because the fresh worktree lacked
`FETCH_HEAD`; that failed artifact is retained. Fetching main supplies the same
prerequisite as the workflow. No source change or response replacement was needed.
All 117 offline iOS workflow/script tests and scoped Ruff checks pass.

The original raw attempt and failed release are independently inspectable:

- [Cancelled attempt 40545760778/1](https://github.com/emirerben/nova/actions/runs/40545760778/attempts/1)
- [Failed release 40567486670/1](https://github.com/emirerben/nova/actions/runs/40567486670/attempts/1)

This repairs a release gate. It does not establish a newly uploaded or installed
build, Cunda request fulfillment, a physical Photos save, or original-media
incident replay. Those launch checks remain separate.
