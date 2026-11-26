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
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(version: 3)), "newer version degrades to the text question")
        XCTAssertNotNil(ChoiceQuestion.parse(payload: payload(version: 2)), "version 2 is this build's own")
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(questionID: "")))
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(options: [])))
        XCTAssertNil(ChoiceQuestion.parse(payload: payload(options: [option("only", "One option")])), "one option is not a choice")
    }

    /// KRI-1560: a title Kria built from the creator's words carries `editable`; the card draws a pencil only then.
    func testEditableIsReadAndOffByDefault() throws {
        var editable = option("title_1", "Black doesn't suit everyone", detail: "0:12 · “Everyone says black goes with everything.”")
        if case .object(var fields) = editable { fields["editable"] = .bool(true); editable = .object(fields) }
        let q = try XCTUnwrap(ChoiceQuestion.parse(payload: payload(options: [editable, option("cancel", "No title")])))
        XCTAssertEqual(q.options.map(\.editable), [true, false])
        XCTAssertEqual(q.options[0].detail, "0:12 · “Everyone says black goes with everything.”")
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

    // MARK: style strip (CA-4)

    /// Failure modes pinned here: three tiles clip on a small iPhone; five tiles shrink instead of scrolling; the
    /// primary does not name the selected style; the unchosen tiles stay at full strength while the send is out; a
    /// second tap while sending changes the answer; Try again resends more than once or a different style; the
    /// receipt cannot find the style the creator chose, or draws a preview for a typed answer; a style without a
    /// preview is drawn as a tile.
    private func styled(_ labels: [String], recommended: Int? = nil, previewed: Bool = true) -> ChoiceQuestion {
        let preview = ChoiceQuestion.Preview(mediaID: "m", isPhoto: false, atSeconds: 0, timeSeconds: 0, layers: [], fonts: [:])
        let options = labels.enumerated().map { index, label in
            ChoiceQuestion.Option(key: "k\(index)", label: label, detail: nil, recommended: index == recommended,
                                  preview: previewed || index > 0 ? preview : nil)
        }
        return ChoiceQuestion(version: 2, questionID: "q", options: options, allowFreeText: true)
    }

    func testThreeTilesFitAtEveryCardWidthAndFiveScroll() {
        for stripWidth in stride(from: 300.0, through: 420.0, by: 5.0) {
            let tile = DesignStripLayout.tileWidth(stripWidth: stripWidth, count: 3)
            XCTAssertLessThanOrEqual(3 * tile + 2 * DesignStripLayout.gap, stripWidth + 0.001, "three tiles clip at \(stripWidth)")
            XCTAssertLessThanOrEqual(tile, DesignStripLayout.tileOuterWidth)
        }
        XCTAssertEqual(DesignStripLayout.tileWidth(stripWidth: 340, count: 3), 108, "at a normal width the tile keeps its size")
        for count in [4, 5, 6] {
            XCTAssertEqual(DesignStripLayout.tileWidth(stripWidth: 340, count: count), 108)
            XCTAssertGreaterThan(CGFloat(count) * 108 + CGFloat(count - 1) * DesignStripLayout.gap, 340, "\(count) tiles scroll")
        }
        XCTAssertEqual(DesignStripLayout.tileWidth(stripWidth: 0, count: 3), 108, "before the strip is measured")
    }

    func testPrimaryNamesTheSelectedStyleStartingFromTheRecommendedOne() {
        let q = styled(["Editorial", "Clean", "Bold"], recommended: 1)
        var pick = DesignPick(q)
        XCTAssertEqual(pick.selected, "k1")
        XCTAssertEqual(pick.primaryTitle(in: q), "Use Clean")
        pick.select("k2")
        XCTAssertEqual(pick.primaryTitle(in: q), "Use Bold")
        XCTAssertEqual(DesignPick(styled(["Editorial", "Clean"])).selected, "k0", "nothing recommended: the first style")
    }

    func testSubmittingDimsTheOthersAndLocksTheSelection() {
        let q = styled(["Editorial", "Clean", "Bold"], recommended: 1)
        var pick = DesignPick(q)
        XCTAssertEqual(pick.send(), "k1")
        XCTAssertEqual(pick.primaryTitle(in: q), "Applying Clean")
        XCTAssertEqual(pick.opacity(of: "k0"), 0.4)
        XCTAssertEqual(pick.opacity(of: "k2"), 0.4)
        XCTAssertEqual(pick.opacity(of: "k1"), 1)
        pick.select("k2")
        XCTAssertEqual(pick.selected, "k1", "a tap while the send is out changes nothing")
        XCTAssertEqual(DesignPick(q).opacity(of: "k0"), 1, "nothing is dimmed before a send")
    }

    func testFailedSendOffersTheSameStyleOnceAndAnotherTapClearsIt() {
        let q = styled(["Editorial", "Clean", "Bold"], recommended: 1)
        var pick = DesignPick(q)
        _ = pick.send()
        pick.sendEnded()
        XCTAssertNil(pick.sent)
        XCTAssertEqual(pick.failed, "k1")
        XCTAssertEqual(pick.opacity(of: "k0"), 1, "tiles are live again")
        XCTAssertEqual(pick.retry(), "k1", "Try again sends the same style")
        XCTAssertNil(pick.retry(), "and only once per tap")
        XCTAssertEqual(pick.sent, "k1")
        pick.sendEnded()
        pick.select("k2")
        XCTAssertNil(pick.failed, "choosing another style drops the failure")
        XCTAssertEqual(pick.send(), "k2")
        var idle = DesignPick(q)
        idle.sendEnded()
        XCTAssertNil(idle.failed, "a send this card never made is not its failure")
    }

    func testReceiptFindsThePreviewOfTheChosenStyleOnly() {
        let q = styled(["Editorial", "Clean", "Bold"], recommended: 1)
        XCTAssertEqual(q.previewedOption(answered: "Clean")?.key, "k1")
        XCTAssertNil(q.previewedOption(answered: "Let Kria decide"))
        XCTAssertNil(q.previewedOption(answered: "something I typed"))
        XCTAssertNil(q.previewedOption(answered: ""))
        XCTAssertNil(styled(["Editorial", "Clean"], previewed: false).previewedOption(answered: "Editorial"), "no preview, no tile")
    }

    func testAnyOptionWithoutAPreviewKeepsTheWholeQuestionAsRows() {
        XCTAssertTrue(styled(["Editorial", "Clean", "Bold"]).hasPreviews)
        XCTAssertFalse(styled(["Editorial", "Clean", "Bold"], previewed: false).hasPreviews)
    }
}
