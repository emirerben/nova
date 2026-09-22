import XCTest
@testable import Kria

final class ChatWorkspaceTests: XCTestCase {
    func testPendingUploadDoesNotCountAsAttachedFootage() {
        let uploading = FootageReadiness(attachedCount: 0, pendingCount: 1)
        XCTAssertEqual(uploading.attachedCount, 0)
        XCTAssertEqual(uploading.pendingCount, 1)
        XCTAssertFalse(uploading.canContinue)

        let attached = FootageReadiness(attachedCount: 1, pendingCount: 1)
        XCTAssertFalse(attached.canContinue)
        XCTAssertTrue(FootageReadiness(attachedCount: 1, pendingCount: 0).canContinue)
    }

    func testTranscriptProjectionMatchesWebAssistantEventAllowlist() throws {
        let assistantEventTypes = [
            "format_prompt", "media_prompt", "upload_prompt", "voiceover_prompt",
            "confirm_generation", "confirmation", "revision_queued", "status_update",
            "assistant_question", "assistant_response", "draft_applied", "assistant_strategy",
            "assistant_review", "assistant_error", "assistant_render_failed", "memory_updated",
            "creator_memory_receipt", "agent_assistant_question", "agent_assistant_strategy",
            "agent_assistant_review", "agent_assistant_error", "agent_assistant_render_failed",
        ]

        for eventType in assistantEventTypes {
            let event = ThreadEvent(
                id: eventType,
                sequence: 1,
                revision: 1,
                role: "assistant",
                eventType: eventType,
                content: "Message for \(eventType)",
                payload: nil,
                createdAt: .now
            )
            XCTAssertEqual(
                try XCTUnwrap(ChatTranscriptMessage.from(event: event)).role,
                .assistant,
                "Expected \(eventType) to be rendered as conversation"
            )
        }
    }

    func testTranscriptHistoryMergeDeduplicatesAndPreservesChronology() {
        let existing = [event(id: "later", sequence: 4), event(id: "first", sequence: 1)]
        let incoming = [
            event(id: "middle", sequence: 2),
            event(id: "later", sequence: 4, content: "same append-only event"),
        ]

        let merged = ChatTranscriptHistory.merge(existing, with: incoming)

        XCTAssertEqual(merged.map(\.id), ["first", "middle", "later"])
        XCTAssertEqual(ChatTranscriptHistory.nextAfterSequence(current: -1, response: 2, events: merged), 4)
    }

    func testLatePollCannotRegressRevisionAfterSubmitOrAction() {
        let revisionAfterSubmit = ThreadRevisionOrder.advance(current: 8, incoming: 9)
        XCTAssertEqual(revisionAfterSubmit, 9)
        XCTAssertFalse(ThreadRevisionOrder.acceptsProjection(current: revisionAfterSubmit, incoming: 8))
        XCTAssertEqual(ThreadRevisionOrder.advance(current: revisionAfterSubmit, incoming: 8), 9)

        let revisionAfterAction = ThreadRevisionOrder.advance(current: revisionAfterSubmit, incoming: 10)
        XCTAssertEqual(revisionAfterAction, 10)
        XCTAssertFalse(ThreadRevisionOrder.acceptsProjection(current: revisionAfterAction, incoming: 9))
        XCTAssertTrue(ThreadRevisionOrder.acceptsProjection(current: revisionAfterAction, incoming: 10))
    }

    func testFormatPromptExposesOnlyServerAvailableChoices() {
        let prompt = ThreadEvent(
            id: "format-prompt",
            sequence: 1,
            revision: 1,
            role: "assistant",
            eventType: "format_prompt",
            content: "Choose a format",
            payload: ["formats": .object(["montage": .string("montage"), "narrated": .string("narrated_planned")])],
            createdAt: .now
        )

        XCTAssertEqual(CreationFormat.available(in: [prompt]), [.montage, .narrated])
    }

