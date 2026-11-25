import XCTest
@testable import Kria

/// The UI-test guard against Core Animation reporting one animation's start twice (KRI-1604).
/// Ways it could go wrong, written before the code:
/// 1. the second start is passed on, so the count of running animations never returns to zero (the defect);
/// 2. a second stop does follow a dropped second start, and passing it on takes the count below zero,
///    so the app is called idle while something still animates;
/// 3. an animation that stops and then starts again has its second start dropped, so a real animation is not counted;
/// 4. a stop for an animation the guard never saw start is swallowed.
final class UITestAnimationStartGuardTests: XCTestCase {
    func testASecondStartBeforeAnyStopIsNotPassedOn() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(), .pass)
        XCTAssertEqual(ledger.start(), .drop, "the second start for an animation that has not stopped")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0, "one start and one stop were passed on")
    }

    func testASecondStopAfterADroppedStartIsDroppedTooSoTheCountNeverGoesBelowZero() {
        var ledger = AnimationStartLedger()
        _ = ledger.start(); _ = ledger.start()
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.stop(), .drop, "the stop that pairs with the dropped start")
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }

    func testAnAnimationThatStopsAndStartsAgainIsPassedOnBothTimes() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(), .pass)
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.start(), .pass, "a start after a stop is a new run of the animation")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }

    func testAStopWithNoStartIsPassedOnAndCounted() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.stop(), .pass, "the guard only ever holds back what it can pair with a dropped start")
        XCTAssertEqual(ledger.unmatchedStops, 1)
    }

    func testThreeStartsInARowPassOne() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(), .pass)
        XCTAssertEqual(ledger.start(), .drop)
        XCTAssertEqual(ledger.start(), .drop)
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }
}
