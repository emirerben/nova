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
