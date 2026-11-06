# Saved editor recovery after render failure

Acceptance before implementation: a cold-open runtime-v2 failed video project with a complete current saved editor snapshot offers Open saved edit and the Editor switch. Opening uses the existing read-only editor loader and exact persisted lanes. The render remains failed; no planning, retry, dispatch or save occurs on open. A no-op chat reply and reopening do not remove this access or erase same-job local changes. Failed first renders without a saved snapshot, reference-only drafts, mismatched item/job/variant/generation, unknown format and slides do not expose video recovery. Real missing media and insufficient source windows still fail honestly.

Production read-only observation on 2026-11-06: the incident job's authored video slot requested 1.933333s against a stored source-duration hint of 1.92s, in_s 0, no source_end_s, playback_rate null, unlocked, not removed. Job and variant remained failed with editor_render_recovery from November 5 20:04 UTC, before the source-window patch deployed. The variant pinned a v2 saved draft reference, while the original phone export remained recorded published. The November 6 white-text turn was a no-op and dispatched nothing. Approved admin diagnostics exposed only the pinned draft ID, not its body or base generation, and stripped the device recipe; current snapshot validity and exact recipe end/hold remain unverified. No production state was changed.

This change must apply through shared native contracts, with no project, account or title exceptions. Opening alone does not establish current-project recovery: legacy source-window preview/Save compatibility and physical iPhone Save/reopen/Photos export require independent evidence.

## Implemented contract

The full native creation projection now retains `editor_state_version`, `editor_draft` and `editor_draft_available`. Recovery requires a full snapshot matching the active item, job and selected variant, plus matching snapshot payload/base generation. A published-source generation alias needs the server's recomputed availability attestation; older servers retain the direct-generation path. Reference-only snapshots, malformed bodies, missing identities and unknown/slides formats grant no recovery. The chat offers Open saved edit and its Editor switch without relabeling the failed render.

Opening uses the existing editor loader unchanged for lane hydration. It arms the existing explicit Retry render flow only for a matching persisted failed saved edit. Ordinary unchanged Save still does nothing. Explicit retry resends the timeline through normal commit validation and generation fences; opening and Refresh dispatch nothing. Concurrent local edits survive and a second tap during the commit does not duplicate it.

The shared legacy numeric helper admits only a positive tail whose nearest 30fps ties-to-even duration equals authored duration within one microsecond, with at most half a frame plus one microsecond of padding. Strict fit/reanchor runs first. Native compilation uses the resolved original's duration for this compatibility exception; missing original duration grants no exception. Exact endpoints, explicit native spans, real freezes and non-grid overruns retain their existing behavior. An authored fractional-frame duration retains its exact clock before sources load; this does not authorize source padding. Its neighboring durations and overlaps retain their continuous authored values. Source admission runs later against the resolved original, independently of saved duration hints. Absent and JSON-null endpoints share semantics; malformed non-null endpoints are refused. The stored lanes are not rewritten.

## Executed evidence

Base: `3d35555837fccd8d06e8c1b7d627cecb7ce13f98`.

- Initial simulator attempts stopped at package cloning and object emission because disk was full. These were infrastructure failures, not a red behavioral result.
- After capacity returned, original production source files from the base were temporarily restored in this isolated worktree. The new tests using only base APIs executed: 9 tests, 15 failed assertions including one thrown `invalidTimeline`. Observed failures included lost saved-draft fields, absent unchanged retry, 0.993103448275862 playback rate, no padding, 6.933 versus 6.933333333 timeline end, and mixed-timeline refusal. Implementation and test bytes were restored automatically afterward.
- Initial candidate (superseded by the review correction below): **232 simulator tests passed**, 0 failures, on iPhone 17 Pro / iOS 26.5. Selection: SavedEditorRecoveryTests (9), SavedDraftPreviewRangeTests (5), SlidePostRoutingTests (12), NativeEditorInteractionTests (33), NativeEditorRenderCompilerTests, NativeEditorSourcesTests (25), ChatDraftStagingTests (75). This includes actual generated 1.92s video plus image timelines, decoded frame attachments at the padded tail, no-op reopening, explicit retry commit payload, source identity guards and concurrent edit retention.
- A standalone execution of the production numeric helper passed 5,042 assertions across rates 0.25, 0.5, 1, 1.3, 2, 3.5 and 4, including exact incident geometry and non-grid/long-gap controls. This supports the numeric contract, not device export.
- One extra exploratory original-duration fixture failed with APIError.unsupported before its intended check. It was removed; no behavior claim or production change was based on that result.

