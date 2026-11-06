import XCTest
@testable import Kria

final class SlidePostAIRecoveryTests: XCTestCase {
    func testPreparedIntentSurvivesRecreationWithExactIdentityAndInput() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "slide-ai-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let project = UUID()
        let record = SlidePostAIRecord(scope: scope, projectID: project, kind: .chatEdit,
                                       input: .object(["message": .string("caption")]), baseline: nil, operationID: UUID())
        try SlidePostAIJournal(root: root).reserve(record)
        let restored = try XCTUnwrap(SlidePostAIJournal(root: root).load(scope: scope, projectID: project, kind: .chatEdit))
        XCTAssertEqual(restored.operationID, record.operationID)
        XCTAssertEqual(restored.input, record.input)
        XCTAssertNil(try SlidePostAIJournal(root: root).load(scope: scope, projectID: UUID(), kind: .chatEdit))
    }

    func testOldResultCannotReplaceNewExplicitIntent() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "slide-ai-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = SlidePostAIJournal(root: root)
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let project = UUID()
        let old = SlidePostAIRecord(scope: scope, projectID: project, kind: .propose, input: .object([:]), baseline: nil, operationID: UUID())
        try journal.reserve(old)
        let newer = SlidePostAIRecord(scope: scope, projectID: project, kind: .propose, input: .object([:]), baseline: nil, operationID: UUID())
        try journal.reserve(newer)
        XCTAssertThrowsError(try journal.update(old))
        XCTAssertEqual(try journal.load(scope: scope, projectID: project, kind: .propose)?.operationID, newer.operationID)
    }

    func testJournalFailurePreventsReservation() {
        let journal = SlidePostAIJournal(root: URL(filePath: "/unused"), io: .init(read: { _ in nil }, write: { _, _ in throw CocoaError(.fileWriteUnknown) }, remove: { _ in }))
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        XCTAssertThrowsError(try journal.reserve(.init(scope: scope, projectID: UUID(), kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID())))
    }
}

/// Explicit test-only bridge for existing business-behavior fixtures. Production has no legacy fallback.
enum SlidePostAIStubTransport {
    static let projectID = UUID(uuidString: "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb")!
    static func adapt(_ legacy: @escaping (URLRequest) throws -> (Int, Data)) -> (URLRequest) throws -> (Int, Data) {
        var receipts: [UUID: Data] = [:]
        return { request in
            let path = request.url!.path
            if path.contains("/ai-requests/"), let id = UUID(uuidString: request.url!.lastPathComponent) {
                return receipts[id].map { (200, $0) } ?? (404, Data("{}".utf8))
            }
            if path.hasSuffix("/ai-requests") {
                let body = try JSONDecoder().decode(SlidePostAIRequest.self, from: NativeEditorTestSupport.bodyData(request))
                if let stored = receipts[body.clientRequestID] { return (200, stored) }
                var adapted = request
                adapted.url = request.url!.deletingLastPathComponent().appending(path: body.kind == .propose ? "propose" : "chat-edit")
                adapted.httpBody = try JSONEncoder().encode(body.input)
                let result = try legacy(adapted)
                guard result.0 == 200 || (400..<500).contains(result.0) else { return result }
                let receipt = SlidePostAIReceipt(clientRequestID: body.clientRequestID,
                    itemID: UUID(uuidString: request.url!.pathComponents[request.url!.pathComponents.count - 3])!, projectID: body.projectID,
                    kind: body.kind, inputDigest: "fixture-exact-input", input: body.input, status: result.0 == 200 ? .completed : .rejected,
                    result: try JSONDecoder().decode(JSONValue.self, from: result.1))
                let data = try JSONEncoder().encode(receipt); receipts[body.clientRequestID] = data
                return (200, data)
            }
            let result = try legacy(request)
            if path.hasSuffix("/slide-post"), request.httpMethod == "GET", result.0 == 200,
               var object = try? JSONSerialization.jsonObject(with: result.1) as? [String: Any] {
                object["ai_request_delivery_version"] = 1; object["ai_request_recovery_version"] = 1; object["ai_project_id"] = projectID.uuidString
                return (200, try JSONSerialization.data(withJSONObject: object))
            }
            return result
        }
    }
}

@MainActor final class SlidePostAISessionRecoveryTests: XCTestCase {
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private let projectID = UUID(uuidString: "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb")!
    private func remote() -> SlidePostState {
        let draft = SlidePostDraft(version: 1, platformProfile: "instagram_carousel",
                                   slides: (1...3).map { .init(id: "s\($0)", assetID: "a\($0)", kind: "image") }, caption: "original")
        return SlidePostState(itemID: itemID, title: "post", draft: draft,
            assets: (1...3).map { .init(id: "a\($0)", kind: "image", status: "ready", sourceURL: URL(string: "https://media.test/a"), durationS: nil, mediaStatus: "available") },
            aiRequestRecoveryVersion: 1, aiProjectID: projectID, aiRequestDeliveryVersion: 1)
    }

    func testLostResponseThenRetryReadsOriginalResultWithoutSecondPost() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "slide-ai-session-\(UUID())")
        let suite = "slide-ai-session-\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite); try? FileManager.default.removeItem(at: root) }
        var now = Date()
        let api = AIRecoveryAPI(state: remote())
        let scope: (any KriaAPIClient, String) throws -> SlidePostSubmissionScope = { _, id in
            .init(owner: "owner", environment: "https://api.test", itemID: id)
        }
        let session = SlidePostSession(defaults: defaults, submissionJournal: .init(root: root), submissionScope: scope, submissionNow: { now })
        await session.refresh(api: api, itemID: itemID)
        let changed = await session.chatEdit(api: api, itemID: itemID, message: "caption")
        XCTAssertFalse(changed)
        let failure = try XCTUnwrap(session.chat.last)
        now = now.addingTimeInterval(3)
        await session.retryChat(api: api, itemID: itemID, bubble: failure)
        XCTAssertEqual(session.draft?.caption, "AI caption")
        let counts = await api.counts()
        XCTAssertEqual(counts.posts, 1)
        XCTAssertEqual(counts.reads, 1)
    }

    func testProcessRecreationRecoversVisibleResultAndPreservesConflictingLocalEdit() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "slide-ai-session-\(UUID())")
        let suite = "slide-ai-session-\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite); try? FileManager.default.removeItem(at: root) }
        var now = Date()
        let api = AIRecoveryAPI(state: remote())
        func make() -> SlidePostSession {
            SlidePostSession(defaults: defaults, submissionJournal: .init(root: root),
                submissionScope: { _, id in .init(owner: "owner", environment: "https://api.test", itemID: id) }, submissionNow: { now })
        }
        let first = make()
        await first.refresh(api: api, itemID: itemID)
        _ = await first.chatEdit(api: api, itemID: itemID, message: "caption")
        first.setCaption("local edit after request")
        let restored = make()
        await restored.refresh(api: api, itemID: itemID)
        now = now.addingTimeInterval(3)
        await restored.retryChat(api: api, itemID: itemID, bubble: try XCTUnwrap(restored.chat.last))
        XCTAssertEqual(restored.draft?.caption, "local edit after request")
        XCTAssertEqual(restored.proposal?.draft.caption, "AI caption")
        let counts = await api.counts()
        XCTAssertEqual(counts.posts, 1)
    }

    func testAccountReplacementCannotObserveOrApplyOriginalReceipt() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "slide-ai-session-\(UUID())")
        let suite = "slide-ai-session-\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite); try? FileManager.default.removeItem(at: root) }
        var now = Date(); var owner = "first"
        let api = AIRecoveryAPI(state: remote())
        let session = SlidePostSession(defaults: defaults, submissionJournal: .init(root: root),
            submissionScope: { _, id in .init(owner: owner, environment: "https://api.test", itemID: id) }, submissionNow: { now })
        await session.refresh(api: api, itemID: itemID)
        _ = await session.chatEdit(api: api, itemID: itemID, message: "caption")
        owner = "replacement"; now = now.addingTimeInterval(3)
        await session.retryChat(api: api, itemID: itemID, bubble: try XCTUnwrap(session.chat.last))
        let counts = await api.counts()
        XCTAssertEqual(counts.reads, 0)
        XCTAssertEqual(session.draft?.caption, "original")
    }
}

