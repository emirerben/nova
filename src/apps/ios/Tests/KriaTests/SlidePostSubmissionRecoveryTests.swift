import XCTest
@testable import Kria

@MainActor enum SlidePostTestSubmissionStorage {
    private static var roots: [ObjectIdentifier: URL] = [:]
    private static var owners: [ObjectIdentifier: UserDefaults] = [:]
    static func reset(defaults: UserDefaults) {
        let key = ObjectIdentifier(defaults)
        if let root = roots.removeValue(forKey: key) { try? FileManager.default.removeItem(at: root) }
        owners.removeValue(forKey: key)
    }
    static func api() -> KriaAPI {
        let payload = Data(#"{"sub":"isolated-unit-test"}"#.utf8).base64EncodedString()
        return NativeEditorTestSupport.api(tokenStore: NativeEditorMemoryTokenStore(.init(accessToken: "test.\(payload).test", refreshToken: "fixture", expiresIn: 3600)))
    }
    static func journal(defaults: UserDefaults) -> SlidePostSubmissionJournal {
        let key = ObjectIdentifier(defaults)
        owners[key] = defaults
        if roots[key] == nil { roots[key] = FileManager.default.temporaryDirectory.appending(path: "SlidePostTestSession-\(UUID())") }
        return .init(root: roots[key]!)
    }
    static func session(defaults: UserDefaults = UserDefaults(suiteName: "SlidePostTestSession-\(UUID())")!, now: @escaping () -> Date = Date.init) -> SlidePostSession {
        SlidePostSession(defaults: defaults, submissionJournal: journal(defaults: defaults),
            submissionScope: { _, id in .init(owner: "isolated-unit-test", environment: "https://native-editor.test", itemID: id) }, submissionNow: now,
            localProjectID: { $0.aiProjectID ?? SlidePostAIStubTransport.projectID }, transportOwner: { _ in nil })
    }
}

final class SlidePostSubmissionRecoveryTests: XCTestCase {
    private var root: URL!

    override func setUp() {
        super.setUp()
        root = FileManager.default.temporaryDirectory.appending(path: "slide-submission-tests-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: root)
        super.tearDown()
    }

    private func scope(owner: String = "owner-a", environment: String = "https://api-a.test", itemID: String = "item-a") -> SlidePostSubmissionScope {
        .init(owner: owner, environment: environment, itemID: itemID)
    }

    func testAuthenticatedScopeCanonicalizesItemAndHostWithoutUsingRotatingTokens() throws {
        let payload = Data("{\"sub\":\"owner-a\"}".utf8).base64EncodedString().replacingOccurrences(of: "=", with: "")
        let token = NativeEditorMemoryTokenStore(.init(accessToken: "test.\(payload).test", refreshToken: "one", expiresIn: 3600))
        let upper = KriaAPI(baseURL: URL(string: "https://API.Example.test/v1/")!, tokenStore: token, session: NativeEditorTestSupport.session())
        let lower = KriaAPI(baseURL: URL(string: "https://api.example.test/v1")!, tokenStore: token, session: NativeEditorTestSupport.session())
        let item = "ABCDEFAB-1111-2222-3333-ABCDEFABCDEF"
        let first = try SlidePostSubmissionJournal.authenticatedScope(api: upper, itemID: item)
        try token.write(.init(accessToken: "test.\(payload).test", refreshToken: "rotated", expiresIn: 3600))
        XCTAssertEqual(first, try SlidePostSubmissionJournal.authenticatedScope(api: lower, itemID: item.lowercased()))
        XCTAssertThrowsError(try SlidePostSubmissionJournal.authenticatedScope(api: NativeEditorTestSupport.api(), itemID: item))
    }

    func testReceiptStatusCanChangeButPinnedJobCannot() throws {
        let key = scope(itemID: "11111111-1111-1111-1111-111111111111")
        let journal = SlidePostSubmissionJournal(root: root)
        let original = try journal.reserve(.init(scope: key, expectedVersion: 1, previousJobID: nil)).record
        let job = UUID()
        let receipt = SlidePostGenerationReceipt(clientRequestID: original.operationID, itemID: UUID(uuidString: key.itemID)!, draftVersion: 1,
            jobID: job, dispatchState: .unconfirmed, jobStatus: "queued", isCurrentTarget: true)
        try journal.update(key, operationID: original.operationID) { $0.receipt = receipt }
        let updated = SlidePostGenerationReceipt(clientRequestID: original.operationID, itemID: receipt.itemID, draftVersion: 1,
            jobID: job, dispatchState: .published, jobStatus: "variants_ready", isCurrentTarget: false)
        try journal.update(key, operationID: original.operationID) { $0.receipt = updated }
        XCTAssertEqual(try journal.load(key)?.receipt, updated)
        XCTAssertThrowsError(try journal.update(key, operationID: original.operationID) {
            $0.receipt = .init(clientRequestID: original.operationID, itemID: receipt.itemID, draftVersion: 1,
                jobID: UUID(), dispatchState: .published, jobStatus: "queued", isCurrentTarget: true)
        })
        XCTAssertEqual(try journal.load(key)?.receipt?.jobID, job)
    }

    func testRecreatedJournalRestoresPreparedOperationInsteadOfAllocatingAnother() throws {
        let key = scope()
        let intent = SlidePostSubmissionRecord(scope: key, expectedVersion: 3, previousJobID: UUID())
        let first = try SlidePostSubmissionJournal(root: root).reserve(intent)
        XCTAssertTrue(first.inserted)
        let reopened = SlidePostSubmissionJournal(root: root)
        let next = try reopened.reserve(.init(scope: key, expectedVersion: 3, previousJobID: nil))
        XCTAssertFalse(next.inserted)
        XCTAssertEqual(next.record.operationID, intent.operationID)
        XCTAssertEqual(next.record.phase, .prepared)
        XCTAssertEqual(next.record.expectedVersion, 3)
        XCTAssertEqual(next.record.previousJobID, intent.previousJobID)
    }

    func testOwnerEnvironmentAndItemHaveSeparateReservations() throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let keys = [scope(), scope(owner: "owner-b"), scope(environment: "https://api-b.test"), scope(itemID: "item-b")]
        var operations = Set<UUID>()
        for key in keys {
            let result = try journal.reserve(.init(scope: key, expectedVersion: 1, previousJobID: nil))
            XCTAssertTrue(result.inserted)
            operations.insert(result.record.operationID)
        }
        XCTAssertEqual(operations.count, 4)
        for key in keys { XCTAssertEqual(try journal.load(key)?.scope, key) }
    }

