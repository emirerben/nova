# Independent Send integration runtime verdict

PASS+NOTES at `edc00dca643d46e9ba84c7e6ede22d1a4554c478`.

The independent verifier did not author this patch or edit product files. Reviewed the Send diff against origin/main and exercised the compiled app on iPhone 17 Pro iOS 26.5 simulator `465558A1-6E90-4686-B020-B033AC871338`. Prove It Works required current-revision execution and screenshot inspection instead of reusing the published-head result.

## Results

- xcodebuild exited 0. The xcresult summary reports 21 passed, 0 failed, 0 skipped.
- All 11 ChatSendReceiptTests passed. These cover immutable retry identity/body, structured response context persistence, stale completion protection, acknowledgement preserving a newer identical draft, canonical recovery shared between views, and abandonment protection.
- All 10 selected UI journeys passed. They cover pending bubble during slow capability preflight, keyboard-open/closed status, rejected draft recovery and retry deduplication, lost-response acknowledgement, uncertain retry, relaunch, reopening a live send while preserving a newer draft, editor-to-main draft persistence, earlier-history scroll position, and structured song-range rejection after uncertain send.
- Inspected all six exported screenshots. Pending bubble and status are legible above the composer with keyboard open/closed. Unconfirmed status and Retry are legible above the composer with keyboard open/closed. The structured rejection appears on the song card, with an empty composer and no generic Retry.
- No actionable integration defect found in this bounded review. No runtime merge blocker found.

## Reproduction and retained evidence

Run `xcodebuild test` with project `src/apps/ios/Kria.xcodeproj`, scheme `Kria`, simulator destination, `CODE_SIGNING_ALLOWED=NO`, `COMPILER_INDEX_STORE_ENABLE=NO`, and these `-only-testing` selectors:

- `KriaTests/ChatSendReceiptTests`.
- The nine Send/draft/reader cases in `CreationUITests` selected in the retained command.
- `KriaUITests/SongRangeQuestionUITests/testUnconfirmedRangeRetryKeepsTheRejectionOnTheCard`.

The task retains `.audit/integration-runtime-approved.log`, `.audit/integration-runtime-approved.xcresult`, `.audit/integration-runtime-summary.json` and six exported screenshots under `.audit/integration-runtime-attachments`. These local artifacts contain fixture UI, not original user media. The exact tested implementation is `edc00dca643d46e9ba84c7e6ede22d1a4554c478`; the following publication commit changes this evidence document only. The implementation/test patch ID is `274df9cc8d020d4335d3cd73ddfcad4701ffc247`, unchanged from the previously published Send patch.

## Limits and execution notes

This is simulator evidence using the repository transport fixtures. It does not certify physical-phone behavior, production/backend delivery, all structured-answer variants, all devices, full native suite, or exported video. Broader acceptance remains with KRI-1043 and the parent review. Required reviews and latest CI/evidence metadata are separate merge gates owned by the parent.

The initial sandbox xcodebuild attempt exited 74 before tests because CoreSimulator and compiler/package caches were inaccessible. Its log is `integration-runtime.log`. The permitted escalated run succeeded. Result export initially met a sandbox TestReport write restriction; its permitted escalated retry succeeded. A sandbox process-list diagnostic was denied and was not retried. There are no remaining blocked or uncertain runtime actions.

No product edits, git mutations, PR comment, approval, merge or deployment performed by this verifier.
