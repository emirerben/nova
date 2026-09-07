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
