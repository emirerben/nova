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
// Dead ends a formal-model check found after those fixes, written down before this fix:
// 16. A recipe this iPhone declined (`unsupported_recipe`) reads "needs your attention"
//     with no button until a retry was refused; the editor's Retry loops into the same
//     decline. It must read "needs a change" with the chat button from the start.
// 17. The phone loses a capability after the video for the request is already rendered:
//     it keeps the file, says "can't sync yet" and never tells the server, which waits
//     forever (polls keep the record fresh). The export it holds must just upload.
// 18. `/retry` on an outdated-app decline from the same build re-pins, the phone declines
//     the re-pin, and Retry loops. `requires_update` must land on "Update Kria".
// 19. A read asked for while another read was in flight (an editor Save racing the chat
//     poll) is dropped; the old request installs and nothing reads the new one.
// 20. A failed read on a synced video never reads again (the chat poll stops reading a
//     settled render), so a request pinned since (an editor Save) is never picked up.
// Round 3 (adversarial model check), written down before the fix:
// 21. The phone loses a capability while its export for the request is still running: it
//     shows "needs a change" and tells the server "won't render" while the export goes on
//     and publishes (the server then refuses the decline).
// 22. A 426 about a newer request (another device saved a feature this build can't decode)
//     is swallowed because the phone still holds a synced video of an older request: the
//     server waits on this phone forever. A 426 about a published request reads as a failure.
// 23. A renderer_outdated record this build can render now shows "Try again", the retry
//     answers requires_update ("Update Kria"), the next read shows "Try again" again.
// 24. An asset-manifest value fault (a zero byte count) reads as "update Kria", which
//     no update fixes; only an unknown key, kind or version is a version gap.
// 25. A kept video (`localReady`) with a change reason shows "Retry sync" next to the change
//     button; with renderer_outdated it has no "Update Kria" at all.
// Round 4 (adversarial model check), written down before the fix:
// 26. A needs_attention answer for a request this iPhone never rendered reads "Ready on
//     this iPhone" because the receipt on disk holds an earlier request's export (cancelled
//     when the newer request arrived): nothing to watch for this request, nothing synced.
// 27. A renderer_outdated record on a build with no version gap any more (a rollback, an
//     update) whose recipe needs a capability this iPhone lacks keeps "Update Kria", which
//     no update fixes; it must ask for a change.
// 28. The same record on a hot phone keeps "Update Kria" instead of waiting to cool down
//     and then offering to render it here.
// 29. A retry refused with requires_update on a build whose own gate has no version gap
//     but lacks a capability (lost since the card was drawn) lands on "Update Kria".
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
/// Renders until the test releases it, then writes the file.
private actor GatedExporter: LocalExporting {
    private var released = false
    func release() { released = true }
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        while !released { try await Task.sleep(for: .milliseconds(10)) }
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

/// Fails the first upload for good (not transient), then publishes.
private actor FlakyPublisher: DeviceRenderPublishing {
    private var publishes = 0
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        publishes += 1
        if publishes == 1 { throw APIError.invalidResponse }
        return .published
    }
}

