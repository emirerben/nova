import XCTest
@testable import Kria

/// Steer now: which message is "Up next", and when the app offers "Steer now" for it. Both are read from the thread's
/// events (the projection carries no turn status). Written before the policy, from docs/pipelines/steer-now.md:
///
/// - F17a: a waiting message keeps its Up next look after its own turn started (it answered, or a Steer promoted it).
/// - F17b: Steer now shows while the video is being made, after the turn ended, or for an old server that does not
///   say which turn the message waits behind.
/// - F17c: the button vanishes while the earlier turn's card is still up, or while it is still planning.
/// - F17d: a message that was never queued (sent when Kria was idle) is shown as Up next.
final class SteerNowPolicyTests: XCTestCase {
    private var sequence = 0

    private func event(_ type: String, role: String = "system", turn: String? = nil, content: String? = nil,
                       extra: [String: JSONValue] = [:]) -> ThreadEvent {
        sequence += 1
        var payload = extra
        if let turn { payload["turn_id"] = .string(turn) }
        return ThreadEvent(id: "e\(sequence)", sequence: sequence, revision: sequence, role: role, eventType: type,
                           content: content, payload: payload, createdAt: .now)
    }

    private func sent(_ turn: String, status: String, behind: String? = nil) -> ThreadEvent {
        var extra: [String: JSONValue] = ["turn_status": .string(status)]
        if let behind { extra["queued_behind_turn_id"] = .string(behind) }
        return event("user_message", role: "user", turn: turn, content: "message \(turn)", extra: extra)
    }

    private func card(_ turn: String) -> [ThreadEvent] {
        [event("assistant_response", role: "assistant", turn: turn, content: "Your draft is ready for review."),
         event("draft_applied", role: "assistant", turn: turn, content: "Draft"),
         event("approval_requested", turn: turn, extra: ["approval_id": .string("approval-\(turn)")])]
    }

    // MARK: - Which message is up next

    func testAQueuedMessageIsUpNextBehindTheRunningTurn() {
        let events = [sent("A", status: "pending"), sent("B", status: "queued", behind: "A")]
        let waiting = SteerNowPolicy.waiting(in: events)
        XCTAssertEqual(waiting?.turnID, "B")
        XCTAssertEqual(waiting?.behindTurnID, "A")
        XCTAssertEqual(waiting?.messageEventID, events[1].id)
    }

    func testAMessageSentWhileKriaWasIdleIsNeverUpNext() {
        XCTAssertNil(SteerNowPolicy.waiting(in: [sent("A", status: "pending")]))
        XCTAssertNil(SteerNowPolicy.waiting(in: [sent("A", status: "completed")]))
    }

    func testUpNextEndsOnceItsOwnTurnSaysAnything() {
        let events = [sent("A", status: "pending"), sent("B", status: "queued", behind: "A")] + card("B")
        XCTAssertNil(SteerNowPolicy.waiting(in: events))
    }

    func testUpNextEndsWhenASteerHandedOverToIt() {
        let events = [sent("A", status: "pending"), sent("B", status: "queued", behind: "A"),
                      event("turn_cancelled", turn: "A", extra: ["steered_to_turn_id": .string("B")])]
        XCTAssertNil(SteerNowPolicy.waiting(in: events))
    }

    func testTurnIDsMatchWhateverTheirCase() {
        let events = [sent("a-turn", status: "pending"), sent("b-turn", status: "queued", behind: "A-TURN"),
                      event("turn_cancelled", turn: "A-TURN", extra: ["steered_to_turn_id": .string("B-TURN")])]
        XCTAssertNil(SteerNowPolicy.waiting(in: events))
    }

    // MARK: - When Steer now is offered

    private func canSteer(_ events: [ThreadEvent]) -> Bool {
        guard let waiting = SteerNowPolicy.waiting(in: events) else { return false }
        return SteerNowPolicy.canSteer(waiting, events: events)
    }

    func testSteerNowWhileTheEarlierTurnPlans() {
        XCTAssertTrue(canSteer([sent("A", status: "pending"), sent("B", status: "queued", behind: "A")]))
    }

    func testSteerNowWhileTheEarlierCardIsUp() {
        XCTAssertTrue(canSteer([sent("A", status: "pending")] + card("A") + [sent("B", status: "queued", behind: "A")]))
    }

    func testNoSteerNowOnceTheVideoIsApprovedOrBeingMade() {
        for started in ["approval_approved", "render_queued", "render_started", "plan_block", "generation_started"] {
            sequence = 0
            let events = [sent("A", status: "pending")] + card("A")
                + [sent("B", status: "queued", behind: "A"), event(started, turn: "A")]
            XCTAssertFalse(canSteer(events), started)
        }
    }

    func testNoSteerNowAfterTheEarlierTurnEnded() {
        for ended in ["turn_cancelled", "approval_denied", "approval_expired", "assistant_error"] {
            sequence = 0
            let events = [sent("A", status: "pending")] + card("A")
                + [sent("B", status: "queued", behind: "A"), event(ended, turn: "A")]
            XCTAssertFalse(canSteer(events), ended)
        }
    }

    func testNoSteerNowWhenTheEarlierTurnAnsweredWithoutACard() {
        let events = [sent("A", status: "pending"), sent("B", status: "queued", behind: "A"),
                      event("assistant_question", role: "assistant", turn: "A", content: "Which clip opens it?")]
        XCTAssertFalse(canSteer(events))
    }

    func testNoSteerNowWhenTheServerDoesNotSayWhatTheMessageWaitsBehind() {
        XCTAssertNotNil(SteerNowPolicy.waiting(in: [sent("A", status: "pending"), sent("B", status: "queued")]))
        XCTAssertFalse(canSteer([sent("A", status: "pending"), sent("B", status: "queued")]))
    }

    // MARK: - Capability

    func testCapabilityDefaultsOffWhenKeyIsMissing() throws {
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats": []}"#.utf8))
        XCTAssertFalse(off.steerNowEnabled)
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats": [], "steer_now": true}"#.utf8))
        XCTAssertTrue(on.steerNowEnabled)
    }
}
