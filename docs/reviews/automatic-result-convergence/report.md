# Automatic result convergence

Base: `05d9720d8b3f838f8b91963e183eb7206c34e538`; isolated branch `codex/automatic-result-convergence`. Publication is unauthorized. No production writes, private media, paid provider calls, or physical-device changes are in scope.

## Acceptance (before implementation)

The current result appears without a refresh/show-new-version tap when its authoritative read succeeds. An unchanged old ready response must not finish a newer update. Responses arriving out of order must not restore an older snapshot. Dirty local work survives status reads. Transient GET failures recover with bounded attempts and backoff; cancellation stops retrying. A render/save/upload/paid request with an uncertain outcome is never blindly repeated. Terminal failures name an actionable next step.

This change is separate from Cunda's unpublished `codex/cunda-editor-recovery`. That owner controls NativeEditorSession, Services, ChatWorkspaceView, NativeEditorView, render/compiler/legacy/banner/header and associated tests. Its corrections cover prompt lifetime, reconcile-on-open dirtiness after suspension, and old badge cleanup. Those files are read-only here. After Cunda is merged into main through its separately authorized process, rebase this branch onto that main, resolve model coverage against the combined tree, then repeat native integration/aggregate gates. Neither branch's isolated results prove the combined build or original Cunda recovery.

## Why the previous proof did not establish zero friction

KRI-934 / PR #1816 addressed killer screens and explicitly deferred duration snapping and plan-level retry. KRI-935 / PR #1817 implemented automatic latest-edit loading for same-job conflicts, retaining a different-job prompt to preserve local work. Its fixture transport did not drive a real-server 409 XCUITest. These are bounded improvements, not a claim that every read/open/failure path converges.

`formal/ios_edit_flow/coverage.json` tracks selected save/poll/device/prompt sync functions, but omits editor load, reconcileOnOpen, adoptLatestJob and workspace refreshNow. The historical empty residual list applies only within its model. Scope excludes retry-cap exhaustion, planner/output verification and some liveness combinations; MaxFaults is 1 with 1–2 saves. Model assertions, executed native tests, physical output and rollout evidence must remain separate.

## Current route/failure matrix

Status legend: **risk** = current code review, not reproduced; **historical-fixed** = changed implementation present, not a fresh executed claim; **regression** = a new executable case with result recorded below; **untested** = evidence absent. Paths are relative to repository root.

| Route / failure | Current evidence and priority | Classification / control |
| --- | --- | --- |
| Chat reopen / failed saved edit | NativeEditorView `loadSession` uses reconcileOnOpen; Cunda owns lifetime/dirtiness fixes | historical-fixed candidate; combined integration untested |
| Gallery open/reopen | EditorViews opens NativeEditorView directly via libraryJobID; no chat poll implied | risk: render retry budget can exhaust without another lifecycle signal |
| New job / busy save | NativeEditorSession adoptLatestJob returns while saving, watcher has 64 attempts | risk, Cunda overlap; verify deferred catch-up in combined tree |
| Variant/project switch / overlapping loads | Native session target/sequence guards exist; Cunda unifies lifetime tokens | historical-fixed candidate; stale lifetime replay is Cunda evidence |
| Review update on same job | ReviewPlanModel waits for job ID to change; backend execute_kria_approval reuses target_job_id | P1 regression; require newer turn identity, reject old-ready negative control |
| Review overlapping reads | Snapshot assignment has no read-order fence | P1 regression; newer snapshot must survive delayed old response |
| Preview convergence | Native poll follows a newer ready generation after two sightings | historical-fixed; no original-project/output claim |
| AI changed/no-op | Native sync stages changed drafts and ignores absent/no-op document without clearing local work | historical-fixed; final combined replay untested |
| AI error / uncertain cloud dispatch | outcome_unknown is absent from periodic dispatched/accepted selection; failed turn/successor authority complicates repair | P1/P2 risk under investigation; never reenqueue unknown work |
| Timing / transient reads | Native refresh/device retries exist; slide refresh was one-shot and export aborted on one read error | reproduction-tested red on slides; bounded read-only retry/cancellation implemented, final native green pending |
| Review read exhaustion | Six failed reads stop follow and ask to reopen; reconnect/foreground recovery is not exercised by this change | P2 remaining limit; no claim of indefinite outage recovery |
| Save/render separation | Slide create saves before generate; export loop allows up to two dispatches | regression: one logical dispatch per export; GET recovery must not repeat mutation |
| Generic failure Refresh | ChatWorkspaceView generic failed card refreshNow performs GET/delta only | P2 observed code behavior; refresh cannot repair a terminal render; Cunda hot-file overlap |
| Reconnect/background/relaunch | DeviceRenderSessions resumes on network/foreground/thermal signals; coordinator persists receipts and parks transient failures | historical-fixed; indefinite outage and killed-process native journey untested |
| Gallery outage exceeds budget | Twelve automatic reads stop until reconnect/foreground/thermal event | P2 risk; service recovery with continuously-online gallery lacks proven independent trigger |
| Exports/Photos | Native exports current output or exact displayed preview with revision/dirtiness guards; Photos retries interruption once | untested physical output/transaction; no blind Photos duplicate loop added |
| Persisted revision / multiple devices | Native save rebase copies full dirty lanes; Slide persistDraft conflict recovery advances the base version then resends the full local draft | P1 remote-change loss risk, no observed user loss; field/lane-level merge semantics require separate deterministic work; native path overlaps Cunda |
| Undo cap | submittedUndoCount/dropFirst applied against capped history | risk, reproduction pending; do not call historical finding freshly reproduced |
| Undo all approval identity | Empty turn ID allows newest/id-less approval matching | risk in hot ChatWorkspaceView; no edit here |
| Unsaved exit / relaunch | Session is memory-owned while exit copy says stored on device | risk: persistence wording/contract not proven; no false crash-recovery claim |

