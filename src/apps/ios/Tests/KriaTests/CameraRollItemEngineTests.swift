import XCTest
import Observation
@testable import Kria

private func makeAsset(id: UUID = UUID(), kind: String = "image") -> SavedMediaAsset {
    SavedMediaAsset(id: id, kind: kind, status: "ready",
        originalDeclaredSHA256: String(repeating: "a", count: 64), originalByteCount: 8, originalAvailability: "device_only",
        metadata: [:],
        sources: [SavedMediaSource(id: UUID(), role: "analysis_proxy", status: "available", byteCount: 8, contentType: "image/jpeg",
                                   uploadedSHA256: String(repeating: "b", count: 64), generation: "1")],
        usage: [], storageWarnings: [])
}

private func makeFile(_ name: String, bytes: Int = 8) throws -> URL {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let url = directory.appendingPathComponent(name)
    try Data(count: bytes).write(to: url)
    return url
}

/// KRI-944 guards: the reducer never moves an item backwards, one failing item never stops its siblings, the
/// upload loop never exceeds its concurrency cap, and a progress tick redraws only its own tile.
final class CameraRollItemStateTests: XCTestCase {
    private typealias State = CameraRollItemState
    private typealias Event = CameraRollItemEvent

    func testHappyPathRunsPickToReady() {
        var state = State.picking
        for (event, expected) in [
            (Event.exportStarted, State.exporting),
            (.uploadProgress(0.4), .uploading(progress: 0.4)),
            (.uploaded, .queued),
            (.server(.analyzing), .analyzing),
            (.server(.ready), .ready),
        ] {
            state = CameraRollItemReducer.reduce(state, event)
            XCTAssertEqual(state, expected)
        }
    }

    func testLateEventsCannotMoveAnItemBackwards() {
        // A progress tick that arrives after the upload finished or failed is dropped.
        XCTAssertEqual(CameraRollItemReducer.reduce(.queued, .uploadProgress(0.5)), .queued)
        XCTAssertEqual(CameraRollItemReducer.reduce(.needsRetry(.uploadFailed), .uploadProgress(0.5)), .needsRetry(.uploadFailed))
        XCTAssertEqual(CameraRollItemReducer.reduce(.uploading(progress: 0.8), .uploadProgress(0.3)), .uploading(progress: 0.8))
        XCTAssertEqual(CameraRollItemReducer.reduce(.uploading(progress: 0.2), .uploadProgress(7)), .uploading(progress: 1))
        // Local failures and rejections only apply while the phone still owns the item.
        XCTAssertEqual(CameraRollItemReducer.reduce(.ready, .failedLocally(.uploadFailed)), .ready)
        XCTAssertEqual(CameraRollItemReducer.reduce(.analyzing, .rejected(.tooLarge)), .analyzing)
        XCTAssertEqual(CameraRollItemReducer.reduce(.analyzing, .exportStarted), .analyzing)
    }

    func testFailureAndExclusionClasses() {
        XCTAssertEqual(CameraRollItemReducer.reduce(.exporting, .failedLocally(.icloudUnavailable)), .needsRetry(.icloudUnavailable))
        XCTAssertEqual(CameraRollItemReducer.reduce(.exporting, .unreachable(.accessRevoked)), .failed(.accessRevoked))
        XCTAssertEqual(CameraRollItemReducer.reduce(.picking, .rejected(.tooLong)), .excluded(.tooLong))
        XCTAssertEqual(CameraRollItemReducer.reduce(.uploading(progress: 0.5), .interrupted), .picking)
        XCTAssertEqual(CameraRollItemReducer.reduce(.analyzing, .interrupted), .analyzing)
        // A local retry restarts the pipeline; a permanent failure does not.
        XCTAssertEqual(CameraRollItemReducer.reduce(.needsRetry(.uploadFailed), .exportStarted), .exporting)
        XCTAssertEqual(CameraRollItemReducer.reduce(.failed(.accessRevoked), .exportStarted), .failed(.accessRevoked))
    }

