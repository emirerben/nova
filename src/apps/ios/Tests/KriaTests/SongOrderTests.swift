import XCTest
@testable import Kria

/// KRI-374: the take-order question is hand-decoded/encoded; these pin the contract, the reorder rules,
/// and the "unknown or absent means hidden, never a crash" requirement.
final class SongOrderTests: XCTestCase {
    private func item(_ id: String, _ status: String = "confident", start: Double? = nil, alternates: [(Double, Double)] = []) -> JSONValue {
        var fields: [String: JSONValue] = [
            "media_id": .string(id), "status": .string(status),
            "alternates": .array(alternates.map { .object(["delta_s": .number($0.0), "score": .number($0.1)]) }),
        ]
        if let start { fields["song_start_s"] = .number(start) }
        return .object(fields)
    }

    private func payload(order: [String] = ["a", "b", "c"], items: [JSONValue]? = nil, id: String = "q1") -> [String: JSONValue] {
        ["song_order_question": .object([
            "question_id": .string(id), "proposed_order": .array(order.map(JSONValue.string)),
            "items": .array(items ?? [item("a", start: 1), item("b", "ambiguous", start: 12.5, alternates: [(-3, 0.4)]), item("c", "unmatched")]),
        ])]
    }

    private func question(order: [String] = ["a", "b", "c"], id: String = "q1") throws -> SongOrderQuestion {
        try XCTUnwrap(SongOrderQuestion.parse(payload: payload(order: order, id: id)))
    }

    // MARK: parsing

    func testParsesOrderItemsStatusesAndAlternates() throws {
        let q = try question()
        XCTAssertEqual(q.questionID, "q1")
        XCTAssertEqual(q.proposedOrder, ["a", "b", "c"])
        XCTAssertEqual(q.items.map(\.status), [.confident, .ambiguous, .unmatched])
        XCTAssertEqual(q.items[1].songStartS, 12.5)
        XCTAssertEqual(q.items[1].alternates, [.init(deltaS: -3, score: 0.4)])
        XCTAssertNil(q.items[2].songStartS)
        XCTAssertEqual(q.uncertainCount, 2)
    }

    func testAbsentOrMalformedPayloadsYieldNoQuestion() {
        XCTAssertNil(SongOrderQuestion.parse(payload: nil))
        XCTAssertNil(SongOrderQuestion.parse(payload: [:]))
        XCTAssertNil(SongOrderQuestion.parse(payload: payload(items: [])), "no items")
        XCTAssertNil(SongOrderQuestion.parse(payload: ["song_order_question": .object(["items": .array([item("a")])])]), "no question id")
        XCTAssertNil(SongOrderQuestion.parse(payload: ["song_order_question": .string("nope")]))
        XCTAssertNil(SongOrderQuestion.parse(payload: ["song_order_question": .object(["question_id": .string("q"), "items": .array([.string("x"), .object([:])])])]))
    }

    func testUnknownStatusIsTreatedAsUncertainAndBadValuesAreDropped() throws {
        let q = try XCTUnwrap(SongOrderQuestion.parse(payload: payload(items: [
            item("a", "brand_new_status", start: -4), item("a", "confident"), item("b", "confident", start: .nan),
            .object(["media_id": .string("c"), "status": .string("ambiguous"), "alternates": .array([.object(["delta_s": .string("x")]), .object(["delta_s": .number(2)])])]),
        ])))
        XCTAssertEqual(q.items.map(\.mediaID), ["a", "b", "c"], "duplicate ids dropped")
        XCTAssertEqual(q.items[0].status, .unmatched)
        XCTAssertNil(q.items[0].songStartS, "negative start dropped")
        XCTAssertNil(q.items[1].songStartS, "non-finite start dropped")
        XCTAssertEqual(q.items[2].alternates, [.init(deltaS: 2, score: 0)])
    }

    func testNormalizedOrderNeverDropsATakeAndIgnoresUnknownIds() throws {
        let q = try XCTUnwrap(SongOrderQuestion.parse(payload: payload(order: ["c", "zzz", "c", "a"])))
        XCTAssertEqual(q.normalizedOrder, ["c", "a", "b"])
        XCTAssertEqual(try XCTUnwrap(SongOrderQuestion.parse(payload: payload(order: []))).normalizedOrder, ["a", "b", "c"])
    }

