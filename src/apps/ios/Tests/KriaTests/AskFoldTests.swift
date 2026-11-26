import XCTest
@testable import Kria

/// CA-1: one ask is open at a time. Failure modes pinned here: two unanswered asks both drawn; an answer to the
/// first leaving the second hidden; a re-issued ask leaving a dead card open in front of the live one; the
/// transcript's event order losing to the ask's type; the same answer event opening the next ask twice; a
/// choice-plus-clips message split across two turns; a text-only ask (capability off) blocking every later card;
/// a free-text reply closing an ask the creator never saw; a pending send reading as answered.
final class AskFoldTests: XCTestCase {
    private typealias Entry = AskFold.Entry

    private func ask(_ message: String, _ kind: AskKind, _ id: String, closer: AskFold.Closer? = nil) -> Entry {
        Entry(messageID: message, asks: [Self.card(kind, id, closer)])
    }

    private static func card(_ kind: AskKind, _ id: String, _ closer: AskFold.Closer? = nil) -> AskFold.Ask {
        AskFold.Ask(id: id, kind: kind, closer: closer ?? (kind == .choice ? .confirmedReply : .answer(id)))
    }

    private func answer(_ message: String, _ kind: AskKind, _ id: String, pending: Bool = false) -> Entry {
        Entry(messageID: message, isUser: true, isPending: pending, answers: [AskFold.AnswerKey(kind: kind, value: id)])
    }

    private func text(_ message: String, pending: Bool = false) -> Entry {
        Entry(messageID: message, isUser: true, isPending: pending)
    }

    private func standing(_ fold: AskFold, _ message: String, _ id: String) -> AskFold.Standing? { fold.standing(message, id) }

    func testEmptyTranscriptHasNoOpenAsk() {
        let fold = AskFold.fold([])
        XCTAssertNil(fold.openMessageID)
        XCTAssertNil(fold.standing("m1", "q1"))
        XCTAssertFalse(fold.holdsRow("m1"))
    }

    func testTwoUnansweredAsksOpenOnlyTheFirst() {
        let fold = AskFold.fold([ask("m1", .choice, "c1"), ask("m2", .moment, "q2")])
        XCTAssertEqual(fold.openMessageID, "m1")
        XCTAssertEqual(standing(fold, "m1", "c1"), .open)
        XCTAssertEqual(standing(fold, "m2", "q2"), .queued)
        XCTAssertTrue(fold.holdsRow("m2"))
        XCTAssertFalse(fold.holdsRow("m1"))
    }

