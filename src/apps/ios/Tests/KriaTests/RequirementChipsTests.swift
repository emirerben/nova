import XCTest
@testable import Kria

/// KRI-207: the receipt and brief models are decoded by hand and must shrug off anything malformed.
final class RequirementChipsTests: XCTestCase {
    private func receipt(_ fields: [String: JSONValue]) -> JSONValue { .object(fields) }

    func testReceiptParsesLabelsWithClipAndFallsBackToPlainInferred() {
        let withLabels = RequirementReceiptItem(json: receipt([
            "requirement_id": .string("r1"), "status": .string("partial"), "reason": .string("why"),
            "inferred": .array([.string("Ignored")]),
            "inferred_labels": .array([.object(["text": .string("Harbor Point"), "media_id": .string("m"), "clip_index": .number(3)])]),
        ]))
        XCTAssertEqual(withLabels?.outcome, .partial)
        XCTAssertEqual(withLabels?.inferredLabels, [InferredLabel(text: "Harbor Point", mediaID: "m", clipIndex: 3)])
        XCTAssertEqual(withLabels?.inferredLabels.first?.guessSentence, "I guessed Harbor Point for clip 4")

        let plain = RequirementReceiptItem(json: receipt([
            "requirement_id": .string("r2"), "status": .string("met"), "inferred": .array([.string("Old Lighthouse"), .string("")]),
        ]))
        XCTAssertEqual(plain?.inferredLabels, [InferredLabel(text: "Old Lighthouse", mediaID: nil, clipIndex: nil)])
        XCTAssertEqual(plain?.inferredLabels.first?.guessSentence, "I guessed Old Lighthouse")
    }

    func testMalformedReceiptsAreSkippedNotFatal() {
        XCTAssertNil(RequirementReceiptItem(json: .string("nope")))
        XCTAssertNil(RequirementReceiptItem(json: receipt(["status": .string("met")])))
        XCTAssertNil(RequirementReceiptItem(json: receipt(["requirement_id": .string("r"), "status": .string("something_new")])))
        let parsed = RequirementReceiptItem.parse(payload: ["requirement_receipts": .array([
            .null, receipt(["requirement_id": .string("ok"), "status": .string("not_possible")]), .number(3),
        ])])
        XCTAssertEqual(parsed.map(\.requirementID), ["ok"])
        XCTAssertTrue(RequirementReceiptItem.parse(payload: nil).isEmpty)
    }

    func testClipIndexMustBeAWholeNonNegativeNumber() {
        func label(_ index: JSONValue) -> InferredLabel? {
            RequirementReceiptItem(json: receipt([
                "requirement_id": .string("r"), "status": .string("met"),
                "inferred_labels": .array([.object(["text": .string("X"), "clip_index": index])]),
            ]))?.inferredLabels.first
        }
        XCTAssertEqual(label(.number(2))?.clipIndex, 2)
        XCTAssertNil(label(.number(2.5))?.clipIndex)
        XCTAssertNil(label(.number(-1))?.clipIndex)
        XCTAssertNil(label(.number(1e30))?.clipIndex)
        XCTAssertEqual(label(.number(2.5))?.text, "X", "The name survives a bad clip number")
    }

    func testBriefDecodesLeniently() throws {
        let json = """
        {"thread_id": "t", "version": 2,
         "requirements": [
           {"id": "a", "kind": "text", "scope": "per_clip", "description": "Label each clip"},
           {"id": "b"},
           {"kind": "order"},
           7],
         "requirement_receipts": [{"requirement_id": "a", "status": "met"}, {"status": "met"}]}
        """
        let brief = try JSONDecoder().decode(CreativeBrief.self, from: Data(json.utf8))
        XCTAssertEqual(brief.version, 2)
        XCTAssertEqual(brief.requirements.map(\.id), ["a", "b"], "One odd requirement must not cost the rest")
        XCTAssertEqual(brief.requirementsByID["a"]?.chipTitle, "Label each clip")
        XCTAssertNil(brief.requirementsByID["b"]?.chipTitle)
        XCTAssertEqual(brief.receipts.map(\.requirementID), ["a"])

        let empty = try JSONDecoder().decode(CreativeBrief.self, from: Data("{}".utf8))
        XCTAssertTrue(empty.requirements.isEmpty)
    }

    func testCorrectionDraftAppendsAndStubIsNotSendable() {
        let label = InferredLabel(text: "Harbor Point", mediaID: nil, clipIndex: 3)
        XCTAssertEqual(label.correctionDraft(appendingTo: ""), "Clip 4 isn't Harbor Point, it's ")
        XCTAssertEqual(label.correctionDraft(appendingTo: "  \n"), "Clip 4 isn't Harbor Point, it's ")
        XCTAssertEqual(label.correctionDraft(appendingTo: "Keep it short"), "Keep it short\nClip 4 isn't Harbor Point, it's ")
        XCTAssertTrue(ChatSubmission.isBareCorrectionStub("Clip 4 isn't Harbor Point, it's "))
        XCTAssertTrue(ChatSubmission.isBareCorrectionStub("Keep it short\nThat isn't Old Lighthouse, it's "))
        XCTAssertFalse(ChatSubmission.isBareCorrectionStub("Clip 4 isn't Harbor Point, it's Besiktas"))
        XCTAssertFalse(ChatSubmission.isBareCorrectionStub("Make it warmer"))
    }

    func testDuplicateGuessesGetDistinctRows() {
        let one = InferredLabel(text: "Harbor Point", mediaID: "m", clipIndex: 3)
        XCTAssertEqual(one.id, InferredLabel(text: "Harbor Point", mediaID: "m", clipIndex: 3).id)
        XCTAssertNotEqual(one.id, InferredLabel(text: "Harbor Point", mediaID: "m2", clipIndex: 4).id)
    }
}
