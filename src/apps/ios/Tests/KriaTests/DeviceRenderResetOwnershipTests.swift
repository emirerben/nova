import XCTest
@testable import KriaMediaEngine
@testable import Kria

@MainActor final class DeviceRenderResetOwnershipTests: XCTestCase {
    func testOlderResetCannotClearNewOwnerReplacement() async throws {
        await assertReplacementSurvivesLateReset(changingOwner: true)
    }

    func testRepeatedSameOwnerResetPreservesReplacement() async throws {
        await assertReplacementSurvivesLateReset(changingOwner: false)
    }

    private func assertReplacementSurvivesLateReset(changingOwner: Bool) async {
        let firstEntered = expectation(description: "X cancelled window, shared cleanup complete")
        let firstStarted = expectation(description: "original export holds perform task")
        let fixture = ResetOwnershipFixture(firstStarted: firstStarted, barriers: [1: firstEntered])
        await fixture.sessions.reconcile(fixture.firstKey, capabilities: fixture.capabilities)
        await fulfillment(of: [firstStarted], timeout: 2)
        let older = Task { await fixture.sessions.stopAll(); await fixture.gate.returned(1) }
        await fulfillment(of: [firstEntered], timeout: 2)
        await fixture.sessions.stopAll()
        let olderReturned = await fixture.gate.hasReturned(1)
        XCTAssertFalse(olderReturned, "X must still be held after shared cleanup while Y returns")
        let replacement = await fixture.selectReplacement(changingOwner: changingOwner)
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        XCTAssertEqual(fixture.sessions.request(for: replacement)?.identity, fixture.second.identity)
        XCTAssertNotNil(fixture.sessions.presentations[replacement])
        XCTAssertEqual(fixture.coordinators.count, 2)
        await fixture.gate.release(1)
        await older.value
        XCTAssertEqual(fixture.sessions.request(for: replacement)?.identity, fixture.second.identity,
                       "An obsolete reset must preserve the new session's request")
        XCTAssertNotNil(fixture.sessions.presentations[replacement], "Late cleanup must preserve the new card")
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        XCTAssertEqual(fixture.coordinators.count, 2, "Late cleanup must not force another coordinator")
        let cleanups = await fixture.gate.cleanups
        XCTAssertEqual(cleanups, 1, "Both resets share the real window's one cleanup")
        await fixture.finish()
    }

    func testNewerResetStillOwnsCleanupWhenOlderReturnsFirst() async throws {
        let firstEntered = expectation(description: "X held after cleanup")
        let secondEntered = expectation(description: "Y held after cleanup")
        let firstStarted = expectation(description: "original export started")
        let fixture = ResetOwnershipFixture(firstStarted: firstStarted, barriers: [1: firstEntered, 2: secondEntered])
        await fixture.sessions.reconcile(fixture.firstKey, capabilities: fixture.capabilities)
        await fulfillment(of: [firstStarted], timeout: 2)
        let older = Task { await fixture.sessions.stopAll() }
        await fulfillment(of: [firstEntered], timeout: 2)
        let newer = Task { await fixture.sessions.stopAll() }
        await fulfillment(of: [secondEntered], timeout: 2)
        await fixture.gate.release(1)
        await older.value
        let replacement = await fixture.selectReplacement(changingOwner: true)
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        XCTAssertNil(fixture.sessions.request(for: replacement), "Y still blocks admission")
        XCTAssertEqual(fixture.coordinators.count, 1)
        await fixture.gate.release(2)
        await newer.value
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        XCTAssertEqual(fixture.sessions.request(for: replacement)?.identity, fixture.second.identity)
        XCTAssertEqual(fixture.coordinators.count, 2, "Legitimate cleanup must permit later owner progress")
        await fixture.finish()
    }

