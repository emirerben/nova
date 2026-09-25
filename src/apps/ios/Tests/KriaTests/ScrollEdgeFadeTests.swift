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
        offset: CGPoint = .zero,
        content: CGSize,
        container: CGSize,
        insets: EdgeInsets? = nil
    ) -> ScrollEdgeFadeMetrics {
        let i = insets ?? noInsets
        let frame = CGSize(
            width: container.width + i.leading + i.trailing,
            height: container.height + i.top + i.bottom
        )
        return ScrollEdgeFadeMetrics(
            visibleRect: CGRect(origin: offset, size: frame), contentSize: content,
            contentInsets: i, length: length
        )
    }

    func testContentThatFitsHasNoFadeOnAnyEdge() {
        let m = metrics(content: CGSize(width: 300, height: 400), container: CGSize(width: 300, height: 400))
        XCTAssertEqual(m, ScrollEdgeFadeMetrics())
    }

    func testAtRestTopFadesOnlyTheBottomEdge() {
        let m = metrics(content: CGSize(width: 300, height: 1000), container: CGSize(width: 300, height: 400))
        XCTAssertEqual(m.top, 0)
        XCTAssertEqual(m.bottom, 1)
    }

    func testMidScrollFadesBothVerticalEdges() {
        let m = metrics(offset: CGPoint(x: 0, y: 300), content: CGSize(width: 300, height: 1000), container: CGSize(width: 300, height: 400))
        XCTAssertEqual(m.top, 1)
        XCTAssertEqual(m.bottom, 1)
    }

    func testAtBottomFadesOnlyTheTopEdge() {
        let m = metrics(offset: CGPoint(x: 0, y: 600), content: CGSize(width: 300, height: 1000), container: CGSize(width: 300, height: 400))
        XCTAssertEqual(m.top, 1)
        XCTAssertEqual(m.bottom, 0)
    }

    func testOverflowShorterThanTheFadeRampsProportionally() {
        // 12pt of content hidden below with a 24pt fade -> half strength.
        let m = metrics(content: CGSize(width: 300, height: 412), container: CGSize(width: 300, height: 400))
        XCTAssertEqual(m.bottom, 0.5, accuracy: 0.0001)
        XCTAssertEqual(m.top, 0)
    }

    func testHorizontalStripMirrorsVertical() {
        let atStart = metrics(content: CGSize(width: 900, height: 52), container: CGSize(width: 300, height: 52))
        XCTAssertEqual(atStart.leading, 0)
        XCTAssertEqual(atStart.trailing, 1)
        XCTAssertEqual(atStart.top, 0)
        XCTAssertEqual(atStart.bottom, 0)

        let atEnd = metrics(offset: CGPoint(x: 600, y: 0), content: CGSize(width: 900, height: 52), container: CGSize(width: 300, height: 52))
        XCTAssertEqual(atEnd.leading, 1)
        XCTAssertEqual(atEnd.trailing, 0)
    }

    func testContentInsetsShiftTheRestingOffsets() {
        // An 80pt bottom inset (composer) on a 400pt viewport: the frame is 480pt,
        // so the last offset is content + inset - frame = 600 (not 520).
        let insets = EdgeInsets(top: 0, leading: 0, bottom: 80, trailing: 0)
        let content = CGSize(width: 300, height: 1000)
        let container = CGSize(width: 300, height: 400)
        XCTAssertEqual(metrics(offset: CGPoint(x: 0, y: 520), content: content, container: container, insets: insets).bottom, 1)
        XCTAssertEqual(metrics(offset: CGPoint(x: 0, y: 600), content: content, container: container, insets: insets).bottom, 0)

        // A top inset makes the resting offset negative; that is still "at top".
        let topInset = EdgeInsets(top: 60, leading: 0, bottom: 0, trailing: 0)
        XCTAssertEqual(metrics(offset: CGPoint(x: 0, y: -60), content: content, container: container, insets: topInset).top, 0)
    }

    func testInsetsAreCarriedSoTheFadeZoneCoversFloatingChrome() {
        let insets = EdgeInsets(top: 92, leading: 0, bottom: 84, trailing: 0)
        let m = metrics(offset: CGPoint(x: 0, y: -92), content: CGSize(width: 300, height: 1000), container: CGSize(width: 300, height: 700), insets: insets)
        XCTAssertEqual(m.bottom, 1, "content below the composer is hidden content")
        XCTAssertEqual(m.insetTop, 92)
        XCTAssertEqual(m.insetBottom, 84)
        XCTAssertEqual(m.top, 0, "at rest the header inset must not count as hidden content")
        // Negative insets (never expected) can't shrink the zone below the base length.
        let odd = metrics(content: CGSize(width: 300, height: 400), container: CGSize(width: 300, height: 400), insets: EdgeInsets(top: -5, leading: 0, bottom: 0, trailing: 0))
        XCTAssertEqual(odd.insetTop, 0)
    }

    func testFloatResidueIsNotTreatedAsHiddenContent() {
        // KRI-128: 4.4e-16 must not light the fade.
        let m = metrics(
            offset: CGPoint(x: 0, y: 4.4e-16),
            content: CGSize(width: 300, height: 400 + 4.4e-16),
            container: CGSize(width: 300, height: 400)
        )
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
        let m = metrics(content: .zero, container: .zero)
        XCTAssertEqual(m, ScrollEdgeFadeMetrics())
        XCTAssertFalse(ScrollEdgeFadeMetrics.strength(hidden: .infinity, length: 24).isNaN)
    }

    func testWorkspaceSurfaceBlendsPaperTowardMenuByProgress() {
        XCTAssertEqual(KriaColor.workspaceSurface(progress: 0), Color(red: 1, green: 1, blue: 1))
        XCTAssertEqual(KriaColor.workspaceSurface(progress: -3), KriaColor.workspaceSurface(progress: 0))
        XCTAssertEqual(KriaColor.workspaceSurface(progress: 9), KriaColor.workspaceSurface(progress: 1))
        XCTAssertEqual(KriaColor.workspaceSurface(progress: 1), KriaColor.menu)
    }
}
