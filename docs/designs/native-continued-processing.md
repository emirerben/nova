# Native continued processing remains inactive

Explicit Save and Share exports need a bounded owner before they can use Apple's continued processing API. This change supplies admission and lifecycle handling without a caller, startup registration, capability change, or production submission. Existing UIKit background windows and durable device-render receipts remain the active behavior.

## Failure modes and acceptance

These cases define acceptance before implementation.

- Automatic recovery or a background caller submits work. Both must cause zero registrations and zero submissions, with one fallback.
- An unsupported OS, missing GPU entitlement, or unsupported device admits the custom compositor. Each must cause zero submissions.
- Registration or submission fails. Existing execution must receive fallback once, without a queued background request.
- A launch arrives after rejection, cancellation, or finish. It must never start a second export and must finish unsuccessfully.
- Expiration and caller cancellation race with completion. Interruption and system completion must happen at most once. An interrupted export must never report success.
- Progress reaches its total before export completion. Progress alone must never finish the task.
- A scheduler retains its handler after export termination. The handler must not retain the runner or export closures.
- Cancellation enters the existing broad retry catch. Activation remains blocked until cancellation propagates without retry.

These are offline lifecycle checks. They cannot establish that an iPhone completes an export after backgrounding, that an output satisfies the creator's request, or that a durable receipt exists.

## Existing ownership

`DeviceRenderCoordinator.recover()` resumes durable work automatically. Its existing background window also covers work that did not originate in a fresh foreground gesture. Replacing that window unconditionally would violate admission requirements.

The explicit source-preview path in `NativeEditorSession.exportDisplayedSourcePreview()` invokes `performLocalExport()`. `RecipeVideoCompositor` uses `CIContext` without a proven CPU-only configuration. The returned `NativeEditorTemporaryVideo` is a temporary export, not the device coordinator's durable output receipt. Its broad retry catch currently retries cancellation errors. This change does not modify those paths.

## Design decision

A continued-processing lease around an existing asynchronous export would let encoding start before scheduler admission. Its caller would need to coordinate late launch, rejection, expiration, and a competing UIKit window. It also invites accidental use by automatic recovery.

A small app-owned runner instead starts the export only when the scheduler delivers the task. One runner represents one request, with idle, awaiting-launch, running, and finished states. A rejected request invokes the existing foreground fallback once. The caller retains the runner until completion or explicit interruption. Dropping an active runner schedules interruption and unsuccessful task completion on the main actor. Main-actor serialization makes cancellation and completion idempotent.

The scheduler boundary combines registration and submission in one synchronous call. The runner checks foreground state directly before that call, without suspension. The Apple implementation rechecks `UIApplication.applicationState` immediately before registering the exact UUID-based identifier and submitting with `.fail`. It always requires `.gpu` because this export has no proven CPU-only path. The GPU entitlement gate defaults to false. Older iOS versions report unsupported admission.

The launch callback captures the runner weakly. The system may retain a registration for the process lifetime because the public API has no unregister operation. That callback retains neither the runner nor the export workload. Each attempt creates a fresh identifier and registers it once. A process-wide identifier set rejects accidental duplicate registration. Terminal states discard export closures and clear the task expiration callback. Late task delivery finishes unsuccessfully. Progress is supplied by measured export work, remains monotonic with a fixed total, and never implies success. Only the export's explicit completion callback can report success. A locked completion state records expiration before relaying it to the main actor, so a racing completion cannot report success. The runner also interrupts work if expiration wins during completion.

Expiration and caller cancellation invoke the supplied `interrupt` operation. Future integration must map that operation to interruption, not `creatorStop`, so existing resumable receipts retain their meaning. No notification or receipt is invented by this runner.

## Activation blockers

The real bridge has no production caller. Activation requires a separate reviewed change that addresses all of these constraints.

- The current GPU export needs `com.apple.developer.background-tasks.continued-processing.gpu`. It is absent, and the user prohibited new permission changes. Keep admission disabled under the current authorization.
- A verified CPU-only export could change the resource decision, but no such path is established here.
- The app needs matching permitted identifier configuration for the bundle-based `continued-export.*` prefix. No Info.plist or entitlement change is included.
- Save and Share must capture fresh foreground user intent. Automatic recovery must keep its existing path.
- Export cancellation must bypass the current broad retry catch and interrupt AVFoundation promptly.
- The app must retain the runner, connect real progress and completion, and define a durable publication receipt before claiming durable output.
- Device validation must cover foreground launch, background completion, lock, expiration, system cancellation, unavailable resources, rejection fallback, output fidelity, and receipt recovery. Simulator mocks do not provide that proof.

The throughput checkpoint is to finish the bounded runner and offline tests first. The parent owns build integration and journey mappings. Runtime wiring is deferred because it would exceed both the permission boundary and the available GPU evidence.

## Apple contracts

The implementation is grounded in the installed iOS 26.5 SDK headers `BGTaskScheduler.h`, `BGTaskRequest.h`, and `BGTask.h`. The headers document unique registration, foreground request creation, `.fail`, `.gpu`, expiration, progress reporting, and explicit completion.

Public references are Apple's [continued processing request](https://developer.apple.com/documentation/backgroundtasks/bgcontinuedprocessingtaskrequest) and [continued processing task](https://developer.apple.com/documentation/backgroundtasks/bgcontinuedprocessingtask) documentation. No physical-device execution is claimed.

## Verification of this inactive change

On 2026-10-29, the 12 `ContinuedProcessingExportTests` passed with zero failures in a temporary macOS Swift package containing exact copies of the adapter and tests. The package excluded the Apple implementation through its platform condition. This verifies the injected scheduler contract, admission, terminal races, progress, and closure cleanup. It does not exercise the real scheduler.

The real Apple implementation passed Swift 6 typechecking against the installed iOS simulator SDK with the deployment target set to iOS 18. The command was run from the repository root.

```sh
xcrun swiftc -typecheck -swift-version 6 -target arm64-apple-ios18.0-simulator -sdk /Applications/Xcode.app/Contents/Developer/Platforms/iPhoneSimulator.platform/Developer/SDKs/iPhoneSimulator.sdk src/apps/ios/Kria/Core/ContinuedProcessingExport.swift -module-cache-path /tmp/kria-continued-module-cache
```

The temporary package test command was:

```sh
CLANG_MODULE_CACHE_PATH=/tmp/kria-continued-module-cache swift test --package-path /tmp/kria-continued-offline --scratch-path /tmp/kria-continued-offline/build --cache-path /tmp/kria-continued-offline/cache --disable-sandbox
```

A negative control removed only the automatic-recovery admission guard in the temporary source copy. `testAutomaticRecoveryAndBackgroundCallersNeverSubmit` then failed because a request was submitted and fallback was missing. The repository source was never mutated for this control. The temporary source was restored afterward.

App-target compilation, simulator execution of the app's test target, and physical-device validation remain separate parent-owned checks. No native export or durable-output fulfillment is claimed.
