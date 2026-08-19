# Conversational edit rollback

Disabling `GUIDED_EDIT_CONVERSATION_ENABLED` is the normal rollback. Restart the API after changing
the flag; the web immediately returns to the existing direction form, while conversation history
stays stored and readable by the current release.

## GUIDED_AUTO_DESIGN_ENABLED

Disabling `GUIDED_AUTO_DESIGN_ENABLED` (`fly secrets set GUIDED_AUTO_DESIGN_ENABLED=false --app
nova-video` + `fly machine restart <id>` for the api and worker process groups) is **not**
byte-identical to never having shipped it:

- New Generate calls stop auto-designing — enforcement 409s again exactly as before the flag
  existed, and `PlanItemResponse.guided_edit_auto_design` flips to `false` (the frontend reverts to
  the strict-gate button behavior on its next fetch, no rebuild needed).
- Proposals an earlier auto-design attempt already approved (`approval_mode="auto"`,
  `status="approved"`) **stay approved**. Nothing retroactively un-approves them — they read
  identically to a manually-approved proposal everywhere `status == "approved"` is checked
  (`proposal_generate_error`, `validate_approved_proposal_media_sync`, the frontend's
  `guidedEditApproved`), so Generate on those items keeps dispatching directly, flag on or off.
- A proposal an auto-design attempt already fell back on (`design_fallback` set, legacy montage
  already rendered) is likewise untouched — that Job already exists.
- An attempt that was mid-flight (`status` `analyzing`/`drafting`, `approval_mode="auto"`) when the
  flag flips keeps running to completion under the old code path already in the worker process; it
  auto-finalizes normally. Only *new* Generate calls after the restart see the flag off.

If a bad auto-design rollout needs a harder stop than "no new attempts" — e.g. a prompt regression
producing bad stories that are then auto-approved and auto-rendered — flip
`GUIDED_EDIT_ENFORCEMENT_ENABLED=false` instead (or in addition): Generate falls back to the legacy
clip path unconditionally, and `GUIDED_AUTO_DESIGN_ENABLED`'s branch in `generate_item` is dead code
whenever enforcement is off (it only runs where enforcement would otherwise 409).

Only use the data step below before rolling the application itself back to a release older than
`0.33.3.0`. Those readers do not recognize the `briefing` status. The update changes only that status
to the older reader's `failed` recovery state; proposal version, typed brief, conversation, draft,
and last approval remain byte-for-byte in the JSONB envelope.

1. Disable the conversation flag and restart the API so no new briefing rows can be written.
2. Preview the exact scope:

```sql
SELECT count(*)
FROM plan_items
WHERE edit_proposal->>'status' = 'briefing';
```

3. In a transaction, convert only those envelopes and verify that none remain:

```sql
BEGIN;

UPDATE plan_items
SET edit_proposal = jsonb_set(edit_proposal, '{status}', '"failed"'::jsonb, false)
WHERE edit_proposal->>'status' = 'briefing';

SELECT count(*)
FROM plan_items
WHERE edit_proposal->>'status' = 'briefing';

COMMIT;
```

4. Roll back the application only after the final count is zero. If the update scope differs from
   the preview, issue `ROLLBACK` instead of `COMMIT` and investigate.

The old UI will reopen these items in its editable direction form using the preserved `brief`. A
later forward deploy can still read the preserved conversation fields unless the creator replans
the item while running the old application.