Reproduce the bounded simulator selection from the worktree:

```sh
xcodegen generate --spec src/apps/ios/project.yml
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria \
  -skipPackagePluginValidation -disableAutomaticPackageResolution \
  -destination 'platform=iOS Simulator,id=<available-iPhone-UUID>' \
  -derivedDataPath /tmp/cunda-recovery-dd CODE_SIGNING_ALLOWED=NO \
  DEBUG_INFORMATION_FORMAT= GCC_GENERATE_DEBUGGING_SYMBOLS=NO COMPILER_INDEX_STORE_ENABLE=NO \
  -only-testing:KriaTests/SavedEditorRecoveryTests \
  -only-testing:KriaTests/SavedDraftPreviewRangeTests \
  -only-testing:KriaTests/SlidePostRoutingTests \
  -only-testing:KriaTests/NativeEditorInteractionTests \
  -only-testing:KriaTests/NativeEditorRenderCompilerTests \
  -only-testing:KriaTests/NativeEditorSourcesTests \
  -only-testing:KriaTests/ChatDraftStagingTests test
```

Scratch evidence on the selected Mac: `/tmp/cunda-recovery-baseline-red.{log,xcresult}` and `/tmp/cunda-recovery-final.{log,xcresult}`. The green result contains retained preview frame attachments. The summary above is the durable safe evidence record; scratch paths are not portable artifacts.

## Combined local integration

The supplied backend patch from parent head `ebb71d002` was reconstructed and all seven target Git blob hashes were checked against the supplied diff before application. `git apply --check` passed. Its 617 socket-blocked offline tests and independent review are parent-provided evidence at that backend head; they are separate from the 232 native simulator checks above. Local integration introduces no changes to native implementation or selected test source. No simulator rebuild is needed for that backend/docs-only addition. Production diagnosis wording now distinguishes stored duration hints from a verified original receipt.

Current main at integration intake is `31477c8e9b0eabf5f143d4b506b9543a7c17f44c`. Its changed paths since the common base do not textually overlap this candidate. Native application source equality to tested commit `4a001e44c34aca09dda34e120c866438ae0341aa` is checked independently after local integration. The prior native results do not establish current-main backend contract behavior; the full 617-test suite and DB-backed/physical-device gates still need revision-bound integration verification before publication.

## Limits and next action

This is native simulator and offline fixture evidence, not recovery of the original production project or a physical iPhone/Photos export. The installed client build and exact pinned production draft body remain unknown. No production write, retry, regeneration, deployment, account setting or issue was changed. Integrate and review together with the backend's recomputed `editor_draft_available` hint and matching legacy compatibility contract. The parent owns publication and original-project verification. No claim that every possible editor failure is prevented follows from this bounded recovery contract.

Post-integration static checks: changed backend Python sources compile without importing application code; scoped Ruff lint and format checks pass; `git diff --check` passes; journey mapping reports no unmapped paths; release-metadata ownership passes. No reliability gate maps to the changed paths. Full preship is not claimed: a revision-bound PR request-evidence record and current-main backend runtime test rerun remain pending, as do database-backed Save/reopen and physical-device/rollout verification. No PR, issue, push or deployment was created.

The native application tree hash is `e96b315aada019529d88078feb4bfc78bb8ec9c9` both at tested native commit `4a001e44c34aca09dda34e120c866438ae0341aa` and after rebasing the combined candidate onto `31477c8e9b0eabf5f143d4b506b9543a7c17f44c`. The native package sources and all seven selected simulator test files are byte-identical too. The 232-test native result therefore retains its source binding; this equality does not establish current-main backend behavior or original-project recovery.


## Review correction — 2026-11-06

Independent review found three gaps in the initial candidate: JSON null skipped
legacy eligibility, stale duration hints chose the projection clock, and a recovered
tail snapped neighboring legacy durations/overlaps onto the frame grid. Tests were
added before the correction. `/tmp/cunda-review-red.{log,xcresult}` executed eight
tests with 25 failed assertions, including 0.993103x/0.982759x rates, missing holds,
and altered off-grid timing. All prior evidence and the failed exploratory fixture
record above remain preserved; the earlier 232-pass result did not cover these gaps.