## Evidence ledger

### Implemented controls

- Review follows a new turn on the same job only with an advancing event cursor. It rejects superseded requests, older revisions/cursors and callbacks after dismissal. Its snapshot proves **plan decisions**, not rendered video readiness. Read failures back off within the existing six-failure/ten-minute bounds; mutation submissions are not automatically repeated.
- Slide GETs use at most three attempts, bounded backoff and Retry-After handling; a server hint beyond the bounded read window ends that attempt. Cancellation, item identity, request order and mutation generation fence success and error callbacks. A successful read clears only its own old read error and preserves dirty drafts.
- One export flow requests at most one render. An existing/acknowledged render remains followed despite lagging non-rendering projections. A 200-read acknowledgement budget prevents indefinite waiting; exhausted/uncertain acknowledgement identity remains protected against redispatch. Exact ready output or a confirmed terminal failure clears the marker; an explicit later user action may retry a definitively failed render. A new version/item invalidates the old marker. Save/create/undo/proposal persistence refuse mismatched item targets.
- An unknown cloud dispatch is observed from durable exact-generation/variant readiness and output metadata. Current ownership, session, target and absence of later turns are required. Completion and deterministic review events happen under the existing lock graph. It never republishes the render or starts paid review/suggestions/successors. Unknown and dispatched scans have independent 50-row limits; unknown observations rotate by timestamp, excluding deleted turn/thread references.

### Executed regressions before fixes

- Slide first red: 2 executed tests / 4 failed assertions, including acknowledged render followed by transient GET failure and successful refresh retaining the previous error (`/tmp/slide-red.log`).
- Slide additional red: 2 executed tests / 3 failed assertions, including an existing-render lag causing redispatch and wrong-item save/create sending mutations (`/tmp/slide-identity-red.log`).
- Review delayed old snapshot test executed 2 failing assertions. Its first same-job test had an expectation instrumentation error (repeated fulfillment) and crashed; this was corrected before relying on it. The clean same-job red then executed 1 test / 1 expected failure: phase stayed updating (`/tmp/review-red-final.log`). Two intermediate builds failed while new Slide tests/helper compiled; they are not behavioral evidence.
- Backend initial red: 3 failures; output/scan controls red: 4 failures; rotation and orphan controls also failed before their corresponding fixes. Final focused offline suite: 29 passed / 57 deselected, including 17 unknown-recovery cases and selected phone observer cases. A separate independent run executed the 17 unknown-recovery tests successfully. Backend broad current and baseline both executed 90 passed / 9 blocked under identical socket/psycopg guards; exact names are in `backend-blocked-tests.txt`.
- The final pure production acknowledgement reducer was extracted and compiled by Swift on macOS; its assertions passed (`/tmp/slide-ack-harness.swift`). This does not establish the whole session/app integration.

