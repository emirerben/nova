import XCTest
import UIKit
@testable import KriaMediaEngine
@testable import Kria

@MainActor final class DeviceRenderExecutionTests: XCTestCase {
    func testStopDuringRetryCleanupPreventsGrantAndReplacement() async throws {
        try await assertStopDuringRetryCleanup(stoppingAll: false)
    }

    func testResetDuringRetryCleanupPreventsGrantAndReplacement() async throws {
        try await assertStopDuringRetryCleanup(stoppingAll: true)
    }

    private func assertStopDuringRetryCleanup(stoppingAll: Bool) async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = renderRequest(revision: 1), gate = RetryCleanupGate(), exporter = RetryHeldExport()
        let second = renderRequest(job: first.identity.jobID, revision: 2)
        let remote = RetryRemote(first)
        await remote.replace(first)
        let scheduler = WindowScheduler()
        scheduler.synchronousTask = WindowTask()
        let initiation = DeviceRenderUserInitiation(isForeground: { true })
        let background = UIKitDeviceRenderBackgroundExecution(scheduler: scheduler, initiation: initiation, isForeground: { true }, fallback: { _ in
            DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in await gate.clean() }, completionBarrier: { _ in }, finishRequested: { await gate.requested() })
        })
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in await remote.status() }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: RetrySources(), publisher: RetryPublisher(), backgroundExecution: background)
            coordinators.append(coordinator)
            return coordinator
        }, initiation: initiation, retryFailure: { _, _, _ in
            await remote.replace(second)
            return try Self.ack(second.identity)
        }, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
        let key = DeviceRenderKey(projectID: UUID(), jobID: first.identity.jobID, variantID: "first")
        let capabilities = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))
        await sessions.reconcile(key, capabilities: capabilities)
        await exporter.waitStarted()
        let retry = Task { await sessions.retryNeedsAttentionFromUser(key) }
        await gate.waitEntered()
        let stop = Task {
            if stoppingAll { await sessions.stopAll() } else { await sessions.cancel(key) }
            await gate.stopReturned()
        }
        await gate.waitForStopRequestOrReturn()
        XCTAssertEqual(initiation.origin(for: second.identity), .automaticRecovery)
        await gate.release()
        await stop.value
        let retried = await retry.value
        await exporter.release()
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        let exports = await exporter.calls
        XCTAssertFalse(retried, "Stop must invalidate the retry while its old coordinator cleans up")
        XCTAssertEqual(coordinators.count, 1)
        XCTAssertEqual(exports, 1)
        XCTAssertEqual(scheduler.submissions, 0)
        await sessions.stopAll()
    }

    func testActualRetryMutationStartsOneAuthorizedDurableJobAndPollingDoesNot() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = renderRequest(revision: 1)
        let second = renderRequest(job: first.identity.jobID, revision: 2)
        let remote = RetryRemote(first)
        let scheduler = WindowScheduler(), delivered = WindowTask()
        scheduler.synchronousTask = delivered
        let initiation = DeviceRenderUserInitiation(isForeground: { true })
        let legacyStarts = ExpirationCounter()
        let background = UIKitDeviceRenderBackgroundExecution(scheduler: scheduler, initiation: initiation, isForeground: { true }, fallback: { _ in
            legacyStarts.increment()
            return DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in })
        })
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in await remote.status() }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: RetryExport(), sources: RetrySources(), publisher: RetryPublisher(), backgroundExecution: background)
            coordinators.append(coordinator)
            return coordinator
        }, initiation: initiation, retryFailure: { _, _, _ in
            await remote.replace(second)
            return try Self.ack(second.identity)
        }, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
        let key = DeviceRenderKey(projectID: UUID(), jobID: first.identity.jobID, variantID: "first")
        let capabilities = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))
        await sessions.reconcile(key, capabilities: capabilities)
        XCTAssertEqual(scheduler.submissions, 0)
        let retried = await sessions.retryNeedsAttentionFromUser(key)
        XCTAssertTrue(retried)
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        XCTAssertEqual(scheduler.submissions, 1)
        XCTAssertEqual(delivered.completions, [true])
        XCTAssertEqual(legacyStarts.count, 0, "An extended grant must never overlap a legacy expiration")
        let saved = await coordinators.last?.snapshot()
        XCTAssertEqual(saved?.request.identity, second.identity)
        XCTAssertEqual(saved?.phase, .synced)
        await sessions.reconcile(key, capabilities: capabilities, retry: true)
        await sessions.resumeParked()
        XCTAssertEqual(scheduler.submissions, 1)
        XCTAssertEqual(initiation.origin(for: second.identity), .automaticRecovery)
        await sessions.stopAll()
    }

    func testRetryAuthorizationIsSingleUseExactAndInvalidatedByForegroundEpoch() async throws {
        let initiation = DeviceRenderUserInitiation(isForeground: { true })
        let identity = renderRequest(revision: 1).identity
        _ = try await initiation.retry(identity: identity) { try Self.ack(identity) }
        let origin = initiation.origin(for: identity)
        XCTAssertTrue(initiation.consume(origin, identity: identity))
        XCTAssertFalse(initiation.consume(origin, identity: identity))
        _ = try await initiation.retry(identity: identity) {
            NotificationCenter.default.post(name: UIApplication.willResignActiveNotification, object: nil)
            NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
            return try Self.ack(identity)
        }
        XCTAssertEqual(initiation.origin(for: identity), .automaticRecovery)
        _ = try await initiation.retry(identity: identity) { try Self.ack(identity) }
        let wrongOrigin = initiation.origin(for: identity)
        XCTAssertFalse(initiation.consume(wrongOrigin, identity: renderRequest(revision: 1).identity))
        XCTAssertFalse(initiation.consume(wrongOrigin, identity: identity))
        do {
            _ = try await initiation.retry(identity: identity) { throw CancellationError() }
            XCTFail("Mutation must fail")
        } catch {}
        XCTAssertEqual(initiation.origin(for: identity), .automaticRecovery)
    }

    func testRetryBeginningInBackgroundOrInvalidatedDuringMutationNeverAuthorizes() async throws {
        var foreground = false
        let initiation = DeviceRenderUserInitiation(isForeground: { foreground })
        let identity = renderRequest(revision: 1).identity
        _ = try await initiation.retry(identity: identity) {
            foreground = true
            return try Self.ack(identity)
        }
        XCTAssertEqual(initiation.origin(for: identity), .automaticRecovery)
        _ = try await initiation.retry(identity: identity) {
            initiation.invalidate()
            return try Self.ack(identity)
        }
        XCTAssertEqual(initiation.origin(for: identity), .automaticRecovery)
    }

    private func renderRequest(job: UUID = UUID(), revision: Int) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: .init(jobID: job, variantID: "first", recipeRevision: revision, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])]))
    }

    nonisolated private static func ack(_ identity: DeviceRenderIdentity) throws -> DeviceRenderRetryAck {
        let raw = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(identity))
        let data = try JSONSerialization.data(withJSONObject: ["identity": raw, "phase": "awaiting_device"])
        return try JSONDecoder().decode(DeviceRenderRetryAck.self, from: data)
    }

    func testAlreadyCancelledAcquisitionDoesNotSubmit() async {
        let scheduler = WindowScheduler()
        let window = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, expiring: {})
        let operation = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await window.acquire(authorized: true)
        }
        do { _ = try await operation.value; XCTFail("Cancelled acquisition returned") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertEqual(scheduler.submissions, 0)
    }

    func testAcceptedRequestWithoutLaunchFallsBackOnceAndRejectsLateLaunch() async throws {
        let scheduler = WindowScheduler(), deadline = LaunchDeadline()
        let window = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, deadline: { await deadline.wait() }, expiring: {})
        let waiting = Task { try await window.acquire(authorized: true) }
        while scheduler.submissions == 0 { await Task.yield() }
        await deadline.fire()
        let granted = try await waiting.value
        XCTAssertFalse(granted)
        XCTAssertEqual(scheduler.cancels, 1)
        let late = WindowTask()
        scheduler.launch?(WindowTaskDelivery(late))
        scheduler.launch?(WindowTaskDelivery(late))
        XCTAssertEqual(late.completions, [false])
        XCTAssertEqual(window.state, .fallback)
    }

    func testCancellationWhileAwaitingLaunchResumesAndRejectsLateTask() async {
        let scheduler = WindowScheduler()
        let window = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, expiring: {})
        let waiting = Task { try await window.acquire(authorized: true) }
        while scheduler.submissions == 0 { await Task.yield() }
        waiting.cancel()
        do { _ = try await waiting.value; XCTFail("Cancelled acquisition returned") }
        catch { XCTAssertTrue(error is CancellationError) }
        let late = WindowTask()
        scheduler.launch?(WindowTaskDelivery(late))
        scheduler.launch?(WindowTaskDelivery(late))
        XCTAssertEqual(late.completions, [false])
        XCTAssertEqual(scheduler.cancels, 1)
    }

    func testUnsupportedRejectedAutomaticAndBackgroundAcquisitionFallBack() async throws {
        for mode in 0..<4 {
            let scheduler = WindowScheduler()
            scheduler.supportsGPUExport = mode != 0
            scheduler.accepted = mode != 1
            let window = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { mode != 3 }, expiring: {})
            let granted = try await window.acquire(authorized: mode != 2)
            XCTAssertFalse(granted)
            XCTAssertEqual(scheduler.submissions, mode == 1 ? 1 : 0)
        }
    }

    func testGrantProgressExpirationAndCompletionAreExactlyOnce() async throws {
        let scheduler = WindowScheduler(), delivered = WindowTask()
        scheduler.synchronousTask = delivered
        let expirations = ExpirationCounter()
        let window = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, expiring: { expirations.increment() })
        let granted = try await window.acquire(authorized: true)
        XCTAssertTrue(granted)
        window.reportProgress(0.7)
        window.reportProgress(0.2)
        window.reportProgress(.nan)
        scheduler.launch?(delivered)
        let duplicate = WindowTask()
        scheduler.launch?(WindowTaskDelivery(duplicate))
        scheduler.launch?(WindowTaskDelivery(duplicate))
        XCTAssertEqual(duplicate.completions, [false])
        let expiry = delivered.expirationHandler
        delivered.isExpired = true
        expiry?()
        window.finish(.completed)
        window.reportProgress(1)
        XCTAssertEqual(delivered.completions, [false])
        XCTAssertEqual(delivered.progress, [700, 700])
        XCTAssertEqual(expirations.count, 1)
    }

    func testRejectedTaskIdentityIsRetainedOnlyForTheWindowLifetime() async throws {
        let scheduler = WindowScheduler()
        scheduler.accepted = false
        var window: DurableDeviceRenderWindow? = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, expiring: {})
        _ = try await window!.acquire(authorized: true)
        var underlying: WindowTask? = WindowTask()
        weak var retained = underlying
        scheduler.launch?(WindowTaskDelivery(underlying!))
        underlying = nil
        XCTAssertNotNil(retained, "A rejected underlying task must stay alive so its identity cannot be reused")
        window = nil
        XCTAssertNil(retained, "The identity history must not outlive the window")
    }

    func testLateLaunchAfterWindowDeallocationCompletesFalseWithoutWork() async throws {
        let scheduler = WindowScheduler()
        scheduler.accepted = false
        var window: DurableDeviceRenderWindow? = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, expiring: {})
        weak var released = window
        let granted = try await window!.acquire(authorized: true)
        XCTAssertFalse(granted)
        window = nil
        XCTAssertNil(released)
        let late = WindowTask()
        scheduler.launch?(WindowTaskDelivery(late))
        XCTAssertEqual(late.completions, [false])
        XCTAssertEqual(scheduler.submissions, 1)
    }

    func testCompletedWindowIgnoresLateExpiryAndProgress() async throws {
        let scheduler = WindowScheduler(), delivered = WindowTask()
        scheduler.synchronousTask = delivered
        let window = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: { true }, expiring: {})
        _ = try await window.acquire(authorized: true)
        let expiry = delivered.expirationHandler
        window.finish(.completed)
        expiry?()
        window.finish(.failed)
        window.reportProgress(1)
        XCTAssertEqual(delivered.completions, [true])
        XCTAssertTrue(delivered.progress.isEmpty)
    }
}

