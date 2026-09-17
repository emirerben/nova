import XCTest
@testable import Kria

@MainActor final class CreationConfirmationConflictTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private func generateError(status: Int, body: String) async -> Error? {
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/creation-threads/B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B/actions")
            return (status, Data(body.utf8))
        }
        do {
            _ = try await NativeEditorTestSupport.api().creationAction(
                threadID: UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!,
                action: "generate",
                payload: [:],
                expectedRevision: 3,
                clientActionID: "generate-1"
            )
            XCTFail("Expected a conflict")
            return nil
        } catch {
            return error
        }
    }

    func testConflictCarriesServerDetailAndStillMatchesExistingConflictChecks() async throws {
        let result = await generateError(
            status: 409,
            body: #"{"detail":"Footage or capabilities changed; review the plan again"}"#
        )
        let error = try XCTUnwrap(result)

        let apiError = try XCTUnwrap(error as? APIError)
        XCTAssertEqual(apiError.conflictDetail, "Footage or capabilities changed; review the plan again")
        XCTAssertEqual(apiError, .conflict)
        XCTAssertNotEqual(apiError, .requestFailed)
        var caughtByPattern = false
        do { throw error } catch APIError.conflict { caughtByPattern = true } catch {}
        XCTAssertTrue(caughtByPattern)
        switch error {
        case APIError.conflict: break
        default: XCTFail("A detailed conflict must match the existing switch pattern")
        }
        XCTAssertFalse(String(describing: apiError).contains("Footage"), "Diagnostics must not copy server text")
    }

    func testConflictDetailIsNilWithoutAStringDetail() async throws {
        let emptyResult = await generateError(status: 412, body: "")
        let empty = try XCTUnwrap(emptyResult as? APIError)
        XCTAssertEqual(empty, .conflict)
        XCTAssertNil(empty.conflictDetail)

        let structuredResult = await generateError(status: 409, body: #"{"detail":{"code":"x"}}"#)
        let structured = try XCTUnwrap(structuredResult as? APIError)
        XCTAssertEqual(structured, .conflict)
        XCTAssertNil(structured.conflictDetail)

        let blankResult = await generateError(status: 409, body: #"{"detail":"  "}"#)
        let blank = try XCTUnwrap(blankResult as? APIError)
        XCTAssertNil(blank.conflictDetail)
        XCTAssertNil(APIError.conflict.conflictDetail)
        XCTAssertNil(APIError.requestFailed.conflictDetail)
    }

    func testKnownConflictDetailsKeepTheirDedicatedErrors() async throws {
        let unavailableResult = await generateError(status: 409, body: #"{"detail":"Content plan is unavailable"}"#)
        let unavailable = try XCTUnwrap(unavailableResult as? APIError)
        XCTAssertEqual(unavailable, .contentPlanUnavailable)
        XCTAssertNotEqual(unavailable, .conflict)
    }

    func testStaleDirectionReasonsOfferARefresh() {
        let manifest = CreationConfirmationConflict(detail: "Footage or capabilities changed; review the plan again", planIdentity: "1|a")
        XCTAssertEqual(manifest.message, "Footage or capabilities changed; review the plan again.")
        XCTAssertTrue(manifest.offersDirectionRefresh)
        XCTAssertEqual(manifest.planIdentity, "1|a")

        for detail in [
            "Kria must prepare a direction in the selected Paper format",
            "This session has used its render attempts",
            "Ask Kria for a direction before confirming",
        ] {
            let conflict = CreationConfirmationConflict(detail: detail, planIdentity: "")
            XCTAssertEqual(conflict.message, detail + ".")
            XCTAssertTrue(conflict.offersDirectionRefresh, detail)
        }

        let punctuated = CreationConfirmationConflict(detail: "Footage lengths changed; review the alternating plan again!", planIdentity: "")
        XCTAssertEqual(punctuated.message, "Footage lengths changed; review the alternating plan again!")

        let missing = CreationConfirmationConflict(detail: nil, planIdentity: "")
        XCTAssertEqual(missing.message, CreationConfirmationConflict.missingReasonMessage)
        XCTAssertTrue(missing.offersDirectionRefresh)
    }

    func testRetryableReasonsKeepCreateAsTheOnlyAction() {
        let wait = CreationConfirmationConflict(detail: "Wait for the current render before confirming", planIdentity: "")
        XCTAssertEqual(wait.message, "Wait for the current render before confirming.")
        XCTAssertFalse(wait.offersDirectionRefresh)

        for detail in ["Creation thread changed", "Creator plan changed", "Idempotency key reused", "speech_cleanup_analysis_changed", "speech_cleanup_pending"] {
            let conflict = CreationConfirmationConflict(detail: detail, planIdentity: "")
            XCTAssertEqual(conflict.message, CreationConfirmationConflict.changedMessage, detail)
            XCTAssertFalse(conflict.offersDirectionRefresh, detail)
        }
    }

    func testPlanIdentityChangesWhenKriaPlansAgain() throws {
        func thread(_ creatorAgent: String) throws -> CreationThread {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(CreationThread.self, from: Data("""
            {"id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","title":"Draft","status":"active","revision":3,
             "runtime_version":1,"state":{},"updated_at":"2026-09-07T12:00:00Z","creator_agent":\(creatorAgent)}
            """.utf8))
        }
        let first = try thread(#"{"status":"awaiting_confirmation","version":1,"plan_hash":"a"}"#)
        let sameAfterRefresh = try thread(#"{"status":"awaiting_confirmation","version":1,"plan_hash":"a","revision":9}"#)
        let replanned = try thread(#"{"status":"awaiting_confirmation","version":2,"plan_hash":"b"}"#)
        XCTAssertEqual(first.creatorPlanIdentity, sameAfterRefresh.creatorPlanIdentity)
        XCTAssertNotEqual(first.creatorPlanIdentity, replanned.creatorPlanIdentity)
        XCTAssertEqual(CreationConfirmationConflict.refreshDirectionMessage, "Keep the same plan with my current footage")
    }
}
