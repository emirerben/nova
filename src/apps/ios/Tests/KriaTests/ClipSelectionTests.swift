import XCTest
@testable import Kria

/// KRI-282: the clip-picker contract is hand-decoded/encoded until the generated client gains it.
final class ClipSelectionTests: XCTestCase {
    private func payload(version: Double = 1, categories: [JSONValue]? = nil, allowNone: Bool = true) -> [String: JSONValue] {
        ["clip_question": .object([
            "version": .number(version), "question_id": .string("q1"), "allow_none": .bool(allowNone),
            "categories": .array(categories ?? [
                category("dodgeball", "Dodgeball", ["a", "b", "c"], suggested: ["b"]),
                category("football", "Football", ["c", "d"]),
            ]),
        ])]
    }

    private func category(_ key: String, _ label: String, _ candidates: [String], suggested: [String] = []) -> JSONValue {
        .object(["key": .string(key), "label": .string(label), "op": .string("group"),
                 "candidate_media_ids": .array(candidates.map(JSONValue.string)),
                 "suggested_media_ids": .array(suggested.map(JSONValue.string))])
    }

    private func question(allowNone: Bool = true) throws -> ClipQuestion {
        try XCTUnwrap(ClipQuestion.parse(payload: payload(allowNone: allowNone)))
    }

    // MARK: parsing

    func testParsesCategoriesSuggestionsAndAllowNone() throws {
        let q = try question()
        XCTAssertEqual(q.questionID, "q1")
        XCTAssertEqual(q.categories.map(\.key), ["dodgeball", "football"])
        XCTAssertEqual(q.categories[0].label, "Dodgeball")
        XCTAssertEqual(q.categories[0].candidateMediaIDs, ["a", "b", "c"])
        XCTAssertEqual(q.categories[0].suggestedMediaIDs, ["b"])
        XCTAssertTrue(q.allowNone)
        XCTAssertFalse(try question(allowNone: false).allowNone)
    }

    func testLowercaseCreatorWordingIsCapitalisedForDisplay() throws {
        let q = try XCTUnwrap(ClipQuestion.parse(payload: payload(categories: [
            category("group:dodgeball", "dodgeball", ["a"]),
            category("group:b", "bookshop photo of two guys with a book", ["b"]),
        ])))
        XCTAssertEqual(q.categories.map(\.label), ["Dodgeball", "Bookshop photo of two guys with a book"])
        XCTAssertEqual(q.categories[0].key, "group:dodgeball")
    }

    /// KRI-520: Turkish wording keeps Turkish capitals; English wording is unchanged.
    func testTurkishWordingKeepsTurkishCapitals() {
        XCTAssertEqual(ClipQuestion.displayLabel("ilk gün", preferredLanguages: ["en-US"]), "İlk gün")
        XCTAssertEqual(ClipQuestion.displayLabel("iftar", preferredLanguages: ["tr-TR", "en-US"]), "İftar")
        XCTAssertEqual(ClipQuestion.displayLabel("ıhlamur", preferredLanguages: ["en-US"]), "Ihlamur")
        XCTAssertEqual(ClipQuestion.displayLabel("iced latte", preferredLanguages: ["en-US"]), "Iced latte")
    }

    func testMalformedOrUnsupportedPayloadsYieldNoQuestion() {
        XCTAssertNil(ClipQuestion.parse(payload: nil))
        XCTAssertNil(ClipQuestion.parse(payload: [:]))
        XCTAssertNil(ClipQuestion.parse(payload: payload(version: 2)), "newer version degrades to the text question")
        XCTAssertNil(ClipQuestion.parse(payload: payload(categories: [])))
        XCTAssertNil(ClipQuestion.parse(payload: payload(categories: [category("x", "X", [])])), "no candidates")
        XCTAssertNil(ClipQuestion.parse(payload: ["clip_question": .object(["version": .number(1), "categories": .array([category("x", "X", ["a"])])])]), "no question id")
    }

    func testSuggestionsOutsideCandidatesAndDuplicateKeysAreDropped() throws {
        let q = try XCTUnwrap(ClipQuestion.parse(payload: payload(categories: [
            category("x", "X", ["a", "a", "b"], suggested: ["zzz", "b"]),
            category("x", "Dup", ["c"]),
        ])))
        XCTAssertEqual(q.categories.count, 1)
        XCTAssertEqual(q.categories[0].candidateMediaIDs, ["a", "b"])
        XCTAssertEqual(q.categories[0].suggestedMediaIDs, ["b"])
    }