    func testNewVersionCannotOverwriteUnresolvedIntentWithoutComparedClear() throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let key = scope()
        let old = try journal.reserve(.init(scope: key, expectedVersion: 2, previousJobID: nil)).record
        let newer = try journal.reserve(.init(scope: key, expectedVersion: 3, previousJobID: nil))
        XCTAssertFalse(newer.inserted)
        XCTAssertEqual(newer.record.operationID, old.operationID)
        XCTAssertEqual(newer.record.expectedVersion, 2)
        XCTAssertFalse(try journal.clear(key, operationID: UUID()))
        XCTAssertEqual(try journal.load(key)?.operationID, old.operationID)
        XCTAssertTrue(try journal.clear(key, operationID: old.operationID))
        XCTAssertTrue(try journal.reserve(.init(scope: key, expectedVersion: 3, previousJobID: nil)).inserted)
    }

    func testExhaustedBudgetSurvivesRecreationAndCannotBeReset() throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let key = scope()
        let intent = try journal.reserve(.init(scope: key, expectedVersion: 1, previousJobID: nil)).record
        XCTAssertTrue(try journal.update(key, operationID: intent.operationID) {
            $0.phase = .exhausted
            $0.remainingReads = 0
        })
        let reopened = SlidePostSubmissionJournal(root: root)
        XCTAssertEqual(try reopened.load(key)?.phase, .exhausted)
        XCTAssertEqual(try reopened.load(key)?.remainingReads, 0)
        XCTAssertThrowsError(try reopened.update(key, operationID: intent.operationID) {
            $0.phase = .prepared
            $0.remainingReads = 200
        })
        XCTAssertEqual(try reopened.load(key)?.remainingReads, 0)
    }

    func testWriteFailureNeverReturnsDispatchAuthorization() throws {
        let io = SlidePostSubmissionJournal.IO(read: { _ in nil }, write: { _, _ in throw CocoaError(.fileWriteOutOfSpace) }, remove: { _ in })
        let journal = SlidePostSubmissionJournal(root: root, io: io)
        var dispatches = 0
        do {
            _ = try journal.reserve(.init(scope: scope(), expectedVersion: 1, previousJobID: nil))
            dispatches += 1
        } catch { }
        XCTAssertEqual(dispatches, 0)
    }

    func testUnreadableAndCorruptRecordsFailClosedRatherThanLookingAbsent() throws {
        let unreadable = SlidePostSubmissionJournal(root: root, io: .init(read: { _ in throw CocoaError(.fileReadNoPermission) }, write: { _, _ in XCTFail("Must not overwrite unreadable intent") }, remove: { _ in }))
        XCTAssertThrowsError(try unreadable.reserve(.init(scope: scope(), expectedVersion: 1, previousJobID: nil)))
        let corrupt = SlidePostSubmissionJournal(root: root, io: .init(read: { _ in Data("not a journal".utf8) }, write: { _, _ in XCTFail("Must not overwrite corrupt intent") }, remove: { _ in }))
        XCTAssertThrowsError(try corrupt.reserve(.init(scope: scope(), expectedVersion: 1, previousJobID: nil)))
        let foreign = try JSONEncoder().encode(SlidePostSubmissionRecord(scope: scope(owner: "other-owner"), expectedVersion: 1, previousJobID: nil))
        let wrongOwner = SlidePostSubmissionJournal(root: root, io: .init(read: { _ in foreign }, write: { _, _ in XCTFail("Must not overwrite another owner") }, remove: { _ in }))
        XCTAssertThrowsError(try wrongOwner.load(scope()))
    }

    func testFailedClearAndStaleCallbackKeepTheCurrentIntent() throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let key = scope()
        let original = try journal.reserve(.init(scope: key, expectedVersion: 1, previousJobID: nil)).record
        let live = SlidePostSubmissionJournal.IO.fileSystem
        let failingClear = SlidePostSubmissionJournal(root: root, io: .init(read: live.read, write: live.write, remove: { _ in throw CocoaError(.fileWriteNoPermission) }))
        XCTAssertThrowsError(try failingClear.clear(key, operationID: original.operationID))
        XCTAssertEqual(try journal.load(key)?.operationID, original.operationID)
        XCTAssertTrue(try journal.clear(key, operationID: original.operationID))
        let replacement = try journal.reserve(.init(scope: key, expectedVersion: 2, previousJobID: nil)).record
        XCTAssertFalse(try journal.clear(key, operationID: original.operationID))
        XCTAssertFalse(try journal.update(key, operationID: original.operationID) { $0.remainingReads = 0 })
        XCTAssertEqual(try journal.load(key)?.operationID, replacement.operationID)
    }
}

