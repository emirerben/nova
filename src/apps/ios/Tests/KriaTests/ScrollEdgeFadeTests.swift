import SwiftUI
import XCTest
@testable import Kria

/// KRI-197: the edge-fade strength math is pure, so it's covered without SwiftUI.
final class ScrollEdgeFadeTests: XCTestCase {
    private let length: CGFloat = 24
    private let noInsets = EdgeInsets()

    /// `container` is the viewport excluding insets (SwiftUI's `containerSize`);
    /// the visible rect spans the whole frame, insets included.
    private func metrics(
        offset: CGFloat = 0,
        content: CGFloat,
        container: CGFloat,
        insets: EdgeInsets? = nil
    ) -> ScrollEdgeFadeMetrics {
        let i = insets ?? noInsets
        return ScrollEdgeFadeMetrics(
            visibleRect: CGRect(x: 0, y: offset, width: 300, height: container + i.top + i.bottom),
            contentSize: CGSize(width: 300, height: content),
            contentInsets: i, length: length
        )
    }

    func testContentThatFitsHasNoFadeOnEitherEdge() {
        XCTAssertEqual(metrics(content: 400, container: 400), ScrollEdgeFadeMetrics())
    }

    func testAtRestTopFadesOnlyTheBottomEdge() {
        let m = metrics(content: 1000, container: 400)
        XCTAssertEqual(m.top, 0)
        XCTAssertEqual(m.bottom, 1)
    }

    func testMidScrollFadesBothEdges() {
        let m = metrics(offset: 300, content: 1000, container: 400)
        XCTAssertEqual(m.top, 1)
        XCTAssertEqual(m.bottom, 1)
    }

    func testAtBottomFadesOnlyTheTopEdge() {
        let m = metrics(offset: 600, content: 1000, container: 400)
        XCTAssertEqual(m.top, 1)
        XCTAssertEqual(m.bottom, 0)
    }

    func testOverflowShorterThanTheFadeRampsProportionally() {
        // 12pt of content hidden below with a 24pt ramp -> half strength.
        let m = metrics(content: 412, container: 400)
        XCTAssertEqual(m.bottom, 0.5, accuracy: 0.0001)
        XCTAssertEqual(m.top, 0)
    }

    func testContentInsetsShiftTheRestingOffsets() {
        // An 80pt bottom inset (composer) on a 400pt viewport: the frame is 480pt,
        // so the last offset is content + inset - frame = 600 (not 520).
        let insets = EdgeInsets(top: 0, leading: 0, bottom: 80, trailing: 0)
        XCTAssertEqual(metrics(offset: 520, content: 1000, container: 400, insets: insets).bottom, 1)
        XCTAssertEqual(metrics(offset: 600, content: 1000, container: 400, insets: insets).bottom, 0)

        // A top inset makes the resting offset negative; that is still "at top".
        let topInset = EdgeInsets(top: 60, leading: 0, bottom: 0, trailing: 0)
        XCTAssertEqual(metrics(offset: -60, content: 1000, container: 400, insets: topInset).top, 0)
    }

    func testContentBeneathFloatingChromeCountsAsHiddenBelowTheComposer() {
        // Header 92pt + composer 84pt insets on a 700pt viewport, resting at the top:
        // the header inset is not hidden content, the content below the composer is.
        let insets = EdgeInsets(top: 92, leading: 0, bottom: 84, trailing: 0)
        let m = metrics(offset: -92, content: 1000, container: 700, insets: insets)
        XCTAssertEqual(m.top, 0, "at rest the header inset must not count as hidden content")
        XCTAssertEqual(m.bottom, 1, "content below the composer is hidden content")
    }

    func testFloatResidueIsNotTreatedAsHiddenContent() {
        // KRI-128: 4.4e-16 must not light the fade.
        let m = metrics(offset: 4.4e-16, content: 400 + 4.4e-16, container: 400)
        XCTAssertEqual(m, ScrollEdgeFadeMetrics())
    }

    func testStrengthIsQuantizedSoTheGeometryActionDoesNotFirePerFrame() {
        // 9.5pt and 9.7pt of 24 both sit in the 0.40 bucket ([9.0, 10.2)pt).
        let a = ScrollEdgeFadeMetrics.strength(hidden: 9.5, length: 24)
        let b = ScrollEdgeFadeMetrics.strength(hidden: 9.7, length: 24)
        XCTAssertEqual(a, b)
        XCTAssertEqual(a, 0.4, accuracy: 0.0001)
        // Crossing into the next bucket does change it.
        XCTAssertEqual(ScrollEdgeFadeMetrics.strength(hidden: 10.8, length: 24), 0.45, accuracy: 0.0001)
    }

    func testZeroLengthOrZeroSizedContainersNeverProduceNaN() {
        XCTAssertEqual(ScrollEdgeFadeMetrics.strength(hidden: 50, length: 0), 0)
        XCTAssertEqual(metrics(content: 0, container: 0), ScrollEdgeFadeMetrics())
        XCTAssertFalse(ScrollEdgeFadeMetrics.strength(hidden: .infinity, length: 24).isNaN)
    }
}
