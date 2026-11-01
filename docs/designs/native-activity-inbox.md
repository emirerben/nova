# Native activity first release

> KRI-1087 update: the in-app Activity inbox described below was removed. Kria sends push notifications only. The records, the list route and the tap path (`openNativeActivity`) remain, and the opt-in lives on the Account screen. The inbox and `ActivityUITests` passages describe the first release.

Parent context is KRI-1039. This is a narrow notification foundation, not completion of that broader routing and recovery work.

## Request and acceptance

The founder requested useful notifications after off-app work and later specified that final rendering must stay on the iPhone. This implementation adds an in-app Activity inbox for a settled native publication or a settled needs-attention transition. It does not add server rendering, push credentials, permission prompts, real customer sends, native continued-processing support, or automated engineering repair.

A compiled recipe, queued verification, or syncing upload is never video ready. Ready requires a current published native record, accepted artifact, and applicable existing output-proof receipt. A failure of a replacement can need attention while its last-good video remains playable. Creator Stop and active automatic repair remain quiet.

## Ownership and shape

`NativeActivity` stores owner, thread, item, Job, variant, ownership epoch, exact device request identity, settled generation, kind, timestamp, and read timestamp. It stores no media paths, signed URLs, customer titles, raw failure detail or transcripts. A semantic digest and deterministic ID deduplicate repeat writes.

Insert shares the existing writer's transaction. Hooks cover accepted publication, settled phone failure, contract refusal, expired repair authority, stale-device sweep and verification exhaustion. It does not depend on observer scheduling or broker delivery. The persisted inbox is the notification in this release. There is no external delivery queue or APNs transport yet.

Each list, lookup, read and navigation resolution checks current owner, plan/persona ownership, quarantine, epoch, thread active item/Job, item current Job, device request revision/digest, prepared-execution fence, current generation, and terminal state. Retry and supersession hide older activity. Database foreign keys remove activity when owner, thread, item or Job is deleted. No automatic backfill of historical completions.

The first native UI opens an Activity sheet from the projects drawer and lists ready and action-required rows. A tap re-fetches the activity, fetches the authenticated project, then acknowledges and selects it. Account identity is checked after asynchronous boundaries. A stale or deleted activity stays in the sheet with an unavailable explanation. The inbox reloads on presentation, pull-to-refresh and foreground. An older backend returns the retryable load-error UI, never fabricated activity.

## Design choice

A separate small table preserves stable identity and read state without changing transcript revisions. Read-time Job projection loses historical timestamps when records are replaced. Transcript events would couple delivery to chat sequencing and observer execution. The table keeps the notification boundary explicit and uses existing controller authority.

The independent architecture review required worker failure hooks and exact final publication generation. An independent diff review found that thread ID alone was insufficient when a thread moved to another item. The query now binds thread, item and Job together, and a real PostgreSQL regression covers the move.

## Verification and limits

Run the focused API journey and neighboring native lifecycle tests with:

```
bash scripts/check-api.sh test tests/routes/test_native_activity_postgres.py tests/routes/test_device_render.py tests/tasks/test_device_render_reaper.py tests/routes/test_background_export_verification.py -q
```

The PostgreSQL cases exercise durable repeat insertion, transaction rollback, foreign accounts, stale generation, retry, deletion cascade, thread target replacement, cancellation, changed ownership epoch, quarantine, missing artifact, missing measured proof, actual failure-report endpoint, user Stop, actual stale-device sweep, and verification-exhaustion writer. The actual publication endpoint test uses synthetic media, real checksum and ffprobe checks, real PostgreSQL ownership/publication transactions, and isolated storage transport doubles. It proves the publication hook and duplicate completion. It does not claim a new native render or production rollout.

`ActivityUITests` exercises the actual native sheet and API transport boundary with an offline response fixture. It covers selection of the activity destination over a different current project, stale lookup refusal and failed-read acknowledgement. It does not establish production APNs or OS background execution.

Follow-ups remain push registration/delivery with explicit setup authority, notification preferences, bounded retention policy, and qualified native continued processing/background final-output transfer. No cloud fallback is proposed. Migration 0127 must run before the backend serves this feature. No production migration or deployment occurred during implementation.
