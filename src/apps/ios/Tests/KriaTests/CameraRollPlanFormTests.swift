import XCTest
@testable import Kria

/// A saved idea that predates the editorial form must keep decoding, and a form this
/// build does not know must not hide the idea or its wording.
final class CameraRollPlanFormTests: XCTestCase {
    private func plan(form: String?) throws -> CameraRollContentPlan {
        var json: [String: Any] = [
            "version": 1, "format": "text_story", "audience": "Walkers", "angle": "A",
            "why_it_works": "W", "opening_text": "O", "closing_text": NSNull(), "caption": "C",
            "beat_reasons": ["B"],
        ]
        if let form { json["form"] = form }
        return try JSONDecoder().decode(CameraRollContentPlan.self, from: JSONSerialization.data(withJSONObject: json))
    }

    func testLegacyPlanHasNoFormAndEncodesWithoutOne() throws {
        let legacy = try plan(form: nil)
        XCTAssertNil(legacy.formLabel)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(legacy), as: UTF8.self).contains("\"form\""))
    }

    func testKnownFormsLabelAndUnknownFormStaysReadable() throws {
        XCTAssertEqual(try plan(form: "guide").formLabel, "Guide")
        XCTAssertEqual(try plan(form: "factual_story").formLabel, "Fact story")
        let future = try plan(form: "mashup")
        XCTAssertNil(future.formLabel)
        XCTAssertEqual(future.openingText, "O")
    }
}
