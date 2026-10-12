# PR acceptance checklist (reliability gates, KRI-577)

Use this for any change on a Kria chat, render, receipt or device path. It exists
because the October 4–11 fixes were correct one symptom at a time and still needed
follow-up PRs: the next failure sat in a neighbouring path, on the phone, or in the
receipt. The audit behind it is
[`docs/reviews/kri-577-session-audit.md`](../reviews/kri-577-session-audit.md).

## Before coding

1. **Replay the incident, not the summary.** Pull the real turn/job with
   `python scripts/admin.py --prod GET creation-threads/<id>/turns` or
   `jobs/<id>/debug`. Replay it through the real boundary (parser, contract,
   dispatch, planner, verifier, publication, reply), with the same inputs the
   product had: iPhone analysis proxies, the exact message, EN/TR.
2. **Enumerate the siblings before patching one.** List every emitter of the
   user-visible string, every caller of the function, every archetype that owns the
   same requirement kind (montage / narrated / subtitled / talking), and every
   alternate path for the same ask (empty vs explicit selection, draft vs
   render-ready, duplicate bytes under different ids, cap boundary). Fix the class,
   or name in the PR which siblings stay open and why.
3. **Check what the gate ledger already covers.** `python3 scripts/reliability-gates.py list`
   and `select` show the incidents that touched your paths. Extend an existing gate
   rather than adding another isolated test.

## Before opening the PR

| Line in the PR template | What satisfies it |
| --- | --- |
| Incident replay | A fixture or test that replays the original failure, named by node id. It must fail with the fix disabled: `python3 scripts/reliability-gates.py red-control <id>` (or a kill-switch flip) and pass with it. |
| Neighbouring cases | The sibling inputs you exercised and the ones you did not. Mocking an internal boundary (`run()` of a planner, a resolver, a compiler) does not count as covering it; mock provider transport only. |
| Platforms and evidence level | One of: offline replay; live model (prompt, outcome, cost); native output (a device or simulator export inspected for text, order, timing, audio); production (revision + UTC observation). Server-only success is insufficient for anything the phone renders. |
| Model / prompt resource envelope | For any `thinking_level`, token, timeout, model or prompt change: tokens, timeout, cost and call count before and after on the largest fixture; one bounded retry at most. |
| Known environment failures vs regressions | Run gates with the scratch DB (`bash scripts/dev/test-db.sh`). List skipped DB-backed tests by name. "92 pre-existing failures" is not an acceptable line. |

Then run `bash scripts/preship-check.sh`. Step 6 runs the gates mapped to your
changed paths; a red gate means the original incident or a neighbour regressed.

## After merge

- Confirm the deployed revision (`fly releases --app nova-video`, image SHA on every
  machine) before telling anyone to test. Watch for an older in-flight deploy re-run.
- A phone-visible fix is **done** only after one re-send of the exact kit on the
  device with the final receipt read. Record the job id, UTC time and outcome in the
  ticket. If it fails, keep the worktree, batch the follow-ups, open one PR.
- Add the incident to `src/apps/api/tests/gates/reliability-gates.json` (or extend
  its gate) so CI runs it for those paths from now on.

## Agent workflow rules that fall out of the audit

- One worktree, one PR, one hot file: never fan out parallel PRs that edit the same
  file (`brief_checks.py`, `MAIN_CREATOR_PROMPT_VERSION`); serialize them or land one PR.
- Never stack a PR on an unmerged branch. A draft did not stop a dead-base merge (#1419).
- Create the scratch test DB first (`bash scripts/dev/test-db.sh`); a run against the
  default `postgres` role is a wasted run and hides real failures.
- Say what evidence level a claim has. "Live" means the fleet image SHA was checked;
  "works" means a device re-send was read; otherwise say "unit tests only".
