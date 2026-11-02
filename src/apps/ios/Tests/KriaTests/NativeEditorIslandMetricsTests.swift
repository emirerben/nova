import XCTest
@testable import Kria

/// KRI-131: the floating tool-island clearance/scrim math is pure and
/// nonisolated, so it's covered directly without spinning up SwiftUI. Only the
/// min-cover invariant is pinned; the rest restated the formula.
final class NativeEditorIslandMetricsTests: XCTestCase {
    func testClearanceAlwaysCoversAtLeastTheIslandItself() {
        for safeArea: CGFloat in [0, 20, 34, 59] {
            for showsContext in [false, true] {
                let clearance = NativeEditorIslandMetrics.bottomClearance(showsContext: showsContext, safeAreaBottom: safeArea)
                XCTAssertGreaterThanOrEqual(
                    clearance,
                    NativeEditorIslandMetrics.islandHeight + NativeEditorIslandMetrics.bottomPadding,
                    "clearance must always cover the island's own height plus its bottom padding (safeArea=\(safeArea), showsContext=\(showsContext))"
                )
            }
        }
    }

    private func layout(_ count: Int, _ width: CGFloat) -> NativeEditorIslandMetrics.ToolRailLayout {
        NativeEditorIslandMetrics.toolRailLayout(toolCount: count, railWidth: width)
    }

    func testRailThatFitsComfortablyDoesNotScrollOrSqueezePastTheComfortFloor() {
        for width: CGFloat in [328, 366, 430] {
            let four = layout(4, width)
            XCTAssertFalse(four.scrolls, "four base tools fit a \(width)pt rail")
            XCTAssertEqual(four.itemWidth, NativeEditorIslandMetrics.toolWidth, accuracy: 0.001, "no squeeze when there is room")
        }
        for count in 4...6 {
            for width: CGFloat in [296, 328, 366, 390, 430, 700] {
                let result = layout(count, width)
                XCTAssertGreaterThanOrEqual(result.itemWidth, NativeEditorIslandMetrics.minToolWidth, "count=\(count) width=\(width)")
                if !result.scrolls {
                    let used = 2 * NativeEditorIslandMetrics.islandHorizontalPadding
                        + CGFloat(count) * result.itemWidth + CGFloat(count - 1) * NativeEditorIslandMetrics.toolSpacing
                    XCTAssertLessThanOrEqual(used, width + 0.001, "a non-scrolling rail must fit: count=\(count) width=\(width)")
                }
            }
        }
    }

    func testOverflowingRailCutsTheNextToolAtItsMiddleSoScrollingIsObvious() {
        for (count, width) in [(5, CGFloat(296)), (5, 328), (6, 296), (6, 328), (6, 366)] {
            let result = layout(count, width)
            XCTAssertTrue(result.scrolls, "count=\(count) width=\(width) cannot show every tool comfortably")
            let step = result.itemWidth + NativeEditorIslandMetrics.toolSpacing
            let visible = width - NativeEditorIslandMetrics.islandHorizontalPadding
            let cutIndex = Int(visible / step)
            let fraction = (visible - CGFloat(cutIndex) * step) / result.itemWidth
            XCTAssertEqual(fraction, 0.5, accuracy: 0.02, "the cut tool shows half: count=\(count) width=\(width)")
            XCTAssertLessThan(cutIndex, count, "a tool is hidden past the edge")
            XCTAssertEqual(cutIndex, result.wholeItemsVisible)
            XCTAssertGreaterThanOrEqual(result.itemWidth, NativeEditorIslandMetrics.minToolWidth)
        }
        XCTAssertEqual(layout(6, 328).wholeItemsVisible, 4, "Text, Captions, Visuals, Background, then half of Sounds")
    }
}