@MainActor final class SlidePostSessionRecoveryTests: XCTestCase {
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private var root: URL!
    private var defaults: UserDefaults!
    private var suite = ""
    private var now = Date(timeIntervalSince1970: 1_000)

    private final class Server: @unchecked Sendable {
        var state: SlidePostState
        var requests: [UUID] = []
        var receipts: [UUID: SlidePostGenerationReceipt] = [:]
        var loseResponses = false
        var wrongReceiptID = false
        var receiptCurrentTargetOverride: Bool?
        var receiptReads = 0
        var stateAfterReceiptRead: SlidePostState?
        var switchTargetOnAdmission: UUID?
        var admittedTargets: [UUID: String] = [:]
        var dispatchState: SlidePostGenerationReceipt.DispatchState = .published
        init(_ state: SlidePostState) { self.state = state }
        func handle(_ request: URLRequest) throws -> (Int, Data) {
            if request.httpMethod == "GET" {
                if request.url!.path.contains("/generation-requests/"), let key = UUID(uuidString: request.url!.lastPathComponent), let pinned = receipts[key] {
                    receiptReads += 1
                    let current = receiptCurrentTargetOverride ?? (state.jobID == pinned.jobID)
                    let latest = SlidePostGenerationReceipt(clientRequestID: pinned.clientRequestID, itemID: pinned.itemID,
                        draftVersion: pinned.draftVersion, jobID: pinned.jobID, dispatchState: pinned.dispatchState,
                        jobStatus: state.jobID == nil ? nil : state.renderStatus, isCurrentTarget: current)
                    if let next = stateAfterReceiptRead { state = next; stateAfterReceiptRead = nil }
                    return (200, try JSONEncoder().encode(latest))
                }
                return (200, try JSONEncoder().encode(state))
            }
            XCTAssertTrue(request.url!.path.hasSuffix("/generation-requests"), "No legacy generate fallback")
            let body = try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as! [String: Any]
            let operation = UUID(uuidString: body["client_request_id"] as! String)!
            let version = body["expected_version"] as! Int
            requests.append(operation)
            XCTAssertNotNil(body["expected_current_job_id"], "The nullable target must be explicit on the wire")
            let expectedTarget = (body["expected_current_job_id"] as? String).flatMap(UUID.init(uuidString:))
            let targetKey = expectedTarget?.uuidString ?? "null"
            if let original = admittedTargets[operation], original != targetKey { return (409, Data()) }
            if receipts[operation] == nil {
                if let successor = switchTargetOnAdmission { state.jobID = successor; switchTargetOnAdmission = nil }
                guard state.jobID == expectedTarget else { return (409, Data()) }
                admittedTargets[operation] = targetKey
                receipts[operation] = .init(clientRequestID: operation, itemID: UUID(uuidString: state.itemID)!, draftVersion: version,
                    jobID: UUID(), dispatchState: dispatchState, jobStatus: nil, isCurrentTarget: true)
                state.jobID = receipts[operation]!.jobID
            }
            if loseResponses { throw URLError(.networkConnectionLost) }
            var receipt = receipts[operation]!
            if wrongReceiptID { receipt.clientRequestID = UUID() }
            return (200, try JSONEncoder().encode(["generation_receipt": receipt]))
        }
    }

