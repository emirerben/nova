import XCTest
@testable import Kria

/// KRI-1341: the analysis task finishes without bumping the thread revision or appending an event,
/// so only a periodic full read resolves "Checking speech…". Failure modes this pins:
/// the card never resolves; a refetch storm on every 1s poll; the deadline firing early or
/// restarting mid-check; a suspended app burning the deadline; a late result being ignored.
final class SpeechCheckWatchTests: XCTestCase {
    private let start = Date(timeIntervalSinceReferenceDate: 1_000)

    /// Runs the watch once per second the way the poll loop does, returning the offsets (seconds
    /// since `start`) at which it asked for a full read.
    private func refetchTimes(
        watch: inout SpeechCheckWatch,
        offer: SpeechCleanupOffer = .checking,
        analysisID: String? = "a1",
        seconds: ClosedRange<Int>
    ) -> [Int] {
        seconds.filter { watch.observe(offer, analysisID: analysisID, now: start.addingTimeInterval(TimeInterval($0))) }
    }

    func testRefetchesFullThreadEveryThreeSecondsNotEveryPoll() {
        var watch = SpeechCheckWatch()
        XCTAssertEqual(refetchTimes(watch: &watch, seconds: 0...12), [3, 6, 9, 12])
    }

    func testFirstRefetchWaitsOneIntervalAfterCheckingBegan() {
        var watch = SpeechCheckWatch()
        XCTAssertEqual(refetchTimes(watch: &watch, seconds: 0...2), [], "the read that showed the spinner is the baseline")
    }

    func testStopsAskingOnceTheAnalysisResolves() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, seconds: 0...4)
        for offer in [SpeechCleanupOffer.plain, .failed, .noSpeech, .choice(SpeechCleanupStats(candidateCount: 1))] {
            XCTAssertFalse(watch.observe(offer, analysisID: "a1", now: start.addingTimeInterval(30)), "\(offer)")
        }
    }

    func testTimesOutAtNinetySecondsAndNotBefore() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, seconds: 0...89)
        XCTAssertFalse(watch.hasTimedOut)
        _ = refetchTimes(watch: &watch, seconds: 90...90)
        XCTAssertTrue(watch.hasTimedOut)
    }

    func testKeepsRefetchingSlowlyAfterTimeoutSoALateResultStillResolvesTheCard() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, seconds: 0...90)
        let late = refetchTimes(watch: &watch, seconds: 91...140)
        XCTAssertEqual(late.count, 3)
        XCTAssertTrue(zip(late, late.dropFirst()).allSatisfy { $1 - $0 == 15 }, "\(late)")
        XCTAssertTrue(watch.hasTimedOut, "no flicker back to the spinner while still checking")
    }

    func testResolvingClearsTheTimeoutSoANewCheckStartsFresh() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, seconds: 0...100)
        XCTAssertTrue(watch.hasTimedOut)
        _ = watch.observe(.failed, analysisID: "a1", now: start.addingTimeInterval(101))
        XCTAssertFalse(watch.hasTimedOut)
        _ = watch.observe(.checking, analysisID: "a1", now: start.addingTimeInterval(102))
        XCTAssertFalse(watch.hasTimedOut)
    }

    func testANewAnalysisRestartsTheDeadline() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, seconds: 0...100)
        XCTAssertTrue(watch.hasTimedOut)
        _ = watch.observe(.checking, analysisID: "a2", now: start.addingTimeInterval(101))
        XCTAssertFalse(watch.hasTimedOut, "a superseded analysis must not inherit the old deadline")
    }

    func testAnAnalysisAppearingAfterNoneRestartsTheDeadline() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, analysisID: nil, seconds: 0...80)
        _ = watch.observe(.checking, analysisID: "a1", now: start.addingTimeInterval(81))
        _ = refetchTimes(watch: &watch, seconds: 82...100)
        XCTAssertFalse(watch.hasTimedOut)
    }

    func testASuspendedAppDoesNotBurnTheDeadlineButRefetchesImmediatelyOnReturn() {
        var watch = SpeechCheckWatch()
        _ = refetchTimes(watch: &watch, seconds: 0...10)
        let back = start.addingTimeInterval(10 + 600)
        XCTAssertTrue(watch.observe(.checking, analysisID: "a1", now: back), "the result may have landed while suspended")
        XCTAssertFalse(watch.hasTimedOut, "ten minutes in the background is not ten minutes of watching a spinner")
    }
}
