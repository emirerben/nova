import CoreGraphics
import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-1582: the design question the server sends (`Tests/Fixtures/KRI1582DesignPicker.json`, written and
/// drift-checked by `tests/kria/test_kri1582_design_picker.py` from the real gate and preview compile) is read and
/// drawn by this build.
///
/// Failure modes: a version 2 option loses its preview (a layer key this build rejects, a font asset it cannot
/// find); a preview that cannot be drawn takes the whole card down, or blanks one tile, instead of falling back
/// to text; a still is drawn without the design's scrim (A2), or with one on a design that has none; a version 1
/// question changes shape.
final class DesignPickerPreviewTests: XCTestCase {
    private func fixture() throws -> [String: JSONValue] {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/KRI1582DesignPicker.json")
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(try JSONDecoder().decode(JSONValue.self, from: data).objectValue)
    }

    private func question() throws -> ChoiceQuestion {
        try XCTUnwrap(ChoiceQuestion.parse(payload: ["choice_question": try XCTUnwrap(fixture()["question"])]))
    }

    func testTheServersQuestionReadsWithAPreviewOnEveryDesign() throws {
        let question = try question()
        XCTAssertEqual(question.version, 2)
        XCTAssertEqual(question.options.map(\.key), ["A1", "A2", "A3", "B1"])
        XCTAssertEqual(question.options.filter(\.recommended).map(\.key), ["B1"])
        XCTAssertTrue(question.hasPreviews)
        let fonts = try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil))
        for option in question.options {
            let preview = try XCTUnwrap(option.preview, option.key)
            XCTAssertEqual(preview.mediaID, "fixture-clip")
            XCTAssertFalse(preview.layers.isEmpty)
            for file in preview.fonts.values {
                XCTAssertTrue(FileManager.default.fileExists(atPath: fonts.appendingPathComponent(file).path), "\(file) is bundled")
            }
        }
    }

    func testAnUndrawablePreviewLeavesTheQuestionAsText() throws {
        var root = try fixture()
        guard case .object(var question) = root["question"], case .array(var options) = question["options"],
              case .object(var first) = options[0], case .object(var preview) = first["preview"] else {
            return XCTFail("fixture shape")
        }
        preview["fonts"] = .object([:])
        first["preview"] = .object(preview)
        options[0] = .object(first)
        question["options"] = .array(options)
        root["question"] = .object(question)
        let parsed = try XCTUnwrap(ChoiceQuestion.parse(payload: ["choice_question": try XCTUnwrap(root["question"])]))
        XCTAssertNil(parsed.options[0].preview, "a run whose font is unknown cannot be drawn exactly")
        XCTAssertNotNil(parsed.options[1].preview)
        XCTAssertFalse(parsed.hasPreviews, "the whole card falls back to text blocks, every option with its words")
        XCTAssertEqual(parsed.options.count, 4, "the card stays answerable")
    }

    func testAVersionOneQuestionIgnoresPreviews() throws {
        guard case .object(var question) = try fixture()["question"] else { return XCTFail("fixture shape") }
        question["version"] = .number(1)
        let parsed = try XCTUnwrap(ChoiceQuestion.parse(payload: ["choice_question": .object(question)]))
        XCTAssertFalse(parsed.hasPreviews)
    }

    /// The still the engine draws: the scrim darkens A2's bottom band over a white picture and nowhere else.
    func testTheEngineDrawsEachDesignWithItsOwnPaint() throws {
        let question = try question()
        let fonts = try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil))
        let white = try XCTUnwrap(CGContext(data: nil, width: 108, height: 192, bitsPerComponent: 8, bytesPerRow: 0,
                                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        white.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        white.fill(CGRect(x: 0, y: 0, width: 108, height: 192))
        let background = try XCTUnwrap(white.makeImage())
        func bottomLuma(_ key: String) throws -> Double {
            let preview = try XCTUnwrap(question.option(key: key)?.preview)
            let image = try TextLayerStill.render(layers: preview.layers, fontURLs: preview.fonts.mapValues { fonts.appendingPathComponent($0) },
                                                  over: background, canvas: .vertical1080, time: preview.timeSeconds, width: 108)
            XCTAssertEqual(image.width, 108)
            XCTAssertEqual(image.height, 192)
            return try Self.meanLuma(image, rows: 185..<190)
        }
        XCTAssertLessThan(try bottomLuma("A2"), 0.6, "A2's scrim darkens the bottom of the frame")
        XCTAssertGreaterThan(try bottomLuma("A1"), 0.95, "A1 has no scrim")
        XCTAssertGreaterThan(try bottomLuma("B1"), 0.95, "B1 has no scrim")
    }

    private static func meanLuma(_ image: CGImage, rows: Range<Int>) throws -> Double {
        let width = image.width, height = image.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &pixels, width: width, height: height, bitsPerComponent: 8,
                                              bytesPerRow: width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        var total = 0.0, count = 0.0
        for row in rows {
            for column in 0..<width {
                let i = (row * width + column) * 4
                total += (0.2126 * Double(pixels[i]) + 0.7152 * Double(pixels[i + 1]) + 0.0722 * Double(pixels[i + 2])) / 255
                count += 1
            }
        }
        return total / count
    }
}
