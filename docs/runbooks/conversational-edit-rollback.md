# Conversational edit rollback

Disabling `GUIDED_EDIT_CONVERSATION_ENABLED` is the normal rollback. Restart the API after changing
the flag; the web immediately returns to the existing direction form, while conversation history
stays stored and readable by the current release.

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
