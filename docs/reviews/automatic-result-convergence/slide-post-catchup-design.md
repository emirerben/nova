# Keyed SlidePost saved-draft catch-up — review proposal

Status: reviewed design implemented in migration0136 and the keyed revision service; final independent snapshot review pending. The original queuedv1/savev2 red is now covered by passing focused checks. See backend-evidence.md for final execution results and limits. No overall/native/deployment signoff.

## Reproduction

`tests/routes/test_slide_post_delivery_postgres.py::test_saved_newer_draft_before_first_variant_gets_one_fenced_catchup` admits keyed v1, performs actual authenticated PUT saving v2 while no variant exists, stages/finalizes the immutable v1 result, then runs the actual observer twice. It proves v1 identity remains intact and v2 is not falsely stamped, but fails because no rebuild publication occurs (`0 != 1`). Log: `/tmp/nova-slide-catchup-red.txt` (1 failed,27 deselected,4.35s). Broker/render publication is mocked; no provider or rendering ran.

Current cause: `plan_items._maybe_rebuild_slide_post` returns without a slides variant; the first worker correctly reads the receipt's immutable v1 snapshot; completion correctly refuses to stamp v2; completed receipts leave the delivery scanner. Saving during the narrower first-upsert/finalize window can already start a separate legacy rebuild, introducing a token overwrite race too.

Existing `dispatch_slide_post_edit` is not a durable outbox: `PendingVariantPublish` is an in-memory callback/rollback descriptor. Its `.delay` uses a normal task ID; `_claim_creator_craft_generation` deduplicates only the creator-craft task prefix, otherwise it returns legacy. Blindly calling this dispatcher from first completion is insufficient.

## Proposed bounded architecture

Keep first-generation receipt identity and result immutable. Add a **separate keyed SlidePost render revision receipt**, scoped only to Jobs already backed by a first-generation receipt. No new queue and no general video/provider rewrite.

Suggested table `slide_post_render_revisions`:

- UUID primary key and immutable `render_generation_id` (same UUID is sufficient).
- Parent client-request UUID, owner, item, Job, ownership epoch, exact draft version; unique(parent request,draft version).
- Immutable draft/asset generation snapshot and digest; preserve the same ownership validation as first generation.
- Requested render destination / native admission policy context, captured by the authoritative Save, so a later observer cannot infer permission to revive a forbidden cloud renderer.
- Pending/running/completed/failed/superseded state; separate publication and staged-finalization attempts/next-observation timestamps, acknowledgement, claimed-at, staged result.
- No FK that erases the historical Job pin on Job deletion; owner/item erasure behavior matches the first receipt.

The current first receipt may record the latest authorized save version (or reference its revision receipt) as metadata. This does not replace original draft_version/request_digest/job_id. Never reuse the original client UUID for a different generation intent.

### Admission and save composition

For keyed Jobs, PUT and compose must stage/update the authorized render revision in the **same transaction as the durable draft save**, using canonical Plan→Persona→Item→Job→first receipt→revision locks. Capture the exact saved draft and asset generations. A duplicate save/version returns the same revision. Do not start a variant rebuild before the first receipt is completed. Coalesce pending older revisions to superseded when a newer authorized save exists; never reclaim running work.

All keyed save paths must bypass legacy immediate dispatch, including the interval where first output has been upserted but not finalized. This prevents first completion from overwriting a newly stamped rebuild generation. Other code that writes SlidePost drafts must either stage the same revision or explicitly declare that it only proposes data; inventory those writers before implementation. A reconciliation query repairs an interrupted observer from the durable Save revision, not by inventing new user intent.

Native/device-only or cloud-disabled saves remain saved and use the existing phone render path. The revision must not authorize a cloud task merely because it has a v2 draft. Existing mutation and execution policy checks remain mandatory at admission, publication, and worker entry.

### Publication and worker execution

