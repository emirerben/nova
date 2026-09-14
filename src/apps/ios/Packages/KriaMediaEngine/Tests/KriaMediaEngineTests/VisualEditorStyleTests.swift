import XCTest
@testable import KriaMediaEngine

final class VisualEditorStyleTests: XCTestCase {
    func testPlacementStylePreservesLegacyFades() throws {
        let style = VisualEditorStyle(rotationDegrees: 25, zoom: 2)
        let placement = VisualMediaPlacement(order: 0, windowStart: 1, windowEnd: 3,
            fadeIn: true, fadeOut: true, editorStyle: style)
        XCTAssertEqual(placement.alpha(at: 1.075), 0.5, accuracy: 0.000001)
        XCTAssertEqual(placement.alpha(at: 2), 1, accuracy: 0.000001)
        XCTAssertEqual(placement.alpha(at: 2.925), 0.5, accuracy: 0.000001)
        var animated = placement
        animated.editorStyle = VisualEditorStyle(animation: .init(entrance: .fade))
        let authored = try XCTUnwrap(animated.editorStyle).sample(time: 0.075, duration: 2).alpha
        XCTAssertEqual(animated.alpha(at: 1.075), 0.5 * authored, accuracy: 0.000001)
        var short = placement
        short.windowEnd = 1.2
        XCTAssertEqual(short.alpha(at: 1.075), 1, accuracy: 0.000001)
    }
    func testPairedPythonReferenceSample() throws {
        let style = VisualEditorStyle(animation: .init(entrance: .pop, exit: .slide, loop: .float))
        let sample = try style.sample(time: 0.3, duration: 3)
        XCTAssertEqual(sample.alpha, 0.984375, accuracy: 0.000001)
        XCTAssertEqual(sample.scale, 0.9953125, accuracy: 0.000001)
        XCTAssertEqual(sample.yTranslate, -8, accuracy: 0.000001)
        XCTAssertEqual(try style.sample(time: 3, duration: 3).alpha, 0)
    }
    func testSpeedPreservesEndBoundaryAndWireRoundTrip() throws {
        let style = VisualEditorStyle(rotationDegrees: 25, fitMode: "contain", zoom: 2,
            animation: .init(entrance: .fade, exit: .zoom, loop: .pulse, speed: 2))
        let data = try JSONEncoder().encode(style)
        XCTAssertEqual(try JSONDecoder().decode(VisualEditorStyle.self, from: data), style)
        XCTAssertGreaterThan(try style.sample(time: 2.99, duration: 3).alpha, 0)
        XCTAssertEqual(try style.sample(time: 3, duration: 3).alpha, 0)
        XCTAssertThrowsError(try VisualEditorStyle(zoom: .nan).validate())
    }
}