The correction removes duration hints from clock selection. An authored fractional
30fps duration keeps its value and a continuous timeline cursor, without rounding
neighbors or transitions. This clock selection grants no source compatibility.
Compilation uses the bound original to restore the authored rate/span, tries strict
fit/reanchor first, and only then admits the existing half-frame tail exception.
Missing original measurements cannot grant padding. Missing/null source endpoints
are equivalent; invalid non-null endpoints fail instead of becoming legacy slots.
Stored authoring is unchanged; no endpoint is injected.

The first corrected neighboring run caught an overly broad measured-duration
requirement on an ordinary strict-fit fixture. That restriction was narrowed while
keeping original measurement mandatory for inferred padding. The intermediate
failure remains in `/tmp/cunda-review-green1.{log,xcresult}`.

Final bounded simulator result: **238 tests passed**, zero failures, at
`/tmp/cunda-review-green2.{log,xcresult}` (same seven classes as above;
SavedDraftPreviewRangeTests now has 11 tests). This covers actual measured 1.92s
video, shorter/longer/missing/null saved hints, original 1.90s refusal and 2.0s strict
fit, missing original measurement, null and malformed endpoints, an off-grid 1.91s
neighbor with 0.157s overlap, and independent text/audio times. No private media or
production project was accessed.

`Tests/Fixtures/saved_editor_legacy_wire.json` is generated from actual backend
`prepare_authored_editor_commit` and `editor_sections` output with JSON serialization.
The backend regression compares that fixture byte-for-value, checks null endpoints,
and verifies exact video/transition/text clocks. Native reopens the same bundled
snapshot through DraftSnapshot and compiles it, with a separate clip-audio lane to
verify its off-grid timing too. This is an in-memory Save preparation and serialized
snapshot contract test, not a database-backed Save/reopen transaction.

Only a backend test was added; backend production files are unchanged. The new
serialization test plus existing frame-padding suite passed **38 offline tests**
(`/tmp/cunda-review-backend.log`), with connect/connect_ex/create_connection blocked
before any app import and a clean environment containing dummy credentials. Scoped
Ruff lint/format and diff checks pass. Parent separately reported 617 backend tests
and green backend preship on base `31477c8`; those were not duplicated on Mac.

Publication remains unauthorized. Physical iPhone recovery, pinned private draft
and immutable original inspection, database Save/reopen, Photos export and rollout
remain unverified. Speed draft #2125 is separate and unmerged: integration must
reconcile NativeEditorSession/ChatWorkspace changes and check conditional-304 and
late-response recovery behavior when that branch is combined. No production or
settings changes, push, merge, source upload or issue update occurred.

The 238-pass source binding is native application tree
`7364702402611518e321c9b8bbbe92e6c3880df2`, iOS tests/fixtures tree
`00693a2d20a8921b1ca1c948a0d80af778990840`, and native packages tree
`a8ae4da503f219d9e6d1573556d68dcd7eb64af5`. Only the evidence document and
backend test assertions/formatting changed after that native run. The new backend
assertions were included in the final 38-pass offline execution.

## Retry-generation and strict-fit correction — 2026-11-06

A second review found that retry intent was marked as a dirty timeline. On a 409,
the normal Save rebase could therefore copy unchanged G1 lanes over G2 and resend.
It also found that original-bound strict fits were still gated on fractional-frame
clock selection: 1.9s with hint 1.888s and original 2.0s slowed to 0.993684x.

New tests preceded implementation: `/tmp/cunda-conflict-red.{log,xcresult}` executed
25 tests with 14 failed assertions. They reproduced the stale second commit, lost
G2 trim/reorder, retry-only dirty state, and the millisecond-aligned slowdown.

Retry now passes explicit submission sections without changing genuine dirty
sections. A retry 409 reloads the newer generation, overlays only actual local
work, and stops with an explicit review/retry state; it never auto-resubmits.
Concurrent local work remains unsaved, duplicate taps stay single-flight, and an
ordinary Save still has its existing genuine-edit conflict/rebase behavior.
Editor-state export continues to send only genuine dirty sections. Original-bound
strict-fit rate/span correction is independent of clock precision; explicit source
endpoints, native moving-span markers and freezes retain their separate semantics.

