import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-637 (device-test feedback): the drawing Animation tab's speed runs slow -> fast left to right, and each
/// Entrance / Exit tile plays the motion it promises.
final class NativeDrawingAnimationTests: XCTestCase {
    // MARK: Speed

    func testSlidingRightIsFasterAndLeftIsSlower() {
        let range = NativeDrawingSpeed.bounds(window: 10)
        var previous = Double.infinity
        for step in 0...10 {
            let position = range.lowerBound + (range.upperBound - range.lowerBound) * Double(step) / 10
            let seconds = NativeDrawingSpeed.seconds(position: position)
            XCTAssertLessThan(seconds, previous, "a higher slider position must draw faster (shorter) at step \(step)")
            XCTAssertGreaterThan(NativeDrawingSpeed.multiplier(position: position), 0)
            previous = seconds
        }
        XCTAssertEqual(NativeDrawingSpeed.seconds(position: range.upperBound), 0.2, accuracy: 0.001, "the right end is the shortest draw the wire allows")
    }

    func testTheDefaultDrawTimeIsTheSliderMiddleAndOneTimes() {
        XCTAssertEqual(NativeDrawingSpeed.position(seconds: 1.2, window: 3), 0, accuracy: 1e-9)
        XCTAssertEqual(NativeDrawingSpeed.multiplier(position: 0), 1, accuracy: 1e-9)
        XCTAssertEqual(NativeDrawingSpeed.seconds(position: 0), 1.2, accuracy: 1e-9)
    }

    func testPositionAndSecondsRoundTripAndStayInsideTheBounds() {
        for seconds in [0.2, 0.35, 0.8, 1.2, 2.5, 3.0] {
            let position = NativeDrawingSpeed.position(seconds: seconds, window: 3)
            XCTAssertEqual(NativeDrawingSpeed.seconds(position: position), seconds, accuracy: 0.0001, "round trip \(seconds)")
            XCTAssertTrue(NativeDrawingSpeed.bounds(window: 3).contains(position))
        }
        // A stored time the window cannot hold, or garbage, clamps instead of escaping the slider.
        let range = NativeDrawingSpeed.bounds(window: 3)
        XCTAssertEqual(NativeDrawingSpeed.position(seconds: 9, window: 3), range.lowerBound, accuracy: 1e-9)
        XCTAssertEqual(NativeDrawingSpeed.position(seconds: 0.001, window: 3), range.upperBound, accuracy: 1e-9)
        XCTAssertEqual(NativeDrawingSpeed.position(seconds: .nan, window: 3), 0)
    }

    func testTheSlowestDrawNeverOutlastsTheDrawingsWindow() {
        XCTAssertEqual(NativeDrawingSpeed.seconds(position: NativeDrawingSpeed.bounds(window: 3).lowerBound), 3, accuracy: 0.0001)
        XCTAssertEqual(NativeDrawingSpeed.seconds(position: NativeDrawingSpeed.bounds(window: 60).lowerBound), DrawingLimits.maxDrawDuration, accuracy: 0.0001,
                       "and never longer than the wire allows")
        XCTAssertTrue(NativeDrawingSpeed.bounds(window: 0.1).lowerBound <= NativeDrawingSpeed.bounds(window: 0.1).upperBound, "a tiny window still has a valid slider")
    }

    func testTheSliderTicksOncePerNotchAndFirmerOnTheDefault() {
        XCTAssertTrue(NativeDrawingSpeed.tick(position: 0).isDefault, "1x is the firm detent")
        XCTAssertTrue(NativeDrawingSpeed.tick(position: NativeDrawingSpeed.notchSize * 0.4).isDefault, "and so is the whole notch around it")
        XCTAssertFalse(NativeDrawingSpeed.tick(position: NativeDrawingSpeed.notchSize * 0.7).isDefault)
        // Dragging across the whole slider crosses every notch exactly once and in order (one tick each).
        let range = NativeDrawingSpeed.bounds(window: 10)
        var notches = [Int]()
        var position = range.lowerBound
        while position <= range.upperBound {
            let notch = NativeDrawingSpeed.tick(position: position).notch
            if notches.last != notch { notches.append(notch) }
            position += 0.01
        }
        XCTAssertEqual(notches, notches.sorted(), "notches only ever move one way while sliding right")
        XCTAssertEqual(Set(notches).count, notches.count, "and none repeats")
        XCTAssertGreaterThan(notches.count, 8, "many ticks across the slider, not just one at the end")
    }

