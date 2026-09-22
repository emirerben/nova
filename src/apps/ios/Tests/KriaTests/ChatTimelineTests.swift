import XCTest
@testable import Kria

final class ChatTimelineTests: XCTestCase {
    func testServerMediaStageAndPendingRowsUseStableOrdering() {
        let pendingID = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!
        let entries = ChatTimeline.build(
            events: [
                event(id: "user", sequence: 1, role: "user", type: "user_message", content: "Make it warm"),
                event(id: "media", sequence: 2, role: "system", type: "media_added", content: nil),
                event(id: "assistant", sequence: 3, role: "assistant", type: "assistant_response", content: "I can do that"),
            ],
            pending: [ChatPendingMessage(content: "Make it warm", clientEventID: "client-1", afterSequence: 1, id: pendingID)],
            stage: ChatTimelineStageAnchor(id: "stage", afterSequence: 2)
        )

        XCTAssertEqual(entries.map(\.id), ["user", "pending-00000000-0000-0000-0000-000000000001", "media", "stage", "assistant"])
        if case .media(let media) = entries[2].content {
            XCTAssertEqual(media.id, "media")
        } else {
            XCTFail("Expected media row")
        }
        if case .stage = entries[3].content {} else { XCTFail("Expected stage row") }
    }

    func testPendingUserStaysBeforeLaterAssistantResponse() {
        let pending = ChatPendingMessage(
            content: "Use the sunset clip",
            clientEventID: "client-1",
            afterSequence: 4,
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000002")!
        )
        let entries = ChatTimeline.build(
            events: [event(id: "assistant", sequence: 5, role: "assistant", type: "assistant_response", content: "I will use it")],
            pending: [pending],
            stage: nil
        )

        XCTAssertEqual(entries.map(\.id), [pending.transcriptMessage.id, "assistant"])
    }

    func testTimelineDeduplicatesByServerEventID() {
        let entries = ChatTimeline.build(
            events: [
                event(id: "same", sequence: 2, role: "assistant", type: "assistant_response", content: "First"),
                event(id: "same", sequence: 4, role: "assistant", type: "assistant_response", content: "Latest"),
            ],
            pending: [],
            stage: nil
        )

        XCTAssertEqual(entries.count, 1)
        if case .message(let message) = entries[0].content {
            XCTAssertEqual(message.id, "same")
            XCTAssertEqual(message.content, "Latest")
        } else {
            XCTFail("Expected message row")
        }
    }

    func testDurableMediaEventReplacesMatchingPendingUploadByAttachClientID() {
        let uploadID = UUID(uuidString: "00000000-0000-0000-0000-000000000021")!
        let entries = ChatTimeline.build(
            events: [event(
                id: "media-server",
                sequence: 5,
                role: "user",
                type: "media_added",
                content: nil,
                clientEventID: "ios-attach-\(uploadID.uuidString)"
            )],
            pending: [],
            stage: nil,
            uploads: [ChatPendingUpload(id: uploadID, afterSequence: 2, localOrder: 0)]
        )

        XCTAssertEqual(entries.map(\.id), ["upload-\(uploadID.uuidString)"])
        if case .media(let media) = entries[0].content {
            XCTAssertEqual(media.id, "media-server")
            XCTAssertEqual(media.clientEventID, "ios-attach-\(uploadID.uuidString)")
        } else {
            XCTFail("Expected the durable media row")
        }
    }

    func testPendingUploadAnchorStaysBeforeLaterAssistantResponse() {
        let uploadID = UUID(uuidString: "00000000-0000-0000-0000-000000000022")!
        let entries = ChatTimeline.build(
            events: [event(id: "assistant", sequence: 3, role: "assistant", type: "assistant_response", content: "I can use it")],
            pending: [],
            stage: nil,
            uploads: [ChatPendingUpload(id: uploadID, afterSequence: 2, localOrder: 0)]
        )

        XCTAssertEqual(entries.map(\.id), ["upload-\(uploadID.uuidString)", "assistant"])
        if case .pendingUpload(let recordID) = entries[0].content {
            XCTAssertEqual(recordID, uploadID)
        } else {
            XCTFail("Expected the pending upload row")
        }
    }

    func testOnlyContiguousMediaReceiptsShareAGroupAndKeepServerIdentities() {
        let firstID = UUID(uuidString: "00000000-0000-0000-0000-000000000031")!
        let secondID = UUID(uuidString: "00000000-0000-0000-0000-000000000032")!
        let thirdID = UUID(uuidString: "00000000-0000-0000-0000-000000000033")!
        let entries = ChatTimeline.build(
            events: [
                event(id: "media-1", sequence: 1, role: "user", type: "media_added", content: nil, clientEventID: "ios-attach-\(firstID.uuidString)"),
                event(id: "media-2", sequence: 2, role: "user", type: "media_added", content: nil, clientEventID: "ios-attach-\(secondID.uuidString)"),
                event(id: "assistant", sequence: 3, role: "assistant", type: "assistant_response", content: "Looks good"),
                event(id: "media-3", sequence: 4, role: "user", type: "media_added", content: nil, clientEventID: "ios-attach-\(thirdID.uuidString)"),
            ],
            pending: [],
            stage: nil
        )

        let groups = ChatTimelineGroup.group(entries)
        XCTAssertEqual(groups.map(\.id), ["upload-\(firstID.uuidString)", "assistant", "upload-\(thirdID.uuidString)"])
        XCTAssertEqual(groups.map(\.entries.count), [2, 1, 1])
        XCTAssertEqual(groups[0].entries.map(\.id), ["upload-\(firstID.uuidString)", "upload-\(secondID.uuidString)"])
    }

