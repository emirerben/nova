import XCTest
@testable import Kria

/// KRI-253: dragging the editor panel down past its smallest size closes it.
final class NativeEditorPanelDismissRuleTests: XCTestCase {
    typealias Rule = NativeEditorPanelDismissRule

    func testPullCountsOnlyTravelBelowTheSmallestSize() {
        XCTAssertEqual(Rule.pull(translation: 100, aboveMinimum: 160), 0, "collapsing a raised panel is not a pull")
        XCTAssertEqual(Rule.pull(translation: 200, aboveMinimum: 160), 40)
        XCTAssertEqual(Rule.pull(translation: 50, aboveMinimum: 0), 50)
        XCTAssertEqual(Rule.pull(translation: -80, aboveMinimum: 0), 0, "dragging up never pulls")
    }

    func testReleaseFarEnoughBelowCloses() {
        XCTAssertFalse(Rule.shouldDismiss(pull: Rule.distance - 1, projectedPull: Rule.distance - 1, startedAtMinimum: true))
        XCTAssertTrue(Rule.shouldDismiss(pull: Rule.distance, projectedPull: Rule.distance, startedAtMinimum: true))
        XCTAssertTrue(Rule.shouldDismiss(pull: Rule.distance, projectedPull: Rule.distance, startedAtMinimum: false))
    }

    func testFlickClosesOnlyFromTheSmallestSize() {
        XCTAssertTrue(Rule.shouldDismiss(pull: 20, projectedPull: 200, startedAtMinimum: true))
        XCTAssertFalse(Rule.shouldDismiss(pull: 20, projectedPull: 200, startedAtMinimum: false),
                       "momentum while collapsing a raised panel must not close it")
        XCTAssertFalse(Rule.shouldDismiss(pull: Rule.flickMinimum - 1, projectedPull: 400, startedAtMinimum: true),
                       "a twitch is not a flick")
        XCTAssertFalse(Rule.shouldDismiss(pull: 30, projectedPull: Rule.flickProjection - 1, startedAtMinimum: true))
    }

    func testPanelFollowsThenResists() {
        XCTAssertEqual(Rule.offset(forPull: 0), 0)
        XCTAssertEqual(Rule.offset(forPull: -20), 0)
        let near = Rule.offset(forPull: 10), armed = Rule.offset(forPull: Rule.distance), far = Rule.offset(forPull: 1_000)
        XCTAssertGreaterThan(near, 8, "small pulls track the finger closely")
        XCTAssertLessThan(near, armed)
        XCTAssertLessThan(armed, far)
        XCTAssertLessThan(far, Rule.maxOffset)
    }
}
