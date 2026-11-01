import XCTest
@testable import KriaMediaEngine
@testable import Kria

/// Session-layer regressions for the durable Stop/retry review: a Stop is only ever cleared by a
/// typed creator Resume, an explicit retry cannot be fenced by a concurrent poll, and a repeated
/// Stop never lowers the revision floor a previous Stop recorded.
@MainActor final class DeviceRenderIntentRepairTests: XCTestCase {
    private let owner = "isolated-test-owner"
    private var capabilities: PhoneRenderingCapabilities {
        PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))
    }

    func testNewerGenerationUnderStopStaysHeldUntilTypedResume() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1), newer = request(job: job, revision: 2)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        try store.save(.init(sourceIdentity: first.identity, baseGeneration: "g1", state: .stopped), for: storageKey)
        let exporter = CountingExport()
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in
            .init(phase: "awaiting_device", request: newer, baseGeneration: "g2")
        }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: NoSources(), publisher: AlwaysPublisher())
            coordinators.append(coordinator)
            return coordinator
        }, intentStore: store, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))

        await sessions.reconcile(key, capabilities: capabilities)
        await sessions.reconcile(key, capabilities: capabilities, retry: true)
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        let automatic = await exporter.calls
        XCTAssertEqual(automatic, 0, "A newer revision from a changed generation must not release a creator Stop")
        XCTAssertEqual(try store.load(storageKey)?.state, .stopped)
        XCTAssertEqual(sessions.presentations[key]?.phase, .cancelled)

        await sessions.resumeFromUser(key)
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        let resumed = await exporter.calls
        XCTAssertEqual(resumed, 1, "Only the typed Resume may render the newer revision")
        XCTAssertEqual(try store.load(storageKey)?.state, .released)
        await sessions.stopAll()
    }

    func testPublishedResultShowsWithoutReleasingTheStopAndALaterRevisionStaysHeld() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1), newer = request(job: job, revision: 2)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        try store.save(.init(sourceIdentity: first.identity, baseGeneration: "g1", state: .stopped), for: storageKey)
        let remote = MovingRemote(first, phase: "published")
        let exporter = CountingExport()
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in await remote.status() }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: NoSources(), publisher: AlwaysPublisher())
            coordinators.append(coordinator)
            return coordinator
        }, intentStore: store, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))

        await sessions.reconcile(key, capabilities: capabilities)
        XCTAssertEqual(sessions.presentations[key]?.phase, .synced, "The finished video is shown")
        XCTAssertEqual(try store.load(storageKey)?.state, .stopped, "Showing it must not release the creator's Stop")

        await remote.move(to: newer)
        await sessions.reconcile(key, capabilities: capabilities)
        await sessions.reconcile(key, capabilities: capabilities, retry: true)
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        let automatic = await exporter.calls
        XCTAssertEqual(automatic, 0, "A later revision stays held after a published result")
        XCTAssertEqual(sessions.presentations[key]?.phase, .cancelled)

        // The decline accepted for the held revision schedules its own follow-up read, so the typed Resume can be
        // queued behind it and return first. Wait for the Resume's real outcome, with a deadline, then assert it.
        await sessions.resumeFromUser(key)
        _ = await eventually { await exporter.calls == 1 && (try? store.load(storageKey))?.state == .released }
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        let resumed = await exporter.calls
        let diagnostic = "presentation=\(String(describing: sessions.presentations[key])) intent=\(String(describing: try? store.load(storageKey)))"
        XCTAssertEqual(resumed, 1, diagnostic)
        XCTAssertEqual(try store.load(storageKey)?.state, .released, diagnostic)
        await sessions.stopAll()
    }

    /// With nothing bound in memory, a Stop targets the earlier Stop's acknowledgement before its observation
    /// before its source. The order is spelled out in `cancel`; this pins it.
    func testStopTargetPrefersTheStoredAcknowledgementOverObservationAndSource() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1), second = request(job: job, revision: 2), third = request(job: job, revision: 3)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        var earlier = DeviceRenderIntent(sourceIdentity: first.identity, baseGeneration: "g1", state: .stopped)
        earlier.acknowledgement = second.identity
        earlier.observedIdentity = third.identity
        try store.save(earlier, for: storageKey)
        let sessions = DeviceRenderSessions(fetch: { _, _ in throw URLError(.notConnectedToInternet) }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: directory, exporter: CountingExport(), sources: NoSources(), publisher: AlwaysPublisher())
        }, intentStore: store, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))

        await sessions.cancel(key)
        let stopped = try XCTUnwrap(try store.load(storageKey))
        XCTAssertEqual(stopped.state, .stopped)
        XCTAssertEqual(stopped.sourceIdentity, second.identity, "The stored acknowledgement is the Stop's target")
        XCTAssertEqual(stopped.acknowledgement, second.identity)
        XCTAssertEqual(stopped.observedIdentity, third.identity, "The later observation is carried, never dropped")
        await sessions.stopAll()
    }

    func testRepeatedStopKeepsTheAcknowledgedRevisionFloor() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1), second = request(job: job, revision: 2)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        let exporter = CountingExport(held: true)
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in
            .init(phase: "awaiting_device", request: first, baseGeneration: "g1")
        }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: NoSources(), publisher: AlwaysPublisher())
            coordinators.append(coordinator)
            return coordinator
        }, intentStore: store, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
        await sessions.reconcile(key, capabilities: capabilities)
        await exporter.waitStarted()

        var earlier = DeviceRenderIntent(sourceIdentity: first.identity, baseGeneration: "g1", state: .stopped)
        earlier.acknowledgement = second.identity
        earlier.observedIdentity = second.identity
        try store.save(earlier, for: storageKey)

        await sessions.cancel(key)
        let stored = try XCTUnwrap(try store.load(storageKey))
        XCTAssertEqual(stored.state, .stopped)
        XCTAssertEqual(stored.acknowledgement, second.identity, "A second Stop must not forget the identity the first one acknowledged")
        XCTAssertEqual(stored.observedIdentity, second.identity)
        await exporter.release()
        await sessions.stopAll()
    }

    func testPollDuringExplicitRetryDoesNotFenceTheRetry() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1), second = request(job: job, revision: 2)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        let remote = MovingRemote(first, phase: "needs_attention")
        let gate = Gate()
        let exporter = CountingExport()
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in await remote.status() }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: NoSources(), publisher: AlwaysPublisher())
            coordinators.append(coordinator)
            return coordinator
        }, intentStore: store, retryFailure: { _, _, _ in
            // The server has already minted the next revision when this poll happens.
            await remote.move(to: second)
            await gate.hold()
            return try Self.ack(second.identity)
        }, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
        await sessions.reconcile(key, capabilities: capabilities)

        let retry = Task { await sessions.retryNeedsAttentionFromUser(key) }
        await gate.waitHeld()
        await sessions.reconcile(key, capabilities: capabilities)
        await gate.open()
        let retried = await retry.value
        for coordinator in coordinators { await coordinator.waitUntilIdle() }

        XCTAssertTrue(retried, "A poll that lands mid-retry must not fence the retry the creator just asked for")
        XCTAssertEqual(try store.load(storageKey)?.state, .released)
        let exports = await exporter.calls
        XCTAssertEqual(exports, 1, "The acknowledged revision renders once, without a second tap")
        await sessions.stopAll()
    }

    func testDifferentNewerRevisionDuringCreatorRetryStaysFenced() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1), second = request(job: job, revision: 2), third = request(job: job, revision: 3)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        let remote = MovingRemote(first, phase: "needs_attention")
        let gate = Gate()
        let exporter = CountingExport()
        var coordinators: [DeviceRenderCoordinator] = []
        let sessions = DeviceRenderSessions(fetch: { _, _ in await remote.status() }, factory: { _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: directory, exporter: exporter, sources: NoSources(), publisher: AlwaysPublisher())
            coordinators.append(coordinator)
            return coordinator
        }, intentStore: store, retryFailure: { _, _, _ in
            await remote.move(to: third)
            await gate.hold()
            return try Self.ack(second.identity)
        }, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
        await sessions.reconcile(key, capabilities: capabilities)
        let retry = Task { await sessions.retryNeedsAttentionFromUser(key) }
        await gate.waitHeld()
        await sessions.reconcile(key, capabilities: capabilities)
        await gate.open()
        let retried = await retry.value
        for coordinator in coordinators { await coordinator.waitUntilIdle() }
        XCTAssertFalse(retried, "A newer revision this retry did not mint is still a stale completion")
        let exports = await exporter.calls
        XCTAssertEqual(exports, 0)
        XCTAssertEqual(try store.load(storageKey)?.state, .retryPending)
        await sessions.stopAll()
    }

    func testDefinitiveRetryRefusalDoesNotWedgeLaterReads() async throws {
        let directory = scratch(), job = UUID()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = request(job: job, revision: 1)
        let store = DeviceRenderIntentStore(root: directory.appendingPathComponent("intents"))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        let storageKey = DeviceRenderIntentKey(owner: owner, projectID: key.projectID, jobID: job, variantID: "first")
        let remote = MovingRemote(first, phase: "needs_attention")
        let sessions = DeviceRenderSessions(fetch: { _, _ in await remote.status() }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: directory, exporter: CountingExport(), sources: NoSources(), publisher: AlwaysPublisher())
        }, intentStore: store, retryFailure: { _, _, _ in
            throw APIError.conflict(detail: ConflictDetail("This edit needs a change.", code: "requires_change"))
        }, environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
        await sessions.reconcile(key, capabilities: capabilities)
        let retried = await sessions.retryNeedsAttentionFromUser(key)
        XCTAssertFalse(retried)
        XCTAssertEqual(try store.load(storageKey)?.state, .released, "The server changed nothing, so no retry marker may remain")
        await sessions.reconcile(key, capabilities: capabilities)
        XCTAssertNotEqual(sessions.presentations[key]?.reasonCode, DeviceRenderSessions.pausedReason)
        XCTAssertEqual(sessions.presentations[key]?.phase, .needsAttention, "The next read reports the server state, not a held retry")
        await sessions.stopAll()
    }

    // MARK: Fixtures

    private func eventually(timeout: Duration = .seconds(5), _ condition: () async -> Bool) async -> Bool {
        let deadline = ContinuousClock.now.advanced(by: timeout)
        while ContinuousClock.now < deadline {
            if await condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return await condition()
    }

    private func scratch() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }

    private func request(job: UUID, revision: Int) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: .init(jobID: job, variantID: "first", recipeRevision: revision, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])]))
    }

    nonisolated private static func ack(_ identity: DeviceRenderIdentity) throws -> DeviceRenderRetryAck {
        let raw = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(identity))
        let data = try JSONSerialization.data(withJSONObject: ["identity": raw, "phase": "awaiting_device"])
        return try JSONDecoder().decode(DeviceRenderRetryAck.self, from: data)
    }
}

