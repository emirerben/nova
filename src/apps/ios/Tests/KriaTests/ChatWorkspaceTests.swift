import XCTest
@testable import Kria

final class ChatWorkspaceTests: XCTestCase {
    func testPendingUploadDoesNotCountAsAttachedFootage() {
        let uploading = FootageReadiness(attachedCount: 0, pendingCount: 1)
        XCTAssertEqual(uploading.attachedCount, 0)
        XCTAssertEqual(uploading.pendingCount, 1)
        XCTAssertFalse(uploading.canContinue)

        let attached = FootageReadiness(attachedCount: 1, pendingCount: 1)
        XCTAssertTrue(attached.canContinue)
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