    func testAcceptedMutationRefreshFailureDoesNotReportTheMutationAsRejected() async throws {
        let failure = await acceptedMutationRefreshError(
            "Your message was sent, but the conversation couldn’t refresh.",
            refresh: { throw APIError.requestFailed(status: 503) }
        )

        XCTAssertEqual(
            failure?.message,
            "Your message was sent, but the conversation couldn’t refresh. \(APIError.requestFailed(status: 503).localizedDescription)"
        )
        XCTAssertEqual(failure?.cause, .server)
        XCTAssertFalse(try XCTUnwrap(failure?.message).contains("wasn’t sent"))
    }

    func testAcceptedMutationRefreshSuccessNeedsNoRecoveryMessage() async {
        let failure = await acceptedMutationRefreshError(
            "The mutation succeeded.",
            refresh: { true }
        )

        XCTAssertNil(failure)
    }

    func testSuccessfulUnchangedPollClearsStaleRecoveryMessage() {
        var failure: ChatFailure? = ChatFailure("Kria lost the live connection. Your conversation is safe.", cause: .connection)

        clearChatRefreshRecoveryMessage(&failure)

        XCTAssertNil(failure)
    }

    func testSuccessfulPollClearsServerErrorRefreshMessage() {
        var failure: ChatFailure? = ChatFailure("Kria couldn’t refresh this conversation.", error: APIError.requestFailed(status: 502))

        clearChatRefreshRecoveryMessage(&failure)

        XCTAssertNil(failure)
    }

    func testSuccessfulUnchangedPollPreservesNonRecoveryMessage() {
        let unsent = ChatFailure("Your message wasn’t sent.", error: URLError(.timedOut))
        var failure: ChatFailure? = unsent

        clearChatRefreshRecoveryMessage(&failure)

        XCTAssertEqual(failure, unsent)
    }

    func testSuccessfulPollClearsUnconfirmedSendRecoveryMessage() {
        var failure: ChatFailure? = ChatFailure("Kria couldn’t confirm that message. Your draft is saved here; retry to check it safely.")

        clearChatRefreshRecoveryMessage(&failure)

        XCTAssertNil(failure)
    }

    func testAssistantQuestionExposesTappableOptionsAndRecommendation() throws {
        let event = ThreadEvent(
            id: "capacity-question",
            sequence: 1,
            revision: 1,
            role: "assistant",
            eventType: "agent_assistant_question",
            content: "This edit cannot show all 34 clips in 30 seconds.",
            payload: [
                "options": .array([
                    .string("Keep 30 seconds with the strongest clips"),
                    .string("Keep 30 seconds and include everything with faster pacing"),
                ]),
                "recommended_option": .string("Keep 30 seconds with the strongest clips"),
            ],
            createdAt: .now
        )

        let message = try XCTUnwrap(ChatTranscriptMessage.from(event: event))

        XCTAssertEqual(message.options, [
            "Keep 30 seconds with the strongest clips",
            "Keep 30 seconds and include everything with faster pacing",
        ])
        XCTAssertEqual(message.recommendedOption, "Keep 30 seconds with the strongest clips")
    }

    func testNonQuestionAssistantEventIgnoresStrayOptionsPayload() throws {
        let event = ThreadEvent(
            id: "strategy",
            sequence: 1,
            revision: 1,
            role: "assistant",
            eventType: "agent_assistant_strategy",
            content: "Here’s the direction I’ll use.",
            payload: ["options": .array([.string("should not render")])],
            createdAt: .now
        )

        let message = try XCTUnwrap(ChatTranscriptMessage.from(event: event))

        XCTAssertEqual(message.options, [])
    }

    func testClipSelectionCapacityHonorsServerLimitAcrossRepeatedSelections() {
        let initial = ClipSelectionCapacity(maximum: 1, existing: 0, reserved: 0)
        XCTAssertEqual(initial.acceptedCount(requested: 4), 1)

        let afterReservation = ClipSelectionCapacity(maximum: 1, existing: 0, reserved: 1)
        XCTAssertEqual(afterReservation.remaining, 0)
        XCTAssertEqual(afterReservation.acceptedCount(requested: 1), 0)

        let partiallyFilled = ClipSelectionCapacity(maximum: 20, existing: 17, reserved: 1)
        XCTAssertEqual(partiallyFilled.remaining, 2)
        XCTAssertEqual(partiallyFilled.acceptedCount(requested: 8), 2)
    }

