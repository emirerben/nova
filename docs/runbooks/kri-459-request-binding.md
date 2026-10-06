# KRI-459 request preservation rollout

Parent: https://linear.app/kria/issue/KRI-459

This delivery preserves complete request entries and binds new drafts to a
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

The connected paths preserve unchecked requirements and ask before a known
requirement-breaking fallback. This does not prove that an exported video fulfills
the request. Model replay, local database tests and simulator checks are reported
separately from paid live-model and physical-device export evidence.

PR #1405 merged as `cf0d99cca` during implementation and is included through
current main. PR #1404 merged as `45d333978`; its alignment code and existing
`NARRATED_CLIP_ALIGNMENT_ENABLED=true` default are inherited from main.
Request-binding writers remain gated separately for the reader-first rollout.
When alignment is unavailable, bound narrated requests ask for recovery rather
than silently assigning clips to equal-duration buckets.

Capabilities continue to come from `services/creator_capabilities.py` and the
existing editor tool registry. Montage supports resolved clip ordering and labels;
Voiceover adds word-timed alignment when enabled; Talking uses its speech/text
operations; Slides uses its slide-specific text/reorder operations. Each operation
is checked against the actual format, renderer and flags, rather than claiming
all four formats support every operation.
