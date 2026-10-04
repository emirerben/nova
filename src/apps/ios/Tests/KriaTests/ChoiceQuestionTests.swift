import XCTest
@testable import Kria

/// KRI-282: the conflict-choice contract is hand-decoded/encoded (the question rides in the free-form event payload).
final class ChoiceQuestionTests: XCTestCase {
    private func option(_ key: String, _ label: String, recommended: Bool = false, detail: String? = nil) -> JSONValue {
        var fields: [String: JSONValue] = ["key": .string(key), "label": .string(label), "recommended": .bool(recommended)]
        if let detail { fields["description"] = .string(detail) }
        return .object(fields)
    }

    private func payload(version: Double = 1, options: [JSONValue]? = nil, questionID: String = "q1") -> [String: JSONValue] {
        ["choice_question": .object([
            "version": .number(version), "question_id": .string(questionID), "allow_free_text": .bool(true),
            "options": .array(options ?? [
                option("group_first", "Group by sport, chronological inside each sport", recommended: true, detail: "One block per sport."),
                option("chronological", "Keep it strictly chronological; sports may interleave"),
            ]),
        ])]
    }

    private func question() throws -> ChoiceQuestion {
        try XCTUnwrap(ChoiceQuestion.parse(payload: payload()))
    }

    // MARK: parsing

    func testParsesOptionsRecommendationAndDetail() throws {
        let q = try question()
        XCTAssertEqual(q.questionID, "q1")
        XCTAssertEqual(q.options.map(\.key), ["group_first", "chronological"])
        XCTAssertEqual(q.options.map(\.recommended), [true, false])
        XCTAssertEqual(q.options[0].detail, "One block per sport.")
        XCTAssertNil(q.options[1].detail)
        XCTAssertTrue(q.allowFreeText)
    }

    func testMalformedOrUnsupportedPayloadsYieldNoQuestion() {
        XCTAssertNil(ChoiceQuestion.parse(payload: nil))
        XCTAssertNil(ChoiceQuestion.parse(payload: [:]))
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(version: 2)), "newer version degrades to the text question")
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(questionID: "")))
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(options: [])))
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(options: [option("only", "One option")])), "one option is not a choice")
    }

    func testDuplicateKeysAndBlankLabelsAreDropped() throws {
        let q = try XCTUnwrap(ChoiceQuestion.parse(payload: payload(options: [
            option("a", "First"), option("a", "Again"), option("b", "   "), option("c", "Third"),
        ])))
        XCTAssertEqual(q.options.map(\.key), ["a", "c"])
    }

    func testTranscriptMessageCarriesChoiceQuestionOnAssistantEventsOnly() throws {
        func event(role: String, type: String) -> ThreadEvent {
            ThreadEvent(id: type, sequence: 1, revision: 1, role: role, eventType: type, content: "Which do you prefer?", payload: payload(), createdAt: .now)
        }
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_response"))?.choiceQuestion?.questionID, "q1")
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "user", type: "user_message"))?.choiceQuestion)
        let plain = ThreadEvent(id: "p", sequence: 1, revision: 1, role: "assistant", eventType: "assistant_response", content: "Hi", payload: nil, createdAt: .now)
        XCTAssertNil(ChatTranscriptMessage.from(event: plain)?.choiceQuestion)
    }

    // MARK: submission + message

    func testSubmissionEncodesTheContractShape() throws {
        let q = try question()
        let submission = q.submission(for: q.options[1])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(submission)) as? [String: Any])
        XCTAssertEqual(json["question_id"] as? String, "q1")
        XCTAssertEqual(json["option_key"] as? String, "chronological")
        XCTAssertEqual(Set(json.keys), ["question_id", "option_key"], "strict body: no extra fields")
    }

    func testMessageIsTheChosenOptionLabel() throws {
        let q = try question()
        XCTAssertEqual(q.message(for: q.options[0]), "Group by sport, chronological inside each sport")
        XCTAssertEqual(q.message(for: q.options[1]), "Keep it strictly chronological; sports may interleave")
        XCTAssertEqual(q.option(key: "chronological")?.label, q.options[1].label)
        XCTAssertNil(q.option(key: "nope"))
    }

    // MARK: capability gating

    func testCapabilityFlagDecodesAndDefaultsOff() throws {
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(off.choiceQuestionsEnabled, "an old server never advertises it: text question only")
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"choice_questions":true}"#.utf8))
        XCTAssertTrue(on.choiceQuestionsEnabled)
        let explicitOff = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"choice_questions":false}"#.utf8))
        XCTAssertFalse(explicitOff.choiceQuestionsEnabled)
    }
}
