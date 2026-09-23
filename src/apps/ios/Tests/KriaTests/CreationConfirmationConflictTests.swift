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
        XCTAssertNotEqual(apiError, .requestFailed(status: 409))
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
        XCTAssertNil(APIError.requestFailed(status: 500).conflictDetail)
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

    /// KRI-132: the confirmation stage reads a phone-gate rejection's structured
    /// code off the most recent `assistant_error` event, since `creator_agent`
    /// doesn't carry `last_error` directly.
    func testLastAssistantErrorCodeReadsTheMostRecentEvent() throws {
        func thread(_ events: String) throws -> CreationThread {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(CreationThread.self, from: Data("""
            {"id":"B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B","title":"Draft","status":"failed","revision":3,
             "runtime_version":1,"state":{},"updated_at":"2026-09-07T12:00:00Z","events":\(events)}
            """.utf8))
        }
        let none = try thread("[]")
        XCTAssertNil(none.lastAssistantErrorCode)
        XCTAssertNil(none.lastAssistantErrorMessage)

        let phoneGate = try thread(#"""
        [
          {"id":"e1","sequence":1,"revision":1,"role":"assistant","event_type":"assistant_error",
           "payload":{"code":"phone_format_unavailable","message":"Only Montage videos can render on your iPhone right now."},
           "created_at":"2026-09-07T11:00:00Z"},
          {"id":"e2","sequence":2,"revision":2,"role":"assistant","event_type":"assistant_review",
           "payload":{},"created_at":"2026-09-07T11:05:00Z"}
        ]
        """#)
        XCTAssertEqual(phoneGate.lastAssistantErrorCode, "phone_format_unavailable")
        XCTAssertEqual(phoneGate.lastAssistantErrorMessage, "Only Montage videos can render on your iPhone right now.")
        XCTAssertTrue(nonRetryablePhoneGateErrorCodes.contains(try XCTUnwrap(phoneGate.lastAssistantErrorCode)))

        let transient = try thread(#"""
        [{"id":"e1","sequence":1,"revision":1,"role":"assistant","event_type":"assistant_error",
          "payload":{"code":"execution_failed","message":"I couldn't start that render."},
          "created_at":"2026-09-07T11:00:00Z"}]
        """#)
        XCTAssertFalse(nonRetryablePhoneGateErrorCodes.contains(try XCTUnwrap(transient.lastAssistantErrorCode)))
    }

    /// KRI-118 item 4: `strategy_invalid` (`app.services.creator_errors
    /// .CreatorStrategyError`), `phone_self_narration_multi_clip`
    /// (`PHONE_GATE_MESSAGES["self_narration_multi_clip"]`), and
    /// `speech_cleanup_unavailable_on_phone` all fail their session the same
    /// deterministic way the original phone-gate codes do (before any Job
    /// exists), so "Retry generation" must stay suppressed for them too --
    /// while an ordinary transient failure keeps offering it.
    func testNonRetryableErrorCodesCoverKRI118StructuralFailuresAlongsidePhoneGate() {
        for code in [
            "phone_not_enrolled", "phone_plan_unapproved", "phone_format_unavailable", "phone_voiceover_unavailable",
            "strategy_invalid", "phone_self_narration_multi_clip", "speech_cleanup_unavailable_on_phone",
        ] {
            XCTAssertTrue(nonRetryablePhoneGateErrorCodes.contains(code), code)
        }
        for code in ["execution_failed", "self_narration_multi_clip", "format_mismatch", "ffmpeg_failed"] {
            XCTAssertFalse(nonRetryablePhoneGateErrorCodes.contains(code), code)
        }
    }

    // MARK: - storyShapeSubtitle (KRI-118 item 2)

    func testStoryShapeSubtitleMapsKnownShapesAndHidesEverythingElse() {
        XCTAssertEqual(storyShapeSubtitle(creatorAgent: ["story_shape": .string("day_vlog")]), "Day vlog")
        XCTAssertEqual(storyShapeSubtitle(creatorAgent: ["story_shape": .string("single_hero")]), "Single hero")
        XCTAssertNil(storyShapeSubtitle(creatorAgent: ["story_shape": .string("classic_montage")]))
        XCTAssertNil(storyShapeSubtitle(creatorAgent: [:]))
        XCTAssertNil(storyShapeSubtitle(creatorAgent: nil))
    }

    // MARK: - mergedWhatKriaChanged (KRI-118 item 3)

    func testWhatKriaChangedMergesAdjustmentsBeforeNoticesAndDedups() {
        let merged = mergedWhatKriaChanged(creatorAgent: [
            "adjustments": .array([.string("Trimmed the intro to match the beat."), .string("Kept the laugh in.")]),
            "notices": .array([.string("Kept the laugh in."), .string("Skipped the voiceover; no clear speech found.")]),
        ])
        XCTAssertEqual(merged, [
            "Trimmed the intro to match the beat.",
            "Kept the laugh in.",
            "Skipped the voiceover; no clear speech found.",
        ])
    }

    func testWhatKriaChangedIsEmptyWhenBothListsAreAbsentOrEmpty() {
        XCTAssertEqual(mergedWhatKriaChanged(creatorAgent: nil), [])
        XCTAssertEqual(mergedWhatKriaChanged(creatorAgent: [:]), [])
        XCTAssertEqual(mergedWhatKriaChanged(creatorAgent: ["adjustments": .array([]), "notices": .array([])]), [])
    }

    // MARK: - nonConfirmationConflictMessage (KRI-118 item 5)

    /// `select_format`'s 409 is not in `confirmationActions`, so it used to
    /// collapse into the generic `changedMessage` regardless of what the
    /// server actually said (e.g. a real `format_mismatch` sentence). It must
    /// now show that real sentence instead.
    func testSelectFormatConflictSurfacesTheRealServerDetail() {
        let detail = "Kria's current direction was prepared for a different format than the one you picked. Ask Kria for a new direction."
        XCTAssertEqual(
            nonConfirmationConflictMessage(action: "select_format", detail: detail, planIdentity: "1|a"),
            detail
        )
        XCTAssertEqual(
            nonConfirmationConflictMessage(action: "select_edit_format", detail: detail, planIdentity: "1|a"),
            detail
        )
    }

    func testSelectFormatConflictFallsBackWhenTheServerSentNoDetail() {
        XCTAssertEqual(
            nonConfirmationConflictMessage(action: "select_format", detail: nil, planIdentity: ""),
            CreationConfirmationConflict.missingReasonMessage
        )
    }

    /// Every other non-confirmation action keeps the old, deliberately vague
    /// fallback -- only `select_format`/`select_edit_format` are known to
    /// carry a server detail worth surfacing verbatim.
    func testOtherNonConfirmationActionsKeepTheGenericChangedMessage() {
        XCTAssertEqual(
            nonConfirmationConflictMessage(action: "remove_media", detail: "some detail", planIdentity: ""),
            CreationConfirmationConflict.changedMessage
        )
    }
}