After the first receipt completes, the observer atomically stamps the slides variant with the exact revision generation and reserves publication before broker I/O. Use existing `rebuild_slide_post_variant` task/queue with an additive revision identifier, or a keyed wrapper invoking its shared render body; do not call the original first-generation orchestrator again. The worker must win a durable permanent pending→running revision claim before any write or compute. Task identity is deterministic but is not treated as a substitute for the SQL claim.

The keyed rebuild reads its revision's snapshot, not the latest mutable draft. It stores its exact result before final publication. Unknown broker outcomes retry only still-pending revisions under the existing bounded budgets. Running claims are never reclaimed. A staged result can finalize without rendering; a no-result deadline becomes explicit policy failure, preserving the original revision. The existing legacy explicit rebuild path stays unchanged.

Reuse the first receipt's established policy values unless review finds a reason to differ: five publication attempts with bounded backoff; acknowledged queued deadline35minutes; five staged-finalization attempts; running no-result deadline35minutes. No timeout is evidence authorizing recomputation. Per-row errors must not abort unrelated observations.

### Immutable media and final publication

Add an optional artifact-generation argument to `_build_slide_post_result` for keyed first/revision renders. Preview, cover, and bundle paths must include that immutable generation (`.../renders/{generation}/...`); old fixed paths remain only for explicitly legacy calls. A late v2 process can then write only v2's objects and cannot overwrite v3 bytes after losing its DB fence.

Normalized derivatives remain content-addressed. Where a trustworthy content fingerprint is absent, include exact object generation in fallback identity for both slides and stickers. Verify same path + changed generation produces different cache keys; no actual storage/render execution is required.

Pinned slide downloads already use generation-bound storage reads. Pinned sticker download currently swallows failures and drops the sticker; keyed complete-render mode must fail closed on a missing pinned sticker. Keep any intentional legacy best-effort behavior explicitly separate. Never stage a supposedly complete result or stamp a version after dropping an authored snapshot asset.

Final publication locks the canonical graph and requires: exact owner/epoch/current Job, revision still running, current variant generation equals this revision, and saved draft version still equals the revision version. Commit the result, revision terminal state, and rendered_version atomically. A newer saved draft or target cannot be stamped or overwritten. Superseded output is retained as historical evidence only; the latest authorized pending revision proceeds through its own receipt. First-generation completion must preserve an already newer generation token if a pre-rollout/in-flight legacy save created one.

## Tests required before implementation

1. Existing queuedv1/savev2 red: one revision generation published, original first intent never rerun, v2 stamped only by v2 publication.
2. First upsert paused, v2 save, first finalize: no token loss or duplicate catch-up.
3. v2 running, v3 saved; late v2 result: distinct artifact paths, no v3 byte overwrite or stamp.
4. Same-path changed source/sticker generation without fingerprint: distinct normalized identities.
5. Pinned sticker download failure: no staged complete result, no ready/version stamp.
6. Concurrent observers and duplicate deliveries: one permanent revision claim; reserved bounded publication/finalization budgets.
7. Commit before publish and lost publish acknowledgement: original revision recovered, no extra Job/first-generation invocation.
8. Killed revision worker after claim versus after result staging: policy failure versus exact-result recovery, no recompute; late writer rejected.
9. Current Job/ownership changes, deleted Job and cloud/native policy changes: no stale mutation or forbidden cloud publication.
10. Legacy explicit rebuild remains behaviorally unchanged; keyed explicit save uses the durable revision path.

## Ownership and rollout

Backend owner: models/new migration, keyed receipt/revision services, localized plan_items Save integration, generative SlidePost body/worker branch, isolated tests and contract changes only if exposed fields are necessary. Native owner must verify automatic phone convergence for policy-blocked server revisions independently. Parent owns formal model/evidence and publication. Independent reviewer audits this design before implementation.

Rollout gate stays false. Extend the existing migration→all API/beat/workers updated/drained→enable sequence to cover revision workers and immutable output keys. Do not enable catch-up on mixed workers. No production release or native execution is authorized by this design document itself.