    func testTranscriptMessageCarriesClipQuestionOnAssistantResponse() throws {
        func event(role: String, type: String) -> ThreadEvent {
            ThreadEvent(id: type, sequence: 1, revision: 1, role: role, eventType: type, content: "Which clips are dodgeball?", payload: payload(), createdAt: .now)
        }
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_response"))?.clipQuestion?.questionID, "q1")
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_question"))?.clipQuestion?.questionID, "q1")
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "user", type: "user_message"))?.clipQuestion)
    }

    /// KRI-282 root-cause pin. This is the wire shape of the production Olympics thread's question event (ids,
    /// label and prose are synthetic): decoded through the real `ThreadEvent` decoder, the newest event must yield
    /// a ready-to-render clip question -- with NO suggestions and many candidates.
    func testProductionShapedQuestionEventDecodesToAPickerQuestion() throws {
        let ids = (1...48).map { String(format: "analysis-proxy-ios-%08X-0000-4000-8000-%012X.mp4", $0, $0) }
        let candidates = ids.map { "\"\($0)\"" }.joined(separator: ",")
        let json = """
        {"id":"db1b66d0-d9af-4a50-9938-8b76b0048bda","sequence":85,"revision":86,"role":"assistant","event_type":"assistant_response",
         "client_event_id":null,"content":"I couldn't tell which of your clips show \\"dodgeball\\". Tap the clips that do, or tell me there aren't any.",
         "created_at":"2026-10-04T12:24:17.698730Z",
         "payload":{"turn_id":"46fac55c-0ffe-4d12-815a-7ee71cceb725","turn_value":"question","receipt_ids":[],"next_actions":[],"schema_version":2,
           "clip_question":{"version":1,"allow_none":true,"question_id":"c61bd324-8553-467e-a332-4e68eae830f3",
             "categories":[{"op":"group","key":"group:dodgeball","label":"dodgeball","candidate_media_ids":[\(candidates)],"suggested_media_ids":[]}]}}}
        """
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { d in
            let raw = try d.singleValueContainer().decode(String.self)
            let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return try XCTUnwrap(f.date(from: raw))
        }
        let event = try decoder.decode(ThreadEvent.self, from: Data(json.utf8))
        let message = try XCTUnwrap(ChatTranscriptMessage.from(event: event))
        let question = try XCTUnwrap(message.clipQuestion, "assistant_response with clip_question must carry the picker")
        XCTAssertEqual(question.questionID, "c61bd324-8553-467e-a332-4e68eae830f3")
        XCTAssertEqual(question.categories.count, 1)
        XCTAssertEqual(question.categories[0].key, "group:dodgeball")
        XCTAssertEqual(question.categories[0].label, "Dodgeball")
        XCTAssertEqual(question.categories[0].candidateMediaIDs, ids)
        XCTAssertTrue(question.categories[0].suggestedMediaIDs.isEmpty)
        let state = ClipSelectionState(question: question)
        XCTAssertEqual(state.selectedCount, 0)
        XCTAssertFalse(state.canSend)
    }

    func testPickerIsOfferedUnlessTheServerExplicitlyDoesNotAdvertiseIt() throws {
        XCTAssertTrue(ClipSelectionAvailability.isAvailable(capabilities: nil), "an unloaded capability read must not hide the picker")
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"clip_selection_questions":true}"#.utf8))
        XCTAssertTrue(ClipSelectionAvailability.isAvailable(capabilities: on))
        let old = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(ClipSelectionAvailability.isAvailable(capabilities: old), "old server: text question")
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"clip_selection_questions":false}"#.utf8))
        XCTAssertFalse(ClipSelectionAvailability.isAvailable(capabilities: off))
    }

    func testSelectAllAndClearAffectOnlyTheirCategory() throws {
        var state = ClipSelectionState(question: try question())
        state.toggleNone("dodgeball")
        state.selectAll(in: "dodgeball")
        XCTAssertEqual(state.count(in: "dodgeball"), 3)
        XCTAssertFalse(state.isNone("dodgeball"), "select all clears none")
        XCTAssertEqual(state.count(in: "football"), 0)
        state.clear(in: "dodgeball")
        XCTAssertEqual(state.count(in: "dodgeball"), 0)
        XCTAssertFalse(state.canSend)
        state.selectAll(in: "unknown")
        XCTAssertEqual(state.selectedCount, 0)
    }

    // MARK: selection state

    func testSuggestedClipsStartTickedAndTapTwiceUnselects() throws {
        var state = ClipSelectionState(question: try question())
        XCTAssertTrue(state.isSelected("b", in: "dodgeball"))
        XCTAssertFalse(state.isSelected("a", in: "dodgeball"))
        state.toggle("a", in: "dodgeball")
        XCTAssertTrue(state.isSelected("a", in: "dodgeball"))
        state.toggle("a", in: "dodgeball")
        XCTAssertFalse(state.isSelected("a", in: "dodgeball"))
    }

    func testSelectionIsMultiSelectPerCategoryAndIndependentAcrossCategories() throws {
        var state = ClipSelectionState(question: try question())
        state.toggle("c", in: "dodgeball")
        XCTAssertTrue(state.isSelected("c", in: "dodgeball"))
        XCTAssertFalse(state.isSelected("c", in: "football"))
        state.toggle("c", in: "football")
        XCTAssertEqual(state.submission.answers.map(\.key), ["dodgeball", "football"])
        state.toggle("not-a-candidate", in: "football")
        XCTAssertEqual(state.selectedCount, 3)
    }

    func testNoneOfTheseIsExclusiveWithTicksInItsCategory() throws {
        var state = ClipSelectionState(question: try question())
        state.toggleNone("dodgeball")
        XCTAssertTrue(state.isNone("dodgeball"))
        XCTAssertFalse(state.isSelected("b", in: "dodgeball"), "none clears the suggestion")
        state.toggle("a", in: "dodgeball")
        XCTAssertFalse(state.isNone("dodgeball"), "ticking a clip clears none")
        state.toggleNone("dodgeball")
        state.toggleNone("dodgeball")
        XCTAssertFalse(state.isNone("dodgeball"))
    }

    func testNoneIsIgnoredWhenServerDoesNotAllowIt() throws {
        var state = ClipSelectionState(question: try question(allowNone: false))
        state.toggleNone("dodgeball")
        XCTAssertFalse(state.isNone("dodgeball"))
    }

    func testCanSendNeedsAtLeastOneDecision() throws {
        var state = ClipSelectionState(question: try XCTUnwrap(ClipQuestion.parse(payload: payload(categories: [category("x", "X", ["a"])]))))
        XCTAssertFalse(state.canSend)
        state.toggle("a", in: "x")
        XCTAssertTrue(state.canSend)
        state.toggle("a", in: "x")
        state.toggleNone("x")
        XCTAssertTrue(state.canSend)
    }

    // MARK: submission + message

    func testSubmissionEncodesTheContractShape() throws {
        var state = ClipSelectionState(question: try question())
        state.toggle("a", in: "dodgeball")
        state.toggleNone("football")
        let data = try JSONEncoder().encode(state.submission)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["question_id"] as? String, "q1")
        XCTAssertEqual(json["none_keys"] as? [String], ["football"])
        XCTAssertEqual(json["skipped"] as? Bool, false)
        let answers = try XCTUnwrap(json["answers"] as? [[String: Any]])
        XCTAssertEqual(answers.count, 1)
        XCTAssertEqual(answers[0]["key"] as? String, "dodgeball")
        XCTAssertEqual(answers[0]["media_ids"] as? [String], ["a", "b"], "candidate order, suggestion kept")
    }

    func testMessageTextComposition() throws {
        let q = try question()
        let positions = ["a": 1, "b": 7, "c": 3, "d": 2]
        var state = ClipSelectionState(question: q)
        state.toggle("c", in: "dodgeball")
        state.toggle("d", in: "football")
        state.toggle("c", in: "football")
        XCTAssertEqual(state.submission.message(question: q, positions: positions),
                       "Dodgeball: clips 3, 7. Football: clips 2, 3")
        var single = ClipSelectionState(question: q)
        single.toggleNone("football")
        XCTAssertEqual(single.submission.message(question: q, positions: positions),
                       "Dodgeball: clip 7. None of these for Football")
        var none = ClipSelectionState(question: q)
        none.toggleNone("dodgeball")
        XCTAssertEqual(none.submission.message(question: q, positions: positions), "None of these for Dodgeball")
        XCTAssertEqual(ClipSelectionSubmission.skip(q).message(question: q, positions: positions), "Skip, decide for me")
        XCTAssertTrue(ClipSelectionSubmission.skip(q).skipped)
    }

    func testSummaryForAnsweredCard() throws {
        let q = try question()
        var state = ClipSelectionState(question: q)
        state.toggleNone("football")
        XCTAssertEqual(state.submission.summary(question: q), "Dodgeball: 1 clip · Football: none")
        XCTAssertEqual(ClipSelectionSubmission.skip(q).summary(question: q), "Skipped, Kria decides")
    }

    func testClipPositionsCountNonAudioMediaInOrderAndNumberUnknownCandidates() throws {
        func media(_ id: String, _ kind: String = "video") -> CreationAttachedMedia {
            CreationAttachedMedia(id: id, filename: id, kind: kind, previewURL: nil)
        }
        let q = try question()
        let positions = ClipPositions.map(media: [media("voice", "audio"), media("a"), media("b")], question: q)
        XCTAssertEqual(positions["a"], 1)
        XCTAssertEqual(positions["b"], 2)
        XCTAssertNil(positions["voice"])
        XCTAssertEqual(Set([positions["c"], positions["d"]]), [3, 4], "unlisted candidates still get stable numbers")
    }

    // MARK: capability gating

    func testCapabilityFlagDecodesAndDefaultsOff() throws {
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(off.clipSelectionQuestionsEnabled, "old server: text question only")
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"clip_selection_questions":true}"#.utf8))
        XCTAssertTrue(on.clipSelectionQuestionsEnabled)
        let explicitOff = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"clip_selection_questions":false}"#.utf8))
        XCTAssertFalse(explicitOff.clipSelectionQuestionsEnabled)
    }
}
