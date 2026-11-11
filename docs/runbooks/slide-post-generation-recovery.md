# Keyed SlidePost generation recovery

`SLIDE_POST_GENERATION_RECOVERY_ENABLED` defaults to **false**. This disables
new keyed admission and automatic recovery; GET SlidePost advertises
`generation_idempotency_version: 0`. Existing receipt reads and exact-key
replays remain available and never publish. Legacy keyless Generate is unchanged.

Deploy in this order; this document does not authorize or perform deployment:

1. Apply migrations 0133, 0134, and 0135.
2. Update **all** API, beat, and render workers. Drain old workers completely,
   including active and reserved tasks. Old workers do not implement the claim.
3. Confirm the permanent keyed claim and late-write fence are installed on the
   generative task entry. They apply regardless of the recovery flag.
4. Enable the flag only after that worker rollout is complete. The strict keyed
   endpoint now admits new requests and advertises capability 1.
5. Release the client using the new endpoint, UUID, and required nullable
   `expected_current_job_id`. A stale target fails before minting another Job.

The receipt, immutable first-render draft/assets, Job, and current target commit
together. A UUID is global; replay must match owner, item, version, and original
expected target. Replays observe the original Job after terminal completion,
successor target, or Job deletion. Receipt GET refreshes status without dispatch.

Pending publication uses the existing queue and stable Job task ID. A durable
attempt budget and backoff are reserved before broker I/O: at most five attempts,
with 30, 60, 120, 240, and 300 second retry windows. A queued request still
unclaimed after the last window becomes an actionable dispatch failure. Broker
exceptions remain unknown; they do not prove rejection or authorize a new Job.
A positively acknowledged queued Job is not republished or exhausted by that
unknown-attempt budget. It has a separate 35-minute queue policy deadline;
expiry reports `slide_post_queue_timeout`, not an unknown broker outcome.

Every delivery must win the permanent `pending -> running` receipt claim and
`queued -> processing` Job transition in one transaction before generic worker
entry. Duplicate deliveries never compute. Claims are never reset or reclaimed.

The worker stores its exact result before publication. Recovery can finalize
that result through the existing contract checks without rendering again.
Recovery reserves one of five durable finalization attempts before publication,
with the same bounded backoff. Concurrent observers cannot reserve the same
window. Publication exceptions are isolated per row; exhaustion reports
`slide_post_publication_failed`. A commit followed by response loss is observed
as completed on the next scan. Neither failure path recomputes the render.
Completion updates Job, receipt, and the matching current draft's rendered
version atomically. Newer targets and newer draft contents are preserved.

A running request without a persisted result after 35 minutes is cancelled by
policy and becomes `slide_post_render_interrupted`. This deadline is **not proof
of worker death**. The terminal receipt rejects late publication by a paused
worker. The original UUID remains failed; only an explicit new Create action
authorizes a different intent. There is no automatic recomputation of uncertain
work and no provider call in the recovery scanner.

The minute sweep uses separate bounded pending/running budgets. Turning the flag
off stops new admission and recovery, but does not disable claim/write fences or
discard receipts. Pending and interrupted receipts remain observable.

Regression evidence lives in `tests/routes/test_slide_post_delivery_postgres.py`
and `tests/routes/test_slide_post_generation_receipts_postgres.py`. Run against
an isolated migrated PostgreSQL database with broker and renderer calls replaced
and paid/external network blocked; never use a shared production database.

Failed Job buckets normalize to SlidePost state `render_status=failed`; the
receipt preserves the precise `job_status`. Failure evidence enables explicit
Create only; recovery never creates a replacement UUID.

## Saved-version catch-up (migration0136)

A Save or compose on a keyed Job writes a separate immutable render revision in
the same transaction as the draft. Its unique identity is the parent request and
saved version; its UUID is the output generation. These saves bypass legacy
immediate rebuild dispatch, including the first-upsert/finalize interval. The
first receipt retains its original version and immutable inputs.

A pending revision waits for first-generation completion. Waiting parents do
not consume the eligible dispatch budget. Parent failure, missing/deleted target,
Stop, ownership changes and newer saved versions settle obsolete revision
metadata without failing a newer Job. Current revision failure uses variant
failure semantics and preserves the Job's last accepted artifact. Stop wins in
GET status immediately, before the observer settles child metadata.

The existing `rebuild_slide_post_variant` task accepts the keyed revision ID.
Before any legacy worker wrapper or rendering, its keyed entry validates the
Job/revision binding and wins a permanent SQL claim. It reads the revision's
snapshot and stages its exact result. Duplicate delivery cannot render again;
no-result policy expiry never reclaims the claim. Persisted results can recover
without rendering. Publication uses the existing cloud contract verifier with
fresh evidence, then atomically commits variant output, revision completion and
only the exact current version's rendered stamp. Rejected replacement evidence
preserves the previous playable artifact.

Revision publication and finalization each have five separately reserved
attempts. Acknowledged queued time starts at publication, not the earlier Save
or parent-generation start. Running policy deadline remains35minutes. Older
revision timeouts affect only their metadata; v3 cannot be failed by v2's timer.

Cloud mutation permission is not cloud execution permission: device-bound Jobs
and variants are explicitly excluded from this server renderer. Policy-blocked
Saves still persist and expose a reason through SlidePost validation errors.
Device-owned output remains the phone's responsibility. These revisions do not
create another first-generation Job or silently retry an unknown paid intent.

Keyed preview, cover and bundle keys include the immutable generation. A late
superseded worker therefore cannot overwrite a newer generation's bytes. Source
and sticker fallback cache identities include object generation. Complete pinned
derivatives use a separate `complete-v1` namespace, so a legacy derivative that
dropped a sticker cannot masquerade as a complete pinned result. Pinned sticker
download failure aborts the keyed result rather than silently omitting it.

Apply0136 before enabling the gate, and update/drain every API and worker as
above. No new queue is introduced. Legacy keyless rebuild behavior remains
separate. Native execution and production deployment are not part of this
backend evidence.
