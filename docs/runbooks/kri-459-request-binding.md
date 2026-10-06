# KRI-459 request preservation rollout

Parent: https://linear.app/kria/issue/KRI-459

This first delivery preserves complete request entries and binds new drafts to a
request version and the server's media snapshot. Later chat messages cannot change
an approved request. Source replacement invalidates a pending approval; enriching
analysis for the same source does not. The dispatch transaction checks again so a
replacement between approval and job creation cannot slip through.

The saved binding lives on the existing draft, execution result and Job assembly
record. There is no new context database. A digest detects corrupt saved inputs;
client draft writes cannot replace the server-owned binding. Completed legacy edits
are untouched. Accepted legacy work uses its persisted request; the worker never
reloads a newer brief. Unbound pending work in the enabled cohort needs a new plan.

## Rollout

1. Deploy compatible API and worker readers with KRIA_BRIEF_BINDING_ENABLED=false
   and an empty KRIA_BRIEF_BINDING_USER_IDS. All newly added fields are optional.
2. After every reader is deployed and journey verification passes, separately
   approve a small account allowlist. No flag is changed by this PR.
3. Verify approval, retry, replacement and a later message during rendering.
4. Expand only after real exported-media and physical iPhone checks.

Rollback stops new bindings by clearing the writer flags. Keep these readers: they
must continue to honor already-saved bindings regardless of flag state. Do not roll
back to an older binary that rejects the added draft fields.

## Explicit limits

Main Creator sees up to three whole-entry context batches. Conflicting batch plans
pause. The downstream clip planner accepts a complete request up to 12,000
characters; exceeding its limit preserves the full ledger/chat and asks the creator
which part to work on first. No prefix is silently treated as the complete request.

This PR does not prove that an exported video fulfills the request. Shared analysis
connections, truthful checks and rendered journey verification follow in the next
stacked deliveries. Model replay, local database tests and simulator checks are
reported separately from paid live-model and physical-device export evidence.
