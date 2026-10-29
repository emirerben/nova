import XCTest
@testable import Kria

/// Legacy ideas have no questions and must still decode, gate nothing and re-encode
/// without the key; a pending question blocks until it is answered or skipped.
final class CameraRollOpenQuestionsTests: XCTestCase {
    private func plan(questions: [[String: Any]]?) throws -> CameraRollContentPlan {
        var json: [String: Any] = [
            "version": 1, "format": "text_story", "audience": "Walkers", "angle": "A",
            "why_it_works": "W", "opening_text": "O", "closing_text": NSNull(), "caption": "C",
            "beat_reasons": ["B"],
        ]
        if let questions { json["open_questions"] = questions }
        return try JSONDecoder().decode(CameraRollContentPlan.self, from: JSONSerialization.data(withJSONObject: json))
    }

    private let pending: [String: Any] = ["id": "q1", "question": "Which port?", "answer": NSNull(), "dismissed": false]

    func testLegacyPlanHasNoQuestionsAndEncodesWithoutThem() throws {
        let legacy = try plan(questions: nil)
        XCTAssertTrue(legacy.unresolvedQuestions.isEmpty)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(legacy), as: UTF8.self).contains("open_questions"))
    }

    func testPendingQuestionBlocksUntilAnsweredOrSkipped() throws {
        let blocked = try plan(questions: [pending])
        XCTAssertEqual(blocked.unresolvedQuestions.map(\.id), ["q1"])
        let answered = blocked.resolving("q1", answer: "  Hamburg ")
        XCTAssertTrue(answered.unresolvedQuestions.isEmpty)
        XCTAssertEqual(answered.openQuestions?.first?.answer, "Hamburg")
        let skipped = blocked.resolving("q1", answer: nil)
        XCTAssertTrue(skipped.unresolvedQuestions.isEmpty)
        XCTAssertEqual(skipped.openQuestions?.first?.dismissed, true)
        XCTAssertNil(skipped.openQuestions?.first?.answer)
    }
}
