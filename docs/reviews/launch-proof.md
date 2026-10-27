# Launch proof

This run starts from `42e131f34ac5a1a68cf541804dfa0ead5a268a68`.
It was later rebased onto `2c9102105` after the audio/photo fixes merged.
Scope is the iPhone app, native output and required backend. No merge or deployment
is authorized by this document. A passing offline check does not certify a release.

## Work list

- [x] Read the Principles section of the poteto-mode skill.
- [x] Phase A: Frame.
- [x] Phase B: Design the workflow.
- [x] Phase C: Run the loop.
- [x] Phase D: Keep the audit trail.
- [ ] Phase E: Verify and hand back.

## Acceptance

Every reported incident has one disposition: reproduced and fixed with a retained
check, already implemented and rechecked, corrected report, or blocked with its
exact remaining prerequisite. The run must never turn an untested state into a
launch approval. Keep original failure and eventual recovery separate.

The request must survive clarification, approval, retry and editor reopening.
The inspected native export must preserve chosen sources, order, text and audio.
Wrong sources, wrong order, missing text and wrong audio must fail their checks.

## Throughput checkpoint

- Blocking first steps. Read Linear incidents and comments, isolate from fetched
  main, prepare a scratch database and record baseline checks.
- Independent workstreams. Investigate sequentially in the current agent. The
  user's standing instruction disables delegation.
- Shared mutable state. Existing unmerged fixes touch shared planning and prompt
  files. Integrate them serially and recheck their combined behavior.
- Smallest safe decomposition. One verified incident at a time. Retain separate
  dispositions for product fixes, measurement, verification coverage and beta work.

## Designed phases

1. Recheck already implemented hook, captions, source admission and export checks.
2. Review and integrate existing listed-order, clip-sound and photo-picker fixes.
3. Reproduce remaining single-take and clarification-state failures. Add incident
   replays before changing behavior, including wrong-output controls.
4. Implement remaining proven request and measurement gaps using existing types
   and telemetry. Do not invent success for unverified exports.
5. Create a complete iPhone verification map with executable health and drive
   instructions. Mark device-only prerequisites explicitly.
6. Run mapped reliability checks, native evidence and preship. Preserve reports.
7. Hand back code, evidence, remaining device and beta requirements. No launch
   clearance until the release candidate and real beta outcomes are observed.

## Ticket corrections

- KRI-940. A later comment on KRI-941 corrects the butcher footage identification.
  Do not treat that specific run as proven wrong-source behavior.
- KRI-651. Fixes are deployed. The outstanding requirement is the marathon
  TestFlight replay and inspected receipt, rather than a known missing code fix.
- KRI-632, KRI-810 and KRI-811 already have implementation and regression checks
  on main. Their open status alone does not establish a current code defect.
- KRI-941 has implementation PR 1846. KRI-942 and KRI-949 merged as PRs 1847 and 1849 during this run.
- KRI-56 requires actual beta participants. Code cannot substitute for that gate.

## Evidence levels

Record offline replay, native output and production separately. TestFlight build,
API revision, worker revision and effective flags must be named for rollout proof.

## Implemented changes

- Reviewed and tested the existing fixes from PRs 1846, 1847 and 1849. After
  rebasing, the audio/photo fixes come from main and the listed-order commit
  retains its original authorship. Upstream Main Creator changes are preserved.
- KRI-948. Missing metadata in a video-only project now uses the existing bounded
  vision budget. An unresolved single-video question asks for a rough time and
  does not offer another identical picker. Provider/budget failure stays technical.
- KRI-947 and KRI-951. Main's dedicated fix shares answers across matching
  moments, including order and caption rows. Loose word-overlap matching stays
  narrower and exclusion stays separate. This does not locate a moment in a take.
- KRI-950. The integration uses main's server-held checked inventory from PR
  1853. A pure picker answer reuses that validated list, while typed corrections
  replan. The overlapping branch implementation was removed. Upstream full-turn
  and PostgreSQL regressions retain the exact photo text and tapped clip.
- KRI-941. `ordered_members` distinguishes an internally listed order from an
  unordered group. The common sequence projection and receipt checker consume
  the same expansion. Resolver/planner prompt versions were bumped.
- KRI-954. Draft receipts include resolved chapter/photo caption text and ignore
  unresolved rows. The live research request and suspected source-selection
  trigger still need production evidence.
