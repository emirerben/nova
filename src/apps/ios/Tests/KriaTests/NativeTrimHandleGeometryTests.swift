import XCTest
@testable import Kria

/// KRI-1399. Failure modes: a handle's touch target shrinks below 44 pt on a short block; the two handles of a
/// short selected block overlap, so the end that is grabbed depends on which one is on top; the capsule drifts
/// off the block edge it belongs to.
final class NativeTrimHandleGeometryTests: XCTestCase {
    func testWideBlockAcceptsTouchesOnBothSidesOfEachEdge() {
        let leading = NativeTrimHandleGeometry.selected(edge: .leading, blockWidth: 200)
        let trailing = NativeTrimHandleGeometry.selected(edge: .trailing, blockWidth: 200)
        XCTAssertEqual(leading, .init(touchWidth: 44, originX: -22, capsuleOffset: 0))
        XCTAssertEqual(trailing, .init(touchWidth: 44, originX: 178, capsuleOffset: 0))
    }

    func testShortSelectedBlockSplitsTheTouchSurfaceAndHangsEachHandleOutward() {
        for width in [8.0, 44.0, 60.0, 87.0] as [CGFloat] {
            let leading = NativeTrimHandleGeometry.selected(edge: .leading, blockWidth: width)
            let trailing = NativeTrimHandleGeometry.selected(edge: .trailing, blockWidth: width)
            XCTAssertEqual(leading.touchWidth, 44)
            XCTAssertEqual(trailing.touchWidth, 44)
            XCTAssertLessThanOrEqual(leading.originX + leading.touchWidth, trailing.originX + 1e-9, "Handles must not overlap at \(width) pt")
            XCTAssertLessThan(leading.originX + leading.touchWidth, width / 2)
            XCTAssertGreaterThan(trailing.originX, width / 2)
            let leadingCapsuleX = leading.originX + 22 + leading.capsuleOffset
            let trailingCapsuleX = trailing.originX + 22 + trailing.capsuleOffset
            XCTAssertEqual(leadingCapsuleX, 0, accuracy: 1e-9, "The capsule stays on the block's start edge")
            XCTAssertEqual(trailingCapsuleX, width, accuracy: 1e-9, "The capsule stays on the block's end edge")
        }
    }

    func testUnselectedEndHandleIsAlways44PointsAndOnlyItsInsidePartCoversTheBlock() {
        let narrow = NativeTrimHandleGeometry.unselectedEnd(blockWidth: 44, insideWidth: 14)
        XCTAssertEqual(narrow.touchWidth, 44)
        XCTAssertEqual(narrow.originX, 30, "14 pt inside the block, 30 pt past its end")
        XCTAssertEqual(narrow.originX + 22 + narrow.capsuleOffset, 44, accuracy: 1e-9)
        let wide = NativeTrimHandleGeometry.unselectedEnd(blockWidth: 300, insideWidth: 44)
        XCTAssertEqual(wide, .init(touchWidth: 44, originX: 256, capsuleOffset: 22), "A wide block is unchanged")
    }
}