private actor AIRecoveryAPI: KriaAPIClient {
    func undoDraft(threadID: UUID, expectedRevision: Int) async throws -> DraftSnapshot { throw APIError.unsupported }
    func approval(threadID: UUID, approvalID: UUID) async throws -> ApprovalSnapshot { throw APIError.unsupported }
    func decideApproval(threadID: UUID, approvalID: UUID, decision: String, expectedThreadRevision: Int, expectedDraftRevision: Int, fingerprint: String, speechCleanupAware: Bool, speechCleanupAnalysisID: String?, speechCleanupChoice: String?, outputOrientation: String?, landscapeFit: String?, automaticRepairAttempts: Int) async throws { throw APIError.unsupported }
    func reserveProjectUpload(threadID: UUID, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> ProjectUploadReservation { throw APIError.unsupported }
    func attachProjectMedia(threadID: UUID, mediaID: String, gcsPath: String, filename: String, contentType: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread { throw APIError.unsupported }
    func submitTurn(threadID: UUID, message: String, expectedRevision: Int) async throws -> TurnAccepted { throw APIError.unsupported }
    func projects() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func project(threadID: UUID) async throws -> CreationThread { throw APIError.unsupported }
    func library() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func createThread(message: String?) async throws -> CreationThread { throw APIError.unsupported }
    func exchangeMobileToken(_ credential: AuthCredential, provider: String) async throws -> MobileSession { throw APIError.unsupported }
    func refreshMobileSession(_ refreshToken: String) async throws -> MobileSession { throw APIError.unsupported }
    func revokeMobileSession(_ refreshToken: String) async throws { throw APIError.unsupported }
    func reviewerSignIn(email: String, password: String) async throws -> MobileSession { throw APIError.unsupported }
    func applyCreationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int) async throws -> CreationThread { throw APIError.unsupported }
    func threadDelta(threadID: UUID, afterSequence: Int) async throws -> ThreadDelta { throw APIError.unsupported }
    func draft(threadID: UUID) async throws -> DraftSnapshot { throw APIError.unsupported }
    func writeDraft(threadID: UUID, snapshot: [String: JSONValue], expectedRevision: Int, etag: String) async throws -> DraftSnapshot { throw APIError.unsupported }
    func playbackURL(jobID: UUID) async throws -> URL { throw APIError.unsupported }
    func editRecipe(jobID: UUID, variantID: String?) async throws -> EditRecipe { throw APIError.unsupported }
    func reserveUpload(filename: String, contentType: String, size: Int64, purpose: UploadPurpose?) async throws -> UploadReservation { throw APIError.unsupported }
    func cancelUpload(reservationID: UUID) async throws { throw APIError.unsupported }
    func addClip(jobID: UUID, gcsPath: String) async throws -> AddClipResult { throw APIError.unsupported }
    func uploadFile(to reservation: UploadReservation, fileURL: URL) async throws { throw APIError.unsupported }

    var state: SlidePostState
    private var successful = false
    private var fault: String?
    private var receiptStatus: SlidePostAIReceipt.Status = .completed
    private var receiptDeliveryVersion: Int? = 1
    private var readError: Error?
    private var lateReadError: Error?
    private var change = "caption"
    private var pausePost = false
    private var postGate: CheckedContinuation<Void, Never>?
    private var pauseRead = false
    private var pauseSave = false
    private var readGate: CheckedContinuation<Void, Never>?
    private var saveGate: CheckedContinuation<Void, Never>?
    private var saves: [SlidePostSaveRequest] = []
    private var receipt: SlidePostAIReceipt?
    private var posts = 0
    private var reads = 0
    init(state: SlidePostState) { self.state = state }
    func configure(successful: Bool = false, fault: String? = nil, status: SlidePostAIReceipt.Status = .completed, readError: Error? = nil, change: String = "caption", deliveryVersion: Int? = 1) {
        self.successful = successful; self.fault = fault; self.receiptStatus = status; self.readError = readError; self.change = change; self.receiptDeliveryVersion = deliveryVersion
    }
    func setState(_ value: SlidePostState) { state = value }
    func slidePost(itemID: String) async throws -> SlidePostState { state }
    func slidePost(itemID: String, expectedOwner: String?) async throws -> SlidePostState { state }
    func submitSlidePostAI(itemID: String, body: SlidePostAIRequest, expectedOwner: String) async throws -> SlidePostAIReceipt {
        posts += 1
        if pausePost { await withCheckedContinuation { postGate = $0 } }
        var draft = state.draft!
        if case let .object(input) = body.input, let sent = input["draft"], sent != .null {
            draft = try JSONDecoder().decode(SlidePostDraft.self, from: JSONEncoder().encode(sent))
        }
        if change == "caption" { draft.caption = "AI caption" }
        if change == "cover" { draft.coverIndex = 1 }
        if change == "order" { draft.slides.reverse() }
        let encoded = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(draft))
        receipt = .init(clientRequestID: body.clientRequestID, itemID: UUID(uuidString: itemID)!, projectID: body.projectID,
            kind: body.kind, inputDigest: "fake-provider-original", input: body.input, status: .completed,
            result: body.kind == .propose
                ? .object(["draft": encoded, "base_version": .number(Double(state.draft?.version ?? 0)), "fallback_used": .bool(false), "summary": .string("AI proposal")])
                : .object(["outcome": .string(change == "noop" ? "no_effect" : change == "failed" ? "failed" : "edited"), "reply": .string("updated"), "base_version": .number(Double(state.draft?.version ?? 0)), "draft": encoded]))
        if successful { return receipt! }
        throw URLError(.networkConnectionLost)
    }
    func slidePostAIReceipt(itemID: String, requestID: UUID, expectedOwner: String) async throws -> SlidePostAIReceipt {
        reads += 1
        if let readError { throw readError }
        guard let receipt, receipt.clientRequestID == requestID else { throw APIError.requestFailed(status: 404, detail: .init("missing")) }
        if pauseRead { await withCheckedContinuation { readGate = $0 } }
        if let lateReadError { throw lateReadError }
        var result = receipt.result
        if fault == "baseline", case var .object(fields)? = result { fields["base_version"] = .number(999); result = .object(fields) }
        return .init(clientRequestID: fault == "operation" ? UUID() : receipt.clientRequestID,
            itemID: fault == "item" ? UUID() : receipt.itemID, projectID: fault == "project" ? UUID() : receipt.projectID,
            kind: fault == "kind" ? .propose : receipt.kind, inputDigest: fault == "digest" ? "changed" : receipt.inputDigest,
            input: fault == "input" ? .object(["message": .string("other")]) : receipt.input,
            status: receiptStatus, result: receiptStatus == .completed ? result : nil, deliveryVersion: receiptDeliveryVersion)
    }
    func setLateReadError(_ error: Error?) { lateReadError = error }
    func suspendPosts() { pausePost = true }
    func waitForPostSuspension() async -> Bool {
        for _ in 0..<1000 { if postGate != nil { return true }; await Task.yield() }
        return false
    }
    func releasePost() { pausePost = false; postGate?.resume(); postGate = nil }
    func suspendReads() { pauseRead = true }
    func suspendSaves() { pauseSave = true }
    func waitForSuspension(save: Bool = false) async -> Bool {
        for _ in 0..<1000 {
            if save ? saveGate != nil : readGate != nil { return true }
            await Task.yield()
        }
        return false
    }
    func releaseRead() { pauseRead = false; readGate?.resume(); readGate = nil }
    func releaseSave() { pauseSave = false; saveGate?.resume(); saveGate = nil }
    func saveSlidePost(itemID: String, request: SlidePostSaveRequest) async throws -> SlidePostDraft {
        saves.append(request)
        if pauseSave { await withCheckedContinuation { saveGate = $0 } }
        var draft = state.draft!
        draft.version = request.expectedVersion + 1; draft.slides = request.slides
        draft.caption = request.caption; draft.coverIndex = request.coverIndex; draft.platformProfile = request.platformProfile
        draft.aspect = request.aspect; draft.briefBinding = request.briefBinding
        state.draft = draft
        return draft
    }
    func saveSlidePost(itemID: String, request: SlidePostSaveRequest, expectedOwner: String?) async throws -> SlidePostDraft {
        try await saveSlidePost(itemID: itemID, request: request)
    }
    func savedRequests() -> [SlidePostSaveRequest] { saves }
    func counts() -> (posts: Int, reads: Int) { (posts, reads) }
}

