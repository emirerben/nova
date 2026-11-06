# Next bounded native lane

No native launch is authorized by this plan. The parent assigns the lane.

Source checkpoint: `4cbe7d2a7a5b89d0decdbd4a1405d2323250aa7a`. Exact source-method inventory: `/tmp/nova-auto-bounded-native-selection.json`. This is a selection manifest, not execution evidence.

## First selection

Run the entire following classes from the rebuilt final source:

| Class | Source methods |
| --- | ---: |
| AuthoredMergeTests | 11 |
| SlidePostMergeTests | 9 |
| NativeEditorMergeTests | 24 |
| EditorSaveHistoryTests | 7 |
| ReviewApprovalIdentityTests | 3 |
| SlidePostSubmissionRecoveryTests | 9 |
| SlidePostSessionRecoveryTests | 26 |
| SlidePostExportTests | 49 |
| SlidePostTests | 39 |
| ReviewPlanTests | 10 |

Also run these exact `NativeEditorSessionTests` methods:

- `testReadyJobReceivedDuringSaveIsRecheckedAfterSaveAndTransientReadFailure`
- `testLegacyDirtyLatestJobDoesNotRequireUnavailableDraftEndpoint`
- `testLatestJobRetriesSixtySecondHintWithoutAnotherNotification`

Total: 190 source methods. Verify actual executed names/counts and nonzero outcomes from the resulting xcresult; selector presence or an xctestrun manifest is insufficient.

## Build and resources

Generate the project using `make ios-generate`, then perform a fresh `build-for-testing` on the selected final source with the existing Kria scheme, `CODE_SIGNING_ALLOWED=NO`, package-plugin validation setting from repository instructions, serial testing and constrained build jobs. Use the assigned simulator and this task's derived-data directory; do not interrupt another lane or boot a simulator before assignment. A previously owned simulator was `E4AC1B99-595C-472C-8B85-5505C43F2729`; its current availability must be established by the coordinator.

Use only the newly built xctestrun receipt for `test-without-building`. The old `/tmp/nova-auto-update-dd` build success predates the current fixes and proves nothing about them. Record final commit/tree, generated-source inputs, toolchain/destination, commands, build result, xcresult and actual executed cases.

Planning estimate: one simulator; approximately 30–60 minutes for compilation plus 5–10 minutes for this selection, based on the previous build under resource pressure. These are scheduling estimates, not final-source measurements. Prefer two build jobs and no parallel simulator destinations. No new dependency/tool installation is planned. Cache/disk/RAM availability must be checked in the assigned lane.

## Integration ordering and remaining checks

The isolated helper/SlidePost selection can find compile and behavioral issues before Cunda lands. It cannot establish that direct/nested load callbacks are safe on this older base. Cunda PR #2126 owns those guards; integrate its canonical load epochs before full session/lifetime sign-off, preserving the new drain reset/deferred-busy hooks. Preserve speed receipts/fixtures and split preflight guards during subsequent integration.

After integrated-source corrections, rebuild and execute the affected full native classes and wider unit selection, then fixture UI cases `SlidePostUITests/testSlidePostCreationIsTheRichLayoutThenCreateThenKriaPropose` and `SlidePostUITests/testNewlyReadyAssetBecomesASlideWithoutTapping`. Revalidate named cases against final source after any change. Physical video output, Photos transactions and private Cunda recovery are separate evidence; this lane must not mutate the private project or make paid calls.

The shared model owner controls correspondence/model/coverage maintenance. Supported Linux evidence-decoder tests, final backend aggregate and hosted CI remain separate final gates. No publication/admin merge readiness follows from this plan alone.