    override func setUp() {
        super.setUp()
        suite = "SlidePostSessionRecoveryTests.\(UUID())"
        defaults = UserDefaults(suiteName: suite)!
        root = FileManager.default.temporaryDirectory.appending(path: suite)
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        SlidePostRetryAfterURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: root)
        super.tearDown()
    }
    private var scope: SlidePostSubmissionScope { .init(owner: "test-owner", environment: "https://native-editor.test", itemID: itemID) }
    private func snapshot(version: Int = 1, capability: Int? = 1) -> SlidePostState {
        let slide = SlidePostSlide(id: "one", assetID: "a", kind: "image")
        return SlidePostState(itemID: itemID, title: "Post", draft: .init(version: version, platformProfile: "tiktok_photo", slides: [slide], caption: "Caption"),
            assets: [.init(id: "a", kind: "image", status: "ready")], generationIdempotencyVersion: capability)
    }
    private func session(journal: SlidePostSubmissionJournal? = nil) -> SlidePostSession {
        let key = scope
        return SlidePostSession(defaults: defaults, submissionJournal: journal ?? .init(root: root),
            submissionScope: { _, id in .init(owner: key.owner, environment: key.environment, itemID: id) }, submissionNow: { self.now }, transportOwner: { _ in nil })
    }

    func testPreparedIntentAutomaticallyResumesSameIDOnFreshSessionRefresh() async throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let prepared = try journal.reserve(.init(scope: scope, expectedVersion: 1, previousJobID: nil)).record
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let reopened = session(journal: .init(root: root))
        await reopened.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(server.requests, [prepared.operationID])
        XCTAssertEqual(server.receipts.count, 1)
        XCTAssertEqual(try journal.load(scope)?.receipt?.clientRequestID, prepared.operationID)
    }

    func testCommittedLostResponseAcrossSessionRecreationReusesOneServerJob() async throws {
        let server = Server(snapshot())
        server.loseResponses = true
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let first = session()
        await first.refresh(api: api, itemID: itemID)
        await first.create(api: api, itemID: itemID)
        let operation = try XCTUnwrap(server.requests.first)
        server.loseResponses = false
        now.addTimeInterval(10)
        let reopened = session(journal: .init(root: root))
        await reopened.refresh(api: api, itemID: itemID)
        XCTAssertEqual(server.requests, [operation, operation])
        XCTAssertEqual(server.receipts.count, 1, "Replay returns the committed receipt, not another job")
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.operationID, operation)
    }

    func testReservationWriteFailurePreventsAnyGeneratePOST() async {
        let io = SlidePostSubmissionJournal.IO(read: { _ in nil }, write: { _, _ in throw CocoaError(.fileWriteOutOfSpace) }, remove: { _ in })
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session(journal: .init(root: root, io: io))
        await value.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await value.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertTrue(server.requests.isEmpty)
        XCTAssertNotNil(value.error)
        XCTAssertEqual(value.draft?.caption, "Caption")
    }

    func testOldServerCannotResumePreparedIntentOrReceiveLegacyFallback() async throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let prepared = try journal.reserve(.init(scope: scope, expectedVersion: 1, previousJobID: nil)).record
        let server = Server(snapshot(capability: nil))
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session(journal: journal)
        await value.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await value.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertTrue(server.requests.isEmpty)
        XCTAssertNotNil(value.error)
        XCTAssertEqual(try journal.load(scope)?.operationID, prepared.operationID)
    }

    func testMalformedReceiptKeepsTheDurableFence() async throws {
        let server = Server(snapshot())
        server.wrongReceiptID = true
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        await value.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await value.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertNotNil(value.error)
        let record = try XCTUnwrap(SlidePostSubmissionJournal(root: root).load(scope))
        XCTAssertEqual(record.operationID, server.requests.first)
        XCTAssertNil(record.receipt)
    }

    func testKnownUnconfirmedReceiptIsNotRepostedOrReplacedAfterRelaunch() async throws {
        let server = Server(snapshot())
        server.dispatchState = .unconfirmed
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let first = session()
        await first.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await first.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        let operation = try XCTUnwrap(server.requests.first)
        now.addTimeInterval(30)
        let reopened = session()
        await reopened.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await reopened.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(server.requests, [operation])
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.receipt?.dispatchState, .unconfirmed)
        XCTAssertNotNil(reopened.error)
    }

    func testReplayBudgetSurvivesRepeatedSessionRecreation() async throws {
        let server = Server(snapshot())
        server.loseResponses = true
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let first = session()
        await first.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await first.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        for _ in 0..<4 {
            now.addTimeInterval(30)
            await session().refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        }
        XCTAssertEqual(server.requests.count, 3)
        XCTAssertEqual(Set(server.requests).count, 1)
        XCTAssertEqual(server.receipts.count, 1)
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.remainingPostAttempts, 0)
    }

    func testNewerRemoteVersionClearsOnlyTheOldIntentWithoutReplayingIt() async throws {
        let journal = SlidePostSubmissionJournal(root: root)
        _ = try journal.reserve(.init(scope: scope, expectedVersion: 1, previousJobID: nil))
        let server = Server(snapshot(version: 2))
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session(journal: journal)
        await value.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(value.state?.draft?.version, 2)
        XCTAssertTrue(server.requests.isEmpty)
        XCTAssertNil(try journal.load(scope))
    }
    func testOnlyExplicitCreateAfterMatchingTerminalFailureStartsAnotherOperation() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        let old = try XCTUnwrap(server.requests.first)
        server.state.jobID = server.receipts[old]!.jobID
        server.state.renderStatus = "failed"
        await value.refresh(api: api, itemID: itemID)
        XCTAssertEqual(server.requests, [old], "Polling cannot request replacement work")
        await value.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests.count, 2)
        XCTAssertNotEqual(server.requests.last, old)
    }

    func testUnrelatedFailedJobDoesNotReleaseUnconfirmedIntent() async throws {
        let server = Server(snapshot())
        server.dispatchState = .unconfirmed
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        let old = try XCTUnwrap(server.requests.first)
        server.state.jobID = UUID()
        server.state.renderStatus = "failed"
        server.receiptCurrentTargetOverride = true
        await value.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests, [old])
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.operationID, old)
    }

    func testPersistedReplayDeadlineSurvivesRecreation() async throws {
        let journal = SlidePostSubmissionJournal(root: root)
        let prepared = try journal.reserve(.init(scope: scope, expectedVersion: 1, previousJobID: nil)).record
        try journal.update(scope, operationID: prepared.operationID) { $0.nextReplayAt = now.addingTimeInterval(60) }
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let origin = now
        for seconds in [3.0, 59.0] {
            now = origin.addingTimeInterval(seconds)
            await session().refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
            XCTAssertTrue(server.requests.isEmpty)
        }
        now = origin.addingTimeInterval(60)
        await session().refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(server.requests, [prepared.operationID])
    }

    func testAccountChangeCannotDispatchThePreviouslyLoadedDraft() async throws {
        var owner = "first-owner"
        let value = SlidePostSession(defaults: defaults, submissionJournal: .init(root: root),
            submissionScope: { _, id in .init(owner: owner, environment: "https://native-editor.test", itemID: id) }, transportOwner: { _ in nil })
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        value.setCaption("Unsent edit")
        owner = "other-owner"
        await value.create(api: api, itemID: itemID)
        XCTAssertTrue(server.requests.isEmpty)
        XCTAssertEqual(value.draft?.caption, "Unsent edit")
        XCTAssertNotNil(value.error)
    }

    func testReadFailuresConsumeDurableWaitBudgetWithoutRepostingKnownReceipt() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        let journal = SlidePostSubmissionJournal(root: root)
        let record = try XCTUnwrap(journal.load(scope))
        try journal.update(scope, operationID: record.operationID) { $0.remainingReads = 1 }
        NativeEditorURLProtocol.handler = { _ in (403, Data()) }
        await value.refresh(api: api, itemID: itemID)
        XCTAssertFalse(value.isRendering)
        XCTAssertEqual(try journal.load(scope)?.remainingReads, 0)
        XCTAssertEqual(server.requests.count, 1)
    }

    func testFreshReceiptReleasesSameVersionSupersededIntentWithoutSubmitting() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        let successor = UUID()
        server.state.jobID = successor
        server.state.renderStatus = "rendering"
        await value.refresh(api: api, itemID: itemID)
        XCTAssertNil(try SlidePostSubmissionJournal(root: root).load(scope))
        XCTAssertEqual(value.state?.jobID, successor)
        XCTAssertEqual(server.requests.count, 1)
        XCTAssertGreaterThan(server.receiptReads, 0)
    }

    func testDeletedPinnedJobReleasesIntentOnlyThroughFreshReceipt() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        server.state.jobID = nil
        server.state.renderStatus = "failed"
        await value.refresh(api: api, itemID: itemID)
        XCTAssertNil(try SlidePostSubmissionJournal(root: root).load(scope))
        XCTAssertFalse(value.isRendering)
        XCTAssertEqual(server.requests.count, 1)
    }

    func testDifferentLaggingJobProjectionCannotClearStillCurrentReceipt() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        let operation = try XCTUnwrap(server.requests.first)
        server.state.jobID = UUID()
        server.state.renderStatus = "failed"
        server.receiptCurrentTargetOverride = true
        await value.refresh(api: api, itemID: itemID)
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.operationID, operation)
        XCTAssertTrue(value.isRendering)
        XCTAssertEqual(server.requests.count, 1)
    }

    func testReadRetryAfterIsHonoredAcrossSessionRecreation() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let first = session()
        await first.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        await first.create(api: NativeEditorTestSupport.api(), itemID: itemID)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SlidePostRetryAfterURLProtocol.self]
        let api = KriaAPI(baseURL: URL(string: "https://native-editor.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        var reads = 0
        var reject = true
        SlidePostRetryAfterURLProtocol.handler = { request, transport in
            if !request.url!.path.contains("/generation-requests/") { reads += 1 }
            if reject { reject = false; transport.respond(status: 503, retryAfter: 60); return }
            do { let (status, data) = try server.handle(request); transport.respond(status: status, data: data) }
            catch { XCTFail("Unexpected fixture failure: \(error)") }
        }
        await first.refresh(api: api, itemID: itemID)
        XCTAssertEqual(reads, 1)
        let origin = now
        for seconds in [3.0, 59.0] {
            now = origin.addingTimeInterval(seconds)
            await session().refresh(api: api, itemID: itemID)
            XCTAssertEqual(reads, 1, "A new session must retain the server's read deadline")
        }
        now = origin.addingTimeInterval(60)
        await session().refresh(api: api, itemID: itemID)
        XCTAssertEqual(reads, 2)
        XCTAssertEqual(server.requests.count, 1)
    }

    func testReadRetryCannotConsumeAnotherOwnersJournalAfterAccountSwitch() async throws {
        var owner = "first-owner"
        let journal = SlidePostSubmissionJournal(root: root)
        let firstScope = SlidePostSubmissionScope(owner: owner, environment: "https://native-editor.test", itemID: itemID)
        let otherScope = SlidePostSubmissionScope(owner: "other-owner", environment: firstScope.environment, itemID: itemID)
        _ = try journal.reserve(.init(scope: firstScope, expectedVersion: 1, previousJobID: nil))
        _ = try journal.reserve(.init(scope: otherScope, expectedVersion: 1, previousJobID: nil))
        let value = SlidePostSession(defaults: defaults, readRetrySleep: { _ in owner = "other-owner" },
            submissionJournal: journal, submissionScope: { _, id in .init(owner: owner, environment: firstScope.environment, itemID: id) }, transportOwner: { _ in nil })
        var reads = 0
        NativeEditorURLProtocol.handler = { _ in reads += 1; return (503, Data()) }
        await value.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(reads, 1)
        XCTAssertEqual(try journal.load(otherScope)?.remainingReads, 200)
        XCTAssertNil(value.state)
        XCTAssertNil(value.error)
    }

    func testExplicitCreateDoesNotReplaceWorkUsingPreReceiptSupersededSnapshot() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        server.state.jobID = UUID()
        server.state.renderStatus = "failed"
        await value.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests.count, 1)
        XCTAssertNil(try SlidePostSubmissionJournal(root: root).load(scope))
    }

    func testAccountSwitchDuringSavePreflightNeverPutsOrTouchesNewOwnerBudget() async throws {
        var owner = "first-owner"
        let journal = SlidePostSubmissionJournal(root: root)
        let firstScope = SlidePostSubmissionScope(owner: owner, environment: "https://native-editor.test", itemID: itemID)
        let otherScope = SlidePostSubmissionScope(owner: "other-owner", environment: firstScope.environment, itemID: itemID)
        _ = try journal.reserve(.init(scope: otherScope, expectedVersion: 1, previousJobID: nil))
        let value = SlidePostSession(defaults: defaults, readRetrySleep: { _ in owner = "other-owner" },
            submissionJournal: journal, submissionScope: { _, id in .init(owner: owner, environment: firstScope.environment, itemID: id) }, transportOwner: { _ in nil })
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        value.setCaption("Keep this unsent edit")
        var puts = 0
        NativeEditorURLProtocol.handler = { request in
            if request.httpMethod == "PUT" { puts += 1 }
            return (503, Data())
        }
        await value.save(api: api, itemID: itemID)
        XCTAssertEqual(puts, 0)
        XCTAssertEqual(value.draft?.caption, "Keep this unsent edit")
        XCTAssertEqual(try journal.load(otherScope)?.remainingReads, 200)
    }

    func testSupersededSnapshotAutomaticallyAdoptsReadySuccessorWithoutAnotherCreate() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        var successor = snapshot()
        successor.jobID = UUID()
        successor.renderStatus = "ready"
        successor.renderedVersion = 1
        successor.draft?.renderedVersion = 1
        successor.slides = [.init(id: "one", assetID: "a", kind: "image", url: URL(string: "https://fixture.test/one.jpg"))]
        server.receiptCurrentTargetOverride = false
        server.stateAfterReceiptRead = successor
        await value.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests.count, 1)
        XCTAssertEqual(value.state?.jobID, successor.jobID)
        XCTAssertTrue(value.canExport)
        XCTAssertNil(try SlidePostSubmissionJournal(root: root).load(scope))
    }

    func testChangedSameVersionTargetRejectsNewAdmissionWithoutCreatingJob() async throws {
        let server = Server(snapshot())
        server.state.jobID = UUID()
        server.switchTargetOnAdmission = UUID()
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests.count, 1)
        XCTAssertTrue(server.receipts.isEmpty, "Stale same-version admission must not mint paid work")
        XCTAssertNotNil(value.error)
        XCTAssertNil(try SlidePostSubmissionJournal(root: root).load(scope), "A definitive first-admission rejection is retryable with a fresh target")
    }

    func testExhaustedAutomaticReplayAllowsOneExplicitSameKeyObservationAfterDeadline() async throws {
        let server = Server(snapshot())
        server.loseResponses = true
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let api = NativeEditorTestSupport.api()
        let first = session()
        await first.refresh(api: api, itemID: itemID)
        await first.create(api: api, itemID: itemID)
        for _ in 0..<2 { now.addTimeInterval(30); await session().refresh(api: api, itemID: itemID) }
        let operation = try XCTUnwrap(server.requests.first)
        XCTAssertEqual(server.requests.count, 3)
        server.loseResponses = false
        now.addTimeInterval(30)
        let reopened = session()
        await reopened.refresh(api: api, itemID: itemID)
        XCTAssertEqual(server.requests.count, 3, "Foreground polling must not reset the automatic budget")
        await reopened.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests, Array(repeating: operation, count: 4))
        XCTAssertEqual(server.receipts.count, 1)
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.receipt?.clientRequestID, operation)
        XCTAssertEqual(try SlidePostSubmissionJournal(root: root).load(scope)?.remainingPostAttempts, 0)
        XCTAssertEqual(server.admittedTargets[operation], "null", "Replay must keep the original nullable target")
    }

    func testKnownTerminalRenderFailuresExcludeUnknownAndActiveStatuses() {
        for status in ["failed", "variants_failed", "matching_failed", "no_labeled_tracks", "processing_failed", "posting_failed", "cancelled"] {
            XCTAssertTrue(SlidePostState.isTerminalRenderFailure(status), status)
        }
        for status in ["queued", "processing", "rendering", "ready", "variants_ready_partial", "unconfirmed", "future_status"] {
            XCTAssertFalse(SlidePostState.isTerminalRenderFailure(status), status)
        }
        XCTAssertFalse(SlidePostState.isTerminalRenderFailure(nil))
    }

    func testRawProcessingFailedRequiresExplicitRetryWithExactReceiptJob() async throws {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        await value.create(api: api, itemID: itemID)
        let original = try XCTUnwrap(server.requests.first)
        server.state.renderStatus = "processing_failed"
        await value.refresh(api: api, itemID: itemID)
        XCTAssertFalse(value.isRendering)
        XCTAssertNotNil(value.error)
        XCTAssertEqual(server.requests, [original], "Terminal observation never creates replacement work")
        await value.create(api: api, itemID: itemID)
        XCTAssertEqual(server.requests.count, 2)
        XCTAssertNotEqual(server.requests.last, original)
    }

    func testExporterStopsOnRawProcessingFailureWithoutAnotherGeneration() async {
        let server = Server(snapshot())
        NativeEditorURLProtocol.handler = { request in
            let response = try server.handle(request)
            if request.httpMethod == "POST" { server.state.renderStatus = "processing_failed" }
            return response
        }
        let value = session()
        let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in XCTFail("Failed output cannot be exported") })
        await exporter.export(.photos, session: value, api: api, itemID: itemID, pollInterval: .zero, maxPolls: 5)
        XCTAssertEqual(server.requests.count, 1)
        guard case let .failed(message) = exporter.status else { return XCTFail("Expected explicit render failure") }
        XCTAssertTrue(message.contains("failed"))
    }

}