    /// KRI-125: once the picker is seeded with what is already chosen, those items occupy picker
    /// slots, so `maxSelectionCount` is the project cap minus only what the picker CAN'T show as a
    /// selection (Files imports, voiceover, media attached elsewhere).
    func testPickerSelectionLimitAccountsForPreselectedClips() {
        // 3 attached, all from Photos and all shown as chosen: room for the other 7.
        let allFromPhotos = ClipSelectionCapacity(maximum: 10, existing: 3, reserved: 0, preselected: 3)
        XCTAssertEqual(allFromPhotos.pickerSelectionLimit, 10)
        XCTAssertEqual(allFromPhotos.remaining, 7)

        // 3 attached but 2 came from Files: only 1 is a picker selection, so the picker may hold 8.
        let mixed = ClipSelectionCapacity(maximum: 10, existing: 3, reserved: 0, preselected: 1)
        XCTAssertEqual(mixed.pickerSelectionLimit, 8)
        XCTAssertEqual(mixed.remaining, 7, "the user can still add exactly as many as before")

        // Talking-to-camera (one clip): the attached clip is shown chosen, and the picker holds 1.
        let single = ClipSelectionCapacity(maximum: 1, existing: 1, reserved: 0, preselected: 1)
        XCTAssertEqual(single.pickerSelectionLimit, 1)
        XCTAssertEqual(single.remaining, 0)
    }

    func testPickerSelectionLimitNeverReachesZeroOrExceedsTheCap() {
        // PhotosUI treats a limit of 0 as "unlimited".
        XCTAssertEqual(ClipSelectionCapacity(maximum: 0, existing: 0, reserved: 0).pickerSelectionLimit, 1)
        XCTAssertEqual(ClipSelectionCapacity(maximum: 5, existing: 5, reserved: 0).pickerSelectionLimit, 1)
        // More preselected than existing (a stale count) must not push the limit past the cap.
        XCTAssertEqual(ClipSelectionCapacity(maximum: 4, existing: 1, reserved: 0, preselected: 3).pickerSelectionLimit, 4)
    }

    /// The cap can drop below what is already attached (a format switch, or capabilities falling back).
    /// A picker limit under the seeded count would make the picker drop items, and a dropped item must
    /// never be able to read as the user un-choosing it.
    func testPickerSelectionLimitIsNeverBelowWhatIsSeededEvenWhenOverTheCap() {
        let overCap = ClipSelectionCapacity(maximum: 6, existing: 8, reserved: 0, preselected: 8)
        XCTAssertEqual(overCap.pickerSelectionLimit, 8, "must hold everything it is seeded with")
        XCTAssertEqual(overCap.remaining, 0, "and there is no room to add more")

        let partlyOver = ClipSelectionCapacity(maximum: 6, existing: 9, reserved: 0, preselected: 5)
        XCTAssertGreaterThanOrEqual(partlyOver.pickerSelectionLimit, 5)
    }

    func testPickerSelectionLimitWithoutPhotosAccessMatchesTheOldBehavior() {
        // No library ⇒ nothing is preselected ⇒ the limit is just the remaining room, as before.
        let capacity = ClipSelectionCapacity(maximum: 10, existing: 3, reserved: 2)
        XCTAssertEqual(capacity.pickerSelectionLimit, capacity.remaining)
    }

    func testClipsBeingPreparedCountAgainstTheLimitWithoutBeingDoubleCounted() {
        // 2 attached from Files (invisible to the picker) + 3 photo picks still being prepared. The 3
        // are in `reserved` AND shown as chosen: they must shrink the room once, not twice.
        let preparing = ClipSelectionCapacity(maximum: 10, existing: 2, reserved: 3, preselected: 3)
        XCTAssertEqual(preparing.remaining, 5)
        XCTAssertEqual(preparing.pickerSelectionLimit, 8)
    }

