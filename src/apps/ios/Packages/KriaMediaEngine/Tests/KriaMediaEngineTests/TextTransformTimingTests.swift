import Foundation
import XCTest
@testable import KriaMediaEngine

final class TextTransformTimingTests: XCTestCase {
    private struct Case: Decodable {
        let effect: PortableTextEffect
        let text: String
        let duration: Double
        let motion: TextMotionParameters
        let samples: [Sample]
    }
    private struct Sample: Decodable { let time: Double; let state: TextTransformSample }

    func testTransformsMatchActualCloudDrawDispatch() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_text_transforms_v2.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(cases.count, 32)
        for test in cases {
            for expected in test.samples {
                let actual = try TextTransformTiming.sample(effect: test.effect, text: test.text, localTime: expected.time,
                                                          duration: test.duration, motion: test.motion)
                let label = "\(test.effect) speed=\(test.motion.speed) t=\(expected.time)"
                XCTAssertEqual(actual.alpha, expected.state.alpha, accuracy: 1e-9, label)
                XCTAssertEqual(actual.scale, expected.state.scale, accuracy: 1e-9, label)
                XCTAssertEqual(actual.xTranslate, expected.state.xTranslate, accuracy: 1e-9, label)
                XCTAssertEqual(actual.yTranslate, expected.state.yTranslate, accuracy: 1e-9, label)
            }
            XCTAssertThrowsError(try TextTransformTiming.sample(effect: .typewriter, text: test.text, localTime: 0,
                                                               duration: test.duration, motion: test.motion))
        }
    }
}
