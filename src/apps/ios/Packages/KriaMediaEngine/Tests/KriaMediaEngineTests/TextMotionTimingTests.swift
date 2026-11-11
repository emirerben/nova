import Foundation
import XCTest
@testable import KriaMediaEngine

final class TextMotionTimingTests: XCTestCase {
    private struct MotionCase: Decodable {
        let effect: PortableTextEffect
        let text: String
        let motion: TextMotionParameters
        let settleDuration: Double
        let rendererSettleDuration: Double
        let totalDuration: Double
        let samples: [Sample]
    }
    private struct Sample: Decodable {
        let time: Double
        let authoredTime: Double
        let smooth: SmoothTypeSample?
        let lineProgresses: [Double]?
    }

    func testNativeTextMotionCharacterBoundaries() {
        let cases: [(String, [String])] = [
            ("नमस्ते दुनिया", ["न", "म", "स्ते", " ", "दु", "नि", "या"]),
            ("क्ष", ["क्ष"]),
            ("क्\u{200C}ष", ["क्\u{200C}", "ष"]),
            ("क्\u{200D}ष", ["क्\u{200D}ष"]),
            ("👩🏽‍🚀 café\ne\u{0301}", ["👩🏽‍🚀", " ", "c", "a", "f", "é", "\n", "e\u{0301}"]),
            ("🇹🇷🇪🇸", ["🇹🇷", "🇪🇸"])
        ]
        for (text, clusters) in cases {
            XCTAssertEqual(text.map(String.init), clusters, text)
        }
    }

    func testEveryTextEffectTimingMatchesCloudReference() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_text_motion_v2.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([MotionCase].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(Set(cases.map(\.effect)), Set(PortableTextEffect.allCases))
        for test in cases {
            XCTAssertEqual(try TextMotionTiming.settleDuration(effect: test.effect, text: test.text, motion: test.motion), test.settleDuration, accuracy: 1e-9, test.text)
            XCTAssertEqual(try TextMotionTiming.rendererSettleDuration(effect: test.effect, text: test.text, motion: test.motion), test.rendererSettleDuration, accuracy: 1e-9, test.text)
            XCTAssertEqual(try TextMotionTiming.totalDuration(effect: test.effect, text: test.text, motion: test.motion), test.totalDuration, accuracy: 1e-9, test.text)
            for expected in test.samples {
                XCTAssertEqual(try TextMotionTiming.authoredTime(effect: test.effect, text: test.text, localTime: expected.time, motion: test.motion), expected.authoredTime, accuracy: 1e-9, test.text)
                if let smooth = expected.smooth, let lines = expected.lineProgresses {
                    let actual = try TextMotionTiming.smoothType(text: test.text, localTime: expected.time, motion: test.motion)
                    XCTAssertEqual(actual.alpha, smooth.alpha, accuracy: 1e-9, test.text)
                    XCTAssertEqual(actual.xTranslate, smooth.xTranslate, accuracy: 1e-9, test.text)
                    XCTAssertEqual(actual.yTranslate, smooth.yTranslate, accuracy: 1e-9, test.text)
                    XCTAssertEqual(actual.blurPx, smooth.blurPx, accuracy: 1e-9, test.text)
                    XCTAssertEqual(actual.revealProgress, smooth.revealProgress, accuracy: 1e-9, test.text)
                    XCTAssertEqual(actual.revealOrigin, smooth.revealOrigin)
                    XCTAssertEqual(actual.settled, smooth.settled)
                    let actualLines = try TextMotionTiming.smoothTypeLineProgresses(lines: test.text.components(separatedBy: "\n"), localTime: expected.time, motion: test.motion)
                    XCTAssertEqual(actualLines.count, lines.count)
                    for (actualLine, line) in zip(actualLines, lines) { XCTAssertEqual(actualLine, line, accuracy: 1e-9, test.text) }
                }
            }
        }
    }
}
