#if canImport(AVFoundation)
import Foundation
import XCTest
@testable import KriaMediaEngine

final class AuthoredHandwritingLayoutTests: XCTestCase {
    func testLocalLayoutMatchesCloudStrokeCoordinatesAndPenTiming() throws {
        struct Fixture: Decodable {
            struct Stroke: Decodable { let points: [[Double]]; let start: Double; let end: Double; let line: Int }
            let text: String; let height: Double; let widths: [Double]; let strokes: [Stroke]
        }
        let api = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../../../../api").standardizedFileURL
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: api.appendingPathComponent("tests/fixtures/native_handwriting_layout.json")))
        let layout = try AuthoredHandwritingLayout(url: api.appendingPathComponent("assets/fonts/handwriting-strokes.json"))
        let layer = try layout.compile(id: "pen", text: fixture.text, start: 0, end: 3,
            style: .init(fontAssetID: "font", size: 100, widthFraction: 0.6,
                         color: .init(red: 1, green: 1, blue: 1, alpha: 1), letterSpacing: 0.03),
            canvas: .init(width: 600, height: 600), lineSpacing: 1.4)
        let content = try XCTUnwrap(layer.handwriting)
        XCTAssertEqual(content.strokes.count, fixture.strokes.count)
        for (actual, expected) in zip(content.strokes, fixture.strokes) {
            XCTAssertEqual(actual.startProgress, expected.start, accuracy: 1e-12)
            XCTAssertEqual(actual.endProgress, expected.end, accuracy: 1e-12)
            for (point, source) in zip(actual.points, expected.points) {
                XCTAssertEqual(point.x, 300 - fixture.widths[expected.line] * 50 + source[0] * 100, accuracy: 1e-9)
                XCTAssertEqual(point.y, 300 - fixture.height * 50 + source[1] * 100, accuracy: 1e-9)
            }
        }
        try layer.validate(duration: 3, manifest: nil)
    }
}
#endif