    func testServerRetryReturnsToAnalyzingOnlyFromNeedsRetry() {
        XCTAssertEqual(CameraRollItemReducer.reduce(.needsRetry(.providerError), .retryRequested), .analyzing)
        XCTAssertEqual(CameraRollItemReducer.reduce(.failed(.noUsableMoment), .retryRequested), .failed(.noUsableMoment))
        XCTAssertEqual(CameraRollItemReducer.reduce(.ready, .retryRequested), .ready)
    }

    func testServerUploadingKeepsLocalByteProgress() {
        XCTAssertEqual(CameraRollItemReducer.reduce(.uploading(progress: 0.6), .server(.uploading(progress: 1))), .uploading(progress: 0.6))
        XCTAssertEqual(CameraRollItemReducer.reduce(.exporting, .server(.queued)), .queued)
    }

    func testAStaleServerSnapshotNeverWalksATileBackwards() {
        XCTAssertEqual(CameraRollItemReducer.reduce(.ready, .server(.analyzing)), .ready)
        XCTAssertEqual(CameraRollItemReducer.reduce(.ready, .server(.queued)), .ready)
        XCTAssertEqual(CameraRollItemReducer.reduce(.analyzing, .server(.queued)), .analyzing)
        // Forward moves and a real failure still land.
        XCTAssertEqual(CameraRollItemReducer.reduce(.queued, .server(.analyzing)), .analyzing)
        XCTAssertEqual(CameraRollItemReducer.reduce(.analyzing, .server(.needsRetry(.providerError))), .needsRetry(.providerError))
    }

    func testAServerUploadingSnapshotKeepsAWaitingItemWaiting() {
        XCTAssertEqual(CameraRollItemReducer.reduce(.queued, .server(.uploading(progress: 1))), .queued)
        XCTAssertEqual(CameraRollItemReducer.reduce(.analyzing, .server(.uploading(progress: 1))), .analyzing)
    }

    func testServerSnapshotsMapEveryContractStateAndSurviveUnknownValues() throws {
        func item(_ state: String, reason: String? = nil) throws -> CameraRollServerItem {
            let reasonJSON = reason.map { "\"\($0)\"" } ?? "null"
            let json = """
            {"asset_id":"\(UUID().uuidString)","kind":"video","group":null,"state":"\(state)","reason":\(reasonJSON),"retryable":true,"attempts":2,"updated_at":"2026-10-26T10:00:00Z"}
            """
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(CameraRollServerItem.self, from: Data(json.utf8))
        }
        XCTAssertEqual(try item("queued").itemState, .queued)
        XCTAssertEqual(try item("analyzing").itemState, .analyzing)
        XCTAssertEqual(try item("ready").itemState, .ready)
        XCTAssertEqual(try item("needs_retry", reason: "budget_paused").itemState, .needsRetry(.budgetPaused))
        XCTAssertEqual(try item("failed", reason: "no_usable_moment").itemState, .failed(.noUsableMoment))
        XCTAssertEqual(try item("excluded", reason: "too_long").itemState, .excluded(.tooLong))
        // A reason or state from a newer server settles as a retryable provider problem instead of failing the decode.
        XCTAssertEqual(try item("needs_retry", reason: "from_the_future").itemState, .needsRetry(.providerError))
        XCTAssertEqual(try item("something_new").itemState, .needsRetry(.providerError))
    }

    func testEveryReasonHasCopy() {
        for reason in CameraRollItemReason.allCases { XCTAssertFalse(reason.message.isEmpty) }
    }

    func testPreflightLimits() {
        XCTAssertNil(CameraRollPreflight.checkFile(kind: "image", pathExtension: "HEIC", byteCount: CameraRollPreflight.maximumImageBytes))
        XCTAssertEqual(CameraRollPreflight.checkFile(kind: "image", pathExtension: "jpg", byteCount: CameraRollPreflight.maximumImageBytes + 1), .tooLarge)
        for ext in ["gif", "dng", "tiff"] {
            XCTAssertEqual(CameraRollPreflight.checkFile(kind: "image", pathExtension: ext, byteCount: 10), .unsupportedType, ext)
        }
        XCTAssertNil(CameraRollPreflight.checkFile(kind: "video", pathExtension: "mov", byteCount: 5_000_000_000))
        XCTAssertNil(CameraRollPreflight.checkMetadata(kind: "video", duration: 30 * 60))
        XCTAssertEqual(CameraRollPreflight.checkMetadata(kind: "video", duration: 30 * 60 + 1), .tooLong)
        XCTAssertNil(CameraRollPreflight.checkMetadata(kind: "image", duration: 0))
    }

