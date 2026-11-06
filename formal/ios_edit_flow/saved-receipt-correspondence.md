# Saved render receipt correspondence

Reviewed native revision: `5621edd443a06d6323e69f706ba225884013e67a` (product fix `f58b9b426ea3f3dac8815e76267f3f7124c86007`). Native implementation was not modified during this formal pass. The unchanged main comparison remains `56e21de1ab785c99f30cd09e5c3e2d7466240af2`; later merged main, including #2117 at `19cc8fdcaa549bdb831adfdb0582b8bea609f206`, requires controlled integration and fresh affected tests before publication.

## Outcome and scope

Updated, bounded verification. `SavedRenderReceipt.tla` complements the existing lifecycle model; it does not replace it. The prior lifecycle PASS described no export receipt or replacement editor. That old pass could not establish these properties.

The companion enumerates one key, two immutable requests/generations, one Save generation advance, a current or retained eligible file, one active reconciliation with a queued/coalesced reread, and two editor identities with a suspended Save completion. Reads and navigation may interleave at every modeled step. The graph is finite without limiting how often status reads or export attempts occur. Request numbers stand for immutable recipe identities, not URLs. File identity means the recipe request whose bytes it contains. Missing files, failed verification and needs-attention are excluded from eligibility by the native receipt reader; this model does not create those states or prove file contents.

## Mapping to indivisible native steps

| Model action/state | Native source and reviewed behavior |
| --- | --- |
| `Commit` / `generation` | `NativeEditorSession.saveOnce`: acknowledged commit generation becomes `pendingDeviceRenderGeneration`; this does not establish which cached render belongs to it. |
| `StartRead`, `Coalesce`, `queued`, `unresolved` | `DeviceRenderSessions.reconcile`: a ticket owns the read; another caller queues a reread and returns immediately. `unresolvedRereads` spans the defer/Task scheduling gap. |
| `Status` | `DeviceRenderSessions.apply`: receives R2/G2, clears `acceptedRenderBindings` and the old presentation before `previous.cancel()` can suspend. `read = cancel` represents the first controlled suspension. |
| `Replace` | `apply` assigns the replacement coordinator and `requests[key]`, then awaits its snapshot. `read = snapshot` is the second controlled suspension. |
| `Snapshot` | After snapshot and ticket/owner checks, `apply` accepts the immutable request/generation binding. The eligible presentation is represented as appearing at this step; actual code may expose it later through checked observation, which is a refinement that cannot enable export earlier. The ticket still prevents export at this step. |
| `SameStatus`, `FailedRead` | A same-generation read may retain a matching file. A failed GET preserves old state; it cannot make an old binding equal the new acknowledged generation. |
| `Finish` | Reconcile's defer releases the ticket. A queued reread retains `unresolved` until the next read owns a ticket. |
| `Receipt`, `Settled`, `Export` | `savedRenderReceipt(for:)` reads binding, request and eligible presentation in one main-actor turn, rejecting active/queued reads. `NativeEditorSession.savedEditDeviceRenderFile` checks receipt base generation against the saved generation and document/published generation. The model uses only the pre-publish document-generation case; the native published-generation alias remains a source/test argument. |
| `BeginSave`, `Navigate`, `FinishSave` | `saveOnce`, render-not-started branch: capture editor A, await committed variant read; editor B may load; a post-await `saveIdentityMatches` guard prevents writing A's retry state into B. The model represents job/thread/variant/document identity with editor number. |

The existing model's `EdSave`, `EdRefresh`, `EdAfter`, `Fence`, `ApplyVariant`, and `PollTick` were read against the task changes. Overlapping committed-variant/device-refresh reads add an interleaving absent from that model. The companion isolates the safety consequence; it does not model text projection or read latency. The 60-second capabilities reuse still depends on server status authority and the explicit fresh Retry path. TTL expiry and cache authorization are not modeled. No full-function resync is justified by this bounded review.

## Properties and separate controls

- `ExportMatchesSaved`: at the export event, the file request renders the saved generation. A history bit records a bad event so a later Save cannot make a previously valid export appear invalid.
- `NoUnresolvedExport`: an export event cannot occur with an active read, coalesced reread, or queued task gap.
- `ReplacementEditorUntouched`: late completion of A cannot write retry state into B.
- `LegacyReceipt` mutation restores the independently assembled old-generation/file behavior at either replacement suspension. It must violate `ExportMatchesSaved`.
- `AllowUnresolved` removes only the unresolved-read fence. A valid same-generation file during a read must violate `NoUnresolvedExport`.
- `LateSaveWrite` removes only the post-await editor check. It must violate `ReplacementEditorUntouched`.
- `NeverNewExport` is a negative reachability control: it must fail with the correct model, proving that a settled replacement can actually be exported.

`LegacyReceipt` is a semantic old-behavior control, not a claim that removing one current Swift guard alone recreates all historical code. The native failing-first test separately reproduced both real suspension windows before the fix. The companion is manually mapped TLA+, not extracted Swift verification.

## Drift and exclusions

Original coverage sync remains `b12bed356ff155ede19908dec43ef89b4c0062df`. Unchanged main has 26 changed covered-function hashes; current native code has 28. The two newly drifted functions are `NativeEditorSession.saveOnce` and `refreshDeviceRender`. Existing baseline-drifted `DeviceRenderSessions.apply`, `reconcile` and related functions also contain task changes. No original hashes or global sync commit were changed: the bounded properties do not review every branch of those functions or the unrelated baseline changes.

UI visibility/typing, pending draft persistence, physical-device timing, canonical media/frame fidelity, verification/repair transport, cache expiry, owner/account changes and crash recovery are not proven by this companion. They retain the existing native tests/source review or open evidence status. No native test results from before #2117 cover integration with that merge.
