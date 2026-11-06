# Read recovery continuation

## Immutable preceding checkpoint

Reviewed result recovery, saved-version catch-up, atomic AI journals and saved-song reconciliation are committed at `62ac28e0909f69fb2531dc4101a7be6081ef7cee`, tree `0f518899dfd20b726b7a455500da6c4390d18561`, based on `05d9720d8b3f838f8b91963e183eb7206c34e538`.

Its component evidence is recorded in backend-evidence.md and slide-post-ai-evidence.md. Final combined native, aggregate and model gates have not run. Contract metadata follow-up: `/tmp/slide-ai-contract-metadata-green.log` records 26 passing contract tests; `/tmp/slide-ai-combined-contract-check.log` records successful combined generation verification. The metadata change intentionally supersedes earlier CLI/OpenAPI fingerprints.

## In-progress read recovery

Review must keep a slow, read-only follower after the fast read budget expires, with one transport reader, response ownership, server pacing and dismissal cancellation. An unchanged old ready plan cannot complete a newer request.

Gallery/device status must retain one coalesced five-minute read after twelve fast retries. A failed read cannot permanently strand an otherwise relevant result. Stop, owner replacement, settlement and shutdown cancel or fence pending work. Retry-After must survive foreground and competing readers.

Explicit Resume authorization needs a stronger fence than a status-read key: a delayed action must remain tied to its original account, operation, request identity and generation. A newer Stop or changed target must not inherit it. Root review identified loss of Resume across a server wait; the first correction retained its action, and subsequent review required exact identity/generation binding before acceptance.

These changes introduce no new generation, upload or export dispatch route. Existing execution authority checks still apply after reads. Regression controls are being written but remain unexecuted under the coordinated native/package/model hold. Static review is not runtime proof.

## Controlled main integration

The original next base was `e090eb668124fee58cfc74c34e662ac78e268342`. It was superseded when Cunda #2126 landed at `70bff5f3738ca8f6873e3a85ddd9fbad374ada49`, verified tree `684c329c20e001ce307ed2a217d9051e9177658d`. A successful `git fetch origin main` observed tip `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`, whose only delta from that merge is release metadata. The ancestry from e090 through #2109 to Cunda was verified locally.

The read-only legacy `git merge-tree` preview found 22 textual conflict blocks, all in `NativeEditorSession.swift`. Twelve files overlap the checkpoint and this main: contract CLI, config, plan-item routes, generative and Kria runtime tasks, Services, ChatUITestTransport, ChatWorkspaceView, NativeEditorSession, NativeEditorView, generated OpenAPI, and NativeEditorSessionTests. A clean textual merge in the other files is not semantic evidence.

Main adds source-replacement ownership/approval retirement, suggestion cards and caption processing changes. Preserve those semantics alongside immutable result identity, strict request claims and saved-version publication fences. Retain strict AI's canonical owner/item locks plus Thread NOWAIT/savepoint: main still has Thread-to-Item mutation paths. Regenerate contracts from the combined source instead of selecting either generated file wholesale.

Independent backend review identified a combined-source regression to prove before acceptance: a take replacement can preserve `item.current_job_id` while recording `SourceReplacement.superseded_job_id`. `_recover_unknown_dispatch` must inspect that replacement marker before announcing the old Job ready; its current exact-target and newer-turn checks alone are insufficient. The correction and combined-source regression are pending integration, not claimed covered by old component tests.

Cunda is now an inherited main dependency; preserve its canonical lifetime, Save-attempt and receipt ownership guards, without replaying it manually. Speed and split remain separately coordinated incoming dependencies. This checkpoint is not publication/merge readiness and does not authorize private-project mutation. Required final-source gates remain independent review, affected backend tests, native compilation and named test execution, model correspondence and verification, aggregate checks and hosted CI.