/// The first read waits until released and answers the request it was asked for;
/// every later read answers the request pinned since.
private actor ReadGate {
    private let first: DeviceRenderRequest, then: DeviceRenderRequest
    private var held: CheckedContinuation<Void, Never>?
    private var reads = 0
    private(set) var waiting = false
    init(first: DeviceRenderRequest, then: DeviceRenderRequest) { self.first = first; self.then = then }
    func read() async throws -> DeviceRenderStatusResponse {
        reads += 1
        guard reads == 1 else { return DeviceRenderStatusResponse(phase: "awaiting_device", request: then) }
        waiting = true
        await withCheckedContinuation { held = $0 }
        return DeviceRenderStatusResponse(phase: "awaiting_device", request: first)
    }
    func release() { held?.resume(); held = nil }
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
            retryFailure: retry ?? { _, _, _ in throw APIError.unsupported },
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
            XCTAssertFalse(sessions.isSettled(key), "the chat poll keeps reading: the next read says what replaced it")
            XCTAssertEqual(DeviceRenderButtonTitle.for(presentation: sessions.presentations[key]!), "Edit updated")
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
        let sessions = sessions(server, exporter: exporter, retry: { _, identity, _ in
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
        await sessions.resumeFromUser(key)
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
        let sessions = sessions(server, retry: { _, _, _ in
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

    // MARK: 16–19: no dead end after a decline, a lost capability or a lost read

    func testDeclinedRecipeAsksForAChangeInChatAsSoonAsItIsDeclined() async throws {
        let job = UUID(), declines = Declines()
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])],
            requiredCapabilities: [.freehandDrawing])
        let request = request(job, recipe: recipe)
        let unverified = PhoneRenderingCapabilities(enabled: true, recipeVersions: [2],
            verifiedFeatures: MediaCapability.allCases.filter { $0 != .freehandDrawing }.map(\.rawValue))
        let sessions = sessions(Server([awaiting(request)]), declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: unverified)
        let declined = try XCTUnwrap(sessions.presentations[key])
        XCTAssertTrue(DeviceRenderAttentionCopy.needsChange(declined), "the chat button shows before any retry was refused")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(declined), .reviewInChat)
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: declined.phase, reasonCode: declined.reasonCode))
        // The server's recorded answer keeps the same way forward.
        let recorded = DeviceRenderPresentation(phase: .needsAttention, reasonCode: "unsupported_recipe", requiresServerRetry: true)
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(recorded), .reviewInChat)
        let outdated = DeviceRenderPresentation(phase: .needsAttention, reasonCode: "renderer_outdated", requiresServerRetry: true)
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(outdated), .updateApp)
        let reported = await eventually { await declines.codes == ["unsupported_recipe"] }
        XCTAssertTrue(reported)
        await sessions.stopAll()
    }

    func testLostCapabilityAfterTheExportUploadsTheVideoItAlreadyHas() async throws {
        let job = UUID(), declines = Declines(), exporter = Exporter()
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])],
            requiredCapabilities: [.freehandDrawing])
        let request = request(job, recipe: recipe)
        let publisher = FlakyPublisher()
        let output = directory()
        let sessions = DeviceRenderSessions(
            fetch: { _, _ in .init(phase: "awaiting_device", request: request) },
            factory: { _, _ in try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: NoSources(), publisher: publisher) },
            reportDecline: { _, identity, code, _ in try await declines.send(identity, code) },
            environment: Phone().environment, sleep: { _ in }
        )
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let parked = await eventually { sessions.presentations[key]?.phase == .localReady }
        XCTAssertTrue(parked, "the export finished; the first upload failed")

        // The server stops listing the feature as verified for this phone.
        let unverified = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2],
            verifiedFeatures: MediaCapability.allCases.filter { $0 != .freehandDrawing }.map(\.rawValue))
        await sessions.reconcile(key, capabilities: unverified)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced, "the rendered video uploads: \(String(describing: sessions.presentations[key]))")
        let codes = await declines.codes
        XCTAssertEqual(codes, [], "nothing to decline: the video for this request exists")
        let exports = await exporter.calls
        XCTAssertEqual(exports, 1, "never rendered again")
        await sessions.stopAll()
    }

    func testOutdatedRetryRefusalLandsOnUpdateKria() async throws {
        let job = UUID(), request = request(job)
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "export_failed"))])
        let sessions = sessions(server, retry: { _, _, _ in
            throw APIError.conflict(detail: ConflictDetail("This edit needs a newer version of Kria.", code: "requires_update"))
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let retried = await sessions.retryNeedsAttention(key)
        XCTAssertFalse(retried)
        let refused = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(refused.reasonCode, "renderer_outdated")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(refused), .updateApp)
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: refused.phase, reasonCode: refused.reasonCode))
        await sessions.stopAll()
    }

    func testReadAskedForDuringAnotherReadRunsAfterItAndInstallsTheNewerRequest() async throws {
        let job = UUID(), first = request(job), second = request(job, revision: 2)
        let gate = ReadGate(first: first, then: second)
        let output = directory()
        let sessions = DeviceRenderSessions(
            fetch: { _, _ in try await gate.read() },
            factory: { _, _ in try DeviceRenderCoordinator(directory: output, exporter: StalledExporter(), sources: NoSources(), publisher: Publisher()) },
            environment: Phone().environment, sleep: { _ in try await Task.sleep(for: .seconds(3600)) }
        )
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        // The chat poll's read is out when the editor saves (the server now pins revision 2).
        let chat = Task { await sessions.reconcile(key, capabilities: enabled) }
        let inFlight = await eventually { await gate.waiting }
        XCTAssertTrue(inFlight)
        await sessions.reconcile(key, capabilities: enabled)   // the editor's refresh: the ticket is held
        await gate.release()
        await chat.value
        let installed = await eventually { sessions.request(for: key)?.identity == second.identity }
        XCTAssertTrue(installed, "the read asked for during the old one runs after it")
        await sessions.stopAll()
    }

    func testFailedReadOnASyncedVideoStillReadsAgainAndPicksUpANewerRequest() async throws {
        let job = UUID(), clock = Clock(), first = request(job), second = request(job, revision: 2)
        let server = Server([awaiting(first)])
        let sessions = sessions(server, clock: clock)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        let finished = try XCTUnwrap(sessions.presentations[key])

        // The editor saves (the server pins revision 2); its read fails offline.
        await server.then([.failure(URLError(.notConnectedToInternet)), awaiting(second)])
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key], finished, "the synced video stays as it is")
        let scheduled = await eventually { await clock.pending == 1 }
        XCTAssertTrue(scheduled, "the failed read is read again on its own")
        await clock.fire()
        let installed = await eventually { sessions.request(for: key)?.identity == second.identity }
        XCTAssertTrue(installed, "the re-read picks up the request pinned since")
        await sessions.stopAll()
        await clock.fire()
    }

    func testMissingOriginalOffersRecoveryNotAPausedRolloutAndDeviceUnavailableSaysSo() {
        let missing = DeviceRenderPresentation(phase: .needsAttention, reasonCode: "original_missing", requiresServerRetry: true)
        XCTAssertTrue(DeviceRenderAttentionCopy.offersOriginalRecovery(missing))
        XCTAssertFalse(DeviceRenderAttentionCopy.needsChange(missing))
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: "original_missing", originalsMissing: true))
        let unavailable = DeviceRenderAttentionCopy.message(phase: .needsAttention, reasonCode: "device_unavailable", fallback: nil) ?? ""
        XCTAssertFalse(unavailable.localizedCaseInsensitiveContains("paused"), "device_unavailable is this iPhone, not a rollout pause")
    }

    // MARK: Round 3

    func testCapabilityLostWhileTheExportRunsLetsItFinishInsteadOfDeclining() async throws {
        let job = UUID(), declines = Declines(), exporter = GatedExporter()
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])],
            requiredCapabilities: [.freehandDrawing])
        let request = request(job, recipe: recipe)
        let output = directory()
        let sessions = DeviceRenderSessions(
            fetch: { _, _ in .init(phase: "awaiting_device", request: request) },
            factory: { _, _ in try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: NoSources(), publisher: Publisher()) },
            reportDecline: { _, identity, code, _ in try await declines.send(identity, code) },
            environment: Phone().environment, sleep: { _ in }
        )
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let rendering = await eventually { sessions.presentations[key]?.phase == .rendering }
        XCTAssertTrue(rendering)

        let unverified = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2],
            verifiedFeatures: MediaCapability.allCases.filter { $0 != .freehandDrawing }.map(\.rawValue))
        await sessions.reconcile(key, capabilities: unverified)
        XCTAssertNotEqual(sessions.presentations[key]?.phase, .needsAttention)
        XCTAssertFalse(DeviceRenderAttentionCopy.needsChange(sessions.presentations[key]), "no change screen for a video on its way")
        await exporter.release()
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced, "\(String(describing: sessions.presentations[key]))")
        let codes = await declines.codes
        XCTAssertEqual(codes, [], "nothing declined: the export for this request finished")
        await sessions.stopAll()
    }

    private func updateRequired(_ identity: DeviceRenderIdentity, phase: String) throws -> Error {
        let encoded = try JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(["identity": identity])) as! [String: Any]
        let data = try JSONSerialization.data(withJSONObject: ["detail": ["code": "client_update_required", "feature": "drawing",
            "phase": phase, "identity": encoded["identity"]!]])
        struct Envelope: Decodable { let detail: [String: JSONValue] }
        let object = try JSONDecoder().decode(Envelope.self, from: data).detail
        return APIError.requestFailed(status: 426, detail: RequestFailureDetail(nil, object: object))
    }

    func testUpdateRequiredForANewerRequestIsReportedEvenWithASyncedOlderVideo() async throws {
        let job = UUID(), first = request(job), second = request(job, revision: 2), declines = Declines()
        let server = Server([awaiting(first)])
        let sessions = sessions(server, declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)

        // Same request: the finished video stays, nothing is reported.
        await server.then([.failure(try updateRequired(first.identity, phase: "published"))])
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key]?.phase, .synced, "a published request never reads as a failure")

        // Another device saved a feature this build can't decode (request 2).
        await server.then([.failure(try updateRequired(second.identity, phase: "awaiting_device"))])
        await sessions.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(presentation.reasonCode, "renderer_outdated")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(presentation), .updateApp)
        XCTAssertFalse(sessions.isSettled(key), "the chat poll keeps reading")
        let reported = await eventually { await declines.sent.map(\.identity) == [second.identity] }
        XCTAssertTrue(reported, "the decline names the newer request")
        await sessions.stopAll()
    }

    func testUpdateRequiredForTheSameRequestKeepsItsFinishedVideo() async throws {
        let job = UUID(), first = request(job), declines = Declines()
        let server = Server([awaiting(first)])
        let sessions = sessions(server, declines: declines)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let synced = await eventually { sessions.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        let finished = sessions.presentations[key]
        await server.then([.failure(try updateRequired(first.identity, phase: "awaiting_device"))])
        await sessions.reconcile(key, capabilities: enabled)
        XCTAssertEqual(sessions.presentations[key], finished)
        let codes = await declines.codes
        XCTAssertEqual(codes, [])
        await sessions.stopAll()
    }

    func testRenderableOutdatedRecordRetriesSayingThisPhoneRendersIt() async throws {
        let job = UUID(), request = request(job)
        actor Flags { var values: [Bool] = []; func add(_ value: Bool) { values.append(value) } }
        let flags = Flags(), retries = Retries()
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "renderer_outdated"))])
        let sessions = sessions(server, retry: { _, identity, rendersLocally in
            await flags.add(rendersLocally)
            return try await retries.record(identity)
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(presentation.reasonCode, DeviceRenderAttentionCopy.renderableReason)
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(presentation), .renderNow, "the editor says Render on this iPhone, not a failure")
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: presentation.phase, reasonCode: presentation.reasonCode))
        XCTAssertFalse(DeviceRenderAttentionCopy.offersOriginalRecovery(presentation))
        let retried = await sessions.retryNeedsAttention(key)
        XCTAssertTrue(retried)
        let sent = await flags.values
        XCTAssertEqual(sent, [true], "the server re-pins instead of answering requires_update")
        await sessions.stopAll()
    }

    // MARK: 26-28: a needs_attention card says what this build and this request really have

    func testNeedsAttentionForANewerRequestNeverShowsAnEarlierRequestsVideo() async throws {
        let job = UUID(), output = directory()
        let first = request(job)
        let earlier = sessions(Server([awaiting(first)]), output: output)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await earlier.reconcile(key, capabilities: enabled)
        let synced = await eventually { earlier.presentations[key]?.phase == .synced }
        XCTAssertTrue(synced)
        await earlier.stopAll()

        // Another device saved a newer schema; this build declined it and the server recorded it.
        let tooNew = KriaMediaEngine.EditRecipe(rendererVersion: "kria-ios-9", assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])])
        let next = request(job, revision: 2, recipe: tooNew)
        let later = sessions(Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: next, reasonCode: "renderer_outdated"))]), output: output)
        await later.reconcile(key, capabilities: enabled)
        let presentation = try XCTUnwrap(later.presentations[key])
        XCTAssertEqual(presentation.phase, .needsAttention, "the receipt on disk is request 1's, not this request's video")
        XCTAssertNil(presentation.localFile)
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(presentation), .updateApp)
        await later.stopAll()
    }

    func testOutdatedRecordOnABuildWithoutAVersionGapAsksForTheChangeItReallyNeeds() async throws {
        let job = UUID()
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])],
            requiredCapabilities: [.freehandDrawing])
        let request = request(job, recipe: recipe)
        let unverified = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2],
            verifiedFeatures: MediaCapability.allCases.filter { $0 != .freehandDrawing }.map(\.rawValue))
        let sessions = sessions(Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "renderer_outdated"))]))
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: unverified)
        let presentation = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(presentation.reasonCode, "unsupported_recipe")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(presentation), .reviewInChat, "no update helps; a change in chat does")
        XCTAssertFalse(DeviceRenderAttentionCopy.needsUpdate(presentation))
        await sessions.stopAll()
    }

    func testOutdatedRecordOnAHotPhoneWaitsToCoolThenOffersToRenderHere() async throws {
        let job = UUID(), request = request(job), phone = Phone(), clock = Clock()
        phone.thermal = .serious
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "renderer_outdated"))])
        let sessions = sessions(server, clock: clock, phone: phone)
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        let waiting = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(waiting.waiting, .cooling)
        XCTAssertFalse(DeviceRenderAttentionCopy.needsUpdate(waiting), "a hot phone is not an outdated one")

        phone.thermal = .fair
        sessions.resumeWaiting(thermalOnly: true)
        let renderable = await eventually { sessions.presentations[key]?.reasonCode == DeviceRenderAttentionCopy.renderableReason }
        XCTAssertTrue(renderable)
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(try XCTUnwrap(sessions.presentations[key])), .renderNow)
        await sessions.stopAll()
        await clock.fire()
    }

    func testRequiresUpdateOnABuildWithoutAVersionGapAsksForAChange() async throws {
        let job = UUID()
        let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])],
            requiredCapabilities: [.freehandDrawing])
        let request = request(job, recipe: recipe)
        let unverified = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2],
            verifiedFeatures: MediaCapability.allCases.filter { $0 != .freehandDrawing }.map(\.rawValue))
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "export_failed"))])
        let sessions = sessions(server, retry: { _, _, _ in
            throw APIError.conflict(detail: ConflictDetail("This edit needs a newer version of Kria.", code: "requires_update"))
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: unverified)
        let retried = await sessions.retryNeedsAttention(key)
        XCTAssertFalse(retried)
        let refused = try XCTUnwrap(sessions.presentations[key])
        XCTAssertEqual(refused.reasonCode, "unsupported_recipe")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(refused), .reviewInChat)
        await sessions.stopAll()
    }

    func testAnOutdatedPhoneNeverClaimsToRenderTheRecipe() async throws {
        let job = UUID()
        let tooNew = KriaMediaEngine.EditRecipe(rendererVersion: "kria-ios-9", assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])])
        let request = request(job, recipe: tooNew)
        actor Flags { var values: [Bool] = []; func add(_ value: Bool) { values.append(value) } }
        let flags = Flags(), retries = Retries()
        let server = Server([.success(DeviceRenderStatusResponse(phase: "needs_attention", request: request, reasonCode: "export_failed"))])
        let sessions = sessions(server, retry: { _, identity, rendersLocally in
            await flags.add(rendersLocally)
            return try await retries.record(identity)
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        _ = await sessions.retryNeedsAttention(key)
        let sent = await flags.values
        XCTAssertEqual(sent, [false])
        await sessions.stopAll()
    }

    func testOnlyAVersionGapInTheAssetManifestIsOutdated() throws {
        let identity = request(UUID()).identity
        let fault = DeviceRenderRecipeUnreadable(identity: identity, phase: "awaiting_device", publishedGeneration: nil, underlying: RenderAssetError.invalidManifest)
        XCTAssertEqual(fault.reasonCode, "unsupported_recipe", "a bad value is a change in chat, not an update")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(DeviceRenderPresentation(phase: .needsAttention, reasonCode: fault.reasonCode)), .reviewInChat)
        let gap = DeviceRenderRecipeUnreadable(identity: identity, phase: "awaiting_device", publishedGeneration: nil, underlying: RenderAssetError.newerManifest)
        XCTAssertEqual(gap.reasonCode, "renderer_outdated")
        // The engine: an unknown key or a newer version is the newer shape; a zero byte count is a value fault.
        let zero = Data(#"{"sha256":"\#(String(repeating: "a", count: 64))","byteCount":0}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(RenderFingerprint.self, from: zero)) { XCTAssertEqual($0 as? RenderAssetError, .invalidManifest) }
        let unknown = Data(#"{"sha256":"\#(String(repeating: "a", count: 64))","byteCount":1,"blake3":"x"}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(RenderFingerprint.self, from: unknown)) { XCTAssertEqual($0 as? RenderAssetError, .newerManifest) }
        let newer = Data(#"{"version":2,"assets":[]}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(RenderAssetManifest.self, from: newer)) { XCTAssertEqual($0 as? RenderAssetError, .newerManifest) }
    }

    func testAKeptVideoOffersTheChangeOrTheUpdateNotARetrySync() {
        let file = URL(fileURLWithPath: "/kept.mp4")
        let change = DeviceRenderPresentation(phase: .localReady, localFile: file, reasonCode: "unsupported_recipe")
        XCTAssertTrue(DeviceRenderAttentionCopy.needsChange(change))
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .localReady, reasonCode: "unsupported_recipe"))
        let update = DeviceRenderPresentation(phase: .localReady, localFile: file, reasonCode: "renderer_outdated")
        XCTAssertTrue(DeviceRenderAttentionCopy.needsUpdate(update), "the kept video gets its Update Kria link")
        XCTAssertEqual(DeviceRenderAttentionCopy.nextStep(update), .updateApp)
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .localReady, reasonCode: "renderer_outdated"))
        // A plain sync failure keeps its Retry sync.
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: .localReady, reasonCode: nil))
        // A stale reason on another phase is not the creator's turn.
        let stale = DeviceRenderPresentation(phase: .rendering, reasonCode: "unsupported_recipe")
        XCTAssertFalse(DeviceRenderAttentionCopy.needsChange(stale))
        XCTAssertFalse(DeviceRenderAttentionCopy.needsUpdate(DeviceRenderPresentation(phase: .synced, reasonCode: "renderer_outdated")))
    }

    func testUpdateKriaOpensTestFlightOrItsAppStorePage() {
        XCTAssertEqual(KriaUpdateLink.testFlight.scheme, "itms-beta")
        XCTAssertTrue(KriaUpdateLink.isUpdate(KriaUpdateLink.destination))
        XCTAssertNotEqual(KriaUpdateLink.appStore.absoluteString, "https://apps.apple.com/", "never the store's home page")
        let schemes = Bundle.main.object(forInfoDictionaryKey: "LSApplicationQueriesSchemes") as? [String]
        XCTAssertEqual(schemes, ["itms-beta"], "canOpenURL can only see a declared scheme")
    }
}
