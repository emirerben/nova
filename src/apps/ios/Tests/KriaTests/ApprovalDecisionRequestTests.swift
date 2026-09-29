import XCTest
@testable import Kria

/// Request-body and 409-decoding coverage for `decideApproval` (the speech-
/// cleanup offer, KRI): every approve/deny must send `speech_cleanup_aware:
/// true`, the two cleanup fields must be omitted (not null) when nil, and a
/// runtime-v2 `KriaProblem` 409 (`kria_runtime.py`'s `{"problem": {...}}`
/// envelope, distinct from the legacy `{"detail": "..."}` string every other
/// route sends) must surface its machine `code` through `APIError.conflictCode`.
/// Uses the same `NativeEditorURLProtocol`/`NativeEditorTestSupport` transport
/// as `CreationConfirmationConflictTests`.
@MainActor final class ApprovalDecisionRequestTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testApproveWithNoCleanupChoiceOmitsTheOptionalFieldsButAlwaysSendsAware() async throws {
        let threadID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
        let approvalID = UUID(uuidString: "C8D594F1-5D75-4C52-BF94-9EA05B9C0D9C")!
        var capturedBody: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads/\(threadID.uuidString)/approvals/\(approvalID.uuidString)/approve")
            capturedBody = (try? JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request))) as? [String: Any] ?? [:]
            return (200, Data("{}".utf8))
        }
        try await NativeEditorTestSupport.api().decideApproval(
            threadID: threadID, approvalID: approvalID, decision: "approve",
            expectedThreadRevision: 3, expectedDraftRevision: 1, fingerprint: "fp-1",
            speechCleanupAware: true, speechCleanupAnalysisID: nil, speechCleanupChoice: nil
        )
        XCTAssertEqual(capturedBody["speech_cleanup_aware"] as? Bool, true)
        XCTAssertNil(capturedBody["speech_cleanup_analysis_id"], "Nil fields must be omitted, never encoded as null")
        XCTAssertNil(capturedBody["speech_cleanup_choice"])
        XCTAssertEqual(capturedBody["expected_thread_revision"] as? Int, 3)
        XCTAssertEqual(capturedBody["expected_draft_revision"] as? Int, 1)
        XCTAssertEqual(capturedBody["expected_approval_fingerprint"] as? String, "fp-1")
    }

    func testApproveWithACleanupChoiceEncodesTheAnalysisIDAndChoice() async throws {
        let threadID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
        let approvalID = UUID(uuidString: "C8D594F1-5D75-4C52-BF94-9EA05B9C0D9C")!
        var capturedBody: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            capturedBody = (try? JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request))) as? [String: Any] ?? [:]
            return (200, Data("{}".utf8))
        }
        for choice in ["clean", "keep_original", "create_without_cleanup"] {
            try await NativeEditorTestSupport.api().decideApproval(
                threadID: threadID, approvalID: approvalID, decision: "approve",
                expectedThreadRevision: 3, expectedDraftRevision: 1, fingerprint: "fp-1",
                speechCleanupAware: true, speechCleanupAnalysisID: "analysis-1", speechCleanupChoice: choice
            )
            XCTAssertEqual(capturedBody["speech_cleanup_analysis_id"] as? String, "analysis-1", choice)
            XCTAssertEqual(capturedBody["speech_cleanup_choice"] as? String, choice)
            XCTAssertEqual(capturedBody["speech_cleanup_aware"] as? Bool, true, choice)
        }
    }

    func testDenyAlsoAlwaysSendsSpeechCleanupAwareTrue() async throws {
        let threadID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
        let approvalID = UUID(uuidString: "C8D594F1-5D75-4C52-BF94-9EA05B9C0D9C")!
        var capturedBody: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            XCTAssertTrue(request.url!.path.hasSuffix("/deny"))
            capturedBody = (try? JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request))) as? [String: Any] ?? [:]
            return (200, Data("{}".utf8))
        }
        try await NativeEditorTestSupport.api().decideApproval(
            threadID: threadID, approvalID: approvalID, decision: "deny",
            expectedThreadRevision: 3, expectedDraftRevision: 1, fingerprint: "fp-1",
            speechCleanupAware: true, speechCleanupAnalysisID: nil, speechCleanupChoice: nil
        )
        XCTAssertEqual(capturedBody["speech_cleanup_aware"] as? Bool, true)
        XCTAssertNil(capturedBody["speech_cleanup_analysis_id"])
        XCTAssertNil(capturedBody["speech_cleanup_choice"])
    }

    // MARK: - runtime-v2 `KriaProblem` 409 decoding

    func testConflictSurfacesTheKriaProblemCodeAndMessage() async throws {
        let threadID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
        let approvalID = UUID(uuidString: "C8D594F1-5D75-4C52-BF94-9EA05B9C0D9C")!
        NativeEditorURLProtocol.handler = { _ in
            (409, Data(#"""
            {"problem": {"code": "speech_cleanup_pending", "phase": "approval",
                         "message": "The speech check is still running.", "retryable": true,
                         "recovery": "ask_user", "trace_id": "t1"}}
            """#.utf8))
        }
        do {
            try await NativeEditorTestSupport.api().decideApproval(
                threadID: threadID, approvalID: approvalID, decision: "approve",
                expectedThreadRevision: 3, expectedDraftRevision: 1, fingerprint: "fp-1",
                speechCleanupAware: true, speechCleanupAnalysisID: "analysis-1", speechCleanupChoice: "clean"
            )
            XCTFail("Expected a conflict")
        } catch {
            let apiError = try XCTUnwrap(error as? APIError)
            XCTAssertEqual(apiError, .conflict)
            XCTAssertEqual(apiError.conflictCode, "speech_cleanup_pending")
            XCTAssertEqual(apiError.conflictDetail, "The speech check is still running.")
            XCTAssertTrue(speechCleanupConflictCodes.contains(try XCTUnwrap(apiError.conflictCode)))
        }
    }

    func testAllFiveSpeechCleanupConflictCodesAreRecognized() {
        for code in [
            "speech_cleanup_pending", "speech_cleanup_choice_required", "speech_cleanup_failed",
            "speech_cleanup_choice_not_allowed", "speech_cleanup_analysis_changed",
        ] {
            XCTAssertTrue(speechCleanupConflictCodes.contains(code), code)
        }
        XCTAssertFalse(speechCleanupConflictCodes.contains("concurrent_update"))
    }

    func testConflictCodeIsNilForTheLegacyDetailStringShape() async throws {
        let threadID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
        let approvalID = UUID(uuidString: "C8D594F1-5D75-4C52-BF94-9EA05B9C0D9C")!
        NativeEditorURLProtocol.handler = { _ in (409, Data(#"{"detail":"Creation thread changed"}"#.utf8)) }
        do {
            try await NativeEditorTestSupport.api().decideApproval(
                threadID: threadID, approvalID: approvalID, decision: "approve",
                expectedThreadRevision: 3, expectedDraftRevision: 1, fingerprint: "fp-1",
                speechCleanupAware: true, speechCleanupAnalysisID: nil, speechCleanupChoice: nil
            )
            XCTFail("Expected a conflict")
        } catch {
            let apiError = try XCTUnwrap(error as? APIError)
            XCTAssertNil(apiError.conflictCode)
            XCTAssertEqual(apiError.conflictDetail, "Creation thread changed")
        }
    }

    func testSpeechCleanupDispatchRefusalKeepsTheFreshDraftRetry() {
        // A v2 speech-cleanup dispatch refusal arrives as render_dispatch_failed;
        // "Refresh project" asks Kria for a fresh draft whose approval asks again.
        XCTAssertFalse(isNonRetryableFailureCode("render_dispatch_failed"))
        XCTAssertFalse(isNonRetryableFailureCode("speech_cleanup_failed"))
        XCTAssertTrue(isNonRetryableFailureCode("speech_cleanup_unavailable_on_phone"))
        XCTAssertTrue(isNonRetryableFailureCode("phone_not_enrolled"))
        XCTAssertFalse(isNonRetryableFailureCode(nil))
    }
}
