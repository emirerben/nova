import XCTest
@testable import Kria

/// The UI-test guard against Core Animation reporting one animation's start twice (KRI-1604).
/// Ways it could go wrong, written before the code:
/// 1. the second start is passed on, so the count of running animations never returns to zero (the defect);
/// 2. a second stop does follow a dropped second start, and passing it on takes the count below zero,
///    so the app is called idle while something still animates;
/// 3. an animation that stops and then starts again has its second start dropped, so a real animation is not counted;
/// 4. a stop for an animation the guard never saw start is swallowed;
/// 5. a second start on a layer that is still there is dropped. UIKit re-aims a sheet's animations that way and
///    stops each twice (270 of 270 on a hosted Mac), so dropping one would call the app idle at the first stop,
///    while the sheet still moves. Only a second start whose layer is already freed is the defect (3 of 3 leaks).
final class UITestAnimationStartGuardTests: XCTestCase {
    func testASecondStartBeforeAnyStopIsNotPassedOn() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(layerIsGone: true), .pass)
        XCTAssertEqual(ledger.start(layerIsGone: true), .drop, "the second start for an animation that has not stopped")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0, "one start and one stop were passed on")
    }

    func testASecondStopAfterADroppedStartIsDroppedTooSoTheCountNeverGoesBelowZero() {
        var ledger = AnimationStartLedger()
        _ = ledger.start(layerIsGone: true); _ = ledger.start(layerIsGone: true)
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.stop(), .drop, "the stop that pairs with the dropped start")
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }

    func testAnAnimationThatStopsAndStartsAgainIsPassedOnBothTimes() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(layerIsGone: true), .pass)
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.start(layerIsGone: true), .pass, "a start after a stop is a new run of the animation")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }

    func testAStopWithNoStartIsPassedOnAndCounted() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.stop(), .pass, "the guard only ever holds back what it can pair with a dropped start")
        XCTAssertEqual(ledger.unmatchedStops, 1)
    }

    func testASecondStartOnALayerThatIsStillThereIsPassedOnWithBothItsStops() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(layerIsGone: false), .pass)
        XCTAssertEqual(ledger.start(layerIsGone: false), .pass, "a sheet being re-aimed starts its animation again")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 1, "still animating after the first stop")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }

    func testAFirstStartIsPassedOnEvenWhenItsLayerIsAlreadyGone() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(layerIsGone: true), .pass, "only a second start is ever dropped")
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }

    func testThreeStartsInARowPassOne() {
        var ledger = AnimationStartLedger()
        XCTAssertEqual(ledger.start(layerIsGone: true), .pass)
        XCTAssertEqual(ledger.start(layerIsGone: true), .drop)
        XCTAssertEqual(ledger.start(layerIsGone: true), .drop)
        XCTAssertEqual(ledger.stop(), .pass)
        XCTAssertEqual(ledger.passedOnBalance, 0)
    }
}