    func testAnsweringTheFirstOpensTheNext() {
        let fold = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .choice, "c2"), answer("u1", .moment, "q1")])
        XCTAssertEqual(standing(fold, "m1", "q1"), .answered)
        XCTAssertEqual(fold.answeredBy("m1", "q1"), "u1")
        XCTAssertEqual(standing(fold, "m2", "c2"), .open)
        XCTAssertEqual(fold.openMessageID, "m2")
        XCTAssertFalse(fold.holdsRow("m2"))
    }

    func testSameQuestionIDReissueReplacesTheOpenAsk() {
        let fold = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .moment, "q1")])
        XCTAssertEqual(standing(fold, "m1", "q1"), .replaced)
        XCTAssertEqual(fold.openMessageID, "m2")
        XCTAssertEqual(standing(fold, "m2", "q1"), .open)
    }

    func testAReissueUnderANewIDOfTheSameKindReplacesTheDeadAskAndKeepsItsPlace() {
        let fold = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .choice, "c2"), ask("m3", .moment, "q3")])
        XCTAssertEqual(standing(fold, "m1", "q1"), .replaced)
        XCTAssertEqual(fold.openMessageID, "m3", "the live moment ask sits where the dead one did, ahead of the choice")
        XCTAssertEqual(standing(fold, "m2", "c2"), .queued)
    }

    func testAnAnsweredAskIsNotReplacedByALaterOneOfItsKind() {
        let fold = AskFold.fold([ask("m1", .moment, "q1"), answer("u1", .moment, "q1"), ask("m2", .moment, "q2")])
        XCTAssertEqual(standing(fold, "m1", "q1"), .answered)
        XCTAssertEqual(standing(fold, "m2", "q2"), .open)
    }

    func testEventOrderWinsOverType() {
        let moments = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .choice, "c1")])
        XCTAssertEqual(moments.openMessageID, "m1")
        let choices = AskFold.fold([ask("m1", .choice, "c1"), ask("m2", .moment, "q1")])
        XCTAssertEqual(choices.openMessageID, "m1")
        let reversed = AskFold.fold([ask("m1", .songRange, "r1"), ask("m2", .clip, "k1"), ask("m3", .region, "p1")])
        XCTAssertEqual(reversed.openMessageID, "m1")
        XCTAssertEqual(standing(reversed, "m3", "p1"), .queued)
    }

    func testADuplicateAnswerEventOpensTheNextAskOnce() {
        let once = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .moment, "q2"), answer("u1", .moment, "q1")])
        XCTAssertEqual(standing(once, "m2", "q2"), .open)
        let twice = AskFold.fold([ask("m1", .songOrder, "q1"), ask("m2", .choice, "c2"),
                                  answer("u1", .songOrder, "q1"), answer("u2", .songOrder, "q1")])
        XCTAssertEqual(twice.answeredBy("m1", "q1"), "u1", "the first answer stands")
        XCTAssertEqual(standing(twice, "m2", "c2"), .open)
        XCTAssertEqual(twice.openMessageID, "m2")
    }

    func testAnAnswerBeforeTheAskOrForAnotherKindOrIDDoesNotClose() {
        XCTAssertEqual(AskFold.fold([answer("u1", .moment, "q1"), ask("m1", .moment, "q1")]).standing("m1", "q1"), .open)
        XCTAssertEqual(AskFold.fold([ask("m1", .moment, "q1"), answer("u1", .songOrder, "q1")]).standing("m1", "q1"), .open)
        XCTAssertEqual(AskFold.fold([ask("m1", .moment, "q1"), answer("u1", .moment, "other")]).standing("m1", "q1"), .open)
        XCTAssertEqual(AskFold.fold([ask("m1", .moment, "q1"), text("u1")]).standing("m1", "q1"), .open, "free text never closes a structured ask")
    }

    func testOneMessageCarryingTwoAsksOpensBothTogether() {
        let both = Entry(messageID: "m1", asks: [Self.card(.choice, "c1", .anyReply), Self.card(.clip, "k1", .anyReply)])
        let fold = AskFold.fold([both, ask("m2", .moment, "q2")])
        XCTAssertEqual(standing(fold, "m1", "c1"), .open)
        XCTAssertEqual(standing(fold, "m1", "k1"), .open)
        XCTAssertEqual(standing(fold, "m2", "q2"), .queued)
        let answered = AskFold.fold([both, ask("m2", .moment, "q2"), text("u1")])
        XCTAssertEqual(standing(answered, "m1", "c1"), .answered)
        XCTAssertEqual(standing(answered, "m1", "k1"), .answered)
        XCTAssertEqual(standing(answered, "m2", "q2"), .open)
    }

    func testAFreeTextReplyClosesOnlyTheAskTheCreatorCanSee() {
        let behindAMoment = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .choice, "c2"), text("u1")])
        XCTAssertEqual(standing(behindAMoment, "m1", "q1"), .open)
        XCTAssertEqual(standing(behindAMoment, "m2", "c2"), .queued, "an ask that was never drawn is not answered by chatting")
        let open = AskFold.fold([ask("m1", .choice, "c1"), ask("m2", .moment, "q2"), text("u1")])
        XCTAssertEqual(standing(open, "m1", "c1"), .answered)
        XCTAssertEqual(open.answeredBy("m1", "c1"), "u1")
        XCTAssertEqual(standing(open, "m2", "q2"), .open)
    }

    func testAPendingReplyKeepsAChoiceOpenButAStructuredAnswerClosesAtOnce() {
        XCTAssertEqual(AskFold.fold([ask("m1", .choice, "c1"), text("u1", pending: true)]).standing("m1", "c1"), .open)
        XCTAssertEqual(AskFold.fold([ask("m1", .choice, "c1"), text("u1")]).standing("m1", "c1"), .answered)
        let clip = ask("m1", .clip, "k1", closer: .anyReply)
        XCTAssertEqual(AskFold.fold([clip, text("u1", pending: true)]).standing("m1", "k1"), .answered)
        XCTAssertEqual(AskFold.fold([ask("m1", .moment, "q1"), answer("u1", .moment, "q1", pending: true)]).standing("m1", "q1"), .answered)
    }

    func testTheServerClosingAnAskFreesTheQueueButNeverUnAnswersOne() {
        let asks = [ask("m1", .songRange, "r1"), ask("m2", .choice, "c2")]
        let closed = AskFold.fold(asks, closed: ["r1"])
        XCTAssertEqual(standing(closed, "m1", "r1"), .stale)
        XCTAssertEqual(standing(closed, "m2", "c2"), .open)
        let answered = AskFold.fold([asks[0], answer("u1", .songRange, "r1")], closed: ["r1"])
        XCTAssertEqual(standing(answered, "m1", "r1"), .answered)
        XCTAssertEqual(standing(AskFold.fold(asks, closed: ["other"]), "m1", "r1"), .open)
    }

    func testAMessageWithNoAskIsNeverHeld() {
        let fold = AskFold.fold([Entry(messageID: "m0"), ask("m1", .choice, "c1"), Entry(messageID: "m2")])
        XCTAssertFalse(fold.holdsRow("m0"))
        XCTAssertFalse(fold.holdsRow("m2"))
    }

    // MARK: the transcript's own messages feed the fold

    private func event(_ id: String, role: String, type: String, payload: [String: JSONValue]) -> ChatTranscriptMessage {
        let event = ThreadEvent(id: id, sequence: 1, revision: 1, role: role, eventType: type, content: "Which part of the song did you film?", payload: payload, createdAt: .now)
        return ChatTranscriptMessage.from(event: event)!
    }

    private func songRange(_ id: String) -> [String: JSONValue] { ["song_range_question": .object(["question_id": .string(id)])] }

    private func choice(_ id: String) -> [String: JSONValue] {
        func option(_ key: String) -> JSONValue { .object(["key": .string(key), "label": .string(key)]) }
        return ["choice_question": .object(["version": .number(1), "question_id": .string(id), "options": .array([option("a"), option("b")])])]
    }

    func testAChoiceThenAnUnansweredSongRangeReadsOneCardThenTheNext() {
        let kinds: Set<AskKind> = [.choice, .songRange]
        var messages = [event("a1", role: "assistant", type: "assistant_question", payload: choice("c1")),
                        event("a2", role: "assistant", type: "assistant_question", payload: songRange("r1"))]
        var fold = AskFold.fold(messages.map { $0.askEntry(enabled: kinds) })
        XCTAssertEqual(fold.standing("a1", "c1"), .open)
        XCTAssertEqual(fold.standing("a2", "r1"), .queued)
        messages.append(event("u1", role: "user", type: "user_message", payload: [:]))
        fold = AskFold.fold(messages.map { $0.askEntry(enabled: kinds) })
        XCTAssertEqual(fold.standing("a1", "c1"), .answered)
        XCTAssertEqual(fold.standing("a2", "r1"), .open)
        let range: [String: JSONValue] = ["song_range": .object(["question_id": .string("r1"), "start_s": .null, "end_s": .null])]
        messages.append(event("u2", role: "user", type: "user_message", payload: range))
        fold = AskFold.fold(messages.map { $0.askEntry(enabled: kinds) })
        XCTAssertEqual(fold.standing("a2", "r1"), .answered)
        XCTAssertEqual(fold.answeredBy("a2", "r1"), "u2")
        XCTAssertNil(fold.openMessageID)
    }

    func testAnAskTheServerDoesNotOfferNeverBlocksTheQueue() {
        let messages = [event("a1", role: "assistant", type: "assistant_question", payload: songRange("r1")),
                        event("a2", role: "assistant", type: "assistant_question", payload: choice("c1"))]
        let fold = AskFold.fold(messages.map { $0.askEntry(enabled: [.choice]) })
        XCTAssertNil(fold.standing("a1", "r1"), "text only: no card, no hold")
        XCTAssertEqual(fold.standing("a2", "c1"), .open)
    }

    func testRegionAnswersMatchTheLabelIgnoringCaseAndSpacing() {
        XCTAssertEqual(ProtectedRegionAnswer.labelKey("  The   Plate "), ProtectedRegionAnswer.labelKey("the plate"))
        XCTAssertNotEqual(ProtectedRegionAnswer.labelKey("the plate"), ProtectedRegionAnswer.labelKey("the cup"))
    }

    // MARK: cost

    func testFoldingAFourHundredEventTranscript() {
        var entries: [Entry] = []
        for index in 0..<100 {
            entries.append(ask("a\(index)", AskKind.allCases[index % AskKind.allCases.count], "q\(index)"))
            entries.append(text("u\(index)"))
            entries.append(Entry(messageID: "p\(index)"))
            entries.append(answer("r\(index)", AskKind.allCases[index % AskKind.allCases.count], "q\(index)"))
        }
        XCTAssertEqual(entries.count, 400)
        measure { _ = AskFold.fold(entries) }
    }
}
