# Read recovery continuation

## Immutable preceding checkpoint

Reviewed result recovery, saved-version catch-up, atomic AI journals and saved-song reconciliation are committed at `62ac28e0909f69fb2531dc4101a7be6081ef7cee`, tree `0f518899dfd20b726b7a455500da6c4390d18561`, based on `05d9720d8b3f838f8b91963e183eb7206c34e538`.

Its component evidence is recorded in backend-evidence.md and slide-post-ai-evidence.md. Final combined native, aggregate and model gates have not run. Contract metadata follow-up: `/tmp/slide-ai-contract-metadata-green.log` records 26 passing contract tests; `/tmp/slide-ai-combined-contract-check.log` records successful combined generation verification. The metadata change intentionally supersedes earlier CLI/OpenAPI fingerprints.

## Read recovery source checkpoints

Device read recovery and nine new regression methods are committed at `86e1afb04c2b1104ad52eee4ab9d5d78a0c2885c`, tree `2aafa93d10cf5bc70a451984ff0e290395dceca2`. Independent static review approved the scoped two-file delta. No native test has executed on this checkpoint.

Review source keeps a slow, read-only follower after the fast read budget expires, with one transport reader, response ownership, server pacing and dismissal cancellation. An unchanged old ready plan cannot complete a newer request. Seeded initial failures also recover. UUID comparison is semantic, so native uppercase and server lowercase forms name the same thread. Foreground and cursor/project signals can rearm paced reads.

Gallery/device status must retain one coalesced five-minute read after twelve fast retries. A failed read cannot permanently strand an otherwise relevant result. Stop, owner replacement, settlement and shutdown cancel or fence pending work. Retry-After must survive foreground and competing readers.

Explicit Resume authorization is tied to its original account, key operation, durable intent operation, request identity and generation. A newer Stop or changed target cannot inherit it. Stop retains server pacing while cancelling authorization. A separate transport reader remains owned until its underlying GET exits, even after a UI ticket is cancelled; this prevents overlapping reads and preserves late Retry-After responses. Valid long hints are retained; invalid or nonrepresentable hints use a finite slow wait.

Review mutation/approval helpers carry a captured scope check through each await, retaining distinct UI-storage and token-derived transport account identities. Six owner-required API methods also forward captured identity to the actual request refresh/send validator; a UI-only precheck cannot prevent replacement-token dispatch. Unsupported clients fail closed. Static search found no existing custom planSnapshot fixture override; the chat UI fixture uses concrete KriaAPI.

These changes introduce no new generation, upload or export dispatch route. Existing execution authority checks still apply after reads. Ten new Review and nine new Device regression methods are written but remain unexecuted under the coordinated native/package/model hold. Run all 20 ReviewPlanTests and all 48 DeviceRenderTransientTests because their shared reader/clock setup and sequencing changed, in addition to the preceding native selection and AI/song cases. This is source selection, not discovery or execution proof. Static review is not runtime proof.

Written controls cover exhaustion followed by readiness without another lifecycle event; unchanged old same-job readiness; wall-time slowdown; seeded recovery; sole-reader sequencing; foreground and cross-call pacing; UUID case/wrong thread; account replacement; dismissal/shutdown; follower reuse; exact Resume after 429; changed identity/generation; later Stop; late 429 from a stopped read; long and invalid hints; and token replacement at the six Review transport boundaries. Scoped `git diff --check` passed.

## Controlled main integration

The original next base was `e090eb668124fee58cfc74c34e662ac78e268342`. It was superseded when Cunda #2126 landed at `70bff5f3738ca8f6873e3a85ddd9fbad374ada49`, verified tree `684c329c20e001ce307ed2a217d9051e9177658d`. A successful `git fetch origin main` observed tip `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`, whose only delta from that merge is release metadata. The ancestry from e090 through #2109 to Cunda was verified locally.

The read-only legacy `git merge-tree` preview found 22 textual conflict blocks, all in `NativeEditorSession.swift`. Twelve files overlap the checkpoint and this main: contract CLI, config, plan-item routes, generative and Kria runtime tasks, Services, ChatUITestTransport, ChatWorkspaceView, NativeEditorSession, NativeEditorView, generated OpenAPI, and NativeEditorSessionTests. A clean textual merge in the other files is not semantic evidence.

Main adds source-replacement ownership/approval retirement, suggestion cards and caption processing changes. Preserve those semantics alongside immutable result identity, strict request claims and saved-version publication fences. Retain strict AI's canonical owner/item locks plus Thread NOWAIT/savepoint: main still has Thread-to-Item mutation paths. Regenerate contracts from the combined source instead of selecting either generated file wholesale.

Independent backend review identified a combined-source regression to prove before acceptance: a take replacement can preserve `item.current_job_id` while recording `SourceReplacement.superseded_job_id`. `_recover_unknown_dispatch` must inspect that replacement marker before announcing the old Job ready; its current exact-target and newer-turn checks alone are insufficient. The correction and combined-source regression are pending integration, not claimed covered by old component tests.

Cunda is now an inherited main dependency; preserve its canonical lifetime, Save-attempt and receipt ownership guards, without replaying it manually. Speed and split remain separately coordinated incoming dependencies. This checkpoint is not publication/merge readiness and does not authorize private-project mutation. Required final-source gates remain independent review, affected backend tests, native compilation and named test execution, model correspondence and verification, aggregate checks and hosted CI.

## Remaining automatic-observation gaps

Independent read-only audit after the Review/Device correction found three further P1 liveness gaps. These are current source findings, not executed reproductions, and block a broad automatic-convergence claim:

| Path | Remaining gap | Required continuation |
| --- | --- | --- |
| Native latest-job adoption | Pending ticket survives fast retry/window exhaustion without a timer; the new-version watcher separately stops after 64 ticks. Existing foreground recovery targets another retry mechanism. | One slow read-only continuation fenced by canonical editor lifetime, owner, thread/job and whole-Save ownership; preserve server cooldown and dirty content. |
| SlidePost AI | Zero read budget and the 40-tick observer limit can strand a later completed original result. | Persist a slow original-UUID observation deadline without resetting fast budgets or resuming POST. |
| SlidePost generation receipt | Zero read budget clears isRendering and stops the workspace poller, even for an uncertain original submission. | Slow original-receipt GET and authoritative state reconciliation; no new UUID or generation request. |

Each needs exhaustion followed by eventual readiness without reopening, long Retry-After, one in-flight reader, account/target replacement, shutdown/Stop and stale-completion controls. Unknown paid outcomes remain observation-only. The existing explicit Check original/Create affordances do not satisfy automatic liveness by themselves.
