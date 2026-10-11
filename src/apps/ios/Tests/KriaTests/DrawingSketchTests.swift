import CoreGraphics
import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-589: what Draw mode shows under the finger is what the painter puts there.
@MainActor final class DrawingSketchTests: XCTestCase {
    /// The live ink is painted by the compositor's painter; a layer not centred on its own strokes' bounding box
    /// is moved by it, so ink drawn in a corner used to appear mid-frame while drawing.
    func testLiveInkStaysWhereTheFingerDrewItInACorner() throws {
        let sketch = NativeDrawingSketch()
        sketch.reset(canvasAspect: 270.0 / 480.0) { _, _ in nil }
        XCTAssertTrue(sketch.begin(at: CGPoint(x: 0.1, y: 0.1)))
        sketch.append(CGPoint(x: 0.3, y: 0.15))
        sketch.append(CGPoint(x: 0.25, y: 0.25))

        let canvas = CGSize(width: 270, height: 480)
        let context = try XCTUnwrap(CGContext(data: nil, width: 270, height: 480, bitsPerComponent: 8, bytesPerRow: 270 * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.translateBy(x: 0, y: canvas.height); context.scaleBy(x: 1, y: -1)
        NativeDrawingPainter.paint(sketch.previewLayer, at: 0.5, canvas: canvas, in: context)

        let data = try XCTUnwrap(context.data).assumingMemoryBound(to: UInt8.self)
        var minX = Int.max, maxX = -1, minY = Int.max, maxY = -1
        for y in 0..<480 { for x in 0..<270 where data[y * context.bytesPerRow + x * 4 + 3] > 8 {
            minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
        } }
        XCTAssertGreaterThan(maxX, 0, "nothing was painted")
        XCTAssertLessThan(maxX, 135, "ink drifted out of the top-left quadrant (x)")
        XCTAssertLessThan(maxY, 240, "ink drifted out of the top-left quadrant (y)")
        XCTAssertLessThan(minX, 80)
        XCTAssertLessThan(minY, 80)
    }
}