    func testExportAndUploadErrorsMapToTheRightClass() {
        XCTAssertEqual(CameraRollUploadFailure.event(for: LibraryAssetExportError.timedOut), .failedLocally(.icloudUnavailable))
        XCTAssertEqual(CameraRollUploadFailure.event(for: LibraryAssetExportError.byteLimitExceeded), .rejected(.tooLarge))
        XCTAssertEqual(CameraRollUploadFailure.event(for: LibraryAssetExportError.assetNotFound), .unreachable(.accessRevoked))
        XCTAssertEqual(CameraRollUploadFailure.event(for: LibraryAssetExportError.noResource), .rejected(.unsupportedType))
        XCTAssertEqual(CameraRollUploadFailure.event(for: URLError(.notConnectedToInternet)), .failedLocally(.uploadFailed))
    }
}

final class CameraRollUploadLoopTests: XCTestCase {
    private actor Events {
        var byID: [String: [CameraRollItemEvent]] = [:]
        var uploaded: [String] = []
        func add(_ id: String, _ event: CameraRollItemEvent) { byID[id, default: []].append(event) }
        func landed(_ id: String) { uploaded.append(id) }
    }
    private actor Gauge {
        private var current = 0
        private(set) var peak = 0
        func enter() { current += 1; peak = max(peak, current) }
        func leave() { current -= 1 }
    }

    private func run(_ items: [CameraRollUploadItem], gauge: Gauge = Gauge(), timeout: Duration = .seconds(5),
                     export: (@Sendable (CameraRollUploadItem) async throws -> URL)? = nil,
                     failing: Set<String> = []) async -> Events {
        let events = Events()
        let made = makeAsset()
        let loop = CameraRollUploadLoop(
            exportTimeout: timeout,
            export: export ?? { item in try await Task.sleep(for: .milliseconds(5)); return try makeFile("\(item.id).jpg") },
            upload: { url, _, progress in
                await gauge.enter()
                defer { Task { await gauge.leave() } }
                try await Task.sleep(for: .milliseconds(25))
                if failing.contains(url.deletingPathExtension().lastPathComponent) { throw URLError(.networkConnectionLost) }
                progress(1)
                return made
            })
        await loop.run(items, uploaded: { id, _ in await events.landed(id) }, report: { id, event in await events.add(id, event) })
        return events
    }

    /// What a tile would show: the loop's progress ticks are unordered tasks, so only the folded state is meaningful.
    private func finalState(_ events: [CameraRollItemEvent]?) -> CameraRollItemState {
        (events ?? []).reduce(CameraRollItemState.picking) { CameraRollItemReducer.reduce($0, $1) }
    }

    private func items(_ count: Int, kind: String = "image") -> [CameraRollUploadItem] {
        (0..<count).map { CameraRollUploadItem(id: "item-\($0)", kind: kind, duration: 3) }
    }

    func testOneItemThrowingNeverStopsTheOthers() async {
        let events = await run(items(7), failing: ["item-2"])
        let uploaded = await events.uploaded
        XCTAssertEqual(Set(uploaded), Set((0..<7).map { "item-\($0)" }).subtracting(["item-2"]))
        let byID = await events.byID
        XCTAssertEqual(finalState(byID["item-2"]), .needsRetry(.uploadFailed))
        for id in uploaded { XCTAssertEqual(finalState(byID[id]), .queued, id) }
    }

    func testConcurrencyNeverExceedsThreeAndReachesIt() async {
        let gauge = Gauge()
        _ = await run(items(10), gauge: gauge)
        let peak = await gauge.peak
        XCTAssertEqual(peak, 3)
    }

