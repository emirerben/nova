import XCTest
import Combine
@testable import Kria

@MainActor final class ChatSendReceiptTests: XCTestCase {
    func testUnconfirmedSubmissionSurvivesRelaunchWithoutChangingItsRequest() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let request = SubmitTurnRequest(message: "Keep my whole take", clientEventID: "one-prompt", expectedThreadRevision: 8)
        var receipt = ChatSendReceipt(message: request.message, clientEventID: request.clientEventID,
            expectedRevision: 8, afterSequence: 12, draft: "Keep my whole take", runtimeVersion: 2)
        receipt.phase = .unconfirmed
        receipt.requestBody = try JSONEncoder().encode(request)
        let store = ChatDraftStore(defaults: defaults)
        try store.setSubmission(receipt, for: id)
        store.setDraft("", for: id)

        let restored = try XCTUnwrap(ChatDraftStore(defaults: defaults).submission(for: id))
        XCTAssertEqual(restored, receipt)
        XCTAssertEqual(ChatDraftStore(defaults: defaults).draft(for: id), "",
                       "Submitted recovery text belongs to the receipt, not to a new composer draft")
        var replay = SubmitTurnRequest(message: "changed", clientEventID: "new-id", expectedThreadRevision: 99)
        replay.frozenBody = restored.requestBody
        let body = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(replay))
        XCTAssertEqual(body.objectValue?["message"], .string("Keep my whole take"))
        XCTAssertEqual(body.objectValue?["client_event_id"], .string("one-prompt"))
        XCTAssertEqual(body.objectValue?["expected_thread_revision"], .number(8))
        try store.setSubmission(nil, for: id)
        XCTAssertNil(store.submission(for: id))
        XCTAssertEqual(store.draft(for: id), "")
    }

    func testStructuredAnswerContextSurvivesRelaunch() throws {
        let suite = "send-context-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        for context in [ChatSendResponseContext.songRange("song-question"),
                        .momentOrder("moment-question"), .protectedRegion("mark-question")] {
            let id = UUID()
            var receipt = ChatSendReceipt(message: "Card answer", clientEventID: UUID().uuidString,
                expectedRevision: 2, afterSequence: 4, draft: "", runtimeVersion: 2)
            receipt.phase = .unconfirmed
            receipt.responseContext = context
            try ChatDraftStore(defaults: defaults).setSubmission(receipt, for: id)
            let restored = try XCTUnwrap(ChatDraftStore(defaults: defaults).submission(for: id))
            XCTAssertEqual(restored.responseContext, context)
            XCTAssertEqual(restored.responseContext?.songRangeQuestionID, context.songRangeQuestionID)
            XCTAssertEqual(restored.responseContext?.momentOrderQuestionID, context.momentOrderQuestionID)
            XCTAssertEqual(restored.responseContext?.protectedRegionQuestionID, context.protectedRegionQuestionID)
            XCTAssertEqual(restored.responseContext?.isProtectedRegion, context.isProtectedRegion)
        }
    }

    func testAcceptanceIsDistinctFromUnconfirmedAndClearsOnlyMatchingIdentity() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        var receipt = ChatSendReceipt(message: "Hello", clientEventID: "first", expectedRevision: 2,
                                      afterSequence: 4, draft: "Hello", runtimeVersion: 2)
        XCTAssertEqual(receipt.phase, .preparing)
        receipt.phase = .accepted
        receipt.turnID = "turn-1"
        try store.setSubmission(receipt, for: id)
        store.acknowledgeSubmission(clientEventID: "older", for: id)
        XCTAssertEqual(store.submission(for: id)?.turnID, "turn-1")
        store.acknowledgeSubmission(clientEventID: "first", for: id)
        XCTAssertNil(store.submission(for: id))
    }
    func testRejectedSubmissionKeepsBothTheSubmittedTextAndANewerDraft() {
        XCTAssertEqual(ChatDraftStore.recoveredDraft(submitted: "Original prompt", current: "Next draft"),
                       "Original prompt\n\nNext draft")
        XCTAssertEqual(ChatDraftStore.recoveredDraft(submitted: "Original prompt", current: ""), "Original prompt")
        XCTAssertEqual(ChatDraftStore.recoveredDraft(submitted: "Original prompt", current: "Original prompt"), "Original prompt")
        XCTAssertEqual(ChatDraftStore.recoveredDraft(submitted: "", current: "Next draft"), "Next draft")
        XCTAssertEqual(ChatDraftStore.recoveredDraft(submitted: "Original prompt", current: "Original prompt\n\nNext draft"),
                       "Original prompt\n\nNext draft")
    }

    func testInterruptedPreflightRestoresSubmittedTextAlongsideTheLatestPersistedDraft() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        let receipt = ChatSendReceipt(message: "Original prompt", clientEventID: "old-view", expectedRevision: 0,
                                      afterSequence: -1, draft: "Original prompt", runtimeVersion: 2)
        try store.setSubmission(receipt, for: id)
        store.setDraft("Newer draft", for: id)
        let reopened = ChatDraftStore(defaults: defaults)
        XCTAssertEqual(reopened.recoverSubmission(clientEventID: "old-view", for: id), "Original prompt\n\nNewer draft")
        XCTAssertEqual(store.draft(for: id), "Original prompt\n\nNewer draft")
        XCTAssertNil(store.submission(for: id))
    }

    func testOldCompletionCannotRecoverOverANewerSubmission() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        let receipt = ChatSendReceipt(message: "New request", clientEventID: "new-view", expectedRevision: 1,
                                      afterSequence: 2, draft: "New request", runtimeVersion: 2)
        try store.setSubmission(receipt, for: id)
        store.setDraft("Newer draft", for: id)
        XCTAssertNil(store.recoverSubmission(clientEventID: "old-view", for: id))
        XCTAssertEqual(store.draft(for: id), "Newer draft")
        XCTAssertEqual(store.submission(for: id), receipt)
    }

    func testAcknowledgementPreservesANewDraftEqualToTheSubmittedMessage() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        var receipt = ChatSendReceipt(message: "Same text", clientEventID: "old-view", expectedRevision: 1,
                                      afterSequence: 2, draft: "Same text", runtimeVersion: 2)
        receipt.phase = .unconfirmed
        try store.setSubmission(receipt, for: id)
        store.setDraft("Same text", for: id)
        store.acknowledgeSubmission(clientEventID: "old-view", for: id)
        XCTAssertEqual(store.draft(for: id), "Same text")
        XCTAssertNil(store.submission(for: id))
    }

    func testLateFailureCannotRecoverOrReplaceAnAcceptedSubmission() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        var accepted = ChatSendReceipt(message: "Accepted text", clientEventID: "same", expectedRevision: 1,
                                       afterSequence: 2, draft: "Accepted text", runtimeVersion: 2)
        accepted.phase = .accepted
        try store.setSubmission(accepted, for: id)
        store.setDraft("Next draft", for: id)
        XCTAssertNil(store.recoverSubmission(clientEventID: "same", for: id))
        var older = accepted
        older.phase = .unconfirmed
        XCTAssertThrowsError(try store.setSubmission(older, for: id))
        XCTAssertEqual(store.submission(for: id), accepted)
        XCTAssertEqual(store.draft(for: id), "Next draft")
    }

    func testViewsShareOneLiveAttemptAndObserveCanonicalRecovery() throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        var visibleDraft = ""
        var observedReceipts: [String?] = []
        let observation = store.$revision.sink { _ in
            visibleDraft = store.draft(for: id)
            observedReceipts.append(store.submission(for: id)?.clientEventID)
        }
        let receipt = ChatSendReceipt(message: "Original prompt", clientEventID: "one-attempt", expectedRevision: 1,
                                      afterSequence: 2, draft: "Original prompt", runtimeVersion: 2)
        XCTAssertTrue(try store.beginSubmission(receipt, for: id))
        XCTAssertFalse(try store.beginSubmission(receipt, for: id))
        XCTAssertTrue(store.isSubmitting(for: id))
        XCTAssertFalse(ChatDraftStore(defaults: defaults).isSubmitting(for: id), "Relaunch does not invent a live task")
        store.setDraft("Newer draft", for: id)
        let beforeRecovery = observedReceipts.count
        XCTAssertEqual(store.recoverSubmission(clientEventID: receipt.clientEventID, for: id), "Original prompt\n\nNewer draft")
        XCTAssertEqual(observedReceipts.count, beforeRecovery + 1, "Recovery publishes one complete state")
        XCTAssertNil(observedReceipts.last!)
        XCTAssertEqual(visibleDraft, "Original prompt\n\nNewer draft")
        store.endSubmission(clientEventID: receipt.clientEventID, for: id)
        XCTAssertFalse(store.isSubmitting(for: id))
        withExtendedLifetime(observation) { }
    }

    @MainActor
    func testUnconfirmedMessageProtectsAnEmptyChatFromAbandonment() async throws {
        let suite = "send-receipt-\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite); NativeEditorURLProtocol.handler = nil }
        let id = UUID()
        let store = ChatDraftStore(defaults: defaults)
        var receipt = ChatSendReceipt(message: "Suggest an edit", clientEventID: "first", expectedRevision: 0,
                                      afterSequence: -1, draft: "", runtimeVersion: 2)
        receipt.phase = .unconfirmed
        try store.setSubmission(receipt, for: id)
        var requests = 0
        NativeEditorURLProtocol.handler = { _ in requests += 1; return (500, Data()) }
        let model = AppModel(api: NativeEditorTestSupport.api(), chatDrafts: store)
        model.projects = [ProjectSummary(id: id, title: "Untitled project", status: .draft,
                                        updatedAt: .now, posterURL: nil, serverRevision: 0)]
        await model.discardIfAbandoned(id)
        XCTAssertEqual(requests, 0)
        XCTAssertNotNil(store.submission(for: id))
    }

    @MainActor
    func testRetrySendsTheSameBodyOverTheActualAPIBoundary() async throws {
        defer { NativeEditorURLProtocol.handler = nil }
        let api = NativeEditorTestSupport.api()
        let id = UUID()
        var bodies: [JSONValue] = []
        NativeEditorURLProtocol.handler = { request in
            bodies.append(try JSONDecoder().decode(JSONValue.self, from: NativeEditorTestSupport.bodyData(request)))
            if bodies.count == 1 { throw URLError(.networkConnectionLost) }
            return (202, Data("{\"turn_id\":\"one-turn\",\"thread_revision\":9,\"status\":\"pending\"}".utf8))
        }
        let request = SubmitTurnRequest(message: "Keep my whole take", clientEventID: "same-message", expectedThreadRevision: 8)
        do {
            _ = try await api.submitTurn(threadID: id, request: request)
            XCTFail("The first connection must fail")
        } catch { }
        var retry = SubmitTurnRequest(message: "changed editor", clientEventID: "different", expectedThreadRevision: 99)
        retry.frozenBody = try JSONEncoder().encode(request)
        let accepted = try await api.submitTurn(threadID: id, request: retry)
        XCTAssertEqual(accepted.turnID, "one-turn")
        XCTAssertEqual(bodies.count, 2)
        XCTAssertEqual(bodies.first, bodies.last)
        XCTAssertEqual(bodies.last?.objectValue?["client_event_id"], .string("same-message"))
    }

}
