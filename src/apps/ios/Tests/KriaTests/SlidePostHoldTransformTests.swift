import XCTest
@testable import Kria

// Hold-to-transform: tap vs hold vs drag vs hold-then-drag vs pinch, and the routing that keeps
// hold-select from opening the Text panel / keyboard. Failure modes first.

final class SlidePostTouchResolverTests: XCTestCase {
    private let p0 = CGPoint(x: 100, y: 100)
    private func down(allowed: Bool = true, onTarget: Bool = true) -> SlidePostTouchResolver {
        var r = SlidePostTouchResolver()
        r.touchDown(at: p0, time: 10, onTarget: onTarget, holdAllowed: allowed)
        return r
    }

    func testSwipeOverEmptyCanvasIsNothing() {
        var r = down(onTarget: false)
        XCTAssertEqual(r.moved(to: CGPoint(x: 160, y: 100)), .none)
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 160, y: 100)), .none)
    }

    func testHoldNeverFiresOverEmptyCanvas() {
        var r = down(onTarget: false)
        XCTAssertEqual(r.holdTimerFired(at: 11), .none)
        XCTAssertEqual(r.touchUp(at: p0), .tap, "an empty-canvas tap still reaches the deselect path")
    }

    func testHoldNeverFiresWhenNotAllowed() {
        var r = down(allowed: false)
        XCTAssertEqual(r.holdTimerFired(at: 12), .none)
        XCTAssertEqual(r.touchUp(at: p0), .tap)
    }

    func testHoldDoesNotFireBeforeTheDuration() {
        var r = down()
        XCTAssertEqual(r.holdTimerFired(at: 10.1), .none)
        XCTAssertEqual(r.holdTimerFired(at: 10.3), .beginHold)
    }

    func testHoldCannotFireAfterTravelPastSlop() {
        var r = down()
        _ = r.moved(to: CGPoint(x: 130, y: 100))
        XCTAssertEqual(r.holdTimerFired(at: 10.4), .none)
    }

    func testLiftAfterHoldIsNotATap() {
        var r = down()
        _ = r.holdTimerFired(at: 10.4)
        XCTAssertEqual(r.touchUp(at: p0), .endHold, "hold-select must not open the editor")
    }

    func testQuickLiftIsATapAndMovementInsideSlopStillTaps() {
        var r = down()
        XCTAssertEqual(r.moved(to: CGPoint(x: 103, y: 103)), .none)
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 103, y: 103)), .tap)
        XCTAssertTrue(r.isIdle)
    }

    func testPlainDragMovesImmediatelyWithoutHold() {
        var r = down()
        XCTAssertEqual(r.moved(to: CGPoint(x: 120, y: 100)), .beginDrag(fromHold: false))
        XCTAssertEqual(r.holdTimerFired(at: 11), .none, "a late hold timer is inert once dragging")
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 120, y: 100)), .endDrag)
    }

    func testHoldThenDragContinuesTheSameGesture() {
        var r = down()
        _ = r.holdTimerFired(at: 10.4)
        XCTAssertEqual(r.moved(to: CGPoint(x: 103, y: 100)), .none, "jitter inside slop stays held")
        XCTAssertEqual(r.moved(to: CGPoint(x: 140, y: 120)), .beginDrag(fromHold: true))
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 140, y: 120)), .endDrag)
    }

    func testPinchTakeoverSuppressesTapAndHold() {
        var r = down()
        r.cancel()
        XCTAssertEqual(r.holdTimerFired(at: 10.4), .none)
        XCTAssertEqual(r.touchUp(at: p0), .none)
    }

    func testResolverIsReusableAfterEnd() {
        var r = down()
        _ = r.touchUp(at: p0)
        r.touchDown(at: p0, time: 20, onTarget: true, holdAllowed: true)
        XCTAssertEqual(r.holdTimerFired(at: 20.3), .beginHold)
    }
}

final class SlidePostDirectSelectionRoutingTests: XCTestCase {
    func testTapOnEmptyCanvasDeselectsAHoldSelectedText() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: nil, panelOpen: false, selectedID: "a", onEditTab: true, keyboardUp: false, directSelected: true))
        XCTAssertNil(outcome.selectID)
        XCTAssertFalse(outcome.opensPanel)
        XCTAssertFalse(outcome.focusesField)
    }

    func testTapOnHoldSelectedTextOpensEditTextWithKeyboard() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: "a", panelOpen: false, selectedID: "a", onEditTab: false, keyboardUp: false, directSelected: true))
        XCTAssertEqual(outcome, .init(selectID: "a", opensPanel: true, showsEditTab: true, focusesField: true))
    }

    func testEmptyTapInPlainBrowseStillDoesNothing() {
        XCTAssertNil(SlidePostTextTap.resolve(hit: nil, panelOpen: false, selectedID: nil, onEditTab: true, keyboardUp: false, directSelected: false))
    }

    func testHitShapeCoversRotatedTextOnlyOrWholeStage() {
        let bounds = CGRect(x: 0, y: 0, width: 400, height: 700)
        let textRect = CGRect(x: 100, y: 300, width: 200, height: 40)
        let union = SlidePostTextHitShape(rects: [(textRect, 0)]).path(in: bounds)
        XCTAssertTrue(union.contains(CGPoint(x: 200, y: 320)))
        XCTAssertFalse(union.contains(CGPoint(x: 20, y: 20)), "empty canvas stays untouched while browsing")
        let rotated = SlidePostTextHitShape(rects: [(textRect, 90)]).path(in: bounds)
        XCTAssertTrue(rotated.contains(CGPoint(x: 200, y: 400)), "rotated 90 degrees about its centre")
        XCTAssertFalse(rotated.contains(CGPoint(x: 120, y: 320)))
        XCTAssertTrue(SlidePostTextHitShape(rects: nil).path(in: bounds).contains(CGPoint(x: 20, y: 20)))
    }
}
