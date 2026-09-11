import Foundation
import XCTest
@testable import KriaMediaEngine

final class TextFadeEnvelopeTests: XCTestCase {
    private struct Case: Decodable {
        let effect: PortableTextEffect
        let duration: Double
        let motion: TextMotionParameters?
        let fade: TextFadeEnvelope
        let samples: [Sample]
    }
    private struct Sample: Decodable { let time: Double; let alpha: Double }
    func testAlphaMatchesProductionDispatcherIncludingMotionAndShortWindows() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_text_fades.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        XCTAssertEqual(cases.count, 72)
        for test in cases {
            for sample in test.samples {
                let state = try TextTransformTiming.sample(effect: test.effect, text: "Hello world", localTime: sample.time,
                    duration: test.duration, motion: test.motion, fade: test.fade)
                XCTAssertEqual(state.alpha, sample.alpha, accuracy: 1e-9, "\(test.effect) \(test.duration) \(test.fade.curve) \(sample.time)")
            }
        }
    }
    func testDefaultsAndZeroFadesAndValidation() throws {
        let none = TextFadeEnvelope(kind: .lyric, inMs: 0, outMs: 0, curve: .square)
        for time in [0.0, 0.2, 4.0] { XCTAssertEqual(try none.alpha(localTime: time, duration: 4), 1) }
        XCTAssertThrowsError(try TextFadeEnvelope(kind: .sequence, inMs: 1, outMs: 20, curve: .sqrt).validate())
        XCTAssertThrowsError(try TextFadeEnvelope(kind: .lyric, inMs: -1, outMs: 20, curve: .sqrt).validate())
        XCTAssertThrowsError(try none.alpha(localTime: .infinity, duration: 4))
    }
}
