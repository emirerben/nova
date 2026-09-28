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
}