private final class AIWriteFault: @unchecked Sendable {
    private let lock = NSLock()
    private var local = false
    private var delivered = false
    private var request = false
    func set(local: Bool = false, delivered: Bool = false, request: Bool = false) { lock.withLock { self.local = local; self.delivered = delivered; self.request = request } }
    var io: SlidePostSubmissionJournal.IO {
        .init(read: SlidePostSubmissionJournal.IO.fileSystem.read, write: { [self] data, url in
            let failure = lock.withLock { (local, delivered, request) }
            if failure.2 && url.pathExtension != "local" { throw CocoaError(.fileWriteUnknown) }
            if failure.0 && url.pathExtension == "local" { throw CocoaError(.fileWriteUnknown) }
            if failure.1, let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], object["delivered"] as? Bool == true {
                throw CocoaError(.fileWriteUnknown)
            }
            try SlidePostSubmissionJournal.IO.fileSystem.write(data, url)
        }, remove: SlidePostSubmissionJournal.IO.fileSystem.remove)
    }
}

@MainActor private final class AIHarness {
    let root = FileManager.default.temporaryDirectory.appending(path: "slide-ai-harness-\(UUID())")
    let suite: String
    let defaults: UserDefaults
    let api: AIRecoveryAPI
    let state: SlidePostState
    var now = Date()
    var owner = "owner"
    var environment = "https://api.test"
    let fault = AIWriteFault()
    init(state: SlidePostState) { let suite = "slide-ai-harness-\(UUID())"; self.suite = suite; self.state = state; defaults = UserDefaults(suiteName: suite)!; api = AIRecoveryAPI(state: state) }
    var scope: SlidePostSubmissionScope { .init(owner: owner, environment: environment, itemID: state.itemID) }
    var journal: SlidePostAIJournal { SlidePostSubmissionJournal(root: root, io: fault.io).aiJournal() }
    func session() -> SlidePostSession {
        SlidePostSession(defaults: defaults, submissionJournal: .init(root: root, io: fault.io),
            submissionScope: { [unowned self] _, id in .init(owner: owner, environment: environment, itemID: id) }, submissionNow: { [unowned self] in now })
    }
    func advance(_ seconds: TimeInterval = 3) { now = now.addingTimeInterval(seconds) }
    func clean() { defaults.removePersistentDomain(forName: suite); try? FileManager.default.removeItem(at: root) }
}