@MainActor private final class WindowScheduler: ContinuedProcessingExportScheduling {
    var supportsGPUExport = true
    var accepted = true
    var synchronousTask: WindowTask?
    var submissions = 0
    var cancels = 0
    var launch: (@MainActor (any ContinuedProcessingExportTask) -> Void)?
    func registerAndSubmit(identifier: String, title: String, subtitle: String,
                           launch: @escaping @MainActor (any ContinuedProcessingExportTask) -> Void) -> Bool {
        submissions += 1
        self.launch = launch
        if let synchronousTask { launch(synchronousTask) }
        return accepted
    }
    func cancel(identifier: String) { cancels += 1 }
}

@MainActor private final class WindowTask: ContinuedProcessingExportTask {
    var expirationHandler: (@MainActor () -> Void)?
    var isExpired = false
    var completions: [Bool] = []
    var progress: [Int64] = []
    func reportProgress(completed: Int64, total: Int64) { progress.append(completed) }
    func complete(success: Bool) -> Bool {
        let actual = success && !isExpired
        completions.append(actual)
        return actual
    }
}

private actor LaunchDeadline {
    private var continuation: CheckedContinuation<Void, Never>?
    private var fired = false
    func wait() async {
        if fired { return }
        await withCheckedContinuation { continuation = $0 }
    }
    func fire() { fired = true; continuation?.resume(); continuation = nil }
}

