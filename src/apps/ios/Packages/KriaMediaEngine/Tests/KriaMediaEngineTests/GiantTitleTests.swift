import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
#endif

final class GiantTitleTests: XCTestCase {
    struct Fixture: Decodable {
        struct Timing: Decodable { let time: Double; let duration: Double; let scale: Double; let alpha: Double }
        struct Row: Decodable {
            struct Frame: Decodable { let time: Double; let pixels: [[UInt64]] }
            let id: String; let layer: PortableTextLayer; let frames: [Frame]
        }
        let width: Int; let height: Int; let cases: [Row]; let timing: [Timing]
    }
    func fixture() throws -> Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_giant_title.json").standardizedFileURL
        return try RecipeJSON.decoder().decode(Fixture.self, from: Data(contentsOf: url))
    }
    func testTransitionRejectsUnknownNonfiniteAndUnsupportedPainter() throws {
        for value in [Double.nan, .infinity, -30001, 30001] {
            XCTAssertThrowsError(try GiantTitleTransition(originX: value, originY: 0).validate())
            XCTAssertThrowsError(try GiantTitleTransition(originX: 0, originY: value).validate())
        }
        XCTAssertThrowsError(try RecipeJSON.decoder().decode(GiantTitleTransition.self,
            from: Data(#"{"origin_x":0,"origin_y":0,"zoom":60}"#.utf8)))
        let layer = try fixture().cases[0].layer
        var document = try JSONSerialization.jsonObject(with: JSONEncoder().encode(layer)) as! [String: Any]
        document["effect"] = "dissolve-out"
        let unsupported = try JSONDecoder().decode(PortableTextLayer.self,
            from: JSONSerialization.data(withJSONObject: document))
        XCTAssertThrowsError(try unsupported.validate(duration: 4, manifest: nil))
    }
    func testTimingMatchesActualCloudHoldZoomAndQuantizedFade() throws {
        for row in try fixture().timing {
            let state = try GiantTitleTiming.sample(localTime: row.time, duration: row.duration)
            XCTAssertEqual(state.scale, row.scale, accuracy: 1e-9)
            XCTAssertEqual(state.alpha, row.alpha, accuracy: 1e-12)
        }
        XCTAssertThrowsError(try GiantTitleTiming.sample(localTime: .nan, duration: 4))
        XCTAssertThrowsError(try GiantTitleTiming.sample(localTime: 0, duration: 0))
    }
#if canImport(AVFoundation)
    func testSmoothBlurColorDoesNotDependOnConsumerWorkingSpace() throws {
        let fixture = try fixture()
        let row = try XCTUnwrap(fixture.cases.first { $0.id == "styled-smooth" })
        let root = String(#filePath.prefix(upTo: #filePath.range(of: "/src/apps/ios/")!.lowerBound))
        let font = URL(fileURLWithPath: root).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let canvas = CGSize(width: fixture.width, height: fixture.height)
        let layer = try RecipeTextLayer.make(row.layer, assetURLs: ["font-Inter-Bold.ttf": font], canvas: canvas)
        let painter = try XCTUnwrap(layer.giantTitle)
        let contexts = [CIContext(options: [.workingColorSpace: NSNull()]),
                        CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])]
        for time in [0.08, 0.2, 1, 2.9] {
            let image = try painter.image(localTime: time, settled: layer.image)
            let pixels = contexts.map { context in
                var values = [UInt8](repeating: 0, count: fixture.width * fixture.height * 4)
                context.render(image, toBitmap: &values, rowBytes: fixture.width * 4,
                    bounds: CGRect(origin: .zero, size: canvas), format: .RGBA8,
                    colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                return values
            }
            XCTAssertLessThanOrEqual(zip(pixels[0], pixels[1]).map { abs(Int($0) - Int($1)) }.max() ?? 0, 1)
        }
    }
    func testVectorFramesMatchCloudAtLargeZoomsAndRotatedStyles() throws {
        let fixture = try fixture()
        let root = String(#filePath.prefix(upTo: #filePath.range(of: "/src/apps/ios/")!.lowerBound))
        let font = URL(fileURLWithPath: root).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let canvas = CGSize(width: fixture.width, height: fixture.height)
        let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
        var maxError = 0.0
        for row in fixture.cases {
            if let selected = ProcessInfo.processInfo.environment["KRIA_GIANT_DEBUG_CASE"], row.id != selected { continue }
            let layer = try RecipeTextLayer.make(row.layer, assetURLs: ["font-Inter-Bold.ttf": font], canvas: canvas)
            let painter = try XCTUnwrap(layer.giantTitle)
            for sample in row.frames {
                if let selected = ProcessInfo.processInfo.environment["KRIA_GIANT_DEBUG_TIME"], Double(selected) != sample.time { continue }
                let image = try painter.image(localTime: sample.time, settled: layer.image)
                var actual = [UInt8](repeating: 0, count: fixture.width * fixture.height * 4)
                context.render(image, toBitmap: &actual, rowBytes: fixture.width * 4,
                    bounds: CGRect(origin: .zero, size: canvas), format: .RGBA8,
                    colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                var expected: [UInt8] = []
                expected.reserveCapacity(actual.count)
                for run in sample.pixels {
                    let pixel = (0..<4).map { UInt8((run[0] >> ($0 * 8)) & 255) }
                    for _ in 0..<Int(run[1]) { expected.append(contentsOf: pixel) }
                }
                XCTAssertEqual(actual.count, expected.count)
                let error = zip(actual, expected).reduce(0.0) { $0 + abs(Double($1.0) - Double($1.1)) } / Double(actual.count)
                maxError = max(maxError, error)
                // Core Graphics and Skia rasterize outlines differently. Bound
                // that tolerance to a two-pixel foreground edge, not a layout shift.
                do {
                    let tolerance = row.id == "styled-0" ? 3 : 2
                    func foreground(_ data: [UInt8], _ index: Int) -> Bool {
                        min(data[index * 4], data[index * 4 + 1], data[index * 4 + 2]) > 127
                    }
                    var distantMismatch = 0
                    for index in 0..<(fixture.width * fixture.height) {
                        let a = foreground(actual, index), e = foreground(expected, index)
                        if a == e { continue }
                        let other = a ? expected : actual
                        let x = index % fixture.width, y = index / fixture.width
                        var nearby = false
                        for sy in max(0, y - tolerance)...min(fixture.height - 1, y + tolerance) {
                            for sx in max(0, x - tolerance)...min(fixture.width - 1, x + tolerance) {
                                if foreground(other, sy * fixture.width + sx) { nearby = true }
                            }
                        }
                        if !nearby { distantMismatch += 1 }
                    }
                    XCTAssertEqual(distantMismatch, 0, "\(row.id) t=\(sample.time) edge displacement")
                }
                // Independent edge checks above prevent layout shifts from hiding
                // in the full-frame average. Hinted text plus large-size outlines
                // currently has a maximum mean RGBA difference of 2.385/255.
                XCTAssertLessThanOrEqual(error, 3.5, "\(row.id) t=\(sample.time) MAE=\(error)")
                if let directory = ProcessInfo.processInfo.environment["KRIA_GIANT_DEBUG_DIR"] {
                    try context.writePNGRepresentation(of: image, to: URL(fileURLWithPath: directory).appendingPathComponent("native-\(row.id)-\(sample.time).png"), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                }
            }
        }
        print("Giant-title max RGBA error: \(maxError)")
    }
#endif
}