    // MARK: Previews

    private typealias Math = NativeDrawingAnimationPreviewMath

    func testTheDrawTileDrawsTheStrokeOnThenHoldsThenStartsOver() {
        let edge = Math.edgeSeconds(.draw, drawSeconds: 1.2)
        XCTAssertEqual(edge, 1.2, accuracy: 1e-9)
        XCTAssertEqual(Math.entrance(.draw, drawSeconds: 1.2, clock: 0).progress, 0, accuracy: 1e-9)
        XCTAssertEqual(Math.entrance(.draw, drawSeconds: 1.2, clock: edge / 2).progress, 0.5, accuracy: 1e-6)
        XCTAssertEqual(Math.entrance(.draw, drawSeconds: 1.2, clock: edge + 0.1), Math.Sample(progress: 1, alpha: 1), "drawn and held")
        let cycle = edge + Math.hold + Math.resetGap
        XCTAssertEqual(Math.entrance(.draw, drawSeconds: 1.2, clock: cycle - 0.05).progress, 0, "blank before it loops")
        XCTAssertEqual(Math.entrance(.draw, drawSeconds: 1.2, clock: cycle + edge / 2).progress, 0.5, accuracy: 1e-6, "and loops")
        XCTAssertEqual(Math.entrance(.draw, drawSeconds: 1.2, clock: 0.3).alpha, 1, "drawing on never fades")
    }

    func testTheDrawTileFollowsTheSpeedSlider() {
        let fast = Math.entrance(.draw, drawSeconds: 0.3, clock: 0.15).progress
        let slow = Math.entrance(.draw, drawSeconds: 2.4, clock: 0.15).progress
        XCTAssertGreaterThan(fast, slow, "a faster speed has drawn more of the stroke at the same moment")
        XCTAssertEqual(Math.edgeSeconds(.draw, drawSeconds: 10), 2.5, "a 10 s draw is shown at a watchable length")
        XCTAssertEqual(Math.edgeSeconds(.draw, drawSeconds: 0.1), 0.3)
    }

    func testTheFadeTilesFadeInAndOutWhileTheStrokeStaysWhole() {
        let fadeIn = Math.entrance(.fade, drawSeconds: 1.2, clock: Math.fadeSeconds / 2)
        XCTAssertEqual(fadeIn.alpha, 0.5, accuracy: 1e-6)
        XCTAssertEqual(fadeIn.progress, 1, "a fade shows the whole stroke")
        XCTAssertEqual(Math.entrance(.fade, drawSeconds: 1.2, clock: Math.fadeSeconds + 0.1).alpha, 1)

        XCTAssertEqual(Math.exit(.fade, clock: 0.2), Math.Sample(progress: 1, alpha: 1), "visible first")
        XCTAssertEqual(Math.exit(.fade, clock: Math.hold + Math.fadeSeconds / 2).alpha, 0.5, accuracy: 1e-6)
        XCTAssertEqual(Math.exit(.fade, clock: Math.hold + Math.fadeSeconds + 0.1).alpha, 0, "then gone before it loops")
    }

    func testNoneTilesNeverMove() {
        for clock in stride(from: 0.0, through: 6.0, by: 0.37) {
            XCTAssertEqual(Math.entrance(.none, drawSeconds: 1.2, clock: clock), Math.stable)
            XCTAssertEqual(Math.exit(.none, clock: clock), Math.stable)
        }
    }
}