### Gates and limits

- Web lint passed with existing warnings; web TypeScript check passed. No web implementation or browser investigation was performed. Documentation size and whitespace checks passed.
- Initial offline aggregate: 8,588 passed, 123 skipped, 2 xfailed, 16 failed, 56 errors. Fifteen failures and all 56 setup errors hit explicit network guards. The remaining media check could not find ffmpeg because the runner PATH was too narrow; with Homebrew tools restored, that exact check passed. This is not a green aggregate result. A new run uses only the uniquely named migrated scratch Postgres database, with provider/Redis/external network blocked.
- Final native app build, full unit execution and scoped UI evidence are pending. An earlier full native attempt compiled but the runner was killed before XCTest bootstrap; zero tests executed. Low disk capacity blocked the task-owned simulator. Only this task's unused simulator and old result bundles were removed, with red logs retained. Capacity later recovered and final compilation resumed on a new task-owned simulator.
- Phone model gate is blocked: the pinned URL downloaded a JAR with SHA256 `654aa50197faa48fa0136b41257270de6469d29ef323e7822ead59d2be43ce10`; the verifier requires `fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`. The integrity guard was preserved and that binary was not executed. No model-pass claim follows.
- Physical iPhone, background force-kill/relaunch with real account, multi-device concurrent Save, private Cunda recovery, Photos output and rollout remain untested. No push, PR, merge, deploy or production mutation is authorized or performed.

Completed outcomes and final pending native/model gates are recorded below.


### PostgreSQL integration evidence

The final `test_unknown_dispatch_recovery_postgres.py` executes against a separate migrated disposable database. The actual SQL query rotates past fifty unresolved receipts; a SQLAlchemy execution barrier forces two observers to finish their pre-reads before the held ContentPlan row lock is released. Exactly one durable completion/review pair is produced. Current runtime: **2 passed**. Immutable `05d9720d8` runtime loaded under its original module name, same final test file and database guard: **2 failed as expected** (no unknown receipt observations, no observer reaches the lock boundary). No render/provider/suggestion dispatch is permitted by the tests.

Commands (API working directory):

```sh
/tmp/nova-integration-db-safe-python -m pytest tests/kria/test_unknown_dispatch_recovery_postgres.py -q --tb=short
/tmp/nova-integration-db-safe-python -m pytest -p nova_unknown_baseline_plugin tests/kria/test_unknown_dispatch_recovery_postgres.py -q --tb=short
```

Logs: `/tmp/nova-unknown-postgres-final-current.txt`, `/tmp/nova-unknown-postgres-final-baseline.txt`. Earlier attempts hit a global 30-second timeout and an unreliable timed pg_stat lock-arrival predicate. The final test uses an explicit execution barrier instead of a timing verdict and a bounded integration infrastructure deadline. A same-load connection probe did not establish why the original global timeout fired; no claim of a proven resource cause is made. The first shared-scratch 2-pass result is superseded by the final separate-database result. The failed polling attempt and initial failure summary remain at `/tmp/nova-unknown-postgres-before-barrier.txt` and `/tmp/nova-unknown-postgres-initial-failure-summary.md`; the latter explicitly records that the first full log was overwritten.

The aggregate and integration databases have distinct names and receipts. Guarded runners clear credentials/disable dotenv, block external/provider/Redis networking, and assert the exact allowed local scratch database for psycopg/asyncpg. No production or saved project data is used.

### Incoming Cunda dependency and resource hold

Owner update: Cunda candidate is now `a7ed058ba570d62add9fb2ee9df2cc6aa91ade5a`, a clean descendant of `f5ab6ef`; its independent bounded review is closed, including prompt refresh, reopen/caller fallback and badge corrections. Parent-provided evidence is 107 focused native and 620 backend checks, with broader native SIGKILL unresolved. That is incoming dependency evidence, not results executed by this branch. The diff paths were inspected read-only; this branch has no textual production/test overlap with its 24 changed paths.

At the parent’s resource-coordination instruction, this branch will let its current build finish without interruption, then launch no further native build/test until expressly released. Its task-owned simulator `E4AC1B99-595C-472C-8B85-5505C43F2729` is shutdown and unused. Native unit/UI and combined-Cunda verification remain pending under that hold.


