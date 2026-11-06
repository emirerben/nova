import XCTest
@testable import Kria

final class ReviewApprovalIdentityTests: XCTestCase {
    private let threadID = UUID()
    private let approvalID = UUID()

    private func event(turn: String? = "turn", draft: String = "draft", revision: Int = 7) -> ThreadEvent {
        var payload: [String: JSONValue] = ["approval_id": .string(approvalID.uuidString), "draft_id": .string(draft), "draft_revision": .number(Double(revision))]
        if let turn { payload["turn_id"] = .string(turn) }
        return ThreadEvent(id: "event", sequence: 12, revision: 9, role: "system", eventType: "approval_requested", content: nil, payload: payload, createdAt: .now)
    }

    private func snapshot(turn: String = "turn", draft: String = "draft", revision: Int = 7, approval: String? = nil) -> ApprovalSnapshot {
        ApprovalSnapshot(approvalID: approval ?? approvalID.uuidString, turnID: turn, draftID: draft, draftRevision: revision, status: "pending", consequenceSummary: "Render", costSummary: nil, expiresAt: .distantFuture, approvalFingerprint: "fingerprint")
    }

    func testTurnSelectionRejectsWrongMissingAndEmptyTurn() throws {
        let identity = try XCTUnwrap(ReviewApprovalIdentity(threadID: threadID, turnID: "turn"))
        XCTAssertNotNil(identity.select(event(), threadID: threadID))
        for turn in [nil, "", "other"] as [String?] {
            XCTAssertNil(identity.select(event(turn: turn), threadID: threadID))
        }
        XCTAssertNil(ReviewApprovalIdentity(threadID: threadID, turnID: "  "))
        XCTAssertNil(identity.select(event(), threadID: UUID()))
    }

    func testRestoredDraftSelectionRequiresExactDraftRevisionAndThread() throws {
        let identity = try XCTUnwrap(ReviewApprovalIdentity(threadID: threadID, draftID: "draft", draftRevision: 7))
        XCTAssertNotNil(identity.select(event(), threadID: threadID))
        XCTAssertNil(identity.select(event(draft: "other"), threadID: threadID))
        XCTAssertNil(identity.select(event(revision: 8), threadID: threadID))
        XCTAssertNil(identity.select(event(), threadID: UUID()))
        XCTAssertNil(identity.select(event(turn: nil), threadID: threadID))
    }

    func testFetchedApprovalMustMatchSelectedEventBeforeDecision() throws {
        let identity = try XCTUnwrap(ReviewApprovalIdentity(threadID: threadID, turnID: "turn"))
        let selected = try XCTUnwrap(identity.select(event(), threadID: threadID))
        XCTAssertTrue(selected.matches(snapshot(), threadID: threadID))
        XCTAssertFalse(selected.matches(snapshot(turn: "other"), threadID: threadID))
        XCTAssertFalse(selected.matches(snapshot(turn: ""), threadID: threadID))
        XCTAssertFalse(selected.matches(snapshot(draft: "other"), threadID: threadID))
        XCTAssertFalse(selected.matches(snapshot(revision: 8), threadID: threadID))
        XCTAssertFalse(selected.matches(snapshot(approval: UUID().uuidString), threadID: threadID))
        XCTAssertFalse(selected.matches(snapshot(), threadID: UUID()))
    }
}