    func testStuckICloudExportTimesOutWithoutBlockingSiblings() async {
        let events = await run(items(4), timeout: .milliseconds(80), export: { item in
            if item.id == "item-1" { try await Task.sleep(for: .seconds(30)) }
            return try makeFile("\(item.id).jpg")
        })
        let byID = await events.byID
        XCTAssertEqual(finalState(byID["item-1"]), .needsRetry(.icloudUnavailable))
        let uploaded = await events.uploaded
        XCTAssertEqual(Set(uploaded), ["item-0", "item-2", "item-3"])
    }

    func testPreflightRejectsBeforeExportOrUpload() async {
        let exported = Events()
        let jobs = [CameraRollUploadItem(id: "long", kind: "video", duration: 31 * 60),
                    CameraRollUploadItem(id: "gif", kind: "image", duration: 0),
                    CameraRollUploadItem(id: "ok", kind: "image", duration: 0)]
        let events = await run(jobs, export: { item in
            await exported.add(item.id, .exportStarted)
            return try makeFile(item.id == "gif" ? "gif.gif" : "\(item.id).jpg")
        })
        let byID = await events.byID
        XCTAssertEqual(byID["long"], [.rejected(.tooLong)])
        XCTAssertEqual(finalState(byID["gif"]), .excluded(.unsupportedType))
        XCTAssertEqual(finalState(byID["ok"]), .queued)
        let exportedIDs = Set(await exported.byID.keys)
        XCTAssertFalse(exportedIDs.contains("long"), "a too-long video must never be downloaded from iCloud")
        let uploaded = await events.uploaded
        XCTAssertEqual(uploaded, ["ok"])
    }
}

@MainActor
final class CameraRollItemEngineTests: XCTestCase {
    private final class Server: CameraRollItemsClient, @unchecked Sendable {
        private let lock = NSLock()
        private var snapshots: [[CameraRollServerItem]]
        private(set) var polls = 0
        private(set) var retried: [UUID] = []
        var retryFails = false
        init(_ snapshots: [[CameraRollServerItem]]) { self.snapshots = snapshots }
        func cameraRollItems(id: UUID) async throws -> CameraRollItemsSnapshot {
            let items = lock.withLock { () -> [CameraRollServerItem] in
                polls += 1
                return snapshots.count > 1 ? snapshots.removeFirst() : (snapshots.first ?? [])
            }
            return CameraRollItemsSnapshot(revision: 1, items: items)
        }
        func retrySavedMedia(id: UUID) async throws -> SavedMediaAsset {
            let fails = lock.withLock { () -> Bool in retried.append(id); return retryFails }
            if fails { throw URLError(.notConnectedToInternet) }
            return makeAsset(id: id)
        }
    }

    private func serverItem(_ asset: UUID, _ state: String, reason: String? = nil) -> CameraRollServerItem {
        let json = """
        {"asset_id":"\(asset.uuidString)","kind":"video","state":"\(state)","reason":\(reason.map { "\"\($0)\"" } ?? "null"),"retryable":false,"attempts":1}
        """
        return try! JSONDecoder().decode(CameraRollServerItem.self, from: Data(json.utf8))
    }

    private func entries(_ ids: [String], assets: [String: UUID]) throws -> [CameraRollCollection.Entry] {
        var collection = try CameraRollCollection(name: "Trip", identifiers: ids)
        for (id, assetID) in assets {
            collection.bind(makeAsset(id: assetID, kind: "video"), to: id)
        }
        return collection.entries
    }

