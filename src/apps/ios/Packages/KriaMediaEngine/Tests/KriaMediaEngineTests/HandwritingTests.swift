import Foundation
import XCTest
#if canImport(AVFoundation)
import CoreImage
#endif
@testable import KriaMediaEngine

final class HandwritingTests: XCTestCase {
    func testPenPathsMatchActualCloudRenderer() throws {
        struct Sample: Decodable { let progress: Double; let paths: [[TextStrokePoint]] }
        struct Fixture: Decodable { let layer: PortableTextLayer; let samples: [Sample] }
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_handwriting.json").standardizedFileURL
        let fixture = try RecipeJSON.decoder().decode(Fixture.self, from: Data(contentsOf: url))
        let content = try XCTUnwrap(fixture.layer.handwriting)
        try content.validate()
        XCTAssertTrue(fixture.layer.runs.isEmpty)
        for sample in fixture.samples {
            let paths = content.strokes.map { $0.visiblePoints(at: sample.progress) }.filter { !$0.isEmpty }
            XCTAssertEqual(paths.count, sample.paths.count, "progress \(sample.progress)")
            for (actual, expected) in zip(paths, sample.paths) {
                XCTAssertEqual(actual.count, expected.count)
                for (a, e) in zip(actual, expected) {
                    XCTAssertEqual(a.x, e.x, accuracy: 0.0001)
                    XCTAssertEqual(a.y, e.y, accuracy: 0.0001)
                }
            }
        }
    }
#if canImport(AVFoundation)
    func testShadowCachePreservesPixelsAndBackwardSeekingWithinBudget() throws {
        struct Fixture: Decodable { let layer: PortableTextLayer }
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_handwriting.json").standardizedFileURL
        var root = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        var layer = root["layer"] as! [String: Any]
        var handwriting = layer["handwriting"] as! [String: Any]
        handwriting["blur_layers"] = [
            ["color": ["red": 0.0, "green": 0.0, "blue": 0.0, "alpha": 0.7], "sigma": 3.0, "dx": 2.0, "dy": 4.0],
            ["color": ["red": 1.0, "green": 0.2, "blue": 0.5, "alpha": 0.4], "sigma": 1.0, "dx": -1.0, "dy": 0.0],
        ]
        layer["handwriting"] = handwriting; root["layer"] = layer
        let fixture = try RecipeJSON.decoder().decode(Fixture.self, from: JSONSerialization.data(withJSONObject: root))
        let cached = try NativeHandwritingPainter(layer: fixture.layer, canvas: CGSize(width: 300, height: 200), maxBitmapBytes: 32_000_000)
        let reference = try NativeHandwritingPainter(layer: fixture.layer, canvas: CGSize(width: 300, height: 200), maxBitmapBytes: 32_000_000, cacheShadows: false)
        XCTAssertLessThanOrEqual(cached.bitmapBytes, 32_000_000)
        XCTAssertGreaterThan(cached.bitmapBytes, reference.bitmapBytes)
        let constrained = try NativeHandwritingPainter(layer: fixture.layer, canvas: CGSize(width: 300, height: 200), maxBitmapBytes: reference.bitmapBytes)
        XCTAssertEqual(constrained.bitmapBytes, reference.bitmapBytes)
        let context = CIContext()
        func pixels(_ painter: NativeHandwritingPainter, _ progress: Double) throws -> [UInt8] {
            let image = try painter.image(progress: progress)
            let width = Int(image.extent.width), height = Int(image.extent.height)
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            context.render(image, toBitmap: &pixels, rowBytes: width * 4, bounds: image.extent,
                format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            return pixels
        }
        for progress in [0.0, 0.25, 0.7, 1.0, 0.25] {
            XCTAssertEqual(try pixels(cached, progress), try pixels(reference, progress))
            XCTAssertEqual(try pixels(constrained, progress), try pixels(reference, progress))
        }
    }

    func testRotatedHandwritingPaintsOnlyRevealedPaths() throws {
        struct Fixture: Decodable { let layer: PortableTextLayer }
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_handwriting.json").standardizedFileURL
        let fixture = try RecipeJSON.decoder().decode(Fixture.self, from: Data(contentsOf: url))
        let painter = try NativeHandwritingPainter(layer: fixture.layer, canvas: CGSize(width: 300, height: 200), maxBitmapBytes: 32_000_000)
        let context = CIContext()
        func coverage(_ progress: Double) throws -> Int {
            let image = try painter.image(progress: progress)
            let width = Int(image.extent.width), height = Int(image.extent.height)
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            context.render(image, toBitmap: &pixels, rowBytes: width * 4, bounds: image.extent,
                           format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            return stride(from: 3, to: pixels.count, by: 4).filter { pixels[$0] > 100 }.count
        }
        XCTAssertEqual(try coverage(0), 0)
        let partial = try coverage(0.25), full = try coverage(1)
        XCTAssertGreaterThan(partial, 0)
        XCTAssertGreaterThan(full, partial)
    }
#endif

}
