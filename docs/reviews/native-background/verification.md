# Inactive adapter verification

The branch depends on PR1939 at b13176d6894189b5a50118d70b9223bfd2a655a9. The API adapter is commit c1a46d9d4 and the native adapter is commit be5918cde. Later mapping/evidence commits do not activate either adapter. This is internal contract evidence, not a finished off-app customer journey.

## APNs transport

From `src/apps/api`, `.venv/bin/python -m pytest tests/services/test_apns.py -q` passed 64 tests. Ruff check and formatting passed for both changed Python files. MockTransport exercises the real fixed-host request builder, raw response parser and result classifier. The real HTTP/2 transport was constructed and closed without a connection. No Apple credentials or live endpoint were used.

Independent review reproduced an unbounded response close after a read deadline and cancellation replaced by a close exception. Fail-first controls caught both. Response cleanup now has an independent one-second cooperative timeout and preserves the original outcome/error. Additional fail-first controls prove that cleanup cannot downgrade fully parsed APNs acceptance into an uncertain result. External cancellation still propagates. The asynchronous request plus response cleanup budget is at most 15 plus 1 seconds under cooperative cancellation. A synchronous signer and transport-wide shutdown are outside that budget. Raw wire tracing must remain disabled.

The retained local replay log is `.dev/apns-final-replay.log`. Tests are committed and reproducible. No device delivery, registry, revocation, retry ledger or notification tap integration is claimed.

## Native admission

Xcode built the app and selected test target, then passed all 12 `ContinuedProcessingExportTests`, with zero failures and zero skips, on this task's dedicated iPhone 17 Pro simulator A66888D1-F7C3-42EC-AFFE-91537B54749E, iOS 26.5. The result bundle was inspected with xcresulttool and its test identifiers passed the repository's journey-gate verifier.

```
xcodegen generate --spec src/apps/ios/project.yml
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria \
  -destination 'platform=iOS Simulator,id=A66888D1-F7C3-42EC-AFFE-91537B54749E' \
  -derivedDataPath .dev/ios-derived -skipPackagePluginValidation -jobs 2 \
  -parallel-testing-enabled NO -only-testing:KriaTests/ContinuedProcessingExportTests \
  -resultBundlePath .dev/continued-processing.xcresult CODE_SIGNING_ALLOWED=NO test
```

The actual build additionally reused the task's existing Swift package checkout cache. Retained artifacts are `.dev/continued-processing-xcode.log`, `.dev/continued-processing.xcresult`, `.dev/continued-processing-summary.json` and `.dev/continued-processing-tests.json`. The worker also typechecked the real Apple bridge against the iOS18 deployment target and passed the mock tests in an isolated macOS package. A negative control that allowed recovery origin failed as expected.

Independent native review found no blocker for this inactive primitive. Remaining coverage gaps include direct testing of the production completion lock, distinct wrappers sharing one underlying task, synchronous launch during submit, and awaiting-launch owner release. The app has no caller for this adapter. No entitlement, Info.plist, permission, startup or active render path changed. Simulator mocks cannot establish physical background GPU execution, Photos save, durable recovery or final video fidelity.

## Shared checks

The CI selection contract suite passed 121 tests with the new journey mappings. Both adapter boundaries have explicit CI journey paths and executable test mappings. No external tickets, approval review, release, migration, push delivery or paid model call occurred.
