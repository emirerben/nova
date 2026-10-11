import KriaMediaEngine
import XCTest
@testable import Kria

/// The crop editor's pure math: whatever a finger does, the rectangle stays inside the frame, never gets
/// smaller than the floor, and (in Fill frame) keeps the shape that will export.
final class KriaCropGeometryTests: XCTestCase {
    private typealias Geometry = KriaCropGeometry
    private let inside = 0.000_001

    private func assertInside(_ rect: NormalizedSourceRect, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertGreaterThanOrEqual(rect.x, -inside, file: file, line: line)
        XCTAssertGreaterThanOrEqual(rect.y, -inside, file: file, line: line)
        XCTAssertLessThanOrEqual(rect.x + rect.width, 1 + inside, file: file, line: line)
        XCTAssertLessThanOrEqual(rect.y + rect.height, 1 + inside, file: file, line: line)
        XCTAssertGreaterThanOrEqual(rect.width, Geometry.minimumSize - inside, file: file, line: line)
        XCTAssertGreaterThanOrEqual(rect.height, Geometry.minimumSize - inside, file: file, line: line)
    }

    func testFillCropHasTheOutputsPixelShapeAndIsCentred() {
        // A 4:3 landscape photo shown in a 9:16 frame keeps a tall middle slice of it.
        let imageAspect = 4.0 / 3.0, outputAspect = 9.0 / 16.0
        let crop = Geometry.fillCrop(imageAspect: imageAspect, outputAspect: outputAspect)
        XCTAssertEqual(crop.height, 1, accuracy: inside)
        XCTAssertEqual(crop.x + crop.width / 2, 0.5, accuracy: inside)
        XCTAssertEqual(crop.width * imageAspect / crop.height, outputAspect, accuracy: 0.0001)
        XCTAssertTrue(Geometry.isCentredFill(crop, imageAspect: imageAspect, outputAspect: outputAspect))
    }

    func testEveryCornerDragOfAFillCropKeepsItsShapeInsideTheFrame() {
        let ratio = Geometry.lockedRatio(imageAspect: 4.0 / 3.0, outputAspect: 9.0 / 16.0)
        let start = NormalizedSourceRect(x: 0.3, y: 0.2, width: 0.3, height: 0.3 / ratio)
        for corner in Geometry.Corner.allCases {
            for (dx, dy) in [(-2.0, -2.0), (2.0, 2.0), (-2.0, 2.0), (2.0, -2.0), (0.4, 0.0), (0.0, -0.4), (-5.0, 0.01)] {
                let rect = Geometry.resized(start, corner: corner, dx: dx, dy: dy, lockedRatio: ratio)
                assertInside(rect)
                XCTAssertEqual(rect.width / rect.height, ratio, accuracy: 0.0001, "\(corner) \(dx),\(dy) changed the shape")
            }
        }
    }

    func testDraggingACornerPastTheOppositeOneStopsAtTheMinimumSize() {
        let start = NormalizedSourceRect(x: 0.2, y: 0.2, width: 0.4, height: 0.4)
        let free = Geometry.resized(start, corner: .bottomTrailing, dx: -3, dy: -3)
        XCTAssertEqual(free.width, Geometry.minimumSize, accuracy: inside)
        XCTAssertEqual(free.height, Geometry.minimumSize, accuracy: inside)
        XCTAssertEqual(free.x, 0.2, accuracy: inside, "The opposite corner stays put")
        let locked = Geometry.resized(start, corner: .topLeading, dx: 3, dy: 3, lockedRatio: 1)
        XCTAssertEqual(locked.width, Geometry.minimumSize, accuracy: inside)
        XCTAssertEqual(locked.x + locked.width, 0.6, accuracy: inside)
        assertInside(locked)
    }

    func testFreeResizeNeverLeavesTheFrame() {
        let start = NormalizedSourceRect(x: 0.1, y: 0.1, width: 0.5, height: 0.5)
        for corner in Geometry.Corner.allCases {
            assertInside(Geometry.resized(start, corner: corner, dx: 4, dy: 4))
            assertInside(Geometry.resized(start, corner: corner, dx: -4, dy: -4))
        }
    }

    func testMovingClampsToTheFrameAndKeepsTheSize() {
        let start = NormalizedSourceRect(x: 0.2, y: 0.3, width: 0.4, height: 0.5)
        let far = Geometry.moved(start, dx: 5, dy: -5)
        XCTAssertEqual(far, NormalizedSourceRect(x: 0.6, y: 0, width: 0.4, height: 0.5))
        XCTAssertEqual(Geometry.moved(start, dx: -5, dy: 5), NormalizedSourceRect(x: 0, y: 0.5, width: 0.4, height: 0.5))
    }

    func testPinchScalesAboutTheCentreAndStaysInsideWithinTheLimits() {
        let start = NormalizedSourceRect(x: 0.3, y: 0.3, width: 0.4, height: 0.4)
        let bigger = Geometry.scaled(start, factor: 1.5)
        XCTAssertEqual(bigger.x + bigger.width / 2, 0.5, accuracy: inside)
        XCTAssertEqual(bigger.width / bigger.height, 1, accuracy: inside)
        let huge = Geometry.scaled(start, factor: 50)
        XCTAssertEqual(huge.width, 1, accuracy: inside)
        XCTAssertEqual(huge.height, 1, accuracy: inside)
        XCTAssertEqual(huge.x, 0, accuracy: inside)
        assertInside(Geometry.scaled(start, factor: 0.001))
        // Near an edge, growing slides the crop back inside rather than past it.
        assertInside(Geometry.scaled(NormalizedSourceRect(x: 0.6, y: 0.6, width: 0.4, height: 0.4), factor: 1.8))
    }

    func testTheCropMapsOntoWhereTheImageIsActuallyDrawn() {
        let frame = Geometry.fitted(aspect: 2, in: CGSize(width: 300, height: 400))
        XCTAssertEqual(frame, CGRect(x: 0, y: 125, width: 300, height: 150))
        let box = Geometry.viewRect(NormalizedSourceRect(x: 0.5, y: 0.5, width: 0.5, height: 0.5), in: frame)
        XCTAssertEqual(box, CGRect(x: 150, y: 200, width: 150, height: 75))
    }
}
