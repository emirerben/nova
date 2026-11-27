import XCTest
@testable import Kria

/// CA-2: every card draws one `AskPhase`. Failure modes pinned here: a queued, replaced or closed ask drawing a card;
/// an answered ask drawing without its receipt text, or a second answered event drawing a second receipt; a refusal
/// for one question leaking onto another; a stale notice hiding the refusal the creator has to fix; a clip-order
/// send that failed reading as open (no failure line, no Try again), one in flight reading as confirmed, and a
/// confirmed order reading as open.
final class AskPhaseTests: XCTestCase {
    private func ask(_ message: String, _ kind: AskKind, _ id: String) -> AskFold.Entry {
        AskFold.Entry(messageID: message, asks: [AskFold.Ask(id: id, kind: kind, closer: .answer(id))])
    }

    private func answer(_ message: String, _ kind: AskKind, _ id: String) -> AskFold.Entry {
        AskFold.Entry(messageID: message, isUser: true, answers: [AskFold.AnswerKey(kind: kind, value: id)])
    }

    private func phase(_ fold: AskFold, _ message: String, _ id: String, answer: String? = "Group first",
                       failure: String? = nil, isStale: Bool = false) -> AskPhase<String>? {
        fold.phase(message, id, answer: { answer }, failure: failure, isStale: isStale)
    }

    func testTheOpenAskIsOpen() {
        let fold = AskFold.fold([ask("m1", .moment, "q1")])
        XCTAssertEqual(phase(fold, "m1", "q1"), .open)
    }

    func testAQueuedAskDrawsNothing() {
        let fold = AskFold.fold([ask("m1", .choice, "c1"), ask("m2", .moment, "q2")])
        XCTAssertNil(phase(fold, "m2", "q2"))
    }

    func testAReplacedAskDrawsNothing() {
        let fold = AskFold.fold([ask("m1", .moment, "q1"), ask("m2", .moment, "q2")])
        XCTAssertNil(phase(fold, "m1", "q1"))
        XCTAssertEqual(phase(fold, "m2", "q2"), .open)
    }

    func testAnAskTheServerClosedDrawsNothing() {
        let fold = AskFold.fold([ask("m1", .songRange, "r1")], closed: ["r1"])
        XCTAssertNil(phase(fold, "m1", "r1"))
    }

    func testAnUnknownAskDrawsNothing() {
        XCTAssertNil(phase(AskFold.fold([]), "m1", "q1"))
    }

    func testAnAnsweredAskIsItsReceipt() {
        let fold = AskFold.fold([ask("m1", .choice, "c1"), answer("u1", .choice, "c1")])
        XCTAssertEqual(phase(fold, "m1", "c1", answer: "Group first"), .answered("Group first"))
    }

    func testAnAnsweredAskWhoseAnswerIsUnknownDrawsNothing() {
        let fold = AskFold.fold([ask("m1", .songOrder, "s1"), answer("u1", .songOrder, "s1")])
        XCTAssertNil(phase(fold, "m1", "s1", answer: nil))
    }

    func testTheSameAnswerTwiceStaysOneReceipt() {
        let fold = AskFold.fold([ask("m1", .choice, "c1"), answer("u1", .choice, "c1"), answer("u2", .choice, "c1")])
        XCTAssertEqual(phase(fold, "m1", "c1"), .answered("Group first"))
        XCTAssertEqual(fold.openMessageID, nil)
    }

    func testARefusalOnTheOpenAskIsFailed() {
        let fold = AskFold.fold([ask("m1", .moment, "q1")])
        XCTAssertEqual(phase(fold, "m1", "q1", failure: "Two of your moments overlap. Move one of them."),
                       .failed("Two of your moments overlap. Move one of them."))
    }

    func testARefusalNeverDrawsOnAnAnsweredCard() {
        let fold = AskFold.fold([ask("m1", .moment, "q1"), answer("u1", .moment, "q1")])
        XCTAssertEqual(phase(fold, "m1", "q1", failure: "Two of your moments overlap."), .answered("Group first"))
    }

    func testAStaleNoticeKeepsTheCardOpen() {
        let fold = AskFold.fold([ask("m2", .moment, "q2")])
        XCTAssertEqual(phase(fold, "m2", "q2", isStale: true), .stale)
    }

    func testARefusalWinsOverAStaleNotice() {
        let fold = AskFold.fold([ask("m2", .moment, "q2")])
        XCTAssertEqual(phase(fold, "m2", "q2", failure: "Two of your moments overlap.", isStale: true), .failed("Two of your moments overlap."))
    }

    func testAStaleNoticeNeverDrawsOnAQueuedAsk() {
        let fold = AskFold.fold([ask("m1", .choice, "c1"), ask("m2", .moment, "q2")])
        XCTAssertNil(phase(fold, "m2", "q2", isStale: true))
    }

    // MARK: clip order

    private let order = ["a", "b", "c"]
    private let failure = "Kria couldn’t save this order. Try again."

    private func clipPhase(_ status: ClipOrderCoordinator.Status, confirmed: Bool = false) -> AskPhase<[String]> {
        ClipOrderCoordinator.phase(status: status, confirmed: confirmed, order: order, failure: failure)
    }

    func testAClipOrderNobodyConfirmedIsOpen() {
        XCTAssertEqual(clipPhase(.idle), .open)
    }

    func testAClipOrderBeingSentIsSubmittingTheOrderTheCreatorSees() {
        XCTAssertEqual(clipPhase(.sending), .submitting(order))
        XCTAssertEqual(clipPhase(.sending, confirmed: true), .submitting(order), "a send in flight is never already confirmed")
    }

    func testAClipOrderThatFailedToSendKeepsItsOrderAndSaysSo() {
        XCTAssertEqual(clipPhase(.failed), .failed(failure))
        XCTAssertEqual(clipPhase(.failed, confirmed: true), .failed(failure))
    }

    func testAConfirmedClipOrderIsAnswered() {
        XCTAssertEqual(clipPhase(.idle, confirmed: true), .answered(order))
    }
}
