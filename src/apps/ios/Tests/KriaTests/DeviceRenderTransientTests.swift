import Foundation
import XCTest
import KriaMediaEngine
@testable import Kria

// Ways the phone render client could still put up a failure screen when
// nothing failed, written down before the fix (the "killer screens" train):
//  1. An offline / 5xx / lock-race status read replaces a synced or
//     locally-finished video with "needs attention" (S1-a, S1-b, S1-d, S1-m).
//  2. A cancelled read (URLError.cancelled) is shown as a failure (S1-c).
//  3. A transient read waits but never retries, so it hangs forever.
//  4. 409 superseded / cancelled / empty draft, 404 and 426 show the generic
//     "couldn't prepare" copy with a Try again that repeats the same answer.
//  5. A recipe this build can't decode shows raw decoder text and the server
//     keeps waiting (S1-f); or the decline is re-sent on every poll tick.
//  6. A hot or full phone shows needs-attention and is reported to the server
//     (forcing a re-approval) instead of waiting and starting on its own.
//  7. A structural decline is never told to the server, or is dropped after
//     one offline attempt (CloudDecisionReported).
//  8. A failed capability read (`.disabled`) reads as "not available".
//  9. A receipt this build can't read blocks the render forever (S1-o).
// 10. A full disk shows a raw Cocoa string (S1-n).
// 11. Try again routes to chat approval while the server still waits on this
//     phone (S1-k).
// 12. An unknown server phase shows a failure (S1-p).
// Cross-lane gaps (pause, server reason codes), written down before the fix:
// 13. The server's `paused_by_user` is ignored: a relaunch restarts a render the
//     creator stopped, or a Stop recorded by an older server reads as
//     "needs attention" with a Try again.
// 14. Resume restarts locally but never lifts the server's pause, so the reaper
//     and the project keep treating it as stopped.
// 15. `/retry` answering `requires_change` (or a `contract_changed` record) keeps
//     offering Try again, which the server refuses identically forever.
// Each test below drives `DeviceRenderSessions` with the real coordinator,
// receipt store and status decoder; only the network and the encoder are fakes.

private actor Exporter: LocalExporting {
    private(set) var calls = 0
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("export".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}
private actor StalledExporter: LocalExporting {
    private(set) var calls = 0
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        while !Task.isCancelled { try await Task.sleep(for: .milliseconds(20)) }
        throw CancellationError()
    }
}
private struct NoSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}
private struct Publisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}

/// Scripted server: each read pops the next answer, then repeats the last.
private actor Server {
    private var answers: [Result<DeviceRenderStatusResponse, Error>]
    private(set) var reads = 0
    init(_ answers: [Result<DeviceRenderStatusResponse, Error>]) { self.answers = answers }
    func read() throws -> DeviceRenderStatusResponse {
        reads += 1
        let answer = answers.count > 1 ? answers.removeFirst() : answers[0]
        return try answer.get()
    }
    func then(_ answers: [Result<DeviceRenderStatusResponse, Error>]) { self.answers = answers }
}

/// `POST /device-render/failures`: records each attempt and fails the scripted ones.
private actor Declines {
    private(set) var sent: [(identity: DeviceRenderIdentity, code: String)] = []
    private var failures: [Error]
    init(failing failures: [Error] = []) { self.failures = failures }
    func send(_ identity: DeviceRenderIdentity, _ code: String) throws {
        sent.append((identity, code))
        if !failures.isEmpty { throw failures.removeFirst() }
    }
    var codes: [String] { sent.map(\.code) }
}

/// `POST /device-render/retry`: the server keeps the paused identity and returns it.
private actor Retries {
    private(set) var identities: [DeviceRenderIdentity] = []
    func record(_ identity: DeviceRenderIdentity) throws -> DeviceRenderRetryAck {
        identities.append(identity)
        let body = try RecipeJSON.encoder().encode(["identity": identity])
        let identityJSON = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        let ack = try JSONSerialization.data(withJSONObject: ["identity": identityJSON["identity"]!, "phase": "awaiting_device"])
        return try JSONDecoder().decode(DeviceRenderRetryAck.self, from: ack)
    }
}

/// Backoff sleeps park here until the test fires them.
private actor Clock {
    private var waiters: [CheckedContinuation<Void, Never>] = []
    var pending: Int { waiters.count }
    func sleep() async { await withCheckedContinuation { waiters.append($0) } }
    func fire() { let ready = waiters; waiters = []; ready.forEach { $0.resume() } }
}

