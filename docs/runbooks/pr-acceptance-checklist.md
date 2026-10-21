# PR acceptance checklist (reliability gates, KRI-577)

Apply the [request-to-output standard](request-to-output-standard.md) before
implementation, review and every new PR. Use this checklist for chat, render,
receipt and device paths; changes outside a user-visible journey explain why it
is not applicable and verify their own changed contract. The
[October session audit](../reviews/kri-577-session-audit.md) explains why isolated
symptom fixes did not prevent neighboring failures.

## Before coding

1. **Define observable acceptance from the original request.** Read the ticket and
   relevant comments, then retrieve the real turn/job through
   `python scripts/admin.py --prod GET creation-threads/<id>/turns` or
   `jobs/<id>/debug`. Record what the result must show or play, including compound
   constraints, refusals and recovery behavior. Do not substitute an internal
   metric such as cut count for the creator's goal.
2. **Trace the relevant boundaries.** Replay the incident with the product's actual
   inputs (for example iPhone analysis proxies and the exact message). Follow
   requirements through parsing, brief, source selection, approval, dispatch,
   export and receipt as relevant. Mock provider transport only; replacing the
   responsible parser/planner/verifier does not cover that boundary.
3. **Enumerate neighboring failures and a negative control.** Select a proportionate
   matrix: compound requests, correction/retry/“simpler version,” EN/TR when
   language is involved, empty vs explicit selection, duplicate media IDs, cap
   boundaries and archetypes sharing the requirement. Record exclusions. Named
   actions need independent timestamped source evidence and a wrong-content
   control that preserves count/duration/order but must fail fulfillment checks.
4. **Use existing gates.** `python3 scripts/reliability-gates.py list` and `select`
   show incidents mapped to the paths. Extend an applicable gate. Before DB-backed
   checks, create the scratch DB with `bash scripts/dev/test-db.sh`.

## Before opening the PR

| Evidence in the PR | What satisfies it |
| --- | --- |
| Acceptance and scope | Original request, observable criteria, traced boundaries and issue/parent incident. A local fix names its remaining gaps and owners; the overall incident stays open until fulfilled. |
| Incident replay and negative control | Exact command/node ID and tested SHA; original failure passes after the fix and fails with it disabled (`python3 scripts/reliability-gates.py red-control <id>` or equivalent). For named-action claims, separately show the same-count/duration wrong-content control is rejected. |
| Neighboring cases and recovery | Cases exercised and omitted with reasons. An unfamiliar request/media case is needed for a broad generalization claim. |
| What was actually verified | Separate plan executability, recipe fidelity and request fulfillment. Link safe source-interval and inspected-output evidence when claiming the requested actions are present. |
| Platform and evidence level | Label offline replay, live model, native output and production separately. Inspect the relevant native output for a native-render claim; server-only checks do not establish phone success. Unavailable evidence stays pending. |
| Model / prompt resource envelope | For thinking, token, timeout, model or prompt changes, report tokens, timeout, cost and call count before/after on the largest relevant fixture; at most one bounded retry. Follow existing approval/budget rules for paid calls; no blanket live-run requirement. |
| Environment and revision | Final tested code SHA, artifact references and exact results. Rerun affected evidence after relevant changes. Name skipped DB checks and environment failures individually; a blanket count of “pre-existing failures” is insufficient. |

Run `bash scripts/preship-check.sh`; it selects mapped reliability gates for changed
paths. A red gate blocks the PR. Supply the PR's required acceptance/evidence record;
CI checks required declarations and mapped checks, while reviewers assess whether
the evidence supports the completion claim. Passing metadata checks alone proves
neither action grounding nor a correct export.

## After merge and authorized rollout

- Confirm the final deployed revision (`fly releases --app nova-video`, image SHA
  on relevant machines) and client build where applicable. Watch for older deploy
  reruns. Record effective API and worker settings separately and the account/cohort
  eligibility; code defaults and enabled flags alone do not establish exposure.
- Re-send the exact incident on the affected product/device path and inspect the
  output and final receipt. Record job reference, UTC time and outcome in the
  authorized ticket/PR handoff. Until then, say “deployed, verification pending.”
- Keep the parent incident open while any original acceptance criteria are unmet.
  Batch follow-ups in its worktree; do not close it because one boundary is fixed.
- Add the incident to `src/apps/api/tests/gates/reliability-gates.json` or extend
  its gate so future relevant changes exercise it.

## Workflow rules

- One worktree, one PR, one hot file: serialize changes to shared files such as
  `brief_checks.py` or `MAIN_CREATOR_PROMPT_VERSION`.
- Never stack a PR on an unmerged branch; verify the destination branch and base.
- Name the evidence level in handoffs. “Implemented” is not “deployed”; a deployed
  revision is not proof that the creator's request succeeded.