extension SlidePostAISessionRecoveryTests {
    func testWrongReceiptIdentityInputOrBaselineNeverStages() async throws {
        for fault in ["operation", "item", "project", "kind", "input", "baseline"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            await h.api.configure(fault: fault)
            h.advance()
            await value.retryChat(api: h.api, itemID: itemID, bubble: try XCTUnwrap(value.chat.last))
            XCTAssertEqual(value.draft?.caption, "original", fault)
            XCTAssertNil(value.proposal, fault)
            let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1, fault)
        }
    }

    func testChangedDigestOfKnownReceiptNeverStages() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        let bubble = try XCTUnwrap(value.chat.last)
        await h.api.configure(status: .running); h.advance()
        await value.retryChat(api: h.api, itemID: itemID, bubble: bubble)
        await h.api.configure(fault: "digest"); h.advance()
        await value.retryChat(api: h.api, itemID: itemID, bubble: bubble)
        XCTAssertEqual(value.draft?.caption, "original")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
    }

    func testLocalCommitFailureRecoversPaidResultAfterRecreation() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        await h.api.configure(successful: true)
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        h.fault.set(local: true)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        XCTAssertEqual(value.draft?.caption, "AI caption")
        h.fault.set()
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
        XCTAssertEqual(restored.draft?.caption, "AI caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1); XCTAssertEqual(counts.reads, 0)
    }

    func testFailedDeliveredMarkThenUndoDoesNotReapplyAfterRecreation() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        await h.api.configure(successful: true)
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        h.fault.set(delivered: true)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        let chatCount = value.chat.count
        value.undoEdit()
        XCTAssertEqual(value.draft?.caption, "original")
        h.fault.set()
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
        XCTAssertEqual(restored.draft?.caption, "original")
        XCTAssertEqual(restored.chat.count, chatCount)
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1); XCTAssertEqual(counts.reads, 0)
    }

    func testCachedCompletedResultAtZeroReadBudgetRecoversWithoutNetwork() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        await h.api.configure(successful: true)
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        h.fault.set(local: true)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        h.fault.set()
        var record = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        record.remainingReads = 0; record.remainingPosts = 0
        try h.journal.update(record)
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
        XCTAssertEqual(restored.draft?.caption, "AI caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.reads, 0); XCTAssertEqual(counts.posts, 1)
    }

    func testOwnerEnvironmentAndProjectReplacementNeverRestorePrivateLocalState() async throws {
        for replacement in ["owner", "environment", "project"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let first = h.session(); await first.refresh(api: h.api, itemID: itemID)
            first.setCaption("private local caption")
            if replacement == "owner" { h.owner = "other" }
            if replacement == "environment" { h.environment = "https://other.test" }
            if replacement == "project" { var state = h.state; state.aiProjectID = UUID(); await h.api.setState(state) }
            await first.refresh(api: h.api, itemID: itemID)
            XCTAssertEqual(first.draft?.caption, "original", replacement)
            XCTAssertTrue(first.chat.isEmpty, replacement)
            let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
            XCTAssertEqual(restored.draft?.caption, "original", replacement)
        }
    }

    func testLegacyUnscopedStateIsRetainedButNeverAdopted() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let bytes = Data(#"{"draft":{"caption":"private legacy draft"}}"#.utf8)
        let key = "kria.slide-post.\(itemID)"
        h.defaults.set(bytes, forKey: key)
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        XCTAssertEqual(value.draft?.caption, "original")
        XCTAssertEqual(h.defaults.data(forKey: key), bytes)
    }

    func testPersistedRetryAfterSuppressesReadsAcrossRecreation() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let first = h.session(); await first.refresh(api: h.api, itemID: itemID)
        _ = await first.chatEdit(api: h.api, itemID: itemID, message: "caption")
        await h.api.configure(readError: APIError.requestFailed(status: 429, detail: .init("wait", retryAfter: 60)))
        h.advance()
        await first.retryChat(api: h.api, itemID: itemID, bubble: try XCTUnwrap(first.chat.last))
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
        h.advance(59)
        await restored.retryChat(api: h.api, itemID: itemID, bubble: try XCTUnwrap(restored.chat.last))
        var counts = await h.api.counts(); XCTAssertEqual(counts.reads, 1)
        await h.api.configure(); h.advance(1)
        await restored.retryChat(api: h.api, itemID: itemID, bubble: try XCTUnwrap(restored.chat.last))
        counts = await h.api.counts(); XCTAssertEqual(counts.reads, 2); XCTAssertEqual(counts.posts, 1)
    }

    func testOldServerCapabilityPreventsEveryPaidEndpoint() async throws {
        var state = remote(); state.aiRequestRecoveryVersion = nil
        let h = AIHarness(state: state); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 0); XCTAssertEqual(counts.reads, 0)
        XCTAssertEqual(value.draft?.caption, "original")
    }
}

extension SlidePostAISessionRecoveryTests {
    func testOldServerKnownLocalProjectRestoresManualDraftWithoutAuthorizingAI() async throws {
        var old = remote(); old.aiProjectID = nil; old.aiRequestRecoveryVersion = nil
        let h = AIHarness(state: old); defer { h.clean() }
        let knownProject = UUID()
        let first = h.session(); await first.refresh(api: h.api, itemID: itemID, projectID: knownProject)
        first.setCaption("manual edit on old server")
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID, projectID: knownProject)
        XCTAssertEqual(restored.draft?.caption, "manual edit on old server")
        _ = await restored.chatEdit(api: h.api, itemID: itemID, message: "caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 0)
        let other = h.session(); await other.refresh(api: h.api, itemID: itemID, projectID: UUID())
        XCTAssertEqual(other.draft?.caption, "original")
    }

    func testManualOnlyUnknownProjectNeverRestoresAIProposal() async throws {
        var old = remote(); old.aiProjectID = nil; old.aiRequestRecoveryVersion = nil
        let h = AIHarness(state: old); defer { h.clean() }
        let first = h.session(); await first.refresh(api: h.api, itemID: itemID)
        first.setCaption("manual-only state")
        first.proposal = .init(draft: old.draft!, baseVersion: 1, fallbackUsed: false, summary: "must not persist as verified AI")
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
        XCTAssertEqual(restored.draft?.caption, "manual-only state")
        XCTAssertNil(restored.proposal)
    }

    func testRolloutOffStillRecoversOriginalResultWithoutNewPost() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        var disabled = h.state; disabled.aiRequestRecoveryVersion = 0
        await h.api.setState(disabled); await value.refresh(api: h.api, itemID: itemID)
        h.advance(); await value.retryChat(api: h.api, itemID: itemID, bubble: try XCTUnwrap(value.chat.last))
        XCTAssertEqual(value.draft?.caption, "AI caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1); XCTAssertEqual(counts.reads, 1)
    }

