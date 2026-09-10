import Foundation
import XCTest
@testable import KriaMediaEngine

final class TextRevealTimingTests: XCTestCase {
    private struct Case: Decodable {
        let effect: PortableTextEffect
        let text: String
        let start: Double
        let schedule: [Double]?
        let motion: TextMotionParameters?
        let samples: [Sample]
    }
    private struct Sample: Decodable {
        let time: Double
        let visibleText: String
        let showCursor: Bool
        let cursorStyle: TextMotionParameters.Cursor
    }
    func testDiscreteRevealMatchesActualCloudDispatcher() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_text_reveal.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(cases.count, 48)
        for test in cases {
            for expected in test.samples {
                let actual = try TextRevealTiming.sample(effect: test.effect, text: test.text, localTime: expected.time,
                                                        start: test.start, schedule: test.schedule, motion: test.motion)
                let label = "\(test.effect) \(test.text) t=\(expected.time) speed=\(test.motion?.speed ?? 1)"
                XCTAssertEqual(Array(actual.visibleText.unicodeScalars), Array(expected.visibleText.unicodeScalars), label)
                XCTAssertEqual(actual.showCursor, expected.showCursor, label)
                XCTAssertEqual(actual.cursorStyle, expected.cursorStyle, label)
            }
        }
    }
    func testPrefixesRetainWrappedLineGeometry() {
        let sample = TextRevealTiming.fixedLines(["Hello", "brave world"], visibleText: "Hello bra")
        XCTAssertEqual(sample.lines, ["Hello", "bra"])
        XCTAssertEqual(sample.cursorLine, 1)
    }
    func testRejectsNonfiniteTimeline() {
        XCTAssertThrowsError(try TextRevealTiming.sample(effect: .typewriter, text: "Hello", localTime: .nan, motion: nil))
        XCTAssertThrowsError(try TextRevealTiming.sample(effect: .typewriter, text: "Hello", localTime: 0, schedule: [.infinity], motion: nil))
    }
}