    func testStaleOwnerSynchronizationCannotAdmitDuringNewerReset() async throws {
        let firstEntered = expectation(description: "owner synchronization X held")
        let secondEntered = expectation(description: "independent reset Y held")
        let firstStarted = expectation(description: "original export started")
        let fixture = ResetOwnershipFixture(firstStarted: firstStarted, barriers: [1: firstEntered, 2: secondEntered])
        await fixture.sessions.reconcile(fixture.firstKey, capabilities: fixture.capabilities)
        await fulfillment(of: [firstStarted], timeout: 2)
        let replacement = await fixture.selectReplacement(changingOwner: true)
        let staleRead = Task { await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities) }
        await fulfillment(of: [firstEntered], timeout: 2)
        let newer = Task { await fixture.sessions.stopAll() }
        await fulfillment(of: [secondEntered], timeout: 2)
        await fixture.gate.release(1)
        await staleRead.value
        let readsWhileResetting = await fixture.remote.reads(for: fixture.second.identity.jobID)
        XCTAssertEqual(readsWhileResetting, 0, "A stale synchronizeOwner must not reach fetch while Y owns reset")
        XCTAssertEqual(fixture.coordinators.count, 1)
        XCTAssertNil(fixture.sessions.request(for: replacement))
        await fixture.gate.release(2)
        await newer.value
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        XCTAssertEqual(fixture.sessions.request(for: replacement)?.identity, fixture.second.identity)
        await fixture.finish()
    }

    func testLogoutDuringOwnerSynchronizationRejectsAdmissionAndAllowsLaterReturn() async throws {
        let firstEntered = expectation(description: "owner synchronization held")
        let firstStarted = expectation(description: "original export started")
        let fixture = ResetOwnershipFixture(firstStarted: firstStarted, barriers: [1: firstEntered])
        await fixture.sessions.reconcile(fixture.firstKey, capabilities: fixture.capabilities)
        await fulfillment(of: [firstStarted], timeout: 2)
        let replacement = await fixture.selectReplacement(changingOwner: true)
        let loading = Task { await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities) }
        await fulfillment(of: [firstEntered], timeout: 2)
        fixture.owner = nil
        await fixture.gate.release(1)
        await loading.value
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        let readsAfterLogout = await fixture.remote.reads(for: fixture.second.identity.jobID)
        XCTAssertEqual(readsAfterLogout, 0)
        XCTAssertEqual(fixture.coordinators.count, 1)
        XCTAssertNil(fixture.sessions.request(for: replacement))
        fixture.owner = "owner-B"
        await fixture.sessions.reconcile(replacement, capabilities: fixture.capabilities)
        XCTAssertEqual(fixture.sessions.request(for: replacement)?.identity, fixture.second.identity)
        await fixture.finish()
    }

    func testMissingOwnerDoesNotFetchOrCreateCoordinator() async throws {
        let fixture = ResetOwnershipFixture(firstStarted: nil, barriers: [:])
        fixture.owner = nil
        await fixture.sessions.reconcile(fixture.firstKey, capabilities: fixture.capabilities)
        let reads = await fixture.remote.reads(for: fixture.first.identity.jobID)
        XCTAssertEqual(reads, 0)
        XCTAssertTrue(fixture.coordinators.isEmpty)
        XCTAssertNil(fixture.sessions.request(for: fixture.firstKey))
        await fixture.finish()
    }
}

@MainActor private final class ResetOwnershipFixture {
    var owner: String? = "owner-A"
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let first: DeviceRenderRequest
    private(set) var second: DeviceRenderRequest
    let firstKey: DeviceRenderKey
    let gate: ResetOwnershipWindowGate
    let exporter: ResetOwnershipExporter
    let remote: ResetOwnershipRemote
    var coordinators: [DeviceRenderCoordinator] = []
    let capabilities = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))

    lazy var sessions: DeviceRenderSessions = makeSessions()

    private func makeSessions() -> DeviceRenderSessions {
        return DeviceRenderSessions(fetch: { [remote] jobID, _ in
            await remote.status(for: jobID)
        }, factory: { [unowned self] _, _ in
            let coordinator = try DeviceRenderCoordinator(directory: self.root.appendingPathComponent(String(self.coordinators.count)),
                exporter: self.exporter, sources: ResetOwnershipSources(), publisher: ResetOwnershipPublisher(),
                backgroundExecution: ResetOwnershipExecution(gate: self.gate))
            self.coordinators.append(coordinator)
            return coordinator
        }, ownerIdentity: { [unowned self] in self.owner },
           environment: .init(thermalState: { .nominal }, freeStorageBytes: { Int64.max }))
    }

    init(firstStarted: XCTestExpectation?, barriers: [Int: XCTestExpectation]) {
        let firstRequest = Self.request(job: UUID(), revision: 1)
        let secondRequest = Self.request(job: UUID(), revision: 2)
        first = firstRequest
        second = secondRequest
        firstKey = DeviceRenderKey(projectID: UUID(), jobID: firstRequest.identity.jobID, variantID: "first")
        remote = ResetOwnershipRemote([firstRequest, secondRequest])
        gate = ResetOwnershipWindowGate(barriers: barriers)
        exporter = ResetOwnershipExporter(firstStarted: firstStarted)
    }

    func selectReplacement(changingOwner: Bool) async -> DeviceRenderKey {
        if changingOwner { owner = "owner-B" }
        else {
            second = Self.request(job: first.identity.jobID, revision: 2)
            await remote.replace(second)
        }
        return DeviceRenderKey(projectID: firstKey.projectID, jobID: second.identity.jobID, variantID: "first")
    }

    func finish() async {
        await gate.releaseAll()
        for coordinator in coordinators { try? await coordinator.cancel() }
        await exporter.releaseAll()
        await exporter.waitUntilDrained()
        await gate.waitUntilFinished()
        await sessions.stopAll()
        try? FileManager.default.removeItem(at: root)
    }

    private static func request(job: UUID, revision: Int) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: .init(jobID: job, variantID: "first", recipeRevision: revision, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])]))
    }
}