    /// The property the arithmetic exists to guarantee: the picker may hold what it already shows as
    /// chosen plus the room that is left — never more (server 409s at attach) and never less (the
    /// user could not add clips they still have room for).
    func testPickerSelectionLimitIsWhatIsChosenPlusRoomLeft() {
        for maximum in [1, 5, 10, 20] {
            for existing in 0...maximum {
                for preselected in 0...existing {
                    let capacity = ClipSelectionCapacity(maximum: maximum, existing: existing, reserved: 0, preselected: preselected)
                    XCTAssertEqual(capacity.pickerSelectionLimit, max(1, preselected + capacity.remaining),
                                   "max \(maximum), existing \(existing), preselected \(preselected)")
                }
            }
        }
    }

    func testOverallProgressAveragesEveryClipOnItsWayAndCountsPreparingOnesAsZero() {
        XCTAssertEqual(FootageReadiness.overallProgress(uploads: [1, 0], preparingCount: 0), 0.5)
        // Two uploads at 100% and 50%, plus two clips not yet uploading: (1 + 0.5) / 4.
        XCTAssertEqual(FootageReadiness.overallProgress(uploads: [1, 0.5], preparingCount: 2), 0.375, accuracy: 0.0001)
        XCTAssertEqual(FootageReadiness.overallProgress(uploads: [], preparingCount: 3), 0, "only preparing clips: nothing has uploaded yet")
        XCTAssertEqual(FootageReadiness.overallProgress(uploads: [], preparingCount: 0), 0, "no division by zero")
        XCTAssertEqual(FootageReadiness.overallProgress(uploads: [3], preparingCount: 0), 1, "clamped to a valid ProgressView value")
    }

    func testCreationFormatFallbackClipLimitProtectsTalkingToCameraOffline() {
        XCTAssertEqual(CreationFormat.talkingToCamera.fallbackMaximumClipCount, 1)
        XCTAssertEqual(CreationFormat.montage.fallbackMaximumClipCount, 10)
    }

    func testAttachedClipCountIgnoresVoiceoverAndVisualMedia() {
        let state: [String: JSONValue] = [
            "media_count": .number(3),
            "media": .array([
                .object(["kind": .string("video")]),
                .object(["kind": .string("audio")]),
                .object(["kind": .string("image")]),
            ]),
        ]

        XCTAssertEqual(attachedVideoClipCount(in: state), 1)
        XCTAssertEqual(attachedVideoClipCount(in: ["media_count": .number(2)]), 2)
    }

    func testAmbiguousTurnRetryReusesIdentityUntilMessageChanges() {
        let first = ChatTurnSubmissionIdentity(
            message: "Keep this concise",
            clientEventID: "turn-1",
            expectedRevision: 7
        )

        XCTAssertEqual(
            ChatTurnSubmissionIdentity.reusing(
                first,
                for: "  Keep   this concise ",
                expectedRevision: 9
            ),
            first
        )
        XCTAssertEqual(
            ChatTurnSubmissionIdentity.reusing(
                first,
                for: "  Keep   this concise ",
                expectedRevision: 9
            ).expectedRevision,
            7
        )
        XCTAssertNotEqual(
            ChatTurnSubmissionIdentity.reusing(
                first,
                for: "Use a warmer opening",
                expectedRevision: 9
            ).clientEventID,
            first.clientEventID
        )
        XCTAssertEqual(
            ChatTurnSubmissionIdentity.reusing(
                first,
                for: "Use a warmer opening",
                expectedRevision: 9
            ).expectedRevision,
            9
        )
    }

    func testDefinitiveConflictDoesNotReuseRetryEnvelope() {
        let first = ChatTurnSubmissionIdentity(
            message: "Keep this concise",
            clientEventID: "turn-1",
            expectedRevision: 7
        )

        let replacement = ChatTurnSubmissionIdentity.reusing(
            nil,
            for: first.message,
            expectedRevision: 9
        )

        XCTAssertNotEqual(replacement.clientEventID, first.clientEventID)
        XCTAssertEqual(replacement.expectedRevision, 9)
    }

