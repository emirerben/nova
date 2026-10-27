import XCTest
@testable import Kria

final class RequestOutcomeContextTests: XCTestCase {
    private func event(_ turn: UUID?, question: [String: JSONValue]? = nil) -> ThreadEvent {
        var payload: [String: JSONValue] = [:]
        if let turn { payload["turn_id"] = .string(turn.uuidString) }
        if let question { payload["choice_question"] = .object(question) }
        return ThreadEvent(id: UUID().uuidString, sequence: 1, revision: 1,
                           role: question == nil ? "user" : "assistant",
                           eventType: question == nil ? "user_message" : "assistant_response",
                           content: nil, payload: payload, createdAt: Date())
    }

    func testOriginalRevisionAndStructuredContinuationAreDifferent() {
        let original = RequestOutcomeContext.forSubmission(events: [], continuation: false)
        XCTAssertEqual(original.relation, .original)
        XCTAssertNil(original.parentTurnID)
        let parent = UUID()
        let revision = RequestOutcomeContext.forSubmission(events: [event(parent)], continuation: false)
        XCTAssertEqual(revision.relation, .revision)
        XCTAssertEqual(revision.parentTurnID, parent)
        let answer = RequestOutcomeContext.forSubmission(events: [event(parent)], continuation: true)
        XCTAssertEqual(answer.relation, .continuation)
        XCTAssertEqual(answer.parentTurnID, parent)
    }

    func testOnlyTheOfferedSimplerChoiceIsARecovery() {
        let parent = UUID()
        let offered: [String: JSONValue] = [
            "question_id": .string("q1"), "conflict": .string("simplification_requires_choice")
        ]
        let selection = ChoiceSelectionSubmission(questionID: "q1", optionKey: "simpler_version")
        let events = [event(parent), event(nil, question: offered)]
        XCTAssertEqual(RequestOutcomeContext.forSubmission(events: events, continuation: true,
                                                          choice: selection).relation, .recovery)
        let stale = ChoiceSelectionSubmission(questionID: "old", optionKey: "simpler_version")
        XCTAssertEqual(RequestOutcomeContext.forSubmission(events: events, continuation: true,
                                                          choice: stale).relation, .continuation)
    }

    func testLegacyMessagesNeverAcquireAKnownOriginal() {
        let context = RequestOutcomeContext.forSubmission(events: [event(nil)], continuation: false)
        XCTAssertEqual(context.relation, .unclassified)
        XCTAssertNil(context.parentTurnID)
    }

    func testBodyOmitsMetadataForOlderServersAndEncodesOpaqueIdentifiersOnly() throws {
        var body = SubmitTurnRequest(message: "Private creator message", clientEventID: "event",
                                     expectedThreadRevision: 2)
        func json() throws -> [String: Any] {
            try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any])
        }
        XCTAssertNil(try json()["outcome_context"])
        body.outcomeContext = RequestOutcomeContext(relation: .revision, parentTurnID: UUID(),
                                                   clientBuild: "1763", clientVersion: "0.1.0")
        let metadata = try XCTUnwrap(try json()["outcome_context"] as? [String: String])
        XCTAssertEqual(Set(metadata.keys), Set(["relation", "parent_turn_id", "client_build", "client_version"]))
        XCTAssertEqual(metadata["relation"], "revision")
        XCTAssertEqual(metadata["client_build"], "1763")
    }
}