The first compile attempt caught an editor-state export caller missing the new
explicit-section argument; it was corrected before execution. The expanded run
`/tmp/cunda-conflict-green2.{log,xcresult}` then ran 383 tests: all 252 session tests,
all recovery/preview/interaction tests passed, with one compiler expectation failure.
That fixture expected a 3s moving span plus hold for a 3s slot at 2x despite a 6s
original. Its corrected expectation uses duration × rate (6s) with no hold; an added
backend parity case verifies the same contract. The focused backend serialization
and strict-fit controls passed 3 tests offline. No backend production code changed.

The preceding 232/238 native and 618 backend results remain historical evidence;
final verification must bind this correction and current-main integration to their
new source trees. Local preship/body and exact-head logs record that final run.

Parent supplied a separate read-only physical-phone observation: installed build
0.1.0 (1902) still displayed the failure card and the already-white no-op. No refresh
or project action occurred. This confirms failure display only, not private draft
validity or recovery. This worker did not access the phone.

The corrected compiler suite then passed all 73 tests in
`/tmp/cunda-conflict-compiler-green.{log,xcresult}`. This closes the sole assertion
failure from the 383-test run; final integrated-head verification remains a separate
run after local main integration.

## Explicit retry failure-state matrix (before failure-action correction)

| Result | Content/dirty state | Visible action | Dispatch/reconciliation |
| --- | --- | --- | --- |
| Successful commit | Acknowledge submitted lanes; preserve in-flight edits | Existing pending/ready render UI | One commit per explicit tap; matching generation required |
| Durable commit with render recovery | Saved baseline stays clean unless genuinely edited | Retry render | Existing acknowledged sections and generation fence |
| Offline or 503 | Preserve lanes and genuine dirty state; no success claim | Failure banner must retain Retry render | No automatic retry; one commit per subsequent explicit tap |
| 409, reload succeeds | Latest baseline plus genuine local work | Explicit review/retry | No automatic resubmit of the stale generation |
| 409, reload fails | Preserve original document/local work | Failure banner must retain Retry render | A later explicit attempt retains the old generation fence; another 409 reloads, never overwrites |
| Cancellation/interrupted/uncertain response | No inferred success or loss of local work | Failure banner retains explicit retry while intent exists | No automatic resend; a later tap uses the existing base generation, so an already committed request conflicts and reconciles instead of replaying on a newer generation |

The recovery action is presentation of pending intent, not an authored edit or a
successful-save state. Ordinary failed Saves without pending render intent keep
their existing Save behavior. Existing duplicate-tap and generation guards remain.

Failure-action red control: the banner's existing action condition was extracted
without changing its behavior, then the new state-machine tests ran. They produced
11 missing-action assertions across 18 tests in `/tmp/cunda-action-red.{log,xcresult}`.
Content, dirty-state, request-count and generation assertions passed in that red run.
The same presentation condition drives the actual SwiftUI Retry render button.

The correction exposes read-only pending retry intent to the failure banner; it
keeps `.failed` and its failure copy, and never marks a lane dirty. Existing render
retry, device retry and ordinary Save behavior retain their own states/actions.
Offline/503, failed conflict reload, cancellation and timeout keep the explicit
action; no automatic dispatch is added. A later tap keeps the original generation
fence, including an uncertain response followed by a 409 for an already-committed
newer generation. Tests also cover successful/durable recovery, genuine local text,
ordinary failed Save without retry intent, and action hiding while a retry is in flight.

The failure-action correction passed all 500 native tests (zero failures) in
`/tmp/cunda-action-green.{log,xcresult}`: the same eight-class selection as the prior
495-test run, with five new failure-state tests. This is simulator integration and
SwiftUI presentation-condition evidence, not physical-device interaction or Photos
export. No backend production code or retry dispatch/reconciliation logic changed
in this correction. The prior main integration remains `ad2f4af1c`; later exact-head
backend/preship results are recorded in the local PR body and logs.

500-test source binding: application tree
`23c65d5dbaf88241389ecf09206e293ba7064f60`, tests/fixtures tree
`392d47eec278eefaba3fe4ec1ae6dd62028d1a36`, packages tree
`a8ae4da503f219d9e6d1573556d68dcd7eb64af5`. Only this evidence document changed
after the test run; the committed native trees contain the exact tested bytes.

### Conflict-reload cancellation terminal invariant (before correction)

