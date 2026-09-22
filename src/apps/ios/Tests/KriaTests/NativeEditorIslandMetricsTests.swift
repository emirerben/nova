import XCTest
@testable import Kria

/// KRI-131: the floating tool-island clearance/scrim math is pure and
/// nonisolated, so it's covered directly without spinning up SwiftUI.
final class NativeEditorIslandMetricsTests: XCTestCase {
    func testBottomClearanceWithoutContextAndZeroSafeArea() {
        let clearance = NativeEditorIslandMetrics.bottomClearance(showsContext: false, safeAreaBottom: 0)
        XCTAssertEqual(
            clearance,
            NativeEditorIslandMetrics.bottomPadding + NativeEditorIslandMetrics.islandHeight + 8
        )
    }

    func testBottomClearanceWithoutContextAndHomeIndicatorSafeArea() {
        let clearance = NativeEditorIslandMetrics.bottomClearance(showsContext: false, safeAreaBottom: 34)
        XCTAssertEqual(
            clearance,
            NativeEditorIslandMetrics.bottomPadding + 34 + NativeEditorIslandMetrics.islandHeight + 8
        )
    }

    func testContextAddsExactlyStackSpacingPlusContextHeight() {
        let withoutContext = NativeEditorIslandMetrics.bottomClearance(showsContext: false, safeAreaBottom: 34)
        let withContext = NativeEditorIslandMetrics.bottomClearance(showsContext: true, safeAreaBottom: 34)
        XCTAssertEqual(
            withContext - withoutContext,
            NativeEditorIslandMetrics.stackSpacing + NativeEditorIslandMetrics.contextHeight
        )
    }

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

    func testScrimHeightExceedsClearanceByScrimExtra() {
        for safeArea: CGFloat in [0, 34] {
            for showsContext in [false, true] {
                let clearance = NativeEditorIslandMetrics.bottomClearance(showsContext: showsContext, safeAreaBottom: safeArea)
                let scrim = NativeEditorIslandMetrics.scrimHeight(showsContext: showsContext, safeAreaBottom: safeArea)
                XCTAssertEqual(scrim, clearance + NativeEditorIslandMetrics.scrimExtra)
                XCTAssertGreaterThan(scrim, clearance)
            }
        }
    }

    func testScrimHeightGrowsWhenContextCapsuleAppears() {
        let collapsed = NativeEditorIslandMetrics.scrimHeight(showsContext: false, safeAreaBottom: 34)
        let expanded = NativeEditorIslandMetrics.scrimHeight(showsContext: true, safeAreaBottom: 34)
        XCTAssertGreaterThan(expanded, collapsed)
    }
}