private actor RetryRemote {
    var request: DeviceRenderRequest
    var phase = "needs_attention"
    init(_ request: DeviceRenderRequest) { self.request = request }
    func replace(_ request: DeviceRenderRequest) { self.request = request; phase = "awaiting_device" }
    func status() -> DeviceRenderStatusResponse { .init(phase: phase, request: request) }
}

private actor RetryExport: LocalExporting {
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        try Data("fixture".utf8).write(to: outputURL)
        progress?(0.5)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}

private struct RetrySources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}

private struct RetryPublisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}

private final class ExpirationCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    var count: Int { lock.withLock { value } }
    func increment() { lock.withLock { value += 1 } }
}

private actor RetryCleanupGate {
    private var requests = 0
    private var stopObserved = false
    private var stopWaiter: CheckedContinuation<Void, Never>?
    func requested() { requests += 1; if requests >= 2 { stopReturned() } }
    func stopReturned() { stopObserved = true; stopWaiter?.resume(); stopWaiter = nil }
    func waitForStopRequestOrReturn() async { if !stopObserved { await withCheckedContinuation { stopWaiter = $0 } } }
    private var entered = false
    private var released = false
    private var entry: CheckedContinuation<Void, Never>?
    private var waiting: [CheckedContinuation<Void, Never>] = []
    func clean() async {
        guard !released else { return }
        entered = true; entry?.resume(); entry = nil
        await withCheckedContinuation { waiting.append($0) }
    }
    func waitEntered() async { if !entered { await withCheckedContinuation { entry = $0 } } }
    func release() { released = true; let saved = waiting; waiting.removeAll(); for waiter in saved { waiter.resume() } }
}

private actor RetryHeldExport: LocalExporting {
    private(set) var calls = 0
    private var started: CheckedContinuation<Void, Never>?
    private var held: CheckedContinuation<Void, Never>?
    func waitStarted() async { if calls == 0 { await withCheckedContinuation { started = $0 } } }
    func release() { held?.resume(); held = nil }
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        started?.resume(); started = nil
        if calls == 1 { await withCheckedContinuation { held = $0 } }
        try Data("fixture".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}

@MainActor private final class WindowTaskDelivery: ContinuedProcessingExportTask {
    let task: WindowTask
    init(_ task: WindowTask) { self.task = task }
    var identity: ObjectIdentifier { task.identity }
    var isExpired: Bool { task.isExpired }
    var expirationHandler: (@MainActor () -> Void)? {
        get { task.expirationHandler }
        set { task.expirationHandler = newValue }
    }
    func reportProgress(completed: Int64, total: Int64) { task.reportProgress(completed: completed, total: total) }
    func complete(success: Bool) -> Bool { task.complete(success: success) }
}