    func testLateOldReceiptCannotConsumeNewExplicitIntent() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "first instruction")
        let oldID = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID)
        let failed = try XCTUnwrap(value.chat.last)
        h.advance(); await h.api.suspendReads()
        let oldCheck = Task { await value.retryChat(api: h.api, itemID: itemID, bubble: failed) }
        let suspended = await h.api.waitForSuspension(); XCTAssertTrue(suspended)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "new explicit instruction")
        let newID = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID)
        XCTAssertNotEqual(oldID, newID)
        await h.api.releaseRead(); await oldCheck.value
        XCTAssertEqual(value.draft?.caption, "original")
        XCTAssertEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID, newID)
        XCTAssertEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.delivered, false)
    }

    func testProjectReplacementDuringReceiptGetRejectsOldCallback() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        h.advance(); await h.api.suspendReads()
        let bubble = try XCTUnwrap(value.chat.last)
        let check = Task { await value.retryChat(api: h.api, itemID: itemID, bubble: bubble) }
        let suspended = await h.api.waitForSuspension(); XCTAssertTrue(suspended)
        var replacement = h.state; replacement.aiProjectID = UUID()
        await h.api.setState(replacement)
        let changed = Task { await value.refresh(api: h.api, itemID: itemID, projectID: replacement.aiProjectID) }
        await Task.yield()
        await h.api.releaseRead(); await check.value; await changed.value
        XCTAssertEqual(value.draft?.caption, "original")
        XCTAssertTrue(value.chat.isEmpty)
        XCTAssertNil(value.proposal)
    }

    func testOldSaveCompletionCannotClearNewContextBusyState() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        value.setCaption("old owner edit")
        await h.api.suspendSaves()
        let oldSave = Task { await value.save(api: h.api, itemID: itemID) }
        let firstSuspended = await h.api.waitForSuspension(save: true); XCTAssertTrue(firstSuspended)
        h.owner = "replacement"
        let newAPI = AIRecoveryAPI(state: h.state)
        await value.refresh(api: newAPI, itemID: itemID)
        value.setCaption("new owner edit")
        await newAPI.suspendSaves()
        let newSave = Task { await value.save(api: newAPI, itemID: itemID) }
        let secondSuspended = await newAPI.waitForSuspension(save: true); XCTAssertTrue(secondSuspended)
        await h.api.releaseSave(); await oldSave.value
        XCTAssertTrue(value.isBusy)
        XCTAssertEqual(value.draft?.caption, "new owner edit")
        await newAPI.releaseSave(); await newSave.value
        XCTAssertFalse(value.isBusy)
        XCTAssertEqual(value.draft?.caption, "new owner edit")
    }

    func testDirtyRequestBaselineAndLaterConflictKeepManualCaptionOnApply() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        await h.api.configure(change: "cover")
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        value.setCaption("unsaved manual caption before AI")
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "pick cover")
        var current = try XCTUnwrap(value.draft); current.coverIndex = 2; value.draft = current
        h.advance(); await value.retryChat(api: h.api, itemID: itemID, bubble: try XCTUnwrap(value.chat.last))
        XCTAssertNotNil(value.proposal)
        await value.applyProposal(api: h.api, itemID: itemID)
        XCTAssertEqual(value.draft?.caption, "unsaved manual caption before AI")
        XCTAssertEqual(value.draft?.coverIndex, 1)
        let saved = await h.api.savedRequests(); XCTAssertEqual(saved.last?.expectedVersion, 1)
        XCTAssertEqual(saved.last?.caption, "unsaved manual caption before AI")
    }

    func testCapturedOwnerStopsAllBoundMutationsBeforeTransport() async throws {
        func token(_ owner: String) -> MobileSession {
            let payload = Data("{\"sub\":\"\(owner)\"}".utf8).base64EncodedString()
            return .init(accessToken: "test.\(payload).test", refreshToken: "fixture", expiresIn: 3600)
        }
        let store = NativeEditorMemoryTokenStore(token("first"))
        let api = NativeEditorTestSupport.api(tokenStore: store)
        let capturedOwner = "first"
        try store.write(token("second"))  // executor hop happened after intent captured its owner
        var requests = 0
        NativeEditorURLProtocol.handler = { _ in requests += 1; return (500, Data()) }
        defer { NativeEditorURLProtocol.handler = nil }
        do { _ = try await api.saveSlidePost(itemID: itemID, request: .init(draft: remote().draft!, expectedVersion: 1), expectedOwner: capturedOwner); XCTFail("old owner PUT escaped") } catch { }
        do { _ = try await api.generateSlidePost(itemID: itemID, expectedVersion: 1, clientRequestID: UUID(), expectedCurrentJobID: nil, expectedOwner: capturedOwner); XCTFail("old owner render escaped") } catch { }
        do { _ = try await api.submitSlidePostAI(itemID: itemID, body: .init(clientRequestID: UUID(), projectID: projectID, kind: .chatEdit, input: .object([:])), expectedOwner: capturedOwner); XCTFail("old owner AI escaped") } catch { }
        do { _ = try await api.slidePostAIReceipt(itemID: itemID, requestID: UUID(), expectedOwner: capturedOwner); XCTFail("old owner receipt escaped") } catch { }
        XCTAssertEqual(requests, 0)
    }
}

extension SlidePostAIRecoveryTests {
    func testFailedAtomicReplacementRetainsOriginalIntent() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "ai-reserve-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let project = UUID()
        let original = SlidePostAIRecord(scope: scope, projectID: project, kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID())
        try SlidePostAIJournal(root: root).reserve(original)
        let failing = SlidePostAIJournal(root: root, io: .init(read: SlidePostSubmissionJournal.IO.fileSystem.read,
            write: { _, _ in throw CocoaError(.fileWriteUnknown) }, remove: SlidePostSubmissionJournal.IO.fileSystem.remove))
        XCTAssertThrowsError(try failing.reserve(.init(scope: scope, projectID: project, kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID())))
        XCTAssertEqual(try failing.load(scope: scope, projectID: project, kind: .chatEdit)?.operationID, original.operationID)
    }

    func testCrossKindNewIntentRejectsOldResult() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "ai-cross-kind-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let project = UUID(); let journal = SlidePostAIJournal(root: root)
        let old = SlidePostAIRecord(scope: scope, projectID: project, kind: .propose, input: .object([:]), baseline: nil, operationID: UUID())
        try journal.reserve(old)
        let newer = SlidePostAIRecord(scope: scope, projectID: project, kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID())
        try journal.reserve(newer)
        XCTAssertThrowsError(try journal.update(old))
        XCTAssertNil(try journal.load(scope: scope, projectID: project, kind: .propose))
        XCTAssertEqual(try journal.load(scope: scope, projectID: project, kind: .chatEdit)?.operationID, newer.operationID)
    }

    func testStaleWriterCannotShortenServerDeadlineOrResetBudget() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "ai-budget-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let project = UUID(); let journal = SlidePostAIJournal(root: root); let now = Date()
        let original = SlidePostAIRecord(scope: scope, projectID: project, kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID())
        try journal.reserve(original)
        var later = try journal.reserveAttempt(original, post: false, explicit: false, now: now)
        later.notBefore = now.addingTimeInterval(60); later.serverNotBefore = later.notBefore
        try journal.update(later)
        try journal.update(original)  // another session's stale snapshot
        let restored = try XCTUnwrap(SlidePostAIJournal(root: root).load(scope: scope, projectID: project, kind: .chatEdit))
        XCTAssertEqual(restored.remainingReads, 39)
        XCTAssertEqual(restored.notBefore, later.notBefore)
        XCTAssertThrowsError(try journal.reserveAttempt(restored, post: false, explicit: true, now: now.addingTimeInterval(59)))
        XCTAssertNoThrow(try journal.reserveAttempt(restored, post: false, explicit: false, now: now.addingTimeInterval(60)))
    }

    func testExhaustedAutomaticBudgetAllowsOneExplicitOriginalIntentObservation() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "ai-explicit-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let project = UUID(); let journal = SlidePostAIJournal(root: root)
        var record = SlidePostAIRecord(scope: scope, projectID: project, kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID())
        record.remainingPosts = 0; record.remainingReads = 0
        try journal.reserve(record)
        XCTAssertThrowsError(try journal.reserveAttempt(record, post: true, explicit: false, now: Date()))
        let manual = try journal.reserveAttempt(record, post: true, explicit: true, now: Date())
        XCTAssertEqual(manual.operationID, record.operationID)
        XCTAssertEqual(manual.remainingPosts, 0)
        XCTAssertEqual(manual.remainingReads, 0)
    }
}