    func testPollingRunsUntilEveryItemSettlesThenStops() async throws {
        let a = UUID(), b = UUID()
        let server = Server([
            [serverItem(a, "analyzing"), serverItem(b, "queued")],
            [serverItem(a, "ready"), serverItem(b, "analyzing")],
            [serverItem(a, "ready"), serverItem(b, "needs_retry", reason: "provider_error")],
        ])
        let engine = CameraRollItemEngine(client: server, pollInterval: .milliseconds(1), sleep: { try await Task.sleep(for: $0) })
        engine.sync(entries: try entries(["a", "b"], assets: ["a": a, "b": b]))
        XCTAssertEqual(engine.store.awaitsServerCount, 2)
        engine.startPolling(collectionID: UUID())
        for _ in 0..<200 where engine.store.awaitsServerCount > 0 { try await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(engine.store.state(of: "a"), .ready)
        XCTAssertEqual(engine.store.state(of: "b"), .needsRetry(.providerError))
        let polls = server.polls
        try await Task.sleep(for: .milliseconds(30))
        XCTAssertEqual(server.polls, polls, "nothing is waiting on the server, so polling must have stopped")
    }

    func testStopPollingEndsAPollThatIsStillWaiting() async throws {
        let a = UUID()
        let server = Server([[serverItem(a, "analyzing")]])
        let engine = CameraRollItemEngine(client: server, pollInterval: .milliseconds(1), sleep: { try await Task.sleep(for: $0) })
        engine.sync(entries: try entries(["a"], assets: ["a": a]))
        engine.startPolling(collectionID: UUID())
        for _ in 0..<200 where server.polls < 3 { try await Task.sleep(for: .milliseconds(5)) }
        engine.stopPolling()
        try await Task.sleep(for: .milliseconds(20))
        let polls = server.polls
        try await Task.sleep(for: .milliseconds(40))
        XCTAssertEqual(server.polls, polls, "a dismissed screen must not keep polling an item that is still analyzing")
    }

    func testADuplicateUploadDropsItsTileAndLeavesTheSurvivorMapped() async throws {
        let shared = UUID()
        let server = Server([[serverItem(shared, "analyzing")]])
        let engine = CameraRollItemEngine(client: server, pollInterval: .milliseconds(1), sleep: { try await Task.sleep(for: $0) })
        final class Box: @unchecked Sendable { var collection: CameraRollCollection; init(_ c: CameraRollCollection) { collection = c } }
        let box = Box(try CameraRollCollection(name: "Trip", identifiers: ["a", "b"]))
        box.collection.bind(makeAsset(id: shared, kind: "video"), to: "a")
        engine.sync(entries: box.collection.entries)
        let loop = CameraRollUploadLoop(
            export: { item in try makeFile("\(item.id).jpg") },
            upload: { _, _, progress in progress(1); return makeAsset(id: shared, kind: "video") })
        // `b` is the same photo as `a`, so binding removes `b` and `a` keeps the asset.
        await engine.upload([CameraRollUploadItem(id: "b", kind: "image", duration: 0)], loop: loop, pollingCollectionID: UUID()) { id, asset in
            box.collection.bind(asset, to: id)
            return box.collection.entries
        }
        XCTAssertNil(engine.store.state(of: "b"), "the removed entry's tile must disappear")
        for _ in 0..<200 where engine.store.state(of: "a") != .analyzing { try await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(engine.store.state(of: "a"), .analyzing, "the survivor receives the server's snapshots")
        engine.stopPolling()
    }

    func testSettledItemsAreNotUploadedAgain() throws {
        let engine = CameraRollItemEngine(client: Server([[]]))
        engine.sync(entries: try entries(["rejected", "revoked", "flaky", "fresh"], assets: [:]))
        engine.store.send(.rejected(.tooLarge), to: "rejected")
        engine.store.send(.exportStarted, to: "revoked"); engine.store.send(.unreachable(.accessRevoked), to: "revoked")
        engine.store.send(.exportStarted, to: "flaky"); engine.store.send(.failedLocally(.uploadFailed), to: "flaky")
        XCTAssertFalse(engine.needsUpload("rejected"))
        XCTAssertFalse(engine.needsUpload("revoked"))
        XCTAssertTrue(engine.needsUpload("flaky"), "a retryable local failure is worth another attempt")
        XCTAssertTrue(engine.needsUpload("fresh"))
    }

    func testRetryOptimisticallyAnalyzesAndRevertsWhenTheCallFails() async throws {
        let a = UUID()
        let server = Server([[serverItem(a, "needs_retry", reason: "provider_error")]])
        let engine = CameraRollItemEngine(client: server, pollInterval: .seconds(60), sleep: { _ in throw CancellationError() })
        engine.sync(entries: try entries(["a"], assets: ["a": a]))
        engine.apply(CameraRollItemsSnapshot(revision: 1, items: [serverItem(a, "needs_retry", reason: "provider_error")]))
        XCTAssertEqual(engine.store.state(of: "a"), .needsRetry(.providerError))
        server.retryFails = true
        let outcome = await engine.retry(entryID: "a", collectionID: UUID())
        XCTAssertEqual(outcome, .failed)
        XCTAssertEqual(server.retried, [a])
        XCTAssertEqual(engine.store.state(of: "a"), .needsRetry(.providerError), "a failed retry call must put the tile back")
    }

    func testRetrySendsTheAssetBackToAnalyzingAndPollsToTheNewResult() async throws {
        let a = UUID()
        let server = Server([[serverItem(a, "analyzing")], [serverItem(a, "ready")]])
        let engine = CameraRollItemEngine(client: server, pollInterval: .milliseconds(1), sleep: { try await Task.sleep(for: $0) })
        engine.sync(entries: try entries(["a"], assets: ["a": a]))
        engine.apply(CameraRollItemsSnapshot(revision: 1, items: [serverItem(a, "needs_retry", reason: "budget_paused")]))
        let outcome = await engine.retry(entryID: "a", collectionID: UUID())
        XCTAssertEqual(outcome, .requested)
        XCTAssertEqual(server.retried, [a])
        XCTAssertEqual(engine.store.state(of: "a"), .analyzing)
        for _ in 0..<200 where engine.store.state(of: "a") != .ready { try await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(engine.store.state(of: "a"), .ready)
    }

    func testRetryOfAnItemThatNeverUploadedAsksTheCallerToReupload() async throws {
        let server = Server([[]])
        let engine = CameraRollItemEngine(client: server)
        engine.sync(entries: try entries(["local"], assets: [:]))
        engine.store.send(.exportStarted, to: "local")
        engine.store.send(.failedLocally(.uploadFailed), to: "local")
        let result = await engine.retryAllFailed(collectionID: UUID())
        XCTAssertEqual(result, .init(requested: 0, failed: 0, needsReupload: ["local"]))
        XCTAssertTrue(server.retried.isEmpty)
    }

    func testAProgressTickRedrawsOnlyItsOwnTile() throws {
        let store = CameraRollItemStore()
        store.register(["a", "b"])
        store.send(.exportStarted, to: "a"); store.send(.exportStarted, to: "b")
        final class Redraws: @unchecked Sendable { var a = 0, b = 0, header = 0 }
        let redraws = Redraws()
        func watchA() { withObservationTracking { _ = store.tile("a").state } onChange: { redraws.a += 1 } }
        func watchB() { withObservationTracking { _ = store.tile("b").state } onChange: { redraws.b += 1 } }
        func watchHeader() { withObservationTracking { _ = store.count(.uploading) } onChange: { redraws.header += 1 } }
        watchA(); watchB(); watchHeader()
        store.send(.uploadProgress(0.1), to: "a")   // exporting -> uploading: a phase change
        XCTAssertEqual([redraws.a, redraws.b, redraws.header], [1, 0, 1])
        watchA(); watchHeader()
        store.send(.uploadProgress(0.5), to: "a")   // a pure tick
        XCTAssertEqual([redraws.a, redraws.b, redraws.header], [2, 0, 1], "a tick must not redraw the header or other tiles")
        XCTAssertEqual(store.count(.uploading), 1)
    }
}

final class CameraRollIdeaBoardTests: XCTestCase {
    private func story(_ id: UUID = UUID(), group: String) -> CameraRollStory {
        let json = """
        {"id":"\(id.uuidString)","revision":1,"title":"t","hook":"h","payoff":"p","rationale":"r","duration_s":10,"moments":[],"rejected":false,"audio_policy":"original","group":"\(group)"}
        """
        return try! JSONDecoder().decode(CameraRollStory.self, from: Data(json.utf8))
    }
    private func member(_ group: String) -> CameraRollCollectionMember {
        CameraRollCollectionMember(asset_id: UUID(), source_id: UUID(), group: group)
    }
    private func ids(_ board: CameraRollIdeaBoard, _ group: String) -> [UUID] { (board.storiesByGroup[group] ?? []).map(\.id) }

    func testDeleteThenUndoRestoresTheIdeaInPlace() {
        let one = story(group: "Paris"), two = story(group: "Paris"), three = story(group: "Paris")
        var board = CameraRollIdeaBoard(storiesByGroup: ["Paris": [one, two, three]])
        XCTAssertEqual(board.reduce(.deleteIdea(storyID: two.id, group: "Paris")), [.deleteStory(two.id)])
        XCTAssertEqual(ids(board, "Paris"), [one.id, three.id])
        XCTAssertEqual(board.reduce(.undoDeleteIdea(storyID: two.id)), [.restoreStory(two.id)])
        XCTAssertEqual(ids(board, "Paris"), [one.id, two.id, three.id])
        XCTAssertTrue(board.reduce(.undoDeleteIdea(storyID: two.id)).isEmpty, "undo is single-use")
    }

    func testAStaleUndoDoesNothing() {
        let one = story(group: "A"), two = story(group: "A")
        var board = CameraRollIdeaBoard(storiesByGroup: ["A": [one, two]])
        board.reduce(.deleteIdea(storyID: one.id, group: "A"))
        board.reduce(.deleteIdea(storyID: two.id, group: "A"))
        XCTAssertTrue(board.reduce(.undoDeleteIdea(storyID: one.id)).isEmpty, "a new deletion makes the earlier one final")
        XCTAssertEqual(board.reduce(.undoDeleteIdea(storyID: two.id)), [.restoreStory(two.id)])
        board.reduce(.undoExpired)
        XCTAssertNil(board.undo)
    }

    func testAnotherIdeaExcludesExistingIdeasAddsOneAndIgnoresDoubleTaps() {
        let one = story(group: "Paris")
        var board = CameraRollIdeaBoard(storiesByGroup: ["Paris": [one], "Rome": []])
        XCTAssertEqual(board.reduce(.anotherIdea(group: "Paris")), [.generate(group: "Paris", excluding: [one.id])])
        XCTAssertTrue(board.reduce(.anotherIdea(group: "Paris")).isEmpty)
        XCTAssertEqual(board.reduce(.anotherIdea(group: "Rome")), [.generate(group: "Rome", excluding: [])])
        let fresh = story(group: "Paris")
        board.reduce(.ideasArrived(group: "Paris", stories: [fresh, one]))
        XCTAssertEqual(ids(board, "Paris"), [one.id, fresh.id], "existing ideas stay; a repeat is not added twice")
        board.reduce(.generationFailed(group: "Rome"))
        XCTAssertFalse(board.generating.contains("Rome"))
    }

    func testDeletingAnEventLeavesOtherEventsAndUndoBringsItBack() {
        let paris = story(group: "Paris"), rome = story(group: "Rome")
        let members = [member("Paris"), member("Paris")]
        var board = CameraRollIdeaBoard(storiesByGroup: ["Paris": [paris], "Rome": [rome]])
        XCTAssertEqual(board.reduce(.deleteEvent(group: "Paris", members: members)), [.deleteGroup("Paris")])
        XCTAssertNil(board.storiesByGroup["Paris"])
        XCTAssertEqual(ids(board, "Rome"), [rome.id])
        XCTAssertEqual(board.reduce(.undoDeleteEvent(group: "Paris")), [.restoreGroup(group: "Paris", members: members, storyIDs: [paris.id])])
        XCTAssertEqual(ids(board, "Paris"), [paris.id])
    }

    func testAnIdeaResultForADeletedEventIsDropped() {
        var board = CameraRollIdeaBoard(storiesByGroup: ["Paris": []])
        board.reduce(.anotherIdea(group: "Paris"))
        board.reduce(.deleteEvent(group: "Paris", members: []))
        board.reduce(.ideasArrived(group: "Paris", stories: [story(group: "Paris")]))
        XCTAssertNil(board.storiesByGroup["Paris"])
    }

    func testARefusedRestoreRemovesTheIdeasAgain() {
        let one = story(group: "A")
        var board = CameraRollIdeaBoard(storiesByGroup: ["A": [one]])
        board.reduce(.deleteIdea(storyID: one.id, group: "A"))
        board.reduce(.undoDeleteIdea(storyID: one.id))
        board.reduce(.restoreFailed(storyIDs: [one.id]))
        XCTAssertTrue(ids(board, "A").isEmpty)
    }
}