private actor MovingRemote {
    private var request: DeviceRenderRequest
    private var phase: String
    init(_ request: DeviceRenderRequest, phase: String) { self.request = request; self.phase = phase }
    func move(to next: DeviceRenderRequest) { request = next; phase = "awaiting_device" }
    func status() -> DeviceRenderStatusResponse { .init(phase: phase, request: request, baseGeneration: "g1") }
}

private actor Gate {
    private var isHeld = false, isOpen = false
    private var heldWaiter: CheckedContinuation<Void, Never>?
    private var openWaiters: [CheckedContinuation<Void, Never>] = []
    func hold() async {
        guard !isOpen else { return }
        isHeld = true; heldWaiter?.resume(); heldWaiter = nil
        await withCheckedContinuation { openWaiters.append($0) }
    }
    func waitHeld() async { if !isHeld { await withCheckedContinuation { heldWaiter = $0 } } }
    func open() { isOpen = true; let saved = openWaiters; openWaiters.removeAll(); for waiter in saved { waiter.resume() } }
}

private actor CountingExport: LocalExporting {
    private(set) var calls = 0
    private let held: Bool
    private var started: CheckedContinuation<Void, Never>?
    private var waiting: [CheckedContinuation<Void, Never>] = []
    private var released = false
    init(held: Bool = false) { self.held = held }
    func waitStarted() async { if calls == 0 { await withCheckedContinuation { started = $0 } } }
    func release() { released = true; let saved = waiting; waiting.removeAll(); for waiter in saved { waiter.resume() } }
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        started?.resume(); started = nil
        if held, !released { await withCheckedContinuation { waiting.append($0) } }
        try Data("fixture".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}

private struct NoSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}

private struct AlwaysPublisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}
