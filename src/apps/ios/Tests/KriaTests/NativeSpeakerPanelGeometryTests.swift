import XCTest
@testable import Kria

final class NativeSpeakerPanelGeometryTests: XCTestCase {
    func testDragUsesSelectedPanelHeightAndPreservesLocalCoordinates() {
        let canvas = CGSize(width: 400, height: 800)
        let top = CGRect(x: 0, y: 0, width: 1, height: 0.3)
        let bottom = CGRect(x: 0, y: 0.6, width: 1, height: 0.4)
        let baseline = CGPoint(x: 0.75, y: 0.75)
        let delta = CGSize(width: -40, height: -48)
        let a = NativeSpeakerPanelGeometry.movedPosition(from: baseline, translation: delta, canvas: canvas, viewport: top)
        let b = NativeSpeakerPanelGeometry.movedPosition(from: baseline, translation: delta, canvas: canvas, viewport: bottom)
        XCTAssertEqual(a.x, 0.65, accuracy: 0.0001)
        XCTAssertEqual(a.y, 0.55, accuracy: 0.0001)
        XCTAssertEqual(b.y, 0.60, accuracy: 0.0001)
        XCTAssertEqual(NativeSpeakerPanelGeometry.canvasPoint(baseline, viewport: bottom, canvas: canvas), CGPoint(x: 300, y: 720))
    }

    func testPanelHitBoxesRemainDisjointAndLegacyViewportIsIdentity() {
        let canvas = CGSize(width: 400, height: 800)
        let a = NativeSpeakerPanelGeometry.pixelRect(CGRect(x: 0.5, y: 0.2, width: 0.4, height: 0.2), canvas: canvas)
        let b = NativeSpeakerPanelGeometry.pixelRect(CGRect(x: 0.5, y: 0.7, width: 0.4, height: 0.2), canvas: canvas)
        XCTAssertFalse(a.intersects(b))
        XCTAssertTrue(b.contains(CGPoint(x: 300, y: 640)))
        XCTAssertFalse(a.contains(CGPoint(x: 300, y: 640)))
        let moved = NativeSpeakerPanelGeometry.movedPosition(from: CGPoint(x: 0.5, y: 0.5), translation: CGSize(width: 40, height: 80),
                                                            canvas: canvas, viewport: CGRect(x: 0, y: 0, width: 1, height: 1))
        XCTAssertEqual(moved, CGPoint(x: 0.6, y: 0.6))
    }
}
