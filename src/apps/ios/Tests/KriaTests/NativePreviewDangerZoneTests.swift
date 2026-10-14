import XCTest
@testable import Kria

final class NativePreviewDangerZoneTests: XCTestCase {
    func testEachEdgeUsesTheWholeFootprintAndContactIsSafe() {
        let canvas = CGSize(width: 1000, height: 2000)
        let safe = NativePreviewDangerZones.safeRect(in: canvas)
        XCTAssertEqual(safe, CGRect(x: 50, y: 200, width: 800, height: 1300))
        XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: safe, rotation: 0, canvas: canvas), [])
        let cases: [(CGRect, NativePreviewDangerZones.Edge)] = [
            (CGRect(x: 400, y: 190, width: 100, height: 100), .top),
            (CGRect(x: 400, y: 1440, width: 100, height: 100), .bottom),
            (CGRect(x: 40, y: 800, width: 100, height: 100), .left),
            (CGRect(x: 790, y: 800, width: 100, height: 100), .right),
        ]
        for (frame, edge) in cases {
            XCTAssertTrue(safe.contains(CGPoint(x: frame.midX, y: frame.midY)))
            XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: frame, rotation: 0, canvas: canvas), [edge])
        }
    }

    func testRotationAndScaleWarnBeforeTheCenterLeavesSafeArea() {
        let canvas = CGSize(width: 1000, height: 2000)
        let frame = CGRect(x: 500, y: 220, width: 200, height: 80)
        XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: frame, rotation: 0, canvas: canvas), [])
        XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: frame, rotation: 90, canvas: canvas), [.top])
        let footprint = NativePreviewFootprint(frame: frame, rotation: 0, canvas: canvas)
            .transformed(scale: 2, rotation: 0, translation: .zero)
        XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: footprint.frame(in: canvas), rotation: footprint.rotation, canvas: canvas), [.top])
    }

    func testLiveMediaTranslationDoesNotDependOnRebuiltPreviewAndScalesWithCanvas() {
        let canvas = CGSize(width: 1000, height: 2000)
        let baseline = NativePreviewFootprint(frame: CGRect(x: 400, y: 600, width: 200, height: 200), rotation: 0, canvas: canvas)
        let moved = baseline.transformed(scale: 1, rotation: 0, translation: CGPoint(x: 0.4, y: 0))
        for size in [canvas, CGSize(width: 250, height: 500)] {
            XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: moved.frame(in: size), rotation: moved.rotation, canvas: size), [.right])
            XCTAssertEqual(NativePreviewDangerZones.overlaps(frame: baseline.frame(in: size), rotation: baseline.rotation, canvas: size), [])
        }
    }
}