    func testFormatChangeRefusesAnOverCapacityTalkingCut() {
        XCTAssertNil(
            formatClipCapacityError(
                format: .talkingToCamera,
                clipLimit: 1,
                occupiedClipCount: 1
            )
        )
        XCTAssertEqual(
            formatClipCapacityError(
                format: .talkingToCamera,
                clipLimit: 1,
                occupiedClipCount: 2
            ),
            "Talking supports 1 clip. Keep your current format or remove extra footage first."
        )
    }

    // MARK: - WorkspaceStage.resolve

    /// A pending plan (from either runtime's confirmation mechanism) always
    /// wins over a stale `.ready` status left by the last job — this is the
    /// A9604B72 bug: a ready cut plus a new proposed plan must show the
    /// confirmation card, not "Your first cut is ready".
    func testReadyWithPendingPlanShowsDirectionNotReady() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .ready, awaitsNewPlanConfirmation: true, isChoosingFormat: false, hasFormat: true),
            .direction
        )
    }

    func testReadyWithoutPendingPlanStaysReady() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .ready, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: true),
            .ready
        )
    }

    /// Documented decision: an in-flight render always wins, even over a plan
    /// proposed while it was still rendering — the old job's progress must
    /// stay visible rather than being buried by a new confirmation card.
    func testRenderingWithPendingPlanStaysRendering() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .rendering, awaitsNewPlanConfirmation: true, isChoosingFormat: false, hasFormat: true),
            .rendering
        )
    }

    func testPreparationWinsOverDraftAndReadyStatuses() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .draft, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: true, preparationIsActive: true),
            .rendering
        )
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .ready, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: true, preparationIsActive: true),
            .rendering
        )
    }

    func testFailedPreparationWinsOverReadyStatus() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .ready, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: true, preparationFailed: true),
            .failed
        )
    }

    func testFailedWithPendingPlanShowsDirectionNotFailed() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .failed, awaitsNewPlanConfirmation: true, isChoosingFormat: false, hasFormat: true),
            .direction
        )
    }

    func testFailedWithoutPendingPlanStaysFailed() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .failed, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: true),
            .failed
        )
    }

    func testDraftCasesAreUnchangedByPendingPlanPrecedence() {
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .draft, awaitsNewPlanConfirmation: true, isChoosingFormat: false, hasFormat: false),
            .direction
        )
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .draft, awaitsNewPlanConfirmation: false, isChoosingFormat: true, hasFormat: true),
            .format
        )
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .draft, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: true),
            .footage
        )
        XCTAssertEqual(
            WorkspaceStage.resolve(status: .draft, awaitsNewPlanConfirmation: false, isChoosingFormat: false, hasFormat: false),
            .format
        )
    }

    // MARK: - workspaceStatusLabel

    func testWorkspaceStatusLabelPrefersPendingPlanOverStaleReadyOrFailed() {
        var summary = ProjectSummary(id: UUID(), title: "Test", status: .ready, updatedAt: .now, posterURL: nil, awaitsConfirmation: true)
        XCTAssertEqual(summary.workspaceStatusLabel, "Shaping direction")

        summary.status = .failed
        XCTAssertEqual(summary.workspaceStatusLabel, "Shaping direction")

        summary.awaitsConfirmation = false
        XCTAssertEqual(summary.workspaceStatusLabel, "Needs attention")

        summary.status = .rendering
        summary.awaitsConfirmation = true
        XCTAssertEqual(summary.workspaceStatusLabel, "Rendering")
    }

    private func event(id: String, sequence: Int, content: String? = nil) -> ThreadEvent {
        ThreadEvent(
            id: id,
            sequence: sequence,
            revision: sequence,
            role: "assistant",
            eventType: "assistant_response",
            content: content ?? id,
            payload: nil,
            createdAt: .now
        )
    }
}