extension SlidePostAISessionRecoveryTests {
    func testPreparedBeforePostResumesSameUUIDAndRecoversOnce() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let operation = UUID()
        let record = SlidePostAIRecord(scope: h.scope, projectID: projectID, kind: .chatEdit,
            input: .object(["message": .string("caption"), "expected_version": .number(1), "client_request_id": .string(operation.uuidString), "turns": .array([])]),
            baseline: h.state.draft, operationID: operation, deliveryVersion: 1)
        try h.journal.reserve(record)
        let restored = h.session(); await restored.refresh(api: h.api, itemID: itemID)
        await restored.checkAIResult(api: h.api, itemID: itemID)
        XCTAssertEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID, operation)
        h.advance(); await restored.checkAIResult(api: h.api, itemID: itemID)
        XCTAssertEqual(restored.draft?.caption, "AI caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
    }

    func testUnknownAndRejectedChecksNeverMintReplacementButNewInstructionCan() async throws {
        for status in [SlidePostAIReceipt.Status.unknown, .rejected] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            let operation = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID)
            await h.api.configure(status: status); h.advance()
            await value.checkAIResult(api: h.api, itemID: itemID)
            h.advance(); await value.checkAIResult(api: h.api, itemID: itemID)
            let before = await h.api.counts(); XCTAssertEqual(before.posts, 1)
            XCTAssertEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID, operation)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "a new explicit instruction")
            let after = await h.api.counts(); XCTAssertEqual(after.posts, 2)
            XCTAssertNotEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID, operation)
        }
    }

    func testCompletedNoopAndFailedResponseDoNotOfferPaidRetry() async throws {
        for outcome in ["noop", "failed"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            await h.api.configure(successful: true, change: outcome)
            let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            let bubble = try XCTUnwrap(value.chat.last)
            XCTAssertNil(bubble.retryText)
            await value.retryChat(api: h.api, itemID: itemID, bubble: bubble)
            let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
            XCTAssertEqual(value.draft?.caption, "original")
        }
    }

    func testRecoveredProposalKeepsLaterLocalCaptionAndNewServerVersionOnApply() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        await h.api.configure(change: "order")
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        await value.propose(api: h.api, itemID: itemID, instruction: "reverse order")
        value.setCaption("later local caption")
        var newer = h.state; newer.draft?.version = 2; newer.draft?.slides[0].alt = "remote alt"
        await h.api.setState(newer); await value.refresh(api: h.api, itemID: itemID)
        h.advance(); await value.checkAIResult(api: h.api, itemID: itemID)
        XCTAssertEqual(value.proposal?.baseVersion, 1)
        await value.applyProposal(api: h.api, itemID: itemID)
        XCTAssertEqual(value.draft?.caption, "later local caption")
        XCTAssertEqual(value.draft?.slides.map(\.id), ["s3", "s2", "s1"])
        XCTAssertEqual(value.draft?.slides.first(where: { $0.id == "s1" })?.alt, "remote alt")
        let saved = await h.api.savedRequests(); XCTAssertEqual(saved.last?.expectedVersion, 2)
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
    }
}

extension SlidePostAISessionRecoveryTests {
    func testFailedRequestReservationMakesNoPost() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        h.fault.set(request: true)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 0); XCTAssertEqual(counts.reads, 0)
        XCTAssertNil(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
    }

    func testAccountAndItemReplacementDuringReadDiscardOldResult() async throws {
        for replacement in ["account", "item"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            h.advance(); await h.api.suspendReads()
            let check = Task { await value.checkAIResult(api: h.api, itemID: itemID) }
            let suspended = await h.api.waitForSuspension(); XCTAssertTrue(suspended)
            let nextID = replacement == "item" ? UUID().uuidString : itemID
            if replacement == "account" { h.owner = "replacement" }
            let next = SlidePostState(itemID: nextID, title: "new context", draft: h.state.draft,
                assets: h.state.assets, aiRequestRecoveryVersion: 1, aiProjectID: projectID, aiRequestDeliveryVersion: 1)
            await h.api.setState(next)
            let changed = Task { await value.refresh(api: h.api, itemID: nextID) }
            await Task.yield()
            await h.api.releaseRead(); await check.value; await changed.value
            XCTAssertEqual(value.state?.itemID, nextID)
            XCTAssertEqual(value.draft?.caption, "original")
            XCTAssertTrue(value.chat.isEmpty)
            XCTAssertNil(value.proposal)
        }
    }
}

extension SlidePostAISessionRecoveryTests {
    func testExhaustedAutomaticObserverGetsLaterResultWithoutPostOrReopen() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        var record = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        record.remainingReads = 0; record.remainingPosts = 0
        record.slowReadNotBefore = h.now.addingTimeInterval(60)
        try h.journal.update(record)
        h.advance(59)
        await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        var counts = await h.api.counts(); XCTAssertEqual(counts.reads, 0)
        h.advance(1)
        await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        counts = await h.api.counts(); XCTAssertEqual(counts.reads, 1); XCTAssertEqual(counts.posts, 1)
        XCTAssertEqual(value.draft?.caption, "AI caption")
        XCTAssertEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.remainingReads, 0)
    }

    func testStoppedObserverDoesNotReadOrApplyCompletedResult() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        h.advance(60); value.stopObserving()
        await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        let counts = await h.api.counts(); XCTAssertEqual(counts.reads, 0); XCTAssertEqual(counts.posts, 1)
        XCTAssertEqual(value.draft?.caption, "original")
    }
}

