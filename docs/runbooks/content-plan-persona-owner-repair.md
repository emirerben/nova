# Content-plan / Persona ownership incident repair

Use this runbook only after the release-1 ownership guards are deployed to
both the API and worker process groups. It repairs a plan whose `persona_id`
points at a Persona owned by another account. It is intentionally forward-only:
never restore the foreign link or make a best-effort guess when a precondition
fails.

## Required release order

1. Deploy release 1 (`0071`): additive ownership epoch/quarantine columns,
   fail-closed application checks, stale-task epoch checks, immutable cancelled
   Jobs, and cancelled-output suppression.
2. Quarantine, cancel, quiesce workers, preserve restricted evidence, delete
   user-signable bad outputs, and repair the affected rows as described below.
3. Require the global mismatch audit to return zero rows.
4. Deploy release 2 (`0072`): the composite same-owner foreign key.
5. Verify the constraint, clear quarantine without resetting the epoch, and
   dispatch a corrected render.

Do not combine these into one Fly release. Fly runs migrations before replacing
the application image; an unexpected corrupt row must not prevent release-1
guards from reaching production.

## 1. Inventory and evidence

Run this read-only audit and require the result to match the incident inventory
exactly. Any unexpected row is a stop condition.

```sql
SELECT cp.id,
       cp.user_id AS plan_user_id,
       cp.persona_id,
       linked.user_id AS linked_persona_user_id,
       owner_persona.id AS owner_persona_id,
       cp.ownership_epoch,
       cp.ownership_quarantined_at
FROM content_plans cp
LEFT JOIN personas linked ON linked.id = cp.persona_id
LEFT JOIN personas owner_persona ON owner_persona.user_id = cp.user_id
WHERE linked.id IS NULL
   OR linked.user_id IS DISTINCT FROM cp.user_id;
```

Create a restricted incident snapshot containing the affected plan, the linked
and intended Persona identifiers, affected PlanItems, Job IDs and original
statuses, `current_job_id` values, Celery task IDs, output object identifiers,
and involved seed IDs. Personal Persona JSON, agent inputs, and output media
must stay in the restricted incident store and must never be copied into logs.

## 2. Transaction A: establish durable fences

In one short transaction, lock in this global order:

1. ContentPlan;
2. linked and intended Persona rows, sorted by UUID;
3. affected PlanItems, sorted by UUID;
4. affected Jobs, sorted by UUID.

Re-check every identifier and expected Job status from the evidence snapshot.
Then:

- increment `content_plans.ownership_epoch` by exactly one;
- set `ownership_quarantined_at = now()`;
- set every affected bad Job to `status = 'cancelled'`,
  `failure_reason = 'persona_owner_mismatch'`, and a generic internal
  `error_detail`, using expected-status predicates;
- set `finished_at` when it is absent;
- require every expected row count before committing.

Do not reset or decrement the epoch later. A stale task can become runnable
again if the old value is restored.

After commit:

1. Copy each known rendered output to restricted evidence storage.
2. Verify size and content hash, then delete the original object so existing
   signed URLs fail immediately.
3. Revoke/terminate every affected Celery task ID.
4. Replace the entire worker process group with the verified release-1 image.
5. Inspect active, reserved, and scheduled queues. No affected plan, item, Job,
   or task may remain.
6. Re-scan affected output prefixes and repeat the copy/hash/delete process for
   any artifact created after the first pass.

If queue inspection is unavailable, an object cannot be deleted, or a hash does
not match, keep the plan quarantined and stop.

## 3. Transaction B: repair forward

Use the same lock order. Re-read all rows, require that every affected Job is
still cancelled, and require exactly one Persona with
`personas.user_id = content_plans.user_id`.

Inside one transaction:

1. Repair only attributable seed contamination under the rules below.
2. Set `plan_items.current_job_id = NULL` only for the cancelled bad Jobs;
   retain `jobs.content_plan_item_id` and all agent/debug records for audit.
3. Set `content_plans.persona_id` to the unique owner Persona.
4. Keep `ownership_quarantined_at` populated and leave the incremented epoch
   unchanged.
5. Re-run the owner-pair and seed postconditions before committing.

Seed repair is deliberately narrow. For each affected
`source_idea_seed_id`, require all of the following:

- the seed exists in the foreign Persona and its text matches the PlanItem;
- no PlanItem owned by the foreign account references that seed ID;
- the destination either lacks the ID or contains an exactly matching seed;
- a matching destination seed may move only from `pending` to `in_plan`;
- a conflicting value or invalid status is a stop condition.

Remove exactly the attributable seed from the foreign Persona, insert it once
under the owner Persona if absent, and require final status `in_plan`. Do not
bulk-copy or bulk-delete Persona seeds.

If any assertion fails, roll back Transaction B. Transaction A's quarantine and
Job cancellation remain the safe state.

## 4. Release-2 gate and verification

Before deploying `0072`, require:

- the mismatch audit returns zero rows globally;
- the attributable seed exists exactly once under the correct Persona and not
  under the foreign Persona;
- no affected worker task is active, reserved, or scheduled;
- no original bad rendered object remains;
- user status/history/item/edit/download/publish endpoints expose no media for
  cancelled Jobs;
- admin Job debug still retains metadata and agent evidence.

After deploying `0072`, verify:

```sql
SELECT conname, contype
FROM pg_constraint
WHERE conname IN (
  'uq_personas_id_user_id',
  'fk_content_plans_persona_owner'
)
ORDER BY conname;
```

Also run a rolled-back smoke transaction proving a cross-owner insert and an
independent owner update fail, while a normal same-owner plan succeeds.

Finally, in one short transaction, lock the repaired plan and its owner Persona,
re-run the global mismatch audit, require the `0072` constraints, clear
`ownership_quarantined_at`, and leave `ownership_epoch` unchanged. Dispatch one
replacement render. Admin Job debug must show only the reporting account's
Persona snapshot; historical cancelled Jobs remain admin-only evidence.

## Rollback policy

- Before remediation begins, release 1 may be rolled back if normal matching
  plans regress.
- After Transaction A, keep a guard-capable image deployed and fix forward.
- If release 2 must be rolled back, use release-2 tooling to downgrade the
  schema to `0071` first while release-1 guards remain live, verify the legacy
  single-column FK, and only then deploy the release-1 image.
- Migration `0071` intentionally refuses downgrade after any epoch or
  quarantine has been used.
- Never restore a cross-account Persona link or restore deleted bad output to a
  user-signable prefix.