- KRI-728. The existing private report separates readiness, export acknowledgement,
  output verification and human fulfillment. Unknown original-request lineage,
  first-attempt fulfillment and Photos acknowledgements stay explicit.
- Added `.agents/skills/verify-kria-ios` with five source-backed native journeys,
  an isolated simulator, input fingerprint checks, result bundles and teardown.

## Retained verification

Evidence is under `.dev/launch-proof/` in this worktree. No media is committed.
Commands are rerunnable from the worktree, except the native package command,
which runs from `src/apps/ios/Packages/KriaMediaEngine`.

| Check | Observed result |
| --- | --- |
| New membership/photo receipt checks before the changes | 2 failed, 3 passed. |
| Missing-moment checks before the changes | Required vision/pending/time recovery did not pass. |
| Typed listed-member checks before the changes | 3 failed, 1 passed. |
| Picker inventory before the change | Real inventory/resolver replay had no retained inventory. |
| Outcome measurement before the change | 2 HTTP journey checks failed because delivery measurements were absent. |
| `python3 scripts/reliability-gates.py run --changed` with scratch `DATABASE_URL` | 2,600 passed, 2 expected failures, no environment skips before the measurement addition. |
| `bash scripts/check-api.sh test tests/integration/test_request_following_queue.py tests/routes/test_actual_export_publication.py -q` | 32 passed. |
| `swift test --scratch-path /private/tmp/nova-launch-native-build` | 361 tests, 7 skipped, 0 failures. macOS native exporter. |
| New skill's chat, media, editor, delivery and slides drives | Each result bundle reports 1 passed, 0 failed, 0 skipped. |
| Existing hook/caption/export-verifier regressions | 67 passed with local socket access. The first sandboxed run failed its HTTP-listener check. |
| Free replay plus clarification regression run | Initially 2,356 passed with two flag-off regressions. The fallback was scoped to the picker flag; all 105 clarification checks then passed. |
| Mobile/tool/OpenAPI contract check | Unchanged, all three match their checked artifacts. |
| Full `make verify-kria` after the receipt correction | 6,573 passed, 6 skipped, 3 expected failures before the final rebase. |
| Simulator approved-hook export and source recovery | 3 passed, 0 skipped. Exported MP4 and inspected Unicode/emoji frame retained. |
| Physical iPhone source admission over Wi-Fi | 9 passed, 0 failed, 0 skipped. Synthetic offset/trim/hold/speed/cleanup and missing-source controls, with exported MP4s retained. |
| Installed build 1759 save and Photos export | Existing changes saved with the user's approval. First Photos import failed with error 3301; one retry succeeded. One new 2:15 video was independently opened in Photos. |

The mapped expected failures include the legacy unmarked collapsed-list limitation.
A new marker cannot establish the meaning of old rows that never stored it.
Live prompt compliance remains unverified. No paid model run was started.

## Remaining launch requirements

| Work | Disposition |
| --- | --- |
| KRI-940 butcher report | Corrected by the reporter in KRI-941 comments. No speculative fix. |
| KRI-651 approved hook | Original phone footage exported with the approved-hook compiler fixture on the physical iPhone. Fresh TestFlight planning and receipt acceptance remain open; the historical server records are missing. |
| KRI-632 long captions | Existing bounded-cue/face-clearance checks pass. Yasin has the original footage needed for its exact replay. |
| KRI-810 and KRI-811 export/source admission | Troya Talking exact source and saved recipe exported twice on the physical iPhone. Installed-app publication and Photos acceptance, plus the exact Montage replay, remain open. |
| KRI-179 imported source index | Main already loads and resolves the fresh pool before changing the document. Existing missing-index/import recovery tests run in the simulator. |
| KRI-954 research | Resolved caption receipt fixed. Live Cunda replay, non-unified research refusal and the suspected source trigger are still open. |
| KRI-728 complete outcome measurement | Current-session counters added. Original request/recovery lineage, Photos acknowledgement and version/cost cohorts remain incomplete. |
| KRI-56 closed beta | Requires real participants and recorded outcomes. No invitations or team messages sent. |
| Release candidate | Wi-Fi checks exercised installed build 1759. Save and Photos delivery recovered after an interrupted first import. Original incident replay, deployed revisions/flags and release approval remain open. No deploy or merge performed. |

The verification map is a tested seed with five journeys, not complete product
coverage. Account deletion/sign-in, camera capture, billing, retention and
publication still require additional mapped journeys. No independent agent review
was run because the user's standing instruction disables delegation.

