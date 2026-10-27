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
| KRI-651 approved hook | Existing fix rechecked offline and by a simulator native export. Original TestFlight acceptance remains open. |
| KRI-632 long captions | Existing bounded-cue/face-clearance checks pass. Original physical footage still needs inspection. |
| KRI-810 and KRI-811 export/source admission | Existing verifier and native source-admission checks pass. Historical exact-file TestFlight acceptance remains open. |
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
to `f3f1aa44e438114bf2b92af9554e0ef739911f38`. No engine/exporter/header source
diff was present between that revision and this branch. The installed binary's
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
establish a cause. No automatic retry or idle-timer patch was added on that
evidence. This is an observed recovery, not proof of reliable first-attempt
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
membership, exact-label and hidden-answer checks. There is no native product
source diff from current main, so the earlier physical output remains evidence
for the unchanged engine/export path. Free prompt replay passed with 2,264 passed, 15 skipped and 7 expected
failures. No live provider or paid judge was enabled. Final preship is recorded
separately after the merge commit.
