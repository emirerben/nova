# Creator memory runbook

This runbook is for local verification and production recovery. Creator
instruction text is private data: never paste it into logs, tickets, commands,
or admin output.

## Provider-free local smoke test

Start the normal local stack, then run the deterministic direction test. It does
not require Gemini, OpenAI, a browser session, or production credentials.

```bash
./scripts/dev-auto.sh
cd src/apps/api
.venv/bin/pytest tests/services/test_creator_direction_postgres.py::test_activate_resolve_replay_undo_and_soft_suggestion -q
```

Expected result is one passing test proving one active item, one immutable
snapshot, an idempotent duplicate delivery, and a single-use exact Undo. The
fixture must use the extractor's deterministic path, not a live model.

For the frontend contract:

```bash
cd src/apps/web
npm test -- --runInBand src/__tests__/plan/PersonalizationPage.test.tsx
```

## Deploy order

1. Apply the additive migration and verify `alembic current` and `alembic heads`.
2. Deploy API and workers with both creator-memory flags off.
3. Confirm the disabled read/mutation contract and worker task registration.
4. Enable the backend flag for the internal cohort.
5. Enable the frontend flag only after the backend response is verified.

Rollback hides the frontend first, disables extraction/injection second, and
keeps additive rows and immutable snapshots for recovery. Do not downgrade a
production migration as an incident response.

## Health and dead-letter recovery

The admin surface exposes counts, age, payload version, ids, and safe error
codes only:

```bash
python scripts/admin.py GET /admin/creator-memory/health
python scripts/admin.py POST admin/creator-memory/outbox/<outbox-id>/retry --yes
```

Replay is idempotent. A retry must re-check the account deletion state, profile
toggle, global feature flag, source ownership, payload version, and item
revision before it mutates memory. Unknown payload versions are safe dead
letters, not best-effort guesses.

## Common failures

| Symptom | Check | Recovery |
| --- | --- | --- |
| Outbox age grows | `GET /admin/creator-memory/health` | Restore the light worker/queue, then replay safe dead letters |
| `stale_revision` | Memory revision in the response | Reload the owner view; never retry with a guessed revision |
| `undo_no_longer_applicable` | Operation/resulting revision | Review the current rule; do not overwrite a newer edit |
| Missing applied rule | Snapshot mode/conformance status | Inspect the generation snapshot and capability result; do not claim enforcement |
| Raw text in a trace | Stop rollout and preserve only ids/request ids | Redact the sink, rotate access if needed, and add the privacy regression fixture |

## Privacy and deletion checks

Account export includes current readable memory and a bounded recent operation
history. Project deletion clears the source link without retaining the project
name. Account deletion invalidates outbox leases before cascading memory rows.
Workers must re-check owner existence immediately before commit so a deleted
account cannot be recreated by a late task.

## Finished-edit learning (KRI-336 / KRI-648 Release 2)

This is a separate owner opt-in, off by default. `CREATOR_EDIT_LEARNING_ENABLED`,
`CREATOR_EDIT_LEARNING_PRIVACY_APPROVED`, and the internal
`CREATOR_EDIT_LEARNING_USER_IDS` allowlist must all admit the account before it
can enable learning. `PHONE_TEXT_STYLING_ENABLED` must also be on so approved
text preferences can be applied. Revocation remains available when it is off.
Keep those deployment defaults off. The allowlist is not
proof of the cohort outcome gate: record the accepted/exported-cut and hands-on
outcomes required by KRI-336, along with a privacy review, before enabling it.
The runtime cohort's real-media, recovery and latency evidence is described in
[the runtime runbook](kria-agent-runtime.md#alerts-and-release-blockers).
Synthetic/local tests do not pass these external gates.

Deploy migration0120, API and workers first, then the compatible iPhone build.
Do not enable phone typography or learning as a side effect of deploying code.
Verify the actual deployed revision and separate API/worker settings before an
approved internal rollout. Rollback disables new collection; owner revocation
and history reset remain available when the rollout switch is off.

The iPhone Text preferences page uses `PATCH /me/memory/edit-learning` and
`POST /me/memory/edit-learning/reset`, with the existing expected revision and
idempotency key. Approval and dismissal use the existing memory item operations.
A stale write requires Reload; it must not overwrite a newer rule. Learning
never activates a memory rule. Chat and settings display the proposed rule and
supporting finished-video count.

Only validated phone Saves stage normalized font/placement/color/effect deltas.
Those candidates are not observations until the exact immutable recipe passes
MP4 verification and publication. Unexported/failed/superseded recipes cannot
vote. Reverting a property before the accepted export clears its candidate;
a later successful replacement export updates the same project's vote.
Explicitly temporary requests fence the submitted text lanes for that project,
including later Saves returning from the one-off look. They cannot become
evidence against the temporary font or style. Generation/compiler layout changes
are not submitted style edits. No video words, messages, media or frames enter
the learning tables, logs or exports.

Version1 retains five recent finished-project observations and at most twenty
unfinished candidate projects. Each project votes once per role/property.
Three agreeing projects within that window, with the latest relevant change in
agreement, can create an inactive suggestion. Contradictory latest evidence
retracts pending suggestions. Repeated target fonts suggest favorites. An
exclusion additionally requires repeated rejection with differing replacement
fonts; one replacement never bans a font.

Disabling learning or resetting its history removes observations, pending
suggestions and dismissal suppression, and invalidates evidence-related Undo
operations. Approved preferences remain ordinary confirmed memory rules with
no retained project provenance. Account export includes the normalized evidence
and its version. Account erasure cascades it; project deletion removes its votes
and source links while retaining already-approved preferences. Platform-control
clearance remains unverified.