    func testSameTextWithDistinctClientIDsRemainsTwoServerMessages() {
        let entries = ChatTimeline.build(
            events: [
                event(id: "server-1", sequence: 1, role: "user", type: "user_message", content: "Keep it short", clientEventID: "client-1"),
                event(id: "server-2", sequence: 2, role: "user", type: "user_message", content: "Keep it short", clientEventID: "client-2"),
            ],
            pending: [],
            stage: nil
        )

        XCTAssertEqual(entries.map(\.id), ["server-1", "server-2"])
    }

    func testLegacyReconciliationRequiresEventAfterAnchorAndMatchesOneToOne() {
        let first = ChatPendingMessage(content: "  Keep   it short ", clientEventID: "client-1", afterSequence: 4, localOrder: 0, id: UUID(uuidString: "00000000-0000-0000-0000-000000000011")!)
        let second = ChatPendingMessage(content: "Keep it short", clientEventID: "client-2", afterSequence: 4, localOrder: 1, id: UUID(uuidString: "00000000-0000-0000-0000-000000000012")!)
        let acknowledged = ChatPendingReconciliation.acknowledged(
            [first, second],
            events: [
                event(id: "before-anchor", sequence: 3, role: "user", type: "user_message", content: "Keep it short"),
                event(id: "after-anchor", sequence: 5, role: "user", type: "user_message", content: "Keep it short"),
            ]
        )

        XCTAssertEqual(acknowledged, [first.id])
    }

    func testExactClientEventIDsReconcileEvenWhenTextDiffers() {
        let pending = ChatPendingMessage(content: "New wording", clientEventID: "retry-2", afterSequence: 8, id: UUID(uuidString: "00000000-0000-0000-0000-000000000013")!)
        let acknowledged = ChatPendingReconciliation.acknowledged(
            [pending],
            events: [event(id: "server", sequence: 9, role: "user", type: "user_message", content: "Old wording", clientEventID: "retry-2")]
        )

        XCTAssertEqual(acknowledged, [pending.id])
    }

    func testThreadEventClientEventIDCodableSupportsMissingAndPresentValues() throws {
        let present = event(id: "present", sequence: 1, role: "user", type: "user_message", content: "Hello", clientEventID: "client-1")
        let encoded = try JSONEncoder().encode(present)
        let decodedPresent = try JSONDecoder().decode(ThreadEvent.self, from: encoded)
        XCTAssertEqual(decodedPresent.clientEventID, "client-1")

        var object = try XCTUnwrap(try JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        object.removeValue(forKey: "client_event_id")
        let missing = try JSONSerialization.data(withJSONObject: object)
        let decodedMissing = try JSONDecoder().decode(ThreadEvent.self, from: missing)
        XCTAssertNil(decodedMissing.clientEventID)
    }

    func testChatSubmissionUsesMediaFallbackOnlyWhenReady() {
        XCTAssertEqual(ChatSubmission.message(text: "  ", readyMediaCount: 2, pendingUploadCount: 0, hasUploadFailures: false), ChatSubmission.mediaOnlyMessage)
        XCTAssertNil(ChatSubmission.message(text: "  ", readyMediaCount: 0, pendingUploadCount: 0, hasUploadFailures: false))
        XCTAssertNil(ChatSubmission.message(text: "Make it cinematic", readyMediaCount: 2, pendingUploadCount: 1, hasUploadFailures: false))
        XCTAssertNil(ChatSubmission.message(text: "Make it cinematic", readyMediaCount: 2, pendingUploadCount: 0, hasUploadFailures: true))
        XCTAssertEqual(ChatSubmission.message(text: "  Make it cinematic \n", readyMediaCount: 0, pendingUploadCount: 0, hasUploadFailures: false), "Make it cinematic")
    }

    private func event(
        id: String,
        sequence: Int,
        role: String,
        type: String,
        content: String?,
        clientEventID: String? = nil
    ) -> ThreadEvent {
        ThreadEvent(
            id: id,
            sequence: sequence,
            revision: sequence,
            role: role,
            eventType: type,
            content: content,
            payload: nil,
            createdAt: Date(timeIntervalSince1970: TimeInterval(sequence)),
            clientEventID: clientEventID
        )
    }
}