    func testTranscriptMessageCarriesTheQuestionOnAssistantEventsOnly() {
        func event(role: String, type: String) -> ThreadEvent {
            ThreadEvent(id: type, sequence: 1, revision: 1, role: role, eventType: type, content: "Where do these clips go?", payload: payload(), createdAt: .now)
        }
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_question"))?.songOrderQuestion?.questionID, "q1")
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "user", type: "user_message"))?.songOrderQuestion)
    }

    func testTurnResponseDecodesTheQuestionLeniently() throws {
        let good = Data(#"{"turn_id":"t","thread_revision":3,"status":"accepted","song_order_question":{"question_id":"q9","proposed_order":["a"],"items":[{"media_id":"a","status":"confident","alternates":[]}]}}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(TurnAccepted.self, from: good).songOrderQuestion?.questionID, "q9")
        let plain = Data(#"{"turn_id":"t","thread_revision":3,"status":"accepted"}"#.utf8)
        XCTAssertNil(try JSONDecoder().decode(TurnAccepted.self, from: plain).songOrderQuestion)
        let garbage = Data(#"{"turn_id":"t","thread_revision":3,"status":"accepted","song_order_question":{"question_id":7,"items":"x"}}"#.utf8)
        let decoded = try JSONDecoder().decode(TurnAccepted.self, from: garbage)
        XCTAssertNil(decoded.songOrderQuestion, "a malformed question must not fail the turn")
        XCTAssertEqual(decoded.threadRevision, 3)
    }

    // MARK: reorder state

    func testStartsInTheProposedOrderUnchanged() throws {
        let state = SongOrderState(question: try question(order: ["b", "a", "c"]))
        XCTAssertEqual(state.order, ["b", "a", "c"])
        XCTAssertFalse(state.isChanged)
        XCTAssertEqual(state.position(of: "a"), 2)
    }

    func testMoveFollowsListOnMoveSemantics() throws {
        var state = SongOrderState(question: try question())
        state.move(from: IndexSet(integer: 0), to: 3)       // a to the end
        XCTAssertEqual(state.order, ["b", "c", "a"])
        state.move(from: IndexSet(integer: 2), to: 0)       // a back to the front
        XCTAssertEqual(state.order, ["a", "b", "c"])
        state.move(from: IndexSet(integer: 1), to: 2)       // dropping directly below itself is a no-op
        XCTAssertEqual(state.order, ["a", "b", "c"])
        state.move(from: IndexSet(integer: 0), to: 2)
        XCTAssertEqual(state.order, ["b", "a", "c"])
        state.move(from: IndexSet([0, 1]), to: 3)           // a multi-row drag keeps the rows' relative order
        XCTAssertEqual(state.order, ["c", "b", "a"])
        state.move(from: IndexSet(integer: 9), to: 0)       // out of range is ignored
        state.move(from: IndexSet(), to: 0)
        XCTAssertEqual(state.order, ["c", "b", "a"])
    }

    func testUpDownButtonsSwapNeighboursAndStopAtTheEdges() throws {
        var state = SongOrderState(question: try question())
        XCTAssertFalse(state.canMoveUp("a")); XCTAssertTrue(state.canMoveDown("a"))
        XCTAssertFalse(state.canMoveDown("c")); XCTAssertFalse(state.canMoveUp("zzz")); XCTAssertFalse(state.canMoveDown("zzz"))
        state.moveUp("a")
        XCTAssertEqual(state.order, ["a", "b", "c"], "already first")
        state.moveDown("a"); state.moveDown("a")
        XCTAssertEqual(state.order, ["b", "c", "a"])
        state.moveDown("a")
        XCTAssertEqual(state.order, ["b", "c", "a"], "already last")
        state.moveUp("c")
        XCTAssertEqual(state.order, ["c", "b", "a"])
        XCTAssertTrue(state.isChanged)
        state.reset()
        XCTAssertEqual(state.order, ["a", "b", "c"])
        XCTAssertFalse(state.isChanged)
    }

    func testMovingBackToTheProposalIsNotAChange() throws {
        var state = SongOrderState(question: try question())
        state.moveDown("a"); state.moveUp("a")
        XCTAssertFalse(state.isChanged)
    }

    // MARK: submission

    func testSubmissionEncodesTheServerKeysAndReflectsTheOrder() throws {
        var state = SongOrderState(question: try question())
        state.moveDown("a")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(state.submission)) as? [String: Any])
        XCTAssertEqual(json["question_id"] as? String, "q1")
        XCTAssertEqual(json["ordered_media_ids"] as? [String], ["b", "a", "c"])
        XCTAssertEqual(Set(json.keys), ["question_id", "ordered_media_ids"])
    }

    func testPositionsMatchTheClipPickerNumberingAndMessagesAreReadable() throws {
        let q = try question()
        let media = [
            CreationAttachedMedia(id: "voice", filename: "v.m4a", kind: "audio", previewURL: nil),
            CreationAttachedMedia(id: "c", filename: "c.mov", kind: "video", previewURL: nil),
            CreationAttachedMedia(id: "a", filename: "a.mov", kind: "video", previewURL: nil),
        ]
        let positions = SongOrderPositions.map(media: media, question: q)
        XCTAssertEqual(positions["c"], 1); XCTAssertEqual(positions["a"], 2)
        XCTAssertEqual(positions["b"], 3, "a take missing from stale thread state still gets a stable number")
        let submission = SongOrderSubmission(questionID: "q1", orderedMediaIDs: ["b", "a", "c"])
        XCTAssertEqual(submission.message(positions: positions), "Use this order: clips 3, 2, 1")
        XCTAssertEqual(submission.summary(positions: positions), "Clip 3 · Clip 2 · Clip 1")
        XCTAssertEqual(SongOrderSubmission(questionID: "q1", orderedMediaIDs: ["zzz"]).message(positions: positions), "Use this order")
    }

    // MARK: transcript fold

    private func answer(_ questionID: String, _ ids: [String] = ["b", "a", "c"]) -> SongOrderSubmission {
        SongOrderSubmission(questionID: questionID, orderedMediaIDs: ids)
    }

    func testOnlyTheNewestUnansweredQuestionIsInteractive() throws {
        let q1 = try question(), q2 = try question(id: "q2")
        typealias E = SongOrderFold.Entry
        let entries = [
            E(messageID: "m1", question: q1, isUser: false),
            E(messageID: "m2", question: nil, isUser: true, answer: answer("q1")),   // a song_order for q1
            E(messageID: "m3", question: q2, isUser: false),
            E(messageID: "m4", question: nil, isUser: false),      // a plain assistant reply does not answer it
        ]
        XCTAssertEqual(SongOrderFold.phases(entries), ["m1": .answered(answer("q1")), "m3": .active])
    }

    func testAPlainUserMessageNeverClosesTheQuestion() throws {
        // The server keeps a question open until a matching song_order arrives, so chatting must not
        // collapse the card into "Order confirmed".
        let q = try question()
        typealias E = SongOrderFold.Entry
        let entries = [
            E(messageID: "m1", question: q, isUser: false),
            E(messageID: "m2", question: nil, isUser: true),                        // "make it faster"
            E(messageID: "m3", question: nil, isUser: true, answer: answer("other")), // an answer to a DIFFERENT question
        ]
        XCTAssertEqual(SongOrderFold.phases(entries), ["m1": .active])
    }

    func testAnswerBeforeTheQuestionOrFromAnAssistantDoesNotCount() throws {
        let q = try question()
        typealias E = SongOrderFold.Entry
        let entries = [
            E(messageID: "m0", question: nil, isUser: true, answer: answer("q1")),   // earlier than the question
            E(messageID: "m1", question: q, isUser: false),
            E(messageID: "m2", question: nil, isUser: false, answer: answer("q1")),  // not a user message
        ]
        XCTAssertEqual(SongOrderFold.phases(entries), ["m1": .active])
    }

    func testAQuestionReplacedBeforeAnyAnswerIsSuperseded() throws {
        let q = try question()
        typealias E = SongOrderFold.Entry
        let entries = [E(messageID: "m1", question: q, isUser: false), E(messageID: "m2", question: q, isUser: false)]
        XCTAssertEqual(SongOrderFold.phases(entries), ["m1": .superseded, "m2": .active])
        XCTAssertTrue(SongOrderFold.phases([]).isEmpty)
    }

    func testAnswerIsReadFromTheStoredUserEventPayload() throws {
        func event(role: String, payload: [String: JSONValue]) -> ThreadEvent {
            ThreadEvent(id: "e", sequence: 2, revision: 2, role: role, eventType: role == "user" ? "user_message" : "assistant_response", content: "Use this order", payload: payload, createdAt: .now)
        }
        let order: [String: JSONValue] = ["song_order": .object(["question_id": .string("q1"), "ordered_media_ids": .array([.string("b"), .string("a")])])]
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "user", payload: order))?.songOrderAnswer, answer("q1", ["b", "a"]))
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "assistant", payload: order))?.songOrderAnswer)
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "user", payload: [:]))?.songOrderAnswer)
        for bad: JSONValue in [.string("x"), .object([:]), .object(["question_id": .string("q1"), "ordered_media_ids": .array([])]), .object(["ordered_media_ids": .array([.string("a")])])] {
            XCTAssertNil(SongOrderSubmission.parse(payload: ["song_order": bad]), "\(bad)")
        }
    }

    func testAnInFlightSendReadsAsAnsweredBeforeTheServerEchoesIt() {
        let pending = ChatPendingMessage(content: "Use this order", clientEventID: "c1", afterSequence: 1, songOrder: answer("q1"))
        XCTAssertEqual(pending.transcriptMessage.songOrderAnswer, answer("q1"))
        XCTAssertNil(ChatPendingMessage(content: "hi", clientEventID: "c2", afterSequence: 1).transcriptMessage.songOrderAnswer)
    }

    @MainActor func testUnmatchedTakesAreCountedSeparatelyAndCaptionedAsFiller() throws {
        let q = try question()
        XCTAssertEqual(q.ambiguousCount, 1)
        XCTAssertEqual(q.unmatchedCount, 1)
        let unmatched = try XCTUnwrap(q.item(for: "c"))
        XCTAssertEqual(SongOrderCard.caption(for: unmatched), "Kria couldn’t place this clip — it will be used as filler")
        XCTAssertEqual(SongOrderCard.caption(for: nil), SongOrderCard.unplacedCaption)
        XCTAssertTrue(SongOrderCard.caption(for: try XCTUnwrap(q.item(for: "b"))).hasPrefix("Could fit a few places"))
    }

    @MainActor func testReorderAdviceIsOnlyGivenWhenSomethingCanBeReordered() {
        let both = SongOrderCard.hint(ambiguous: 1, unmatched: 2)
        XCTAssertTrue(both.contains("drag or use the arrows to fix the order"))
        XCTAssertTrue(both.contains("2 clips Kria couldn’t place will be used as filler"))
        let onlyUnmatched = SongOrderCard.hint(ambiguous: 0, unmatched: 1)
        XCTAssertFalse(onlyUnmatched.contains("fix the order"), "nothing doubtful is fixable by dragging")
        XCTAssertTrue(onlyUnmatched.contains("1 clip Kria couldn’t place will be used as filler"))
        XCTAssertEqual(SongOrderCard.hint(ambiguous: 0, unmatched: 0), "Tap a clip to watch it, then drag or use the arrows to change the order.")
    }

    @MainActor func testAnsweredCopyNeverClaimsAConfirmationItCannotName() {
        XCTAssertEqual(SongOrderCard.answeredText(summary: "Clip 2 · Clip 1"), "Order confirmed: Clip 2 · Clip 1")
        XCTAssertEqual(SongOrderCard.answeredText(summary: nil), "Order question closed")
    }

    func testStaleConflictCodeMatchesTheServerContract() {
        XCTAssertEqual(SongOrderSubmission.staleConflictCode, "song_order_stale")
    }
}