### Native resource release and final source delta

At 09:59 UTC the current build finished with **TEST BUILD SUCCEEDED** (`/tmp/nova-convergence-build-owned.log`). xcodebuild PID 36961/session 87085 and its Swift compiler processes exited. The task simulator remained shutdown and unused; no test execution was launched. Compiled receipt: `/tmp/nova-auto-update-dd/Build/Products/Kria_iphonesimulator26.5-arm64.xctestrun`.

After that build, a regression assertion/test was added before a one-line correction: an authoritative, non-stale ready slide result clears only the two acknowledgement-owned failure/wait messages, preserving unrelated mutation errors. Independent static review approved that final delta. **The build receipt predates this correction and test.** Final native rebuild, all KriaTests (including the new named cases), and the two fixture UI cases remain unexecuted under the explicit resource hold. The full native handoff is `/tmp/slide-convergence-handoff.md`. Earlier red logs are retained; deleted old xcresult bundles are not claimed as retained artifacts.

Incoming Cunda was published by its owner as draft PR #2126 at `a7ed058ba570d62add9fb2ee9df2cc6aa91ade5a`; hosted CI is running. This branch stays on main `05d9720d8`, is not stacked on that unmerged branch, and has no publication authorization.

Additional bounded limits: Review still stops after its read-failure budget and asks to reopen; slide export stops after its own read/poll budget and may require a new explicit export action after a sustained outage. The workspace can later heal its read state, but no claim is made that a completed/aborted export intent survives every outage or process death. Server active-job/version locks remain the relaunch dispatch defense; no new persistent client mutation queue was introduced.


### Final uncertain-POST correction (native execution pending)

A further static control found a concrete hole in the first candidate: generate acknowledgement was installed only after POST success. A committed render with a lost response left no marker, stopped workspace polling, and a subsequent create/export could POST again. The backend only suppresses active jobs; an already-ready first render is terminal and can permit another same-version job. The earlier single-flow uncertain-error test did not establish safety across subsequent actions.

The final correction records the submitted version immediately before POST, after cancellation preflight. A nested POST catch clears it only on definitive rejection; transport loss, in-flight cancellation, 5xx and undecodable outcomes retain it. GET failures after an acknowledged POST cannot clear the submission fence. An owned checking-state message can heal on a matching ready result without erasing unrelated errors. Four additional regressions were written before this correction: committed/lost-response then create/new-export is GET-only; cancelled response retains the fence and unrelated errors; definitive rejection allows an explicit later attempt; a post-ack GET 403 never authorizes another POST. Independent static review approved these exact branches.

These four regressions and the final ready-error cleanup regression remain **unexecuted** under the resource hold. The earlier successful native build predates **both** final corrections. No final native build, full unit or UI-green claim is made. The submitted-version marker is in-memory in this session; no durable cross-process idempotency queue/key was introduced. Cold process death/relaunch remains an explicit evidence and durability limit, with existing server active-job/version fencing as the defense.

The submitted-marker correction also includes `session.isRendering` in the workspace polling task's identity. Its previous key watched raw server status and busy/assets only; a fast uncertain response could coalesce busy notifications and leave that key unchanged, so the existing task never restarted even though the new marker required GET polling. Independent static review confirmed the dependency and approved the one-line key correction. This final view change is also uncompiled/unexecuted under the native hold.


### Completed backend aggregate and isolated rechecks

`make verify-kria API_PYTHON=/tmp/nova-auto-db-safe-python` completed: **9,749 passed, 11 failed, 13 setup errors, 7 skipped, 3 xfailed**. All contract/OpenAPI drift checks passed. This is a red aggregate, not an all-green result (`/tmp/nova-auto-aggregate-db.log`). The 24 failed/error nodes were then rerun, unchanged, against the task database plus a new private Redis instance, with all other networking blocked: **17 passed / 7 failed** (`/tmp/nova-auto-infra-recheck.log`). This resolves the 13 Redis setup errors, two Redis-dependent sweep failures, one PostgreSQL connection-cap failure and one outer 30-second replay timeout without changing production code or relaxing their assertions/caps. The original PostgreSQL rejection explicitly said too many clients; the replay timeout fired while waiting for its subprocess. Their precise load contributors are not established.