private actor ResetOwnershipWindowGate {
    private let barriers: [Int: XCTestExpectation]
    private var cancelledCalls = 0
    private var released: Set<Int> = []
    private var allReleased = false
    private var waiting: [Int: CheckedContinuation<Void, Never>] = [:]
    private var returnedCalls: Set<Int> = []
    private(set) var cleanups = 0
    private var openedRuns: Set<UUID> = []
    private var finishedRuns: Set<UUID> = []
    private var finishWaiters: [CheckedContinuation<Void, Never>] = []
    init(barriers: [Int: XCTestExpectation]) { self.barriers = barriers }
    func cleanup() { cleanups += 1 }
    func opened(_ run: UUID) { openedRuns.insert(run) }
    func waitUntilFinished() async {
        if !openedRuns.isSubset(of: finishedRuns) {
            await withCheckedContinuation { finishWaiters.append($0) }
        }
    }
    func afterCleanup(_ outcome: DeviceRenderWindowOutcome, run: UUID) async {
        guard outcome == .cancelled else {
            finishedRuns.insert(run)
            if openedRuns.isSubset(of: finishedRuns) {
                let saved = finishWaiters
                finishWaiters.removeAll()
                for waiter in saved { waiter.resume() }
            }
            return
        }
        cancelledCalls += 1
        let index = cancelledCalls
        guard let entered = barriers[index], !allReleased, !released.contains(index) else { return }
        entered.fulfill()
        await withCheckedContinuation { waiting[index] = $0 }
    }
    func returned(_ index: Int) { returnedCalls.insert(index) }
    func hasReturned(_ index: Int) -> Bool { returnedCalls.contains(index) }
    func release(_ index: Int) { released.insert(index); waiting.removeValue(forKey: index)?.resume() }
    func releaseAll() {
        allReleased = true
        let saved = Array(waiting.values)
        waiting.removeAll()
        for continuation in saved { continuation.resume() }
    }
}

private struct ResetOwnershipExecution: DeviceRenderBackgroundExecution {
    let gate: ResetOwnershipWindowGate
    func begin(context: DeviceRenderExecutionContext, expiring: @escaping @Sendable () -> Void) async throws -> DeviceRenderExecutionWindow {
        await gate.opened(context.runID)
        return DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in await gate.cleanup() },
            completionBarrier: { outcome in await gate.afterCleanup(outcome, run: context.runID) })
    }
}

private actor ResetOwnershipExporter: LocalExporting {
    private let firstStarted: XCTestExpectation?
    private var calls = 0
    private var released = false
    private var waiting: [CheckedContinuation<Void, Never>] = []
    private var inFlight = 0
    private var drainWaiters: [CheckedContinuation<Void, Never>] = []
    init(firstStarted: XCTestExpectation?) { self.firstStarted = firstStarted }
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String,
                progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        inFlight += 1
        defer {
            inFlight -= 1
            if inFlight == 0 {
                let saved = drainWaiters
                drainWaiters.removeAll()
                for waiter in saved { waiter.resume() }
            }
        }
        calls += 1
        if calls == 1 { firstStarted?.fulfill() }
        if !released { await withCheckedContinuation { waiting.append($0) } }
        try Task.checkCancellation()
        try Data("fixture".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
    func waitUntilDrained() async {
        if inFlight != 0 { await withCheckedContinuation { drainWaiters.append($0) } }
    }
    func releaseAll() {
        released = true
        let saved = waiting
        waiting.removeAll()
        for continuation in saved { continuation.resume() }
    }
}

private actor ResetOwnershipRemote {
    private var requests: [UUID: DeviceRenderRequest]
    private var counts: [UUID: Int] = [:]
    init(_ requests: [DeviceRenderRequest]) { self.requests = Dictionary(uniqueKeysWithValues: requests.map { ($0.identity.jobID, $0) }) }
    func replace(_ request: DeviceRenderRequest) { requests[request.identity.jobID] = request }
    func reads(for job: UUID) -> Int { counts[job, default: 0] }
    func status(for job: UUID) -> DeviceRenderStatusResponse {
        counts[job, default: 0] += 1
        return .init(phase: "awaiting_device", request: requests[job]!)
    }
}

private struct ResetOwnershipSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}

private struct ResetOwnershipPublisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}