Cancellation from generation reconciliation is an operation outcome, not proof of
navigation. Every completed retry that still owns the active editor must leave
`isSaving == false` and a non-saving terminal state; pending retry intent must be
visible after failure. A superseded attempt must not change the replacement editor's
document, dirty state, banner, pending intent or loading flag. Identity includes a
fresh load/reset of the same job and variant, not just different identifiers.
Tests cover same-editor cancellation with/without dirty text, and old success or
cancellation completing after a new job, new variant, or same-target reset, both
while the replacement is loading and after it has loaded. The prior failure-state
matrix and its offline/503/uncertain-response controls remain in force.

Cancellation/navigation red control: `/tmp/cunda-cancel-red.{log,xcresult}` executed
22 tests with 19 failed assertions. It reproduced same-editor `.saving` after
cancellation, a preview left preparing, old commit changes after a same-target
reset, and old reload responses changing replacement content/banner/loading state.

The correction assigns a load epoch to editor operations, so a reset to identical
job/item/variant/thread IDs is still a new editor. Save attempts retain one active
attempt identity through conflict reconciliation; duplicate calls cannot enter
between stages. Commit and reload results, cleanup, and outer continuations verify
ownership before changing state. Reload returns a distinct superseded result;
CancellationError while still owned is an ordinary failure, including preview
reconciliation. A shared owning-attempt exit guard converts any remaining `.saving`
state to truthful failure after `isSaving` clears. New loads invalidate old attempts
and their transient conflict flags, so old cleanup cannot clear a new load's flag.

Exit review: early duplicate/no-intent calls own no attempt; missing save context
fails visibly; acknowledged/pending render responses retain their existing terminal
states; commit errors preserve the failed state; 409 success stops for explicit
review; 409 failure/cancellation fails visibly; invalidated identities return without
changing the new editor. Every still-owned attempt has a terminal-state fallback.
No generation fence was removed and no automatic retry was added.

The complete bounded native selection passed **504 tests, zero failures** in
`/tmp/cunda-cancel-green.{log,xcresult}`. It includes all 253 session tests and 21
saved-recovery tests, plus the prior preview/compiler/source/chat/routing classes.
The complete original failure matrix remains covered. Backend production code is
unchanged. Physical-device/private-project and rollout limits still apply.

504-test source binding: app `b1a6e45a01c6f29ffc82b344390d10676b19358d`,
tests/fixtures `3913a19389542c30c3d541bcc8f6fdd87ed7682b`, packages
`a8ae4da503f219d9e6d1573556d68dcd7eb64af5`. Native bytes were unchanged after
execution; only this evidence document was updated before the local commit.

## Post-commit async ownership audit (integrated main 05d9720d8)

The corrected pre-fix suite ran 22 tests with three failures in
`/tmp/cunda-boundaries-red2.{log,xcresult}`: stale commit cleared replacement
reconnecting state; post-commit read replaced clean text and a dirty replacement's
undo baseline. Earlier exploratory runs in `/tmp/cunda-postread-red` and
`/tmp/cunda-boundaries-red` used a fixture without armed retry and did not establish
the post-read race; their logs remain preserved.

Ownership inventory for Save/retry and its render continuation:

- `save` awaits `saveOnce`, conflict reload, and ordinary Save's second submission:
  attempt ID and complete operation identity precede subsequent state changes.
  The final song-removal submission has no following writes. Owned terminal cleanup
  is fenced by attempt ID and load identity.
- `saveOnce` awaits commit: epoch, target IDs and submitted generation now precede
  refresh-streak/sync writes as well as acknowledgement, revision, baseline,
  undo/redo, duration, gesture and pending-render mutations. All commit catches
  use the same fence before conflict, song-removal or failure state changes.
- Its authoritative post-commit read now checks epoch/IDs/committed generation
  immediately before text reconciliation (including clean baseline and undo
  rebasing). Read failure changes no state; the common fence still precedes
  render state changes. Same-editor reconciliation retains existing behavior.
- Its device refresh await is followed by the same committed-generation fence
  before pending identity and polling writes. `refreshDeviceRender` itself now
  fences capability success/failure before cached capabilities or pending flags
  and before dispatch to the app-owned, keyed renderer. The renderer owns its
  asynchronous keyed operation; no editor writes follow its reconcile await.
- Conflict reload fences success and catch before document/baseline/undo/preview
  mutation. Its cleanup is identity-owned. `saveOnce` cleanup is epoch-owned.