The remaining seven tests exercise evidence decoding that explicitly requires non-root Linux (`render_review_evidence._resource_limits_supported` / `_run`). On macOS the initial review is unavailable; six race wrappers assert ready evidence, and that assertion is caught as retryable/pending before the intended race mutation. The identical seven nodes with immutable `05d9720d8` runtime, same private infrastructure: **7 failed** with the same statuses (`/tmp/nova-auto-infra-baseline.log`). Their source/decoder safety code is unchanged. Decoder resource limits were not bypassed. These need the supported Linux environment; no evidence-ready/media verification claim is made here. Exact names are in `backend-platform-limited-tests.txt`.

The private Redis is on a unique loopback port with no other worker attached; production/dev Redis and production databases were never used. Separate task and integration database receipts remain in `.dev/test-db-url` and `.dev/test-db-integration-url`. The aggregate's production backend file remained unchanged throughout; final PostgreSQL race/query tests were executed separately after their harness was finalized. No blanket final native or monolithic all-green claim follows from these combined results.

### Final cross-call server-backoff correction

Static composition review found that ending one read after a long Retry-After was insufficient: the workspace could issue another GET three seconds later. The final candidate retains per-item monotonic deadlines and the corresponding read error, checks them before every GET, and records applicable server hints even on a final/cancelled/superseded real response. Late shorter hints cannot shorten an existing deadline; suppressed calls cannot extend their own deadline or retry themselves. Transport pacing metadata does not adopt stale content or overwrite public errors.

Four clock-controlled regression cases were written before this delta: calls at +3/+59 cannot GET before a 60-second hint, +60 permits one; unrelated items remain independent; final-attempt hints persist; superseded/late responses cannot bypass or shorten the wait. Independent static review approved the final code. These tests and this delta remain **uncompiled/unexecuted** under the native hold. The final native rebuild must include all post-build corrections before any test-without-building receipt can be treated as current.

### Mechanical preship receipt

At local implementation commit `9173b61ae22e63688fbd336229f09fbb85847d49`, preship passed scoped Ruff, drift/release ownership, **540 mapped incident checks**, journey mapping (11 selected journeys) and the honest local-fix request-evidence declaration. Some database legs were skipped in that sandbox invocation. Mapping and declaration acceptance are not executed native journey evidence; the actual database runs above are separate. The final backoff delta was committed locally as `7eb6a83982ef4cd04c1907a19ac592b3e4aee8c8`; final preship on that implementation head passed the same 540 mapped checks, mapping and updated honest declaration (`/tmp/nova-auto-preship-head.log`), with database skips explicitly retained. This does not authorize publication.


### Final handoff state

The private Redis was identity-checked (task PID 13232) and cleanly shut down after all rechecks; shared Redis was untouched. Native PID 36961 and compiler resources remain released; task simulator stays shutdown and no further native launch occurred. Rebuild the final native source before running any existing xctestrun receipt: the retained successful build predates the ready-error, submitted-POST/poll-key and retained-backoff corrections.

After Cunda PR #2126 is merged to main through its separately authorized process, rebase this isolated branch on that main and run the combined checks. Do not stack on or mutate the unmerged Cunda branch. Textual path overlap is empty, but its lifetime helpers are an incoming behavioral dependency, so separate branch results do not prove combined behavior. Required pending native selection: all KriaTests with named ReviewPlanTests and SlidePostExportTests execution verified in the xcresult; then the fixture-only `SlidePostUITests/testSlidePostCreationIsTheRichLayoutThenCreateThenKriaPropose` and `SlidePostUITests/testNewlyReadyAssetBecomesASlideWithoutTapping`. Physical output, process-death durability, multi-device merge semantics, the original private Cunda project, and rollout remain outside the completed evidence.

All final native deltas have independent static approval with no remaining static blocker in their stated same-session scope. This is an unpublished local candidate, **not final native verification or request-fulfillment sign-off**. The explicit native coordination hold and pinned model-checker integrity failure remain blockers. No automatic-review rejection or uncertain production action occurred in this task.

This final handoff documentation update changes no application or test source from implementation commit `7eb6a83982ef4cd04c1907a19ac592b3e4aee8c8`. Final source remains uncompiled after the held native corrections.
