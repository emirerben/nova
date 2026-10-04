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
        XCTAssertEqual(r.holdTimerFired(at: 10.6), .beginHold)
    }

    func testHoldCannotFireAfterTravelPastSlop() {
        var r = down()
        _ = r.moved(to: CGPoint(x: 130, y: 100))
        XCTAssertEqual(r.holdTimerFired(at: 10.7), .none)
    }

    func testLiftAfterHoldIsNotATap() {
        var r = down()
        _ = r.holdTimerFired(at: 10.7)
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
        _ = r.holdTimerFired(at: 10.7)
        XCTAssertEqual(r.moved(to: CGPoint(x: 103, y: 100)), .none, "jitter inside slop stays held")
        XCTAssertEqual(r.moved(to: CGPoint(x: 140, y: 120)), .beginDrag(fromHold: true))
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 140, y: 120)), .endDrag)
    }

    func testPinchTakeoverSuppressesTapAndHold() {
        var r = down()
        r.cancel()
        XCTAssertEqual(r.holdTimerFired(at: 10.7), .none)
        XCTAssertEqual(r.touchUp(at: p0), .none)
    }

    func testResetAfterPinchTakeoverRearmsTheNextTap() {
        var r = down()
        r.cancel()                       // pinch took over; the cancelled drag may never deliver its own end
        XCTAssertFalse(r.isIdle)
        r.reset()                        // pinch end re-arms the resolver
        r.touchDown(at: p0, time: 30, onTarget: true, holdAllowed: false)
        XCTAssertEqual(r.touchUp(at: p0), .tap)
    }

    func testResolverIsReusableAfterEnd() {
        var r = down()
        _ = r.touchUp(at: p0)
        r.touchDown(at: p0, time: 20, onTarget: true, holdAllowed: true)
        XCTAssertEqual(r.holdTimerFired(at: 20.6), .beginHold)
    }

    // MARK: Real-finger jitter (a real tap drifts and lingers; synthetic XCUITest taps do not)
    // Thresholds: slop 10pt, hold 0.5s (the iOS long-press default). A press released inside both is a tap.

    func testThresholdsAreFingerSized() {
        XCTAssertGreaterThanOrEqual(SlidePostTouchResolver.slop, 10)
        XCTAssertGreaterThanOrEqual(SlidePostTouchResolver.holdDuration, 0.5)
    }

    func testTapWithEightToTenPointDriftStillTaps() {
        for drift in [8.0, 9.0, 10.0] {
            var r = down()
            XCTAssertEqual(r.moved(to: CGPoint(x: 100 + drift, y: 100)), .none, "drift \(drift) stays a press")
            XCTAssertEqual(r.touchUp(at: CGPoint(x: 100 + drift, y: 100)), .tap, "drift \(drift) lifts as a tap")
        }
    }

    func testSlowTapUnderHalfASecondStillTapsRegardlessOfDuration() {
        for duration in [0.3, 0.4, 0.45] {
            var r = down()
            // The hold timer is cancelled/never due before holdDuration, so a fire at this time is inert.
            XCTAssertEqual(r.holdTimerFired(at: 10 + duration), .none, "no hold at \(duration)s")
            XCTAssertEqual(r.touchUp(at: CGPoint(x: 105, y: 104)), .tap, "lift at \(duration)s is a tap")
        }
    }

    func testJitterWanderingInsideSlopThenReturningIsATap() {
        var r = down()
        for point in [CGPoint(x: 107, y: 96), CGPoint(x: 94, y: 104), CGPoint(x: 109, y: 103)] { XCTAssertEqual(r.moved(to: point), .none) }
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 102, y: 101)), .tap)
    }

    func testDragPastSlopIsADragEvenIfItEndsNearTheStart() {
        var r = down()
        XCTAssertEqual(r.moved(to: CGPoint(x: 125, y: 100)), .beginDrag(fromHold: false))
        // The text already moved; ending back at the start finishes the drag, it never opens the editor.
        XCTAssertEqual(r.touchUp(at: CGPoint(x: 101, y: 100)), .endDrag)
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

/// The rich editor's preview must never hide editable text (a ready/rendered slide used to show the burned render
/// with no canvas texts, so there was nothing to tap).
final class SlidePostPreviewPolicyTests: XCTestCase {
    private let rendered = URL(string: "https://cdn.example/render/slide-0.jpg")
    private let source = URL(string: "https://cdn.example/source/a.jpg")!
    private func asset() -> SlidePostAsset {
        var a = SlidePostAsset(id: "a", kind: "image", status: "ready"); a.sourceURL = source; return a
    }

    func testRichPreviewShowsSourceMediaEvenWhenTheSlideIsRendered() {
        XCTAssertEqual(SlidePostPreviewPolicy.mediaURL(rich: true, canExport: true, renderedURL: rendered, asset: asset()), source)
    }

    func testLegacyReadyPreviewStillShowsTheRender() {
        XCTAssertEqual(SlidePostPreviewPolicy.mediaURL(rich: false, canExport: true, renderedURL: rendered, asset: asset()), rendered)
        XCTAssertEqual(SlidePostPreviewPolicy.mediaURL(rich: false, canExport: false, renderedURL: rendered, asset: asset()), source)
    }

    func testRichTextsAreEditableForEverySlide() {
        var edits = SlidePostEdits()
        edits.setTexts([SlidePostTextElement(text: "Athens")])
        XCTAssertEqual(SlidePostPreviewPolicy.editableTexts(rich: true, edits: edits).map(\.text), ["Athens"])
        XCTAssertTrue(SlidePostPreviewPolicy.editableTexts(rich: true, edits: nil).isEmpty)
    }

    func testLegacySingleTextBecomesOneEditableOverlay() {
        var edits = SlidePostEdits()
        edits.text = SlidePostText(content: "Old caption", position: "bottom")
        let texts = SlidePostPreviewPolicy.editableTexts(rich: true, edits: edits)
        XCTAssertEqual(texts.map(\.text), ["Old caption"])
        XCTAssertEqual(texts.first?.position, "bottom")
    }
}