extension SlidePostSessionRecoveryTests {
    func testExhaustedReceiptObserverFindsReadyWithoutCreateOrReopen() async throws {
        let server = Server(snapshot()); NativeEditorURLProtocol.handler = { try server.handle($0) }
        let value = session(); let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID); await value.create(api: api, itemID: itemID)
        let journal = SlidePostSubmissionJournal(root: root)
        let record = try XCTUnwrap(journal.load(scope))
        try journal.update(scope, operationID: record.operationID) { $0.remainingReads = 0; $0.phase = .exhausted; $0.nextObservationAt = now.addingTimeInterval(60) }
        let reads = server.receiptReads
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        XCTAssertEqual(server.receiptReads, reads)
        now.addTimeInterval(60)
        server.state.renderStatus = "ready"; server.state.renderedVersion = 1
        server.state.draft?.renderedVersion = 1
        server.state.slides = [.init(id: "one", assetID: "a", kind: "image", url: URL(string: "https://fixture.test/one.jpg"))]
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        XCTAssertTrue(value.canExport); XCTAssertEqual(server.requests.count, 1)
        XCTAssertNil(try journal.load(scope))
    }

    func testExhaustedMissingReceiptObservationNeverPostsAndPersistsPacing() async throws {
        let server = Server(snapshot()); let journal = SlidePostSubmissionJournal(root: root)
        var reads = 0
        NativeEditorURLProtocol.handler = { request in
            reads += 1
            if request.url!.path.contains("/generation-requests/") { return (404, Data()) }
            return try server.handle(request)
        }
        let value = session(); let api = NativeEditorTestSupport.api()
        await value.refresh(api: api, itemID: itemID)
        let record = try journal.reserve(.init(scope: scope, expectedVersion: 1, previousJobID: nil)).record
        try journal.update(scope, operationID: record.operationID) { $0.remainingReads = 0; $0.phase = .exhausted; $0.remainingPostAttempts = 0 }
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        let first = reads
        XCTAssertTrue(value.needsSubmissionObservation); XCTAssertTrue(server.requests.isEmpty)
        now.addTimeInterval(29)
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        XCTAssertEqual(reads, first)
        now.addTimeInterval(1)
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        XCTAssertGreaterThan(reads, first); XCTAssertTrue(server.requests.isEmpty)
        XCTAssertEqual(try journal.load(scope)?.remainingReads, 0)
        value.stopObserving(); now.addTimeInterval(30)
        let stopped = reads
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        XCTAssertEqual(reads, stopped)
    }
}