## Physical continuation, 27 October

The iPhone 13 Pro connected through CoreDevice over Wi-Fi without USB or
Mirroring. Separate QA applications preserved the installed Kria app. The native
source-admission suite ran nine existing regressions with no failures or skips.
The phone's MP4s were copied back, and the inspected offset export begins with
the expected source picture; its video and audio tracks start at zero. These are
synthetic sources, not the exact retained Troya original.

After the user enabled UI Automation, the installed-app inspector passed. The
client had advanced from 0.1.0 (1754) to 0.1.0 (1759). Successful TestFlight run
[40397013091](https://github.com/emirerben/nova/actions/runs/40397013091) maps 1759
to `f3f1aa44e438114bf2b92af9554e0ef739911f38`. At that checkpoint, no
engine/exporter/header source diff was present between that revision and this branch. The installed binary's
signature was not independently checked.

The open project contained unsaved changes. Export correctly explained that a
save was required. The user explicitly approved saving those existing changes
and exporting a copy. The app reached **Saved**. The first Photos attempt failed
at 07:13 UTC with `PHPhotosErrorDomain` 3301. Apple's installed `PHError.h` names
this `operationInterrupted`, a transient error. Photos still contained 12,695
items afterward. A single controlled retry reached **Saved to Photos** at
07:21 UTC. The refreshed Photos library contained 12,696 items, including a new
2:15 video dated 07:20. That new item was opened and its playback inspected.

The interruption's cause is unproven. A later memory-pressure log does not
establish a cause. That initial check added no retry or idle-timer patch.
The subsequent bounded Photos recovery is recorded below. This is an observed recovery, not proof of reliable first-attempt
delivery. The full video's sound and request fidelity were not inspected, and
this existing project does not replace the original compound incident replays.
API/worker revisions, effective flags and exposure to this unmerged branch were
not established by this UI check.

Retained local evidence is in `.dev/launch-proof/wireless-runner/`:
`native-offset-01.xcresult`, `inspect-03.xcresult`, `save-01.xcresult`,
`save-photos-01.xcresult`, `save-photos-02.xcresult`, and
`photos-open-video-01.xcresult`, with extracted screenshots, accessibility trees,
the failed screen recording and native MP4s. The isolated XcodeGen project and
test sources are retained there. No private footage is committed.

## Integration with current main

Current main `34f06bb203ca7018eb4bee25d3cc609c5c5c4b8c` includes the dedicated
KRI-941, KRI-947/951 and KRI-950 fixes, plus later speech/receipt repairs. The
integration preserves those implementations, removes this branch's duplicate
picker record and test, and retains the additional single-take recovery,
structured ordered-members, caption receipt and outcome-measurement changes.
The upstream KRI-950 full-turn and PostgreSQL checks are included in this
branch's launch gate. Both affected prompt versions are
`2026-10-27-launch-integration` so earlier prompt checkpoints cannot be reused.
The physical UI evidence above remains tied to installed build 1759; it is not
an execution of this integrated backend branch.

Integrated verification: 140 focused incident checks passed, followed by
`make verify-kria` with 6,653 passed, 6 skipped and 3 expected failures. The
single-take time question was restored during reconciliation; the upstream
joined-row tests now assert that intentional wording while retaining their
membership, exact-label and hidden-answer checks. At merge `ce61705ad`, there
was no native product source diff from main. The subsequent Photos saver change
below requires its own proof; the earlier installed release does not contain it. Free prompt replay passed with 2,264 passed, 15 skipped and 7 expected
failures. No live provider or paid judge was enabled. Final preship is recorded
separately after the merge commit.


## Photos interruption fix and remaining replays

On 27 October, the same-agent investigation found an unhandled legitimate Photos
failure. Apple's installed SDK identifies error 3301 as `operationInterrupted`, a
transient transaction failure. Why Photos interrupted the first real import is
unknown. The fix handles that supported failure without attributing it to screen
locking, memory pressure or malformed media.

`PhotoLibrarySaver` now retries exactly once after that specific domain/code pair,
using the same exported URL and bytes. Authorization is requested once. The saver
checks cancellation before requesting permission and before the first transaction;
cancellation during the short recovery pause prevents the second transaction.
Other domains, invalid/missing resources, storage full, denied/restricted access
and user cancellation are never retried. A second interruption is still an error.
The editor already retains its temporary export until the save returns, so no
second render or new editor state is needed. Editor, ready-video and teleprompter
callers retain the existing shared `PhotoLibrarySaving` contract.

The before-fix commit is `80c6bbfae`. The fix is `2ca366c1b`. Its source SHA-256 is
`0d3552c15e69107124c8b990c9fcfaa136d790a033580a5a221fc0aae9807e8f`.

| Check | Observed result |
| --- | --- |
| Unchanged saver, physical iPhone 13 Pro, iOS 27.0.1 | 3 passed, 4 failed, 0 skipped. Recovery, retry limit and cancellation controls reproduce the missing behavior. |
| Patched saver, isolated iOS 26.5 simulator host | 7 passed, 0 failed, 0 skipped. Only external Photos calls are injected. |
| Patched saver, full Kria app test target | 7 passed, 0 failed, 0 skipped. Full app builds with the extracted adapter. |
| Patched saver, physical iPhone 13 Pro | 7 passed, 0 failed, 0 skipped. External Photos authorization and transaction results are injected. |
| Real simulator Photos library after injected 3301 | One successful transaction after two attempts. One actual library video, with the exact exported-file SHA-256 and inspected red first frame. |
| Real physical Photos library after injected 3301 | Two attempts, one successful transaction, library count 12,696 to 12,697. Saved acknowledgement and actual video independently inspected; read-only checks pass. |
| Retained hook, Tigre caption, Don Julio picker/order/audio, Cunda research and receipt replays | 308 passed, 1 expected failure, no skips. No paid provider calls. |
| Actual-export verifier, publication and recovery controls | 73 passed, no skips or expected failures. Real generated media and isolated PostgreSQL. |
| Corrected verification-skill replay command | 44 passed. Replaces the deleted KRI-950 test path with upstream full-turn and PostgreSQL checks. |

The expected failure is the legacy collapsed listed-order row without structured
member-order data. Current marked rows are covered; the old ambiguous record is
not certified. These replays establish their retained fixture behavior, not fresh
live-model compliance or exact original footage on a release candidate.

At 09:28 UTC, all six started production machines used image revision
`a406e44796d3b07cd2ecdbe1a2cd2ebd0f104be9`. Separate API and worker profile reads
showed clip intents, clip-selection questions, phone rendering and smart captions
enabled. Those snapshots describe each selected machine's effective configuration;
they do not prove account eligibility or exposure to this unmerged branch.

Fresh production reads still show Troya Montage `57602026` and Talking `c3e51ae5`
awaiting a device, with their variants needing attention. The marathon thread
`96b82e69` returns `thread_not_found`, and job `76bf6baf` returns `Job not found`.
The recorded Tigre and Don Julio Downloads folders are absent. The user confirmed
those originals are with Yasin. Troya and marathon footage were then recovered
from Kria on the user's phone; the native replay below supersedes the earlier
missing-footage finding for those two sources. Full original-case acceptance
remains open for KRI-651, KRI-632, KRI-810/830, KRI-811 and KRI-946.
Cunda live acceptance, complete outcome measurement and KRI-56's real closed beta
also remain open. No Linear state was changed.

The Fix Root Causes principle kept the recovery limited to Photos' documented
transient failure. Prove It Works kept injected boundary checks, inspected native
output and deployed release acceptance separate. The user's no-delegation rule
was preserved. No merge, deploy or release approval was performed.


## Retained originals recovered from the iPhone

`retained-incidents-device-04.xcresult` reports **2 passed, 0 failed, 0 skipped**
on the physical iPhone. The source manifests and files were read from the two
specific incident projects. No production media binding or draft was edited.

- Troya Talking. Source SHA-256 `b4d825ce0b381f2a66385815d77d51322de3f63e3b2d3871532840234e644ef2`
  matches the saved recipe's original fingerprint. The exact saved native recipe
  revision 2 and digest `430fe45a0118deb8b126f65981626f21217924d56912e16ada32ae8b3172aa17`
  were exported twice, rebuilding the preview each time. The first non-empty
  video segment starts at 21 ms while audio starts at zero. Both exports complete,
  contain picture and audio, and include the shipped 1.6-second outro. The
  retained source itself contains synthetic QA narration and labels over Troya
  footage. This is the exact incident original, not the earlier solid-color proxy.
- Marathon. Source SHA-256 `cf2692918db6eb9beee06ba4a7f1b00691871c51cf8f86cf8ebd28c6e14d9f59`
  matches its phone manifest. The existing approved-hook compiler fixture was
  bound to that original and exported on the phone. Structured text equals
  `POV: 38k in and the free açaí was a lie 😤`, with one authored text layer.
  This verifies native rendering on the original footage. It does not recreate
  the missing server thread or prove a fresh provider/approval/receipt journey.

Earlier retained-source attempts exposed QA expectation errors, not new product
fixes. AVFoundation's track range includes its empty head; the non-empty segment
is the correct observation. Its native time precision is milliseconds, so a
10-microsecond assertion was invalid. The output duration includes the existing
1.6-second brand outro. Styled title runs must be joined as in the existing
approved-hook regression. Failed bundles and earlier MP4s remain retained.

A raw FFmpeg audio comparison is inconclusive across this source's discontinuous
AAC timestamps. Matched short windows retain the same speech waveform, but the
comparison does not certify sample-exact audio timing or every spoken word.
No audio implementation was changed on that evidence.

The installed app automatically advanced to build 1761 during this run. Successful
[TestFlight run 40468818912](https://github.com/emirerben/nova/actions/runs/40468818912)
uses source revision `34f06bb203ca7018eb4bee25d3cc609c5c5c4b8c`. The native engine
source matches this branch; the new Photos saver is still unmerged. This QA
source equivalence does not establish deployment of the Photos fix.

The initial real physical Photos recovery check reached its add-only permission
prompt, then subsequent attempts found denied access in the disposable QA app.
Its own status recorded add-only `denied` and read/write `notDetermined`; those
attempts remain failed prerequisites. The continuation below supersedes this
permission blocker. The production app's Photos permission was not changed.

The successful physical files were copied back and inspected. Troya's sampled
frames show its source scenes and timed captions. The marathon frame shows the
full approved wording, accented characters and color emoji on the actual source.
The MP4 hashes are `7ca85b30a1de6a32d227157547aca4bf182851dd9afe8b019afb9cdab3162eef`
for Troya and `59bed8dfa2d2138a11dfe54af8ab57a7f171fcb0fc5c9fa6e60c4adef68f0e63`
for marathon. Both are H.264/AAC at 1080×1920, with picture and audio starting at
zero. Their video durations are 27.388333 and 5.7 seconds, including the outro.
These artifacts remain local; no original footage or rendered media is committed.


## Physical Photos recovery verified after permission

On 27 October 2026 the user enabled add-only access for the disposable QA app.
Its live status then reported add-only `authorized` and read/write
`notDetermined`. The fresh Photos baseline contained **12,696 items**.

At **10:38:03 UTC**, the patched shared saver received an injected
`PHPhotosErrorDomain/3301` at the external Photos transaction boundary. It retried
with the same exported URL and bytes. The retained receipt records **2 attempts,
1 successful real Photos transaction**, and SHA-256
`e32c26069297057ac80af4b4c865b9d2008d412712505876526bd1db700c53c3`.
The app displayed **Saved to Photos after interruption**. Independent Photos UI
inspection showed **12,697 items**, an increase of exactly one.

The new two-second asset was opened in Apple Photos and its solid red video frame
was inspected. Its Photos date is 26 October at 23:05, matching the source file's
embedded creation time; the save occurred on 27 October. The source is the
retained synthetic native export. The first error is injected; the retry uses the
real Photos framework. This verifies bounded recovery on the physical iPhone,
without attributing a cause to the earlier operating-system interruption.

The authorized composite test reached the saved acknowledgement and new library count,
then failed while trying to capture the background QA app after switching to
Photos. That failure remains in `photos-real-library-device-complete-04.xcresult`.
The harness now captures the active app. The non-mutating re-drive
`photos-real-library-device-inspect-05.xcresult` passed **1/1**, and opening the
actual asset in `photos-recovered-asset-open-06.xcresult` also passed **1/1**.
Neither inspection invoked another Photos write. The earlier baseline navigation
failure was a missing Back button because Photos already showed its library;
its retained hierarchy supplies the fresh baseline count.

Local evidence also includes `photos-real-device-report04.json`,
`photos-device-authorization-after-grant.json`, the library hierarchies and the
saved-video screenshot. No private screenshots or media are committed. The test
clip remains in Photos for inspection. Temporary task QA apps are removed after
evidence collection; the installed Kria app and its data remain in place.

This closes the patched saver's physical Photos recovery gate. A release build
containing the fix, original compound-request production acceptance, Yasin's
Tigre/Don Julio footage and the real beta remain separate launch requirements.