private final class Phone: @unchecked Sendable {
    private let lock = NSLock()
    private var _thermal: ThermalState = .nominal
    private var _free: Int64? = 64 * 1024 * 1024 * 1024
    var thermal: ThermalState { get { lock.withLock { _thermal } } set { lock.withLock { _thermal = newValue } } }
    var free: Int64? { get { lock.withLock { _free } } set { lock.withLock { _free = newValue } } }
    var environment: DeviceRenderEnvironment {
        DeviceRenderEnvironment(thermalState: { self.thermal }, freeStorageBytes: { self.free })
    }
}

@MainActor final class DeviceRenderTransientTests: XCTestCase {
    private let enabled = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))
    private var directories: [URL] = []

    override func tearDown() {
        for directory in directories { try? FileManager.default.removeItem(at: directory) }
        super.tearDown()
    }

    private func request(_ job: UUID, revision: Int = 1, recipe: KriaMediaEngine.EditRecipe? = nil) -> DeviceRenderRequest {
        DeviceRenderRequest(
            identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: revision, recipeDigest: String(repeating: "\(revision)", count: 64)),
            recipe: recipe ?? KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])])
        )
    }

    private func directory() -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        directories.append(url)
        return url
    }

    private func sessions(_ server: Server, exporter: any LocalExporting = Exporter(), output: URL? = nil,
                          declines: Declines = Declines(), clock: Clock = Clock(), phone: Phone = Phone(),
                          retry: DeviceRenderSessions.RetryFailure? = nil,
                          diagnostics: (@Sendable (UUID, Error) async -> Void)? = nil) -> DeviceRenderSessions {
        let output = output ?? directory()
        return DeviceRenderSessions(
            fetch: { _, _ in try await server.read() },
            factory: { _, _ in
                try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: NoSources(), publisher: Publisher())
            },
            retryFailure: retry ?? { _, _ in throw APIError.unsupported },
            reportPreparationFailure: diagnostics ?? { _, _ in },
            reportDecline: { _, identity, code, _ in try await declines.send(identity, code) },
            environment: phone.environment,
            sleep: { _ in await clock.sleep() }
        )
    }

    private func eventually(_ condition: @MainActor () async -> Bool) async -> Bool {
        for _ in 0..<300 {
            if await condition() { return true }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return await condition()
    }

    private func published(_ request: DeviceRenderRequest) -> Result<DeviceRenderStatusResponse, Error> {
        .success(DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "gen"))
    }
    private func awaiting(_ request: DeviceRenderRequest) -> Result<DeviceRenderStatusResponse, Error> {
        .success(DeviceRenderStatusResponse(phase: "awaiting_device", request: request))
    }

    // MARK: 1–3: transient reads wait and recover on their own

    func testOfflineOrServerErrorNeverDowngradesAFinishedVideo() async throws {
        let job = UUID(), request = request(job)
        let server = Server([awaiting(request)])
        let sessions = sessions(server)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        let finished = try XCTUnwrap(sessions.presentations[key])
        XCTAssertNotNil(finished.localFile)

        for failure: Error in [URLError(.notConnectedToInternet), APIError.requestFailed(status: 503),
                               APIError.conflict(detail: ConflictDetail("That project was being updated at the same time. Try again.")),
                               APIError.sessionExpired, APIError.invalidResponse] {
            await server.then([.failure(failure)])
            await sessions.reconcile(key, capabilities: enabled)
            XCTAssertEqual(sessions.presentations[key], finished, "\(failure) must not replace a synced video")
        }
        await sessions.stopAll()
    }

    func testCancelledReadIsNotAFailure() async {
        let job = UUID()
        let server = Server([.failure(URLError(.cancelled))])
        let diagnostics = expectation(description: "no preparation diagnostic")
        diagnostics.isInverted = true
        let sessions = sessions(server, diagnostics: { _, _ in diagnostics.fulfill() })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertNil(sessions.presentations[key])
        await fulfillment(of: [diagnostics], timeout: 0.2)
    }

    func testOfflineFirstReadWaitsQuietlyAndRendersWhenTheConnectionReturns() async throws {
        let job = UUID(), request = request(job), clock = Clock()
        let server = Server([.failure(URLError(.networkConnectionLost)), .failure(APIError.requestFailed(status: 502)), awaiting(request)])
        let sessions = sessions(server, clock: clock)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")

        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.phase, .preparing)
        XCTAssertEqual(sessions.presentations[key]?.waiting, .connection)
        XCTAssertNil(sessions.presentations[key]?.message)
        XCTAssertEqual(DeviceRenderAttentionCopy.waitingNote(.connection), "Waiting for connection. Kria will continue automatically.")

        // The backoff retry fires by itself; the 502 keeps waiting as "busy".
        let parked = await eventually { await clock.pending == 1 }
        XCTAssertTrue(parked)
        await clock.fire()
        let busy = await eventually { sessions.presentations[key]?.waiting == .server }
        XCTAssertTrue(busy)
        let parkedAgain = await eventually { await clock.pending == 1 }
        XCTAssertTrue(parkedAgain)
        await clock.fire()
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced, "\(String(describing: sessions.presentations[key]))")
        XCTAssertNil(sessions.presentations[key]?.waiting)
        let reads = await server.reads
        XCTAssertEqual(reads, 3, "no user action was needed")
        await sessions.stopAll()
        await clock.fire()
    }

    // MARK: 4: server answers map to the state they describe

    func testSupersededAnswerRetiresTheOldRenderInsteadOfFailing() async throws {
        let job = UUID(), request = request(job), exporter = StalledExporter()
        for answer: Error in [APIError.conflict(detail: ConflictDetail("Device recipe changed")),
                              APIError.conflict(detail: ConflictDetail("Project changed")),
                              APIError.conflict(detail: ConflictDetail(nil, code: "superseded"))] {
            let server = Server([awaiting(request)])
            let sessions = sessions(server, exporter: exporter)
            let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
            await sessions.reconcile(key, capabilities: enabled)
            let started = await eventually { sessions.presentations[key]?.phase == .rendering }
            XCTAssertTrue(started)
            await server.then([.failure(answer)])
            await sessions.reconcile(key, capabilities: enabled)
            XCTAssertEqual(sessions.presentations[key]?.phase, .superseded, "\(answer)")
            XCTAssertNil(sessions.request(for: key), "the old coordinator no longer owns this key")
            XCTAssertTrue(sessions.isSettled(key))
            XCTAssertEqual(DeviceRenderButtonTitle.for(presentation: sessions.presentations[key]!), "Newer edit available")
            await sessions.stopAll()
        }
    }

    func testCancelledJobEmptyDraftAndMissingRecordNeverShowAFailure() async {
        let job = UUID()
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")

        let cancelled = sessions(Server([.failure(APIError.conflict(detail: ConflictDetail("Render cancelled")))]))
        await cancelled.reconcile(key, capabilities: enabled)
        let stopped = cancelled.presentations[key]
        XCTAssertEqual(stopped?.phase, .cancelled)
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .cancelled, reasonCode: stopped?.reasonCode), "retrying a cancelled job only answers 409")
        XCTAssertFalse(DeviceRenderAttentionCopy.offersOriginalRecovery(stopped))
        XCTAssertTrue(cancelled.isSettled(key))

        for answer: Error in [APIError.conflict(detail: ConflictDetail("Editor draft has no video to render")),
                              APIError.requestFailed(status: 404, detail: RequestFailureDetail("Device recipe unavailable")),
                              APIError.requestFailed(status: 404, detail: RequestFailureDetail("Job not found"))] {
            let sessions = sessions(Server([.failure(answer)]))
            await sessions.reconcile(key, capabilities: enabled)
            XCTAssertNil(sessions.presentations[key], "\(answer): the device panel hides")
            XCTAssertTrue(sessions.isInactive(key))
            XCTAssertTrue(sessions.isSettled(key))
        }
    }

    func testUpdateRequiredSaysUpdateWithoutRetryAndTellsTheServer() async throws {
        let job = UUID(), first = request(job), declines = Declines()
        let server = Server([.success(DeviceRenderStatusResponse(phase: "awaiting_device", request: first)), .failure(APIError.requestFailed(status: 426))])
        let sessions = sessions(server, exporter: StalledExporter(), declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        await sessions.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(presentation.phase, .needsAttention)
        XCTAssertEqual(presentation.reasonCode, "renderer_outdated")
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: presentation.phase, reasonCode: presentation.reasonCode))
        let reported = await eventually { await declines.codes == ["renderer_outdated"] }
        XCTAssertTrue(reported)
        let identity = await declines.sent.first?.identity
        XCTAssertEqual(identity, first.identity)
        await sessions.stopAll()
    }

    // MARK: 5: unreadable recipes are declined once, with the identity

    func testRecipeThisBuildCannotReadIsDeclinedOnceAndTheServerAnswerShows() async throws {
        let job = UUID(), request = request(job), declines = Declines()
        let body = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(request)) as! [String: Any]
        func status(_ phase: String, key: String, value: Any) throws -> DeviceRenderStatusResponse {
            var request = body, recipe = body["recipe"] as! [String: Any]
            recipe[key] = value
            request["recipe"] = recipe
            let data = try JSONSerialization.data(withJSONObject: ["phase": phase, "request": request, "reason_code": "renderer_outdated"])
            return try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: data)
        }
        // A future recipe key and a future capability both decode-fail before any classification.
        for (key, value) in [("hologramLayers", [] as [Any]), ("required_capabilities", ["hologram"])] as [(String, Any)] {
            XCTAssertThrowsError(try status("awaiting_device", key: key, value: value)) { error in
                XCTAssertEqual((error as? DeviceRenderRecipeUnreadable)?.reasonCode, "renderer_outdated", key)
                XCTAssertEqual((error as? DeviceRenderRecipeUnreadable)?.identity, request.identity, key)
            }
        }
        let server = Server([.failure(Self.unreadable { try status("awaiting_device", key: "hologramLayers", value: []) })])
        let sessions = sessions(server, declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.reasonCode, "renderer_outdated")
        XCTAssertNil(sessions.presentations[key]?.message, "never raw decoder text")
        // Polling again does not resend the same decline.
        await sessions.reconcile(key, capabilities: enabled)
        _ = await eventually { await declines.sent.count >= 1 }
        try await Task.sleep(for: .milliseconds(50))
        let codes = await declines.codes
        XCTAssertEqual(codes, ["renderer_outdated"])
        await sessions.stopAll()
    }

    private static func unreadable(_ make: () throws -> DeviceRenderStatusResponse) -> Error {
        do { _ = try make(); return APIError.invalidResponse } catch { return error }
    }

    // MARK: 6: heat and storage wait, then start on their own

    func testHotPhoneWaitsWithoutReportingAndStartsWhenItCools() async throws {
        let job = UUID(), request = request(job), declines = Declines(), phone = Phone(), exporter = Exporter()
        phone.thermal = .serious
        let sessions = sessions(Server([awaiting(request)]), exporter: exporter, declines: declines, phone: phone)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let waiting = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(waiting.waiting, .cooling)
        XCTAssertNotEqual(waiting.phase, .needsAttention)
        XCTAssertEqual(DeviceRenderAttentionCopy.waitingTitle(.cooling), "Waiting for your iPhone to cool down")
        XCTAssertEqual(DeviceRenderButtonTitle.for(presentation: waiting), "Waiting to render")
        XCTAssertFalse(sessions.retryNeedsApproval(key))

        phone.thermal = .fair
        sessions.resumeWaiting(thermalOnly: true)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        let calls = await exporter.calls
        XCTAssertEqual(calls, 1)
        let sent = await declines.sent.count
        XCTAssertEqual(sent, 0, "a hot phone is never reported: that would force a re-approval")
        await sessions.stopAll()
    }

    func testLowStorageSaysHowMuchToFreeAndResumesOnTheRecheck() async throws {
        let job = UUID(), request = request(job), declines = Declines(), phone = Phone(), clock = Clock()
        phone.free = 0
        let sessions = sessions(Server([awaiting(request)]), declines: declines, clock: clock, phone: phone)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        guard case .storage(let needed)? = sessions.presentations[key]?.waiting else {
            return XCTFail("expected a storage wait: \(String(describing: sessions.presentations[key]))")
        }
        XCTAssertGreaterThan(needed, 200 * 1024 * 1024)
        XCTAssertTrue(DeviceRenderAttentionCopy.waitingTitle(.storage(neededBytes: needed))?.hasPrefix("Free up ") == true)

        phone.free = 64 * 1024 * 1024 * 1024
        let parked = await eventually { await clock.pending == 1 }
        XCTAssertTrue(parked)
        await clock.fire()
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        let sent = await declines.sent.count
        XCTAssertEqual(sent, 0)
        await sessions.stopAll()
        await clock.fire()
    }

    // MARK: 7: structural declines reach the server, through an outage

    func testUnsupportedRecipeIsReportedUntilTheServerAcceptsItThenShowsItsAnswer() async throws {
        let job = UUID(), clock = Clock()
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])],
            requiredCapabilities: [.freehandDrawing])
        let request = request(job, recipe: recipe)
        let unverified = PhoneRenderingCapabilities(enabled: true, recipeVersions: [2],
            verifiedFeatures: MediaCapability.allCases.filter { $0 != .freehandDrawing }.map(\.rawValue))
        let declines = Declines(failing: [URLError(.notConnectedToInternet)])
        let server = Server([awaiting(request)])
        let exporter = Exporter()
        let sessions = sessions(server, exporter: exporter, declines: declines, clock: clock)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: unverified)
        XCTAssertEqual(sessions.presentations[key]?.reasonCode, "unsupported_recipe")
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: "unsupported_recipe"))

        // First report fails offline; the backoff resends it without a new reconcile.
        let parked = await eventually { await clock.pending == 1 }
        XCTAssertTrue(parked)
        await server.then([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request,
            reason: "Renderer does not support required capabilities", reasonCode: "unsupported_recipe"))])
        await clock.fire()
        let accepted = await eventually { sessions.presentations[key]?.requiresServerRetry == true }
        XCTAssertTrue(accepted, "the accepted report re-reads the server's needs_attention")
        let codes = await declines.codes
        XCTAssertEqual(codes, ["unsupported_recipe", "unsupported_recipe"])
        let calls = await exporter.calls
        XCTAssertEqual(calls, 0)
        await sessions.stopAll()
        await clock.fire()
    }

    func testOutdatedRendererIsDeclinedAndOffersRetryOnceThisBuildCanRenderIt() async throws {
        let job = UUID(), declines = Declines()
        let tooNew = KriaMediaEngine.EditRecipe(rendererVersion: "kria-ios-9", assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])])
        let outdated = request(job, recipe: tooNew)
        let sessions = sessions(Server([awaiting(outdated)]), declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.reasonCode, "renderer_outdated")
        let reported = await eventually { await declines.codes == ["renderer_outdated"] }
        XCTAssertTrue(reported)
        await sessions.stopAll()

        // After the app update the same server answer arrives for a recipe this build renders.
        let current = request(job)
        let updated = self.sessions(Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: current, reasonCode: "renderer_outdated"))]))
        await updated.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(updated.presentations[key])
        XCTAssertTrue(presentation.requiresServerRetry)
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: presentation.phase, reasonCode: presentation.reasonCode))
        await updated.stopAll()
    }

    // MARK: 8: a failed capability read is "checking", never "not available"

    func testDisabledCapabilitiesNeverDeclineAndReuseTheLastEnabledSet() async throws {
        let job = UUID(), declines = Declines(), exporter = Exporter()
        let sessions = sessions(Server([awaiting(request(job))]), exporter: exporter, declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: .disabled)
        XCTAssertEqual(sessions.presentations[key]?.waiting, .checking)
        XCTAssertNotEqual(sessions.presentations[key]?.phase, .needsAttention)
        var calls = await exporter.calls
        XCTAssertEqual(calls, 0)

        let other = DeviceRenderKey(projectID: UUID(), jobID: UUID(), variantID: "first")
        await sessions.reconcile(other, capabilities: enabled)
        await sessions.reconcile(key, capabilities: .disabled)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced, "a later blip renders against the last set the server enabled")
        calls = await exporter.calls
        XCTAssertGreaterThanOrEqual(calls, 1)
        let sent = await declines.sent.count
        XCTAssertEqual(sent, 0)
        await sessions.stopAll()
    }

    // MARK: 9–10: local storage problems

    func testUnreadableReceiptIsSetAsideAndTheRenderStartsFresh() async throws {
        let job = UUID(), output = directory(), exporter = Exporter(), pinned = request(job)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        try Data(#"{"request":{"identity":{"future":true}}"#.utf8).write(to: output.appending(path: "device-render.json"))
        let sessions = DeviceRenderSessions(
            fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: pinned) },
            factory: { _, _ in
                try DeviceRenderSessions.openCoordinator(directory: output) {
                    try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: NoSources(), publisher: Publisher())
                }
            }
        )
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced, "\(String(describing: sessions.presentations[key]))")
        XCTAssertTrue(FileManager.default.fileExists(atPath: output.appending(path: "device-render.unreadable.json").path))
        await sessions.stopAll()
    }

    func testFullDiskWaitsForStorageInsteadOfShowingTheSystemError() async throws {
        let job = UUID(), clock = Clock(), pinned = request(job)
        let full = CocoaError(.fileWriteOutOfSpace, userInfo: [NSUnderlyingErrorKey: POSIXError(.ENOSPC)])
        let sessions = DeviceRenderSessions(
            fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: pinned) },
            factory: { _, _ in throw full },
            sleep: { _ in await clock.sleep() }
        )
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        guard case .storage? = sessions.presentations[key]?.waiting else {
            return XCTFail("\(String(describing: sessions.presentations[key]))")
        }
        XCTAssertNil(sessions.presentations[key]?.message)
        await sessions.stopAll()
        await clock.fire()
    }

    // MARK: 11–12: Try again and unknown phases

    func testChatApprovalIsOnlyForAServerThatNeedsANewAttempt() async throws {
        let job = UUID(), request = request(job)
        let server = Server([.success(DeviceRenderStatusResponse(phase: "awaiting_device", request: request, retryRequiresApproval: true))])
        let sessions = sessions(server, exporter: StalledExporter())
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertFalse(sessions.retryNeedsApproval(key), "the server still waits on this phone: retry here")
        await server.then([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "export_failed", retryRequiresApproval: true))])
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertTrue(sessions.retryNeedsApproval(key))
        await sessions.stopAll()
    }

    func testUnknownServerPhaseKeepsCheckingInsteadOfFailing() async throws {
        let job = UUID(), clock = Clock()
        let server = Server([.success(DeviceRenderStatusResponse(phase: "verifying", request: request(job)))])
        let sessions = sessions(server, clock: clock)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.waiting, .checking)
        XCTAssertNotEqual(sessions.presentations[key]?.phase, .needsAttention)
        let polling = await eventually { await clock.pending == 1 }
        XCTAssertTrue(polling)
        await sessions.stopAll()
        await clock.fire()
    }

    func testPhoneHeatingOrFillingMidRenderKeepsTheRenderAndItsStopButton() async throws {
        let job = UUID(), request = request(job), phone = Phone(), exporter = StalledExporter()
        let sessions = sessions(Server([awaiting(request)]), exporter: exporter, phone: phone)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let started = await eventually { sessions.presentations[key]?.phase == .rendering }
        XCTAssertTrue(started)

        phone.thermal = .serious
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.phase, .rendering, "a hot phone never pauses an encode already running")
        XCTAssertNil(sessions.presentations[key]?.waiting, "no wait note, so the Stop rendering button stays")

        phone.thermal = .nominal
        phone.free = 0
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.phase, .rendering, "the export uses the space its estimate reserved")
        XCTAssertNil(sessions.presentations[key]?.waiting)
        let calls = await exporter.calls
        XCTAssertEqual(calls, 1, "the running encode is never restarted")
        await sessions.stopAll()
    }

    func testDeclineIgnoresAFinishedExportFromAnEarlierRequest() async throws {
        let job = UUID(), output = directory(), declines = Declines()
        let first = request(job)
        let earlier = sessions(Server([awaiting(first)]), output: output)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await earlier.reconcile(key, capabilities: enabled)
        let synced = await eventually { earlier.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        await earlier.stopAll()

        // The server retried with a recipe this build can't render; the old receipt is not this request's video.
        let tooNew = KriaMediaEngine.EditRecipe(rendererVersion: "kria-ios-9", assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])])
        let next = request(job, revision: 2, recipe: tooNew)
        let later = sessions(Server([awaiting(next)]), output: output, declines: declines)
        await later.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(later.presentations[key])
        XCTAssertEqual(presentation.phase, .needsAttention)
        XCTAssertEqual(presentation.reasonCode, "renderer_outdated")
        XCTAssertNil(presentation.localFile)
        let reported = await eventually { await declines.codes == ["renderer_outdated"] }
        XCTAssertTrue(reported, "the server hears the decline instead of waiting for the reaper")
        await later.stopAll()
    }

    // MARK: 13–15: a pause is resumable; a change-required reason has no Try again

    func testServerPauseNeverRestartsOnItsOwnAndResumeLiftsItUnderTheSameIdentity() async throws {
        let job = UUID(), request = request(job), exporter = Exporter()
        let resumed = Retries()
        let server = Server([.success(DeviceRenderStatusResponse(phase: "awaiting_device", request: request, pausedByUser: true))])
        let sessions = sessions(server, exporter: exporter, retry: { _, identity in
            try await resumed.record(identity)
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")

        // A fresh launch (no receipt on this phone) sees the creator's Stop.
        await sessions.reconcile(key, capabilities: enabled)
        let paused = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(paused.phase, .cancelled)
        XCTAssertTrue(DeviceRenderAttentionCopy.isPaused(paused))
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: paused.phase, reasonCode: paused.reasonCode))
        XCTAssertEqual(DeviceRenderButtonTitle.for(presentation: paused), "Render paused")
        XCTAssertFalse(DeviceRenderAttentionCopy.offersOriginalRecovery(paused), "a pause is not a missing original")
        let idleCalls = await exporter.calls
        XCTAssertEqual(idleCalls, 0, "a paused render never starts by itself")

        // Resume: the server lifts the pause for this identity, then the phone renders it.
        await server.then([.success(DeviceRenderStatusResponse(phase: "awaiting_device", request: request, pausedByUser: true)), awaiting(request)])
        await sessions.reconcile(key, capabilities: enabled, retry: true)
        let identities = await resumed.identities
        XCTAssertEqual(identities, [request.identity], "Resume calls /retry with the identity the server still holds")
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        await sessions.stopAll()
    }

    func testStopRecordedByAnOlderServerIsAPauseNotNeedsAttention() async throws {
        let job = UUID(), request = request(job)
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reason: "Rendering was stopped", reasonCode: "cancelled_by_user"))])
        let sessions = sessions(server)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(presentation.phase, .cancelled)
        XCTAssertTrue(presentation.requiresServerRetry, "Resume re-pins through /retry on this server")
        XCTAssertNil(DeviceRenderAttentionCopy.message(phase: presentation.phase, reasonCode: presentation.reasonCode, fallback: presentation.message))
        await sessions.stopAll()
    }

    func testChangeRequiredReasonsHideTryAgainAndPointToChat() async throws {
        let job = UUID(), request = request(job)
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "export_failed"))])
        let sessions = sessions(server, retry: { _, _ in
            throw APIError.conflict(detail: ConflictDetail("That exact text isn't in this draft. Your edit is saved.", code: "requires_change"))
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: sessions.presentations[key]?.reasonCode))

        let retried = await sessions.retryNeedsAttention(key)
        XCTAssertFalse(retried)
        let refused = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(refused.reasonCode, "requires_change")
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: refused.phase, reasonCode: refused.reasonCode))
        XCTAssertTrue(DeviceRenderAttentionCopy.needsChange(refused))
        XCTAssertEqual(DeviceRenderAttentionCopy.message(phase: refused.phase, reasonCode: refused.reasonCode, fallback: refused.message),
                       "That exact text isn't in this draft. Your edit is saved.", "the server's reason, not a raw conflict string")

        await server.then([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "contract_changed"))])
        await sessions.reconcile(key, capabilities: enabled)
        let changed = try XCTUnwrap(sessions.presentations[key])
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: changed.phase, reasonCode: changed.reasonCode))
        XCTAssertTrue(DeviceRenderAttentionCopy.needsChange(changed))
        XCTAssertFalse(DeviceRenderAttentionCopy.offersOriginalRecovery(changed))
        await sessions.stopAll()
    }

    func testMissingOriginalOffersRecoveryNotAPausedRolloutAndDeviceUnavailableSaysSo() {
        let missing = DeviceRenderPresentation(phase: .needsAttention, reasonCode: "original_missing", requiresServerRetry: true)
        XCTAssertTrue(DeviceRenderAttentionCopy.offersOriginalRecovery(missing))
        XCTAssertFalse(DeviceRenderAttentionCopy.needsChange(missing))
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: "original_missing", originalsMissing: true))
        let unavailable = DeviceRenderAttentionCopy.message(phase: .needsAttention, reasonCode: "device_unavailable", fallback: nil) ?? ""
        XCTAssertFalse(unavailable.localizedCaseInsensitiveContains("paused"), "device_unavailable is this iPhone, not a rollout pause")
    }
}