extension SlidePostSessionRecoveryTests {
    func testExhaustedUnknownReceiptCannotExportAnUnverifiedReadyProjection() async throws {
        let server = Server(snapshot()); let journal = SlidePostSubmissionJournal(root: root)
        let value = session(); let api = NativeEditorTestSupport.api()
        NativeEditorURLProtocol.handler = { try server.handle($0) }
        await value.refresh(api: api, itemID: itemID)
        let record = try journal.reserve(.init(scope: scope, expectedVersion: 1, previousJobID: nil)).record
        try journal.update(scope, operationID: record.operationID) { $0.remainingReads = 0; $0.phase = .exhausted; $0.remainingPostAttempts = 0 }
        server.state.jobID = UUID(); server.state.renderStatus = "ready"; server.state.renderedVersion = 1
        server.state.draft?.renderedVersion = 1
        server.state.slides = [.init(id: "one", assetID: "a", kind: "image", url: URL(string: "https://fixture.test/one.jpg"))]
        NativeEditorURLProtocol.handler = { request in
            if request.url!.path.contains("/generation-requests/") { return (404, Data()) }
            return try server.handle(request)
        }
        await value.refresh(api: api, itemID: itemID, readOnlyObservation: true)
        XCTAssertFalse(value.canExport)
        XCTAssertTrue(value.needsSubmissionObservation)
        XCTAssertTrue(server.requests.isEmpty)
    }
}
