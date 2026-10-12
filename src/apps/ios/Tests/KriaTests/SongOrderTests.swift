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
        XCTAssertEqual(q.items[0].songStartS, -4, "a negative start is real: the take was filmed before the song")
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
        XCTAssertEqual(Set(json.keys), ["question_id", "ordered_media_ids"], "no placements key unless the timeline sends one")
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

    // MARK: song timeline (KRI-561)

    private func block(_ id: String, start: Double? = nil, status: SongOrderQuestion.Status = .confident, duration: Double? = 8,
                       candidates: [(Double, Double)] = []) -> SongOrderQuestion.Item {
        SongOrderQuestion.Item(mediaID: id, status: status, songStartS: start, alternates: [], durationS: duration,
                               candidates: candidates.map { .init(deltaS: $0.0, likelihood: $0.1, matchStartS: nil, matchEndS: nil) })
    }

    private func timeline(_ items: [SongOrderQuestion.Item], songDuration: Double? = 200, maxWindow: Double? = nil,
                          mediaDurations: [String: Double] = [:]) -> SongTimelineState {
        let question = SongOrderQuestion(questionID: "tq", proposedOrder: items.map(\.mediaID), items: items,
                                         songDurationS: songDuration, maxWindowS: maxWindow, songGeneration: 2)
        let media = items.map { CreationAttachedMedia(id: $0.mediaID, filename: "\($0.mediaID).mov", kind: "video", previewURL: nil, durationS: mediaDurations[$0.mediaID]) }
        return SongTimelineState(question: question, media: media)
    }

    func testOldServerPayloadWithNoNewFieldsStillParsesAndGivesATimelineWithDefaults() throws {
        let q = try question()
        XCTAssertNil(q.songDurationS); XCTAssertNil(q.maxWindowS); XCTAssertNil(q.firstLineS); XCTAssertNil(q.songGeneration)
        XCTAssertEqual(q.items.map(\.candidates), [[], [], []])
        XCTAssertNil(q.items[0].durationS); XCTAssertNil(q.items[0].likelihood); XCTAssertNil(q.items[0].reason)
        let state = SongTimelineState(question: q)
        XCTAssertEqual(state.maxWindowS, 120)
        XCTAssertEqual(state.duration(for: "a"), 8, "unknown take length is drawn as 8 s")
        XCTAssertEqual(Set(state.placed.keys), ["a", "b"], "the unmatched take starts in the tray")
        XCTAssertEqual(state.tray, ["c"])
    }

    func testNewFieldsParseAndABadOneNeverDropsTheCard() throws {
        let payload: [String: JSONValue] = ["song_order_question": .object([
            "question_id": .string("q"), "song_duration_s": .number(90), "max_window_s": .number(75), "first_line_s": .number(4.5), "song_generation": .number(7),
            "items": .array([
                .object(["media_id": .string("a"), "status": .string("confident"), "song_start_s": .number(-3), "duration_s": .number(6.5),
                         "likelihood": .number(0.8), "reason": .string("tie"),
                         "candidates": .array((0..<6).map { .object(["delta_s": .number(Double($0)), "likelihood": .number(0.5), "match_start_s": .number(1), "match_end_s": .number(2)]) })]),
                .object(["media_id": .string("b"), "status": .string("ambiguous"), "duration_s": .number(0), "reason": .string("brand_new"),
                         "candidates": .array([.string("x"), .object(["delta_s": .string("y")]), .object(["delta_s": .number(-2.5)])])]),
            ]),
        ])]
        let q = try XCTUnwrap(SongOrderQuestion.parse(payload: payload))
        XCTAssertEqual([q.songDurationS, q.maxWindowS, q.firstLineS], [90, 75, 4.5]); XCTAssertEqual(q.songGeneration, 7)
        XCTAssertEqual(q.items[0].songStartS, -3); XCTAssertEqual(q.items[0].durationS, 6.5); XCTAssertEqual(q.items[0].reason, .tie)
        XCTAssertEqual(q.items[0].candidates.count, 4, "at most four candidates")
        XCTAssertEqual(q.items[0].candidates[0], .init(deltaS: 0, likelihood: 0.5, matchStartS: 1, matchEndS: 2))
        XCTAssertNil(q.items[1].durationS, "a zero length is no length"); XCTAssertNil(q.items[1].reason)
        XCTAssertEqual(q.items[1].candidates, [.init(deltaS: -2.5, likelihood: 0, matchStartS: nil, matchEndS: nil)])
        let junk: [String: JSONValue] = ["song_order_question": .object(["question_id": .string("q"), "song_duration_s": .string("long"), "max_window_s": .number(-1),
                                                                         "song_generation": .number(1.5), "items": .array([item("a")])])]
        let tolerant = try XCTUnwrap(SongOrderQuestion.parse(payload: junk))
        XCTAssertNil(tolerant.songDurationS); XCTAssertNil(tolerant.maxWindowS); XCTAssertNil(tolerant.songGeneration)
    }

    func testTakeLengthFallsBackToTheAttachedMediaThenEightSeconds() {
        let state = timeline([block("a", start: 0, duration: nil), block("b", start: 20, duration: 0), block("c", start: 40, duration: nil)], mediaDurations: ["a": 5, "b": 3])
        XCTAssertEqual(state.duration(for: "a"), 5)
        XCTAssertEqual(state.duration(for: "b"), 3, "a zero server length is ignored")
        XCTAssertEqual(state.duration(for: "c"), 8)
    }

    func testNegativeDeltaIsKeptOnTheWireButClippedOnTheSongAxis() {
        var state = timeline([block("a", start: -3, duration: 8), block("b", start: 20)])
        XCTAssertEqual(state.segments().first, SongTimelineState.Segment(mediaID: "a", start: 0, end: 5, hidden: false))
        XCTAssertEqual(state.gaps(), [SongTimelineState.Gap(index: 0, start: 5, end: 20)])
        XCTAssertEqual(state.submission(includePlacements: true).placements?.first, .init(mediaID: "a", deltaS: -3))
        XCTAssertNil(state.move("a", toDelta: -50).refusal, "a drag far before the song stops with a second of the take still on it")
        XCTAssertEqual(state.delta(of: "a"), -7, "8 s take keeps 1 s on the song")
    }

    func testBlocksWithTheSameStartKeepTheLongerOneAndHideTheOther() {
        let state = timeline([block("a", start: 10, duration: 6), block("b", start: 10, duration: 9), block("c", start: 10, duration: 9)])
        let hidden = Dictionary(uniqueKeysWithValues: state.segments().map { ($0.mediaID, $0.hidden) })
        XCTAssertEqual(hidden, ["a": true, "b": false, "c": true], "longest wins; an exact duplicate is hidden by capture order")
    }

    func testABlockInsideAnotherIsHiddenAndNeverCountsAsCoverage() {
        let state = timeline([block("a", start: 0, duration: 20), block("b", start: 5, duration: 4), block("c", start: 30, duration: 5)])
        XCTAssertEqual(state.segments().filter(\.hidden).map(\.mediaID), ["b"])
        XCTAssertEqual(state.gaps(), [SongTimelineState.Gap(index: 0, start: 20, end: 30)])
    }

    func testAServerSeedThatExtendsThePreviousBlockBy099StaysHiddenUntilTheCreatorMovesSomething() {
        let state = timeline([block("a", start: 0, duration: 10), block("b", start: 5, duration: 5.99)])
        XCTAssertEqual(state.segments().map(\.hidden), [false, true], "ends 0.99 s past the previous block: the server drops it")
        XCTAssertEqual(state.placed, ["a": 0, "b": 5], "loading never rewrites the server's seed")
        XCTAssertEqual(timeline([block("a", start: 0, duration: 10), block("b", start: 5, duration: 6)]).segments().map(\.hidden), [false, false], "1.0 s past: kept")
    }

    func testAClipDroppedOverAnotherNeverRefusesItPushesTheOtherAside() {
        var state = timeline([block("a", start: 10, duration: 6), block("b", duration: 30)])
        let outcome = state.move("b", toDelta: 0)
        XCTAssertNil(outcome.refusal, "a 30 s clip over a is allowed now")
        XCTAssertEqual(state.placed, ["a": 30, "b": 0], "the anchor stays; a moves to its end")
        XCTAssertEqual(outcome.moved, [.init(mediaID: "a", from: 10, to: 30)])
    }

    func testGapIsOfferedFromSixTenthsOfASecond() {
        XCTAssertEqual(timeline([block("a", start: 0, duration: 10), block("b", start: 10.6)]).gaps(), [SongTimelineState.Gap(index: 0, start: 10, end: 10.6)])
        XCTAssertTrue(timeline([block("a", start: 0, duration: 10), block("b", start: 10.59)]).gaps().isEmpty)
        XCTAssertTrue(timeline([block("a", start: 0, duration: 10), block("b", start: 8)]).gaps().isEmpty, "overlap is not a gap")
        XCTAssertTrue(timeline([block("a", start: 4)]).gaps().isEmpty, "the song before and after the video is not a gap")
    }

    func testEverythingInTheTrayIsStillSendable() throws {
        let state = timeline([block("a", status: .unmatched), block("b", status: .unmatched)])
        XCTAssertTrue(state.segments().isEmpty); XCTAssertTrue(state.gaps().isEmpty); XCTAssertEqual(state.span, 0)
        let submission = state.submission(includePlacements: true)
        XCTAssertEqual(submission.orderedMediaIDs, ["a", "b"])
        XCTAssertEqual(submission.placements, [], "an empty list, not a missing one: the creator placed nothing")
        XCTAssertNil(state.submission(includePlacements: false).placements)
        XCTAssertEqual(submission.message(positions: ["a": 1, "b": 2]), "Use this arrangement: clip 1 as background footage, clip 2 as background footage")
        XCTAssertEqual(state.footerNotice, .trayIsBackground)
    }

    func testDraggingSnapsToACandidateWithinAQuarterSecondOnly() {
        var state = timeline([block("a", start: 40, duration: 8, candidates: [(40, 0.9), (10, 0.3)])])
        XCTAssertEqual(state.snapped("a", 10.25), 10, "exactly 0.25 s away snaps")
        XCTAssertEqual(state.snapped("a", 9.75), 10)
        XCTAssertEqual(state.snapped("a", 10.26), 10.3, "0.26 s away stays free (rounded to 0.1 s)")
        XCTAssertNil(state.move("a", toDelta: 10.2).refusal)
        XCTAssertEqual(state.delta(of: "a"), 10)
    }

    func testTheVideoMayRunExactlyTheCapAndNotAMomentLonger() {
        var state = timeline([block("a", start: 0, duration: 8), block("b", duration: 8)], maxWindow: 20)
        XCTAssertNil(state.move("b", toDelta: 12).refusal, "0 to 20 s is exactly the cap")
        XCTAssertEqual(state.span, 20)
        XCTAssertEqual(state.move("b", toDelta: 12.1).refusal, .tooLong(maxSeconds: 20))
        XCTAssertEqual(state.delta(of: "b"), 12, "a refused move leaves the arrangement as it was")
        XCTAssertEqual(SongTimelineState.Refusal.tooLong(maxSeconds: 20).message, "That makes the video longer than 20 seconds.")
        XCTAssertNil(state.move("b", toDelta: 11).refusal, "moving closer is always allowed")
    }

    func testAServerArrangementOverTheCapCanBeFixedByMovingTowardTheCap() {
        var state = timeline([block("a", start: 0, duration: 8), block("b", start: 40, duration: 8)], maxWindow: 20)
        XCTAssertEqual(state.span, 48)
        XCTAssertNil(state.move("b", toDelta: 30).refusal, "shortening an over-long video is allowed")
        XCTAssertEqual(state.move("b", toDelta: 60).refusal, .tooLong(maxSeconds: 20), "lengthening it is not")
    }

    func testResetReturnsToTheServersProposalAfterAnyEdits() {
        var state = timeline([block("a", start: 5), block("b", start: 30), block("c", status: .unmatched, candidates: [(18, 0.5)])])
        let seed = state.placed
        state.moveToTray("a"); _ = state.move("b", toDelta: 50); _ = state.move("c", toDelta: 18)
        XCTAssertTrue(state.isChanged)
        state.reset()
        XCTAssertEqual(state.placed, seed); XCTAssertFalse(state.isChanged); XCTAssertEqual(state.tray, ["c"])
    }

    func testFillingAGapUsesTheTakesBestCandidateInsideItElseTheGapStart() {
        var state = timeline([block("a", start: 0, duration: 10), block("b", start: 40, duration: 10),
                              block("c", status: .unmatched, duration: 6, candidates: [(5, 0.9), (22, 0.2), (30, 0.6)]),
                              block("d", status: .unmatched, duration: 6, candidates: [(2, 0.9)])])
        let gap = state.gaps()[0]
        XCTAssertEqual(gap, SongTimelineState.Gap(index: 0, start: 10, end: 40))
        XCTAssertNil(state.place("c", inGap: gap).refusal)
        XCTAssertEqual(state.delta(of: "c"), 30, "the likeliest candidate that starts inside the gap, not the 0.9 one outside it")
        XCTAssertNil(state.place("d", inGap: state.gaps()[0]).refusal)
        XCTAssertEqual(state.delta(of: "d"), 10, "no candidate in the gap: its start")
    }

    func testFillingAGapWithATakeTooLongForItPushesTheNeighbourAside() {
        var state = timeline([block("a", start: 0, duration: 10), block("b", start: 20, duration: 10), block("c", status: .unmatched, duration: 20)])
        let outcome = state.place("c", inGap: state.gaps()[0])
        XCTAssertNil(outcome.refusal)
        XCTAssertEqual(state.placed, ["a": 0, "b": 30, "c": 10], "c keeps its spot; b makes room (left would have pushed a off the song)")
        XCTAssertEqual(outcome.moved, [.init(mediaID: "b", from: 20, to: 30)])
        XCTAssertTrue(state.tray.isEmpty)
    }

    func testAddToTheSongWorksWithNoGapAndPrefersTheLikeliestFittingCandidate() {
        var empty = timeline([block("a", status: .unmatched, candidates: [(3, 0.2), (9, 0.7)])])
        XCTAssertNil(empty.placeAnywhere("a").refusal)
        XCTAssertEqual(empty.delta(of: "a"), 9)
        var after = timeline([block("a", start: 0, duration: 10), block("b", status: .unmatched)])
        XCTAssertNil(after.placeAnywhere("b").refusal)
        XCTAssertEqual(after.delta(of: "b"), 10, "straight after the last clip")
    }

    func testSubmissionListsPlacedTakesByTimeThenTheTrayInCaptureOrderAndEveryTakeOnce() throws {
        var state = timeline([block("a", start: 40), block("b", status: .unmatched), block("c", start: 5), block("d", status: .unmatched)])
        let submission = state.submission(includePlacements: true)
        XCTAssertEqual(submission.orderedMediaIDs, ["c", "a", "b", "d"])
        XCTAssertEqual(submission.placements, [.init(mediaID: "c", deltaS: 5), .init(mediaID: "a", deltaS: 40)])
        state.moveToTray("c")
        XCTAssertEqual(state.submission(includePlacements: true).orderedMediaIDs, ["a", "b", "c", "d"], "tray takes keep capture order")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(submission)) as? [String: Any])
        XCTAssertEqual(Set(json.keys), ["question_id", "ordered_media_ids", "placements"])
        let placements = try XCTUnwrap(json["placements"] as? [[String: Any]])
        XCTAssertEqual(placements.first?["media_id"] as? String, "c"); XCTAssertEqual(placements.first?["delta_s"] as? Double, 5)
        XCTAssertNil((try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(state.submission(includePlacements: false))) as? [String: Any]))["placements"])
    }

    func testArrangementMessageAndSummaryNameTimesAndBackgroundClips() {
        let state = timeline([block("a", start: 21.5), block("b", start: 8), block("c", status: .unmatched)])
        let positions = ["a": 1, "b": 2, "c": 3]
        let submission = state.submission(includePlacements: true)
        XCTAssertEqual(submission.message(positions: positions), "Use this arrangement: clip 2 at 0:08, clip 1 at 0:22, clip 3 as background footage")
        XCTAssertEqual(submission.summary(positions: positions), "Clip 2 at 0:08 · Clip 1 at 0:22 · Clip 3 in the background")
    }

    func testTheStoredAnswerKeepsItsPlacementsAndToleratesBadOnes() {
        let echo: [String: JSONValue] = ["song_order": .object([
            "question_id": .string("q1"), "ordered_media_ids": .array([.string("b"), .string("a")]),
            "placements": .array([.object(["media_id": .string("b"), "delta_s": .number(-2)]), .object(["media_id": .string("a")]), .string("junk")]),
        ])]
        XCTAssertEqual(SongOrderSubmission.parse(payload: echo), SongOrderSubmission(questionID: "q1", orderedMediaIDs: ["b", "a"], placements: [.init(mediaID: "b", deltaS: -2)]))
    }

    func testFooterNoticeSaysWhatTheServerWillDoWithTheTrayAndEmptySpots() {
        let placed = [block("a", start: 0, duration: 10), block("b", start: 20, duration: 10)]
        XCTAssertEqual(timeline(placed).footerNotice, .emptySpotsCannotBeFilled, "a gap and an empty tray: a warning")
        XCTAssertEqual(timeline(placed + [block("t", status: .unmatched)]).footerNotice, .trayFillsEmptySpots)
        XCTAssertEqual(timeline([block("a", start: 0, duration: 10), block("b", start: 10, duration: 10), block("t", status: .unmatched)]).footerNotice, .trayIsBackground)
        XCTAssertNil(timeline([block("a", start: 0, duration: 10), block("b", start: 10, duration: 10)]).footerNotice)
        XCTAssertTrue(SongTimelineState.FooterNotice.emptySpotsCannotBeFilled.isWarning)
        XCTAssertEqual(SongTimelineState.FooterNotice.trayFillsEmptySpots.message, "Clips in the tray will fill empty spots where they fit.")
        XCTAssertEqual(SongTimelineState.FooterNotice.emptySpotsCannotBeFilled.message, "Empty spots can't be filled — clips on one side may be left out.")
        XCTAssertEqual(SongTimelineState.FooterNotice.trayIsBackground.message, "Clips in the tray play as background footage.")
    }

    func testCapabilityAndAudioLinkDecodeTolerantly() throws {
        let caps = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"song_order_questions":true,"song_order_placements":true}"#.utf8))
        XCTAssertTrue(caps.songOrderPlacementsEnabled)
        XCTAssertFalse(try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"song_order_questions":true}"#.utf8)).songOrderPlacementsEnabled)
        let link = try JSONDecoder().decode(SongAudioLink.self, from: Data(#"{"url":"https://x.test/a.mp3?sig=1","generation":4,"duration_s":93.5,"expires_at":"2026-10-09T10:15:00Z"}"#.utf8))
        XCTAssertEqual(link.generation, 4); XCTAssertEqual(link.durationS, 93.5); XCTAssertEqual(SongAudioCache.fileExtension(for: link.url, contentType: nil), "mp3")
        XCTAssertEqual(SongAudioCache.fileExtension(for: URL(string: "https://x.test/blob")!, contentType: "audio/mpeg; charset=x"), "mp3")
        XCTAssertEqual(SongAudioCache.key(threadID: UUID(uuidString: "11111111-1111-1111-1111-111111111111")!, generation: 4), "11111111-1111-1111-1111-111111111111-4.audio")
    }

    // MARK: push aside (KRI-575)

    private func tray(_ id: String, duration: Double = 8) -> SongOrderQuestion.Item { block(id, status: .unmatched, duration: duration) }
    private func visibleOverlaps(_ state: SongTimelineState) -> Bool {
        let visible = state.segments().filter { !$0.hidden }
        return zip(visible, visible.dropFirst()).contains { $1.start < $0.end - 1e-6 }
    }

    func testTheDroppedClipNeverMovesAndTheOverlappedOneGoesToTheSideNeedingTheSmallerShift() {
        var left = timeline([block("a", start: 40, duration: 20), tray("x", duration: 4)])
        let outcome = left.move("x", toDelta: 53)
        XCTAssertEqual(left.placed, ["a": 33, "x": 53], "left needs 7 s, right 17 s: a ends exactly where x starts")
        XCTAssertEqual(outcome.moved, [.init(mediaID: "a", from: 40, to: 33)])
        var right = timeline([block("a", start: 40, duration: 20), tray("x", duration: 4)])
        right.move("x", toDelta: 48)
        XCTAssertEqual(right.placed, ["a": 52, "x": 48], "a tie goes right: a starts exactly where x ends")
        var nearer = timeline([block("a", start: 40, duration: 20), tray("x", duration: 4)])
        nearer.move("x", toDelta: 41)
        XCTAssertEqual(nearer.placed["a"], 45, "dropped near a's start the right side is the smaller shift")
    }

    func testPushingCascadesThroughAdjacentClipsAndKeepsThemAdjacentAndInOrder() {
        var state = timeline([block("a", start: 40, duration: 8), block("b", start: 48, duration: 8), block("c", start: 56, duration: 8), tray("x")])
        let outcome = state.move("x", toDelta: 61)
        XCTAssertEqual(state.placed, ["a": 37, "b": 45, "c": 53, "x": 61], "c, then b, then a each end where the next starts")
        XCTAssertEqual(outcome.moved.map(\.mediaID), ["a", "b", "c"])
        XCTAssertFalse(visibleOverlaps(state))
        var right = timeline([block("a", start: 0, duration: 8), block("b", start: 8, duration: 8), block("c", start: 16, duration: 8), tray("x")])
        right.move("x", toDelta: 4)
        XCTAssertEqual(right.placed, ["a": 12, "b": 20, "c": 28, "x": 4], "blocked at 0 on the left, so all three go right, adjacent")
    }

    func testAPushLeftBlockedAtZeroGoesRightInsteadOfLosingTheClip() {
        var state = timeline([block("a", start: 0, duration: 8), tray("x")])
        let outcome = state.move("x", toDelta: 4)
        XCTAssertEqual(state.placed, ["a": 12, "x": 4])
        XCTAssertTrue(outcome.sentToTray.isEmpty, "the way that keeps everything placed wins")
    }

    func testAClipThatFitsNeitherWayGoesToTheTrayAndTheOutcomeSaysSo() {
        var state = timeline([block("a", start: 0, duration: 8), tray("x", duration: 4)], songDuration: 10)
        let outcome = state.move("x", toDelta: 2)
        XCTAssertEqual(outcome.sentToTray, ["a"])
        XCTAssertEqual(state.tray, ["a"])
        XCTAssertEqual(state.placed, ["x": 2])
        XCTAssertEqual(outcome.message(clipNumber: { $0 == "a" ? 1 : 2 }), "Clip 1 didn't fit and went to the tray.")
    }

    func testRatherThanPushPastTheSongEndTheClipsGoLeftAndStayOnTheSong() {
        var state = timeline([block("a", start: 90, duration: 8), block("b", start: 82, duration: 8), tray("x", duration: 10)], songDuration: 100)
        let outcome = state.move("x", toDelta: 86)
        XCTAssertNil(outcome.refusal)
        XCTAssertEqual(state.delta(of: "x"), 86)
        XCTAssertFalse(visibleOverlaps(state))
        XCTAssertTrue(state.segments().allSatisfy { $0.end <= 100 + 1e-9 })
    }

    func testExactlyAdjacentDoesNotPushButAnOverlapOfEvenUnderASecondDoes() {
        var adjacent = timeline([block("a", start: 0, duration: 8), tray("x")])
        let flush = adjacent.move("x", toDelta: 8)
        XCTAssertFalse(flush.displacedAnything); XCTAssertEqual(adjacent.placed, ["a": 0, "x": 8])
        var sliver = timeline([block("a", start: 0, duration: 7.99), tray("x")])
        let pushed = sliver.move("x", toDelta: 7)
        XCTAssertEqual(pushed.moved, [.init(mediaID: "a", from: 0, to: 15)], "0.99 s of overlap is still overlap")
    }

    func testMovingAClipWithinItsOwnOldSpanNeverPushesItself() {
        var alone = timeline([block("a", start: 0, duration: 8)])
        let moved = alone.move("a", toDelta: 2)
        XCTAssertEqual(alone.placed, ["a": 2]); XCTAssertFalse(moved.displacedAnything); XCTAssertNil(moved.refusal)
        var withNeighbour = timeline([block("a", start: 0, duration: 8), block("b", start: 8, duration: 8)])
        let outcome = withNeighbour.move("a", toDelta: 3)
        XCTAssertEqual(withNeighbour.placed, ["a": 3, "b": 11], "only the neighbour it now covers moves")
        XCTAssertEqual(outcome.moved.map(\.mediaID), ["b"])
    }

    func testDroppingOnAHiddenSeedPushesThatBlockOutOfTheWayToo() {
        var state = timeline([block("a", start: 0, duration: 20), block("h", start: 5, duration: 4), tray("x", duration: 3)])
        XCTAssertEqual(state.segments().filter(\.hidden).map(\.mediaID), ["h"], "h is a hidden seed under a")
        let outcome = state.move("x", toDelta: 6)
        XCTAssertEqual(state.delta(of: "x"), 6, "the anchor does not move")
        XCTAssertFalse(visibleOverlaps(state))
        XCTAssertTrue(state.segments().filter(\.hidden).isEmpty, "nothing is left hidden under the drop")
        XCTAssertEqual(Set(outcome.moved.map(\.mediaID)), ["a", "h"])
    }

    func testAnOverflowingVideoSendsThePushedClipsFarthestFromTheDropToTheTray() {
        var state = timeline([block("a", start: 0, duration: 10), block("b", start: 10, duration: 10), block("c", start: 20, duration: 10), tray("x", duration: 10)], maxWindow: 30)
        let outcome = state.move("x", toDelta: 5)
        XCTAssertNil(outcome.refusal)
        XCTAssertEqual(outcome.sentToTray, ["c"], "c is the farthest, so it goes first and that is enough")
        XCTAssertEqual(state.placed, ["a": 15, "b": 25, "x": 5])
        XCTAssertEqual(state.span, 30)
    }

    func testAnAnchorLongerThanTheCapIsStillRefusedAndNothingChanges() {
        var state = timeline([block("a", start: 0, duration: 8), tray("x", duration: 25)], maxWindow: 20)
        let outcome = state.move("x", toDelta: 30)
        XCTAssertEqual(outcome.refusal, .tooLong(maxSeconds: 20))
        XCTAssertEqual(state.placed, ["a": 0]); XCTAssertEqual(state.tray, ["x"])
    }

    func testResetUndoesEveryPushAndTheSubmissionListsEveryTakeOnceByTime() {
        var state = timeline([block("a", start: 40, duration: 8), block("b", start: 48, duration: 8), tray("x")])
        let seed = state.placed
        state.move("x", toDelta: 44)
        XCTAssertTrue(state.isChanged)
        let submission = state.submission(includePlacements: true)
        XCTAssertEqual(submission.placements?.map(\.deltaS), submission.placements?.map(\.deltaS).sorted(), "placements are in song order")
        XCTAssertEqual(Set(submission.orderedMediaIDs), ["a", "b", "x"]); XCTAssertEqual(submission.orderedMediaIDs.count, 3)
        state.reset()
        XCTAssertEqual(state.placed, seed); XCTAssertEqual(state.tray, ["x"])
    }

    func testMovedClipsAreNamedInOneSentenceWithTheirNumbers() {
        let number: (String) -> Int = { ["a": 1, "b": 2, "c": 3, "d": 4][$0] ?? 0 }
        func outcome(_ moved: [String], tray: [String] = []) -> SongTimelineState.MoveOutcome {
            .init(moved: moved.map { .init(mediaID: $0, from: 0, to: 1) }, sentToTray: tray, refusal: nil)
        }
        XCTAssertEqual(outcome(["b"]).message(clipNumber: number), "Clip 2 moved over to make room.")
        XCTAssertEqual(outcome(["b", "c"]).message(clipNumber: number), "Clips 2 and 3 moved over to make room.")
        XCTAssertEqual(outcome(["a", "b", "d"]).message(clipNumber: number), "Clips 1, 2 and 4 moved over to make room.")
        XCTAssertEqual(outcome([], tray: ["c", "d"]).message(clipNumber: number), "Clips 3 and 4 didn't fit and went to the tray.")
        XCTAssertEqual(outcome(["a"], tray: ["d"]).message(clipNumber: number), "Clip 1 moved over to make room. Clip 4 didn't fit and went to the tray.")
        XCTAssertNil(outcome([]).message(clipNumber: number), "nothing moved: no notice")
    }

    func testPreviewingAMoveShowsTheLandingWithoutChangingTheRealArrangement() {
        let state = timeline([block("a", start: 40, duration: 20), tray("x", duration: 4)])
        let landing = state.previewing("x", toDelta: 53)
        XCTAssertEqual(landing.placed, ["a": 33, "x": 53])
        XCTAssertEqual(state.placed, ["a": 40], "the real arrangement is untouched until the drop")
    }
}