- Explicit device retry captures operation identity before renderer retry/resume;
  both continuation boundaries check identity, key, generation and cancellation
  before refresh or UI/poll mutations. Renderer state remains app-owned/keyed.
- Preview polling captures load identity. After status and device-refresh awaits,
  identity/generation/cancellation precede reconciliation and failure UI. Sleep
  and read-error paths only update task-local counters; terminal UI is fenced.
- `retryRender` sets its intent synchronously, awaits the owned Save, and performs
  no subsequent mutations. Ordinary Save, duplicate/generation checks and all
  existing cancellation controls remain unchanged.

New regression suspends the actual post-commit read, then resets the same target
at the committed generation, with clean text and genuine local text edits. It
asserts document, dirty state, undo availability, save/refresh UI and dispatch
counts, then undoes local work to verify the replacement baseline survives.
The existing stale-commit reset test now also establishes reconnecting state.
Private-project, original receipt, DB, physical-device/export, CI and rollout
acceptance remain pending. This audit does not claim a global review of unrelated
editor loaders or app-owned renderer internals.

## Unified load lifetime and deferred work

A public load/reset now creates one epoch; project routing passes it through its
nested draft/job loader rather than creating a competing epoch. Every asynchronous
load read (project resolution, promotion, variants, capabilities, playback, staged
draft) checks that epoch before applying data. Success/failure/retry scheduling
and loading cleanup are owned by the same epoch. Capability tasks started by a
load are cancelled on that load's exit. Source preview uses its existing sequence,
now invalidated on every load, including same-target resets.

Beginning a lifetime invalidates this session's prior poll, source-preview/quiet
retry, applied-message delay, background/load retry and new-job watch. App-owned
renderer sessions and durable user authoring are not cancelled. Scheduled conflict
reload, source-preview rebuild, applied-message clearing and load retry capture
ownership before task creation; they cannot acquire ownership merely by starting
later. New-job readiness/watch/adoption and post-load device refresh likewise
validate the scheduling lifetime. Existing source/permission/error behavior is
retained; an identity match does not imply render readiness.

The supplied boundary inventory is covered as follows:

| Family | Ownership before mutation/cleanup | Regression evidence |
| --- | --- | --- |
| Save/commit/post-commit read | Attempt + epoch/target/generation | Prior cancellation matrix; replacement clean/dirty text, undo baseline and refresh hint |
| Overlapping draft/project/job loads | One propagated epoch after all reads, catch and defer | Same target/generation and different job/variant; clean/dirty; success/failure/cancellation; replacement still loading |
| Load capability / device capability | Epoch/identity before capability or reconciliation writes | Delayed read success/failure, permissions, loading flag, no obsolete renderer dispatch |
| Poll read/device refresh/timeout | Captured identity + generation + cancellation | Ready-output old poll after reset with clean and dirty replacements |
| Scheduled conflict/rebuild/load retry/applied delay | Scheduling identity/epoch, not eventual entry identity | Deferred conflict remains a passing control; existing source-preview, retry and banner neighbors pass |
| Device retry/resume | Identity + key/generation before refresh/UI continuation | Existing device retry/session controls remain green |

Grouped red against reviewed source `6b281b67`:
`/tmp/cunda-lifetime-final-red.{log,xcresult}` ran **27 tests / 37 failures**:
24 overlapping-load assertions, five capability-load assertions, four poll
assertions, two post-commit reconciliation assertions, one stale refresh hint,
and one obsolete device dispatch. The initial poll fixture omitted a ready output
URL and was a passing negative control; the final test includes a local file URL
and reaches output application. Earlier `/tmp/cunda-lifetime-red` (24/24) and
`/tmp/cunda-lifetime-full-red` (26/32) logs remain preserved. The deferred-conflict
case passed pre-fix under its existing sequence fence; it is not claimed as a red.

Comprehensive green: **562 tests, zero failures** in
`/tmp/cunda-lifetime-green.{log,xcresult}`, including 27 saved-recovery tests,
253 session tests and the same compiler/source/clock/chat/routing/background
neighbors. No source or test change followed this run. The original private
project, immutable incident receipt, database Save/reopen, physical iPhone/Photos,
hosted CI and rollout remain unverified. Speed integration remains separate.

## Open, prompt refresh and transient UI correction

