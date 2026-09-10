import Foundation
import XCTest
@testable import KriaMediaEngine

final class TextTransformTimingTests: XCTestCase {
    private struct Case: Decodable {
        let effect: PortableTextEffect
        let text: String
        let duration: Double
        let motion: TextMotionParameters?
        let samples: [Sample]
    }
    private struct Sample: Decodable { let time: Double; let state: TextTransformSample }

    func testTransformsMatchActualCloudDrawDispatch() throws {
        try checkFixture("phone_text_transforms_v2.json")
    }

    func testLegacyTransformsMatchActualCloudDrawDispatch() throws {
        try checkFixture("phone_text_transforms_legacy.json")
    }

    private func checkFixture(_ filename: String) throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/\(filename)").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(cases.count, 48)
        for test in cases {
            for expected in test.samples {
                let actual = try TextTransformTiming.sample(effect: test.effect, text: test.text, localTime: expected.time,
                                                          duration: test.duration, motion: test.motion)
                let label = "\(test.effect) speed=\(test.motion?.speed ?? 1) t=\(expected.time)"
                XCTAssertEqual(actual.alpha, expected.state.alpha, accuracy: 1e-9, label)
                XCTAssertEqual(actual.scale, expected.state.scale, accuracy: 1e-9, label)
                XCTAssertEqual(actual.xTranslate, expected.state.xTranslate, accuracy: 1e-9, label)
                XCTAssertEqual(actual.yTranslate, expected.state.yTranslate, accuracy: 1e-9, label)
                XCTAssertEqual(actual.revealProgress, expected.state.revealProgress, accuracy: 1e-9, label)
            }
            XCTAssertThrowsError(try TextTransformTiming.sample(effect: .karaokeLine, text: test.text, localTime: 0,
                                                               duration: test.duration, motion: test.motion))
        }
    }
}
