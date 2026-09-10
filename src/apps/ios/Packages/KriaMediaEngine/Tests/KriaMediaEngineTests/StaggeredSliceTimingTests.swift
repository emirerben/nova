import Foundation
import XCTest
@testable import KriaMediaEngine

final class StaggeredSliceTimingTests: XCTestCase {
    private struct Case: Decodable { let text: String; let duration: Double; let motion: TextMotionParameters?; let samples: [Sample] }
    private struct Sample: Decodable { let time: Double; let state: StaggeredSliceSample }
    func testGlyphBuildMatchesProductionForLegacyAndAuthoredTiming() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_staggered_timing.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(cases.count, 105)
        for test in cases {
            for sample in test.samples {
                let actual = try StaggeredSliceTiming.sample(text: test.text, localTime: sample.time, duration: test.duration, motion: test.motion)
                XCTAssertEqual(actual.settled, sample.state.settled)
                XCTAssertEqual(actual.settleS, sample.state.settleS, accuracy: 1e-9)
                XCTAssertEqual(actual.lines.count, sample.state.lines.count)
                for (line, expected) in zip(actual.lines, sample.state.lines) {
                    XCTAssertEqual(line.text, expected.text)
                    XCTAssertEqual(line.kind, expected.kind)
                    XCTAssertEqual(line.glyphs.count, expected.glyphs.count)
                    for (glyph, reference) in zip(line.glyphs, expected.glyphs) {
                        XCTAssertEqual(glyph.grapheme, reference.grapheme)
                        XCTAssertEqual(glyph.opacity, reference.opacity, accuracy: 1e-9)
                        XCTAssertEqual(glyph.translateYEm, reference.translateYEm, accuracy: 1e-9)
                        XCTAssertEqual(glyph.rotateDeg, reference.rotateDeg, accuracy: 1e-9)
                    }
                }
            }
        }
    }
    func testRejectsInvalidInputBeforeTimeConversion() {
        XCTAssertThrowsError(try StaggeredSliceTiming.sample(text: "A", localTime: .infinity, duration: 1, motion: nil))
        XCTAssertThrowsError(try StaggeredSliceTiming.sample(text: "A", localTime: 1, duration: 0, motion: nil))
    }
}