extension SlidePostAISessionRecoveryTests {
    func testExhaustedMissingAIReceiptNeverResumesPostAndStopFencesLateCompletion() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        var record = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        record.remainingReads = 0; record.remainingPosts = 0; try h.journal.update(record)
        await h.api.configure(readError: APIError.requestFailed(status: 404, detail: .init("missing")))
        h.advance(30); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        var counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1); XCTAssertEqual(counts.reads, 1)
        await h.api.configure(); await h.api.suspendReads(); h.advance(30)
        let pending = Task { await value.recoverAI(api: h.api, itemID: itemID, explicit: false) }
        let suspended = await h.api.waitForSuspension(); XCTAssertTrue(suspended)
        value.stopObserving(); value.startObserving()
        await h.api.releaseRead(); await pending.value
        XCTAssertEqual(value.draft?.caption, "original")
        counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
    }
}

extension SlidePostAISessionRecoveryTests {
    func testLateRetryAfterSurvivesObserverStopAndRestart() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        var record = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        record.remainingReads = 0; record.remainingPosts = 0; try h.journal.update(record)
        h.advance(30); await h.api.suspendReads()
        let reading = Task { await value.recoverAI(api: h.api, itemID: itemID, explicit: false) }
        let suspended = await h.api.waitForSuspension(); XCTAssertTrue(suspended)
        value.stopObserving(); value.startObserving()
        await h.api.setLateReadError(APIError.requestFailed(status: 429, detail: .init("wait", retryAfter: 60)))
        await h.api.releaseRead(); await reading.value
        await h.api.setLateReadError(nil)
        h.advance(59); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        var counts = await h.api.counts(); XCTAssertEqual(counts.reads, 1)
        h.advance(1); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        counts = await h.api.counts(); XCTAssertEqual(counts.reads, 2); XCTAssertEqual(counts.posts, 1)
        XCTAssertEqual(value.draft?.caption, "AI caption")
    }
}

extension SlidePostAISessionRecoveryTests {
    func testSlowObserverRejectsWrongReceiptAndChangedOwnerWithoutNewWork() async throws {
        for fault in ["item", "project", "operation", "owner"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            var record = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
            record.remainingReads = 0; record.remainingPosts = 0; try h.journal.update(record)
            if fault == "owner" { h.owner = "other" } else { await h.api.configure(fault: fault) }
            h.advance(30); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
            let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
            XCTAssertNotEqual(value.draft?.caption, "AI caption")
            if fault == "owner" { XCTAssertNil(value.draft); XCTAssertTrue(value.chat.isEmpty); XCTAssertEqual(counts.reads, 0) }
        }
    }
}

extension SlidePostAISessionRecoveryTests {
    func testGuaranteedPendingDeliveryChecksNeverResubmitAndLaterResultApplies() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        await h.api.configure(status: .pending)
        h.advance(); await value.checkAIResult(api: h.api, itemID: itemID)
        h.advance(); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        let waiting = await h.api.counts(); XCTAssertEqual(waiting.posts, 1)
        XCTAssertEqual(value.originalAIActionTitle, "Check original result")
        await h.api.configure(status: .completed)
        h.advance(); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        XCTAssertEqual(value.draft?.caption, "AI caption")
        let done = await h.api.counts(); XCTAssertEqual(done.posts, 1)
    }

    func testUncertainMissingReceiptExplicitCheckIsGetOnlyEvenWithFastBudget() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        await h.api.configure(readError: APIError.requestFailed(status: 404, detail: .init("missing")))
        h.advance(); await value.checkAIResult(api: h.api, itemID: itemID)
        h.advance(); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
        XCTAssertEqual(value.draft?.caption, "original")
    }

    func testOldServerWithoutDeliveryCapabilityCannotAdmitNewPaidIntent() async throws {
        var state = remote(); state.aiRequestDeliveryVersion = nil
        let h = AIHarness(state: state); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 0)
        XCTAssertEqual(value.draft?.caption, "original")
    }
}

extension SlidePostAISessionRecoveryTests {
    func testRejectedSameInstructionExplicitSendIsNewIntentButUnknownIsObservationOnly() async throws {
        for status in [SlidePostAIReceipt.Status.rejected, .unknown] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
            _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            let original = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID)
            await h.api.configure(status: status)
            h.advance(); await value.checkAIResult(api: h.api, itemID: itemID)
            h.advance(); _ = await value.chatEdit(api: h.api, itemID: itemID, message: "caption")
            let current = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID)
            let counts = await h.api.counts()
            if status == .rejected { XCTAssertNotEqual(current, original); XCTAssertEqual(counts.posts, 2) }
            else { XCTAssertEqual(current, original); XCTAssertEqual(counts.posts, 1) }
        }
    }
}

extension SlidePostAIRecoveryTests {
    func testDeliveryV1AdmissionReservationIsAtomicAcrossStaleSessions() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "ai-once-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = SlidePostAIJournal(root: root)
        let scope = SlidePostSubmissionScope(owner: "owner", environment: "https://api.test", itemID: UUID().uuidString)
        let record = SlidePostAIRecord(scope: scope, projectID: UUID(), kind: .chatEdit, input: .object([:]), baseline: nil, operationID: UUID(), deliveryVersion: 1)
        try journal.reserve(record)
        let now = Date()
        _ = try journal.reserveAttempt(record, post: true, explicit: false, now: now)
        XCTAssertThrowsError(try journal.reserveAttempt(record, post: true, explicit: false, now: now.addingTimeInterval(60)))
        XCTAssertThrowsError(try journal.reserveAttempt(record, post: true, explicit: true, now: now.addingTimeInterval(60)))
        XCTAssertEqual(try journal.load(scope: scope, projectID: record.projectID, kind: .chatEdit)?.remainingPosts, 2)
    }
}

extension SlidePostAISessionRecoveryTests {
    func testLateInitialAdmissionCannotApplyAcrossStopAndResume() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); await value.refresh(api: h.api, itemID: itemID)
        await h.api.configure(successful: true); await h.api.suspendPosts()
        let pending = Task { await value.chatEdit(api: h.api, itemID: itemID, message: "caption") }
        let suspended = await h.api.waitForPostSuspension(); XCTAssertTrue(suspended)
        value.stopObserving(); value.startObserving()
        await h.api.releasePost(); _ = await pending.value
        XCTAssertEqual(value.draft?.caption, "original")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
        h.advance(); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        XCTAssertEqual(value.draft?.caption, "AI caption")
    }
}