The bounded follow-up carries the initiating lifetime into prompt refresh before
any success/retry/adoption mutation, including after catch-path adoption. Retry
scheduling captures ownership and running-flag cleanup is epoch-owned. Reopening
checks identity on success and error, recomputes local dirtiness after suspension,
and returns handled for superseded results. Dirty conflict reads use the existing
new-job decision (preserve/prompt), not a full-load fallback. The view passes its
initiating lifetime into a single fallback-load/device-refresh helper; that helper
also rechecks saving and dirty state before starting. Its returned lifetime fences
the view's final output installation. Reset clears the cancelled lifetime's visible
Edit applied badge; a replacement's later announcement retains normal dismissal.

Red evidence: `/tmp/cunda-open-red` ran 33 tests with nine failures (five reopen,
three refresh/adoption, one badge); normal active-job adoption and the targeted
device-retry/reset control passed. `/tmp/cunda-open-conflict-red` ran 31 tests with
three failures for dirty conflict reads. `/tmp/cunda-open-fallback-red` ran one
test with three failures for edits made after reconciliation returned but before
caller fallback. All earlier red/green artifacts remain preserved.

The first comprehensive attempt `/tmp/cunda-open-green` was NOT green: xcresult
records 524 passes and one test-host SIGKILL in
`NativeEditorRenderCompilerTests/testLivePreviewStillDecodesAfterTrimAndUndo`.
Verification is split into smaller serial selections; no cause for the kill is
claimed. This is local fixture evidence only, not private-project or device recovery.
# Bounded open / refresh / reconcile ownership inventory

Scope: the parent-narrowed Cunda correction and direct caller fallbacks; no new automatic propagation behavior. Prior load/save/poll/device inventory remains in the tracked review report.

| Entry or continuation | Post-await mutations | Ownership |
| --- | --- | --- |
| NativeEditorView open task | fallback load, device refresh and output installation | initiating lifetime is passed into loadForEditorOpen; returned lifetime checked before output UI |
| reconcileOnOpen | loaded revision, newer-job prompt, transient refresh failure; Bool permits fallback | captures identity before read; superseded success/error returns handled; dirtiness recomputed after read; caller fallback separately fenced |
| synchronizePromptRevision | retry-loop flag and delegation | initiating identity passed to runPromptRevisionSync; checked after delegation |
| runPromptRevisionSync | refresh-success/retry cancellation, readiness adoption, permissions, draft/baseline/undo/dirty/preview; failure retry/banner | identity + sequence checked immediately after project/draft/variant reads before writes/adoption; baseline checked before authoring; after catch adoption check again |
| scheduleRefreshRetry / runRefreshRetry / resumeRefreshSync | retry task, running flag and cleanup | scheduling identity captured; retry cleanup epoch-owned; resume has no post-await writes; load reset clears owned running flag |
| loadForEditorOpen | starts fallback lifetime, load and device refresh, caller token | expected initiating lifetime checked before start; one new epoch passed into nested loader; guards before refresh and return |
| load(api), load(project), load(editorJobID), load(libraryJobID) | authoring, clean baseline/history, permission/readiness, playback and loading cleanup | previous propagated epoch guards retained; library is terminal delegation |
| adoptLatestJob / adoptActiveJobFromThread / switchToLatestJob / watchNewVersion | readiness hints, prompt, new load, device refresh and watch cleanup | prior initiating identity / propagated epoch guards; normal same-editor adoption covered |
| refreshDeviceRender / retryDeviceRender | capability/pending flags, renderer dispatch, retry banner/poll | prior identity + key/generation guards; app-owned renderer retains its own keyed durable intent |
| reloadLatestKeepingLocalEdits / deferred conflict | rebase baseline/undo/local edits, source preview and cleanup | captured initiating identity before scheduling/read; superseded result cannot touch replacement |
| startPreviewRefresh / retryPreviewRefresh / retryPreview | generation/readiness/output/text/dirty/history and timeout banner | prior identity + generation/cancellation, source-preview sequence; synchronous retry entry or terminal delegation |
| announceEditApplied / beginEditorLoad | applied badge clearing | scheduling identity; reset explicitly clears cancelled lifetime's visible badge |
| prepareSourcePreview and nested source/audio/media compilation | preview, sources, duration and failure state | existing source sequence invalidated at every load; no new propagation behavior |

Not applicable to this narrowed correction: media-gallery refresh/import/paste/export operations and chat submission orchestration are separate user actions, not reopening or fallback reloads. No new behavior is added to those paths. This inventory does not claim a whole-session or automatic-update redesign.
