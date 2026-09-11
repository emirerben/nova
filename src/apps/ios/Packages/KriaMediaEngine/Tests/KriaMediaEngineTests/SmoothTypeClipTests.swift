import Foundation
import XCTest
@testable import KriaMediaEngine

final class SmoothTypeClipTests: XCTestCase {
    private struct Case: Decodable {
        let order: TextMotionParameters.Order
        let lines: [Line]
        let samples: [Sample]
    }
    private struct Line: Decodable { let text: String; let rtl: Bool; let bounds: [Double] }
    private struct Sample: Decodable { let progress: Double; let calls: [Call] }
    private struct Call: Decodable { let text: String; let clip: [Double]? }

    func testMasksMatchActualCloudCanvasClips() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_smooth_clips.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(cases.count, 18)
        for test in cases {
            for sample in test.samples {
                XCTAssertEqual(test.lines.count, sample.calls.count)
                for (line, call) in zip(test.lines, sample.calls) {
                    XCTAssertEqual(line.text, call.text)
                    let bounds = try SmoothTypeClip(left: line.bounds[0], top: line.bounds[1], right: line.bounds[2], bottom: line.bounds[3])
                    let actual = try bounds.revealed(progress: sample.progress, order: test.order, firstStrongRTL: line.rtl)
                    if let expected = call.clip {
                        let mask = try XCTUnwrap(actual)
                        for (value, reference) in zip([mask.left, mask.top, mask.right, mask.bottom], expected) {
                            XCTAssertEqual(value, reference, accuracy: 0.0001, "\(line.text) \(test.order) \(sample.progress)")
                        }
                    } else { XCTAssertNil(actual) }
                }
            }
        }
    }

    func testRejectsInvalidGeometryAndProgress() throws {
        XCTAssertThrowsError(try SmoothTypeClip(left: .nan, top: 0, right: 1, bottom: 1))
        XCTAssertThrowsError(try SmoothTypeClip(left: 2, top: 0, right: 1, bottom: 1))
        let bounds = try SmoothTypeClip(left: 0, top: 0, right: 100, bottom: 100)
        for progress in [Double.nan, .infinity, -0.01, 1.01] {
            XCTAssertThrowsError(try bounds.revealed(progress: progress, order: .forward, firstStrongRTL: false))
        }
    }
}