// Unsupported legacy pending envelopes are observed as unknown, never rejected or resubmitted.
// Written against the real client journal/recovery paths; native execution remains held.
extension SlidePostAISessionRecoveryTests {
    private func seedLegacyUnknown(_ h: AIHarness, session: SlidePostSession) async throws -> UUID {
        await session.refresh(api: h.api, itemID: itemID)
        let baseline = try XCTUnwrap(session.draft)
        let draft = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(baseline))
        let input: JSONValue = .object(["message": .string("caption"), "expected_version": .number(1), "draft": draft])
        let id = UUID()
        var record = SlidePostAIRecord(scope: h.scope, projectID: projectID, kind: .chatEdit,
            input: input, baseline: baseline, operationID: id, deliveryVersion: 0)
        record.remainingReads = 0; record.remainingPosts = 0
        try h.journal.reserve(record)
        await h.api.configure(status: .unknown, deliveryVersion: 0)
        // Represents the original pre-upgrade provider invocation, not a client recovery POST.
        _ = try? await h.api.submitSlidePostAI(itemID: itemID,
            body: .init(clientRequestID: id, projectID: projectID, kind: .chatEdit, input: input), expectedOwner: h.owner)
        h.advance(30); await session.recoverAI(api: h.api, itemID: itemID, explicit: false)
        let observed = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        XCTAssertEqual(observed.receipt?.status, .unknown)
        XCTAssertEqual(observed.receipt?.deliveryVersion, 0)
        XCTAssertFalse(observed.delivered)
        return id
    }

    func testLegacyUnknownSlowObservationAcceptsOriginalLateCompletionWithoutPost() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); defer { value.stopObserving() }
        let id = try await seedLegacyUnknown(h, session: value)
        h.advance(29); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        var counts = await h.api.counts(); XCTAssertEqual(counts.reads, 1)
        await h.api.configure(status: .completed, deliveryVersion: 0)
        h.advance(1); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        XCTAssertEqual(value.draft?.caption, "AI caption")
        counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1); XCTAssertEqual(counts.reads, 2)
        let final = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        XCTAssertEqual(final.operationID, id); XCTAssertTrue(final.delivered)
    }

    func testLegacyUnknownStopRejectsLateGetAndReentryOnlyObservesOriginal() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); defer { value.stopObserving() }
        let id = try await seedLegacyUnknown(h, session: value)
        await h.api.configure(status: .completed, deliveryVersion: 0); await h.api.suspendReads()
        h.advance(30)
        let read = Task { await value.recoverAI(api: h.api, itemID: itemID, explicit: false) }
        let held = await h.api.waitForSuspension(); XCTAssertTrue(held)
        value.stopObserving(); await h.api.releaseRead(); await read.value
        XCTAssertEqual(value.draft?.caption, "original")
        h.advance(30); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        let stopped = await h.api.counts(); XCTAssertEqual(stopped.reads, 2); XCTAssertEqual(stopped.posts, 1)
        value.startObserving(); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        XCTAssertEqual(value.draft?.caption, "AI caption")
        let resumed = await h.api.counts(); XCTAssertEqual(resumed.posts, 1)
        XCTAssertEqual(try h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit)?.operationID, id)
    }

    func testLegacyUnknownUnresolvedConflictDoesNotRejectOrReplaceOriginalReceipt() async throws {
        let h = AIHarness(state: remote()); defer { h.clean() }
        let value = h.session(); defer { value.stopObserving() }
        let id = try await seedLegacyUnknown(h, session: value)
        await h.api.configure(status: .unknown, readError: APIError.requestFailed(status: 409, detail: .init("outcome unresolved")), deliveryVersion: 0)
        h.advance(30); await value.checkAIResult(api: h.api, itemID: itemID)
        let retained = try XCTUnwrap(h.journal.load(scope: h.scope, projectID: projectID, kind: .chatEdit))
        XCTAssertEqual(retained.operationID, id); XCTAssertEqual(retained.receipt?.status, .unknown); XCTAssertFalse(retained.delivered)
        await h.api.configure(status: .completed, deliveryVersion: 0)
        h.advance(30); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
        XCTAssertEqual(value.draft?.caption, "AI caption")
        let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
    }

    func testLegacyUnknownLateCompletionDoesNotOverwriteNewerDraftOrSource() async throws {
        for change in ["caption", "source"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); defer { value.stopObserving() }
            _ = try await seedLegacyUnknown(h, session: value)
            var newer = try XCTUnwrap(value.draft)
            if change == "caption" { newer.caption = "new local caption" }
            else { newer.slides[0].assetID = "replacement-source" }
            value.stageDraft(newer)
            await h.api.configure(status: .completed, deliveryVersion: 0)
            h.advance(30); await value.recoverAI(api: h.api, itemID: itemID, explicit: false)
            XCTAssertTrue(value.draft?.hasSameContent(as: newer) == true, "Old AI result cannot auto-adopt across replaced source bindings")
            XCTAssertEqual(value.proposal?.draft.caption, "AI caption", "Retain paid output for explicit review")
            let counts = await h.api.counts(); XCTAssertEqual(counts.posts, 1)
        }
    }

    func testLegacyUnknownLateReadCannotApplyAfterNewInstructionOrAccountTarget() async throws {
        for change in ["instruction", "account"] {
            let h = AIHarness(state: remote()); defer { h.clean() }
            let value = h.session(); defer { value.stopObserving() }
            let originalScope = h.scope
            let id = try await seedLegacyUnknown(h, session: value)
            await h.api.configure(status: .completed, deliveryVersion: 0); await h.api.suspendReads()
            h.advance(30)
            let read = Task { await value.recoverAI(api: h.api, itemID: itemID, explicit: false) }
            let held = await h.api.waitForSuspension(); XCTAssertTrue(held)
            if change == "instruction" { _ = await value.chatEdit(api: h.api, itemID: itemID, message: "new explicit request") }
            else { h.owner = "other-account" }
            await h.api.releaseRead(); await read.value
            XCTAssertNotEqual(value.draft?.caption, "AI caption")
            XCTAssertNil(value.proposal)
            let record = try XCTUnwrap(h.journal.load(scope: originalScope, projectID: projectID, kind: .chatEdit))
            XCTAssertFalse(record.delivered)
            if change == "instruction" { XCTAssertNotEqual(record.operationID, id) }
            else { XCTAssertEqual(record.operationID, id) }
            let counts = await h.api.counts(); XCTAssertEqual(counts.posts, change == "instruction" ? 2 : 1)
        }
    }
}
