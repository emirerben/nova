# `test-runs.jsonl` — local-test ledger contract

This doc is the contract between the `/test-template-locally` skill (writer)
and the `require-local-test.yml` workflow / future verifier (reader). The
v0 workflow does **not** yet verify ledger entries — see "v0 vs v1" below.

## File location

```
~/.gstack/projects/$SLUG/test-runs.jsonl
```

- `$SLUG` is the gstack project slug. For this repo it's `nova` (resolved by
  gstack from the git remote; the convention is documented in
  `~/.gstack/projects/`).
- The directory is created by `gstack` on first use; the skill MUST `mkdir -p`
  before writing.
- The file is **append-only JSONL** (one entry per line, never rewritten in
  place). This guarantees survival across rebase/amend on the source branch —
  the ledger is keyed by `run_id`, not by commit SHA, so an amended commit
  doesn't orphan its entries.

## Schema

One JSON object per line, no trailing comma. All fields are required unless
explicitly marked optional.

```json
{
  "run_id": "a1091488-abcd-4ef0-9876-1234567890ab",
  "ts": "2026-05-23T12:34:56Z",
  "commit": "abc1234",
  "branch": "feat/eval-ci-gates-2026-05-23",
  "gcs_output_url": "https://storage.googleapis.com/...signed-url...",
  "verdict": "pass",
  "notes": "Rendered Brazil template against impressing-myself clips; overlays look right."
}
```

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | Hex/uuid-ish slug, ≥6 chars, matches `[a-f0-9-]{6,}`. This is what the PR body cites in `Local test: <run_id>`. |
| `ts` | string | ISO-8601 UTC timestamp with `Z` suffix. |
| `commit` | string | Short SHA of `HEAD` at the time the skill ran. Informational only; do NOT use to invalidate entries (rebase/amend are legal). |
| `branch` | string | The branch the skill ran against. Informational. |
| `gcs_output_url` | string | Signed URL of the rendered video the operator actually watched. The verifier (v1+) checks this is reachable. |
| `verdict` | string | One of `pass`, `fail`, `inconclusive`. The CI gate (v1+) will accept only `pass`. |
| `notes` | string \| null | Optional free text. Whatever the operator typed when the skill prompted. |

The schema is intentionally minimal. New fields are additive — older readers
ignore unknown keys.

## v0 vs v1 vs v2

| Version | What the CI workflow checks | Status |
|---|---|---|
| v0 (this PR) | PR body contains a line matching `^Local test: [a-f0-9-]{6,}$`. The `run_id` is **not** cross-checked against any ledger. | Shipped — `require-local-test.yml`. |
| v1 (follow-up) | CI fetches the ledger via a small artifact-publishing step in the skill (or via the operator's gstack daemon), looks up `run_id`, requires `verdict == "pass"` and `commit` reachable from PR HEAD via merge-base. | Not started. Blocked on the skill landing the writer. |
| v2 (later) | CI re-renders the cited template against a reference clip set and diffs the output against `gcs_output_url`. | Speculative. |

The v0 honor-system gate is intentional: it raises the cost of skipping
local-test from zero to "write a sentence into the PR body or use the escape
hatch with a real reason," which is enough to change behavior. Real
verification is layered on once the ledger exists in practice.

## Escape hatch

If the PR truly cannot be locally tested — e.g. a docstring-only change, a
comment fix, or a workflow YAML touch — the operator may add the following
line to the PR body in lieu of a ledger reference:

```
[skip-local-test] <reason of ≥10 characters>
```

The reason is human-readable, not parsed. Code review is the safety net for
abuse.

## Writer responsibilities (skill follow-up)

The `/test-template-locally` skill must, on each successful run:

1. Resolve the project slug from `git remote` (or fall back to the directory
   basename, e.g. `nova` for a `nova-*` worktree).
2. `mkdir -p ~/.gstack/projects/$SLUG/`.
3. Append one JSON line to `test-runs.jsonl` with all required fields.
4. Print the `run_id` to stdout in a copy-friendly form, e.g.:

   ```
   Local test: a1091488-abcd-4ef0-9876-1234567890ab
     -- paste this into your PR body
   ```

The skill MUST NOT rewrite the ledger or remove entries. If the operator
re-runs the skill on the same branch, that's a new entry — the latest
`run_id` is what they cite.

## Reader responsibilities (v1 verifier — future)

When the v1 verifier ships it will:

1. Read the `Local test: <run_id>` line from the PR body.
2. Locate the ledger (via skill-published artifact or daemon).
3. Look up the entry, assert `verdict == "pass"`.
4. Sanity-check the entry's `commit` is reachable from the PR head (so a
   stale `run_id` pasted from an unrelated PR is rejected).
5. Optionally HEAD the `gcs_output_url` to confirm the output still exists.

Until v1 ships, the workflow at `.github/workflows/require-local-test.yml`
does steps 1 only and treats any well-formed `Local test:` line as
satisfying the gate.
