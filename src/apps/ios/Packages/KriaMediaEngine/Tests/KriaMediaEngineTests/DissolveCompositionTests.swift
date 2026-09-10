#if canImport(AVFoundation)
import Foundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import KriaMediaEngine

final class DissolveCompositionTests: XCTestCase {
    private struct Fixture: Decodable { let width: Int; let height: Int; let rectangles: [[Int]]; let cases: [Case] }
    private struct Case: Decodable { let seed: UInt32; let time: Double; let alphaRuns: [[Int]]; let totalAlpha: Int }

    func testFullDissolveMatchesCloudComposition() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_dissolve_composition.json").standardizedFileURL
        let fixture = try RecipeJSON.decoder().decode(Fixture.self, from: Data(contentsOf: url))
        let width = fixture.width, height = fixture.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        for rect in fixture.rectangles {
            for y in rect[1]..<(rect[1] + rect[3]) {
                for x in rect[0]..<(rect[0] + rect[2]) {
                    let offset = (y * width + x) * 4
                    for channel in 0..<4 { bytes[offset + channel] = 255 }
                }
            }
        }
        let input = CIImage(bitmapData: Data(bytes), bytesPerRow: width * 4, size: CGSize(width: width, height: height), format: .RGBA8, colorSpace: nil)
        let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
        for seed in Set(fixture.cases.map(\.seed)) {
            let painter = try NativeDissolveRenderer(width: width, height: height, seed: seed, maxBitmapBytes: width * height * 8)
            for test in fixture.cases where test.seed == seed {
                let image = try painter.image(source: input, localTime: test.time, duration: 4)
                var output = [UInt8](repeating: 0, count: bytes.count)
                context.render(image, toBitmap: &output, rowBytes: width * 4, bounds: painter.warp.extent, format: .RGBA8, colorSpace: nil)
                if let directory = ProcessInfo.processInfo.environment["KRIA_DISSOLVE_DEBUG_DIR"],
                   let cgImage = context.createCGImage(image, from: painter.warp.extent) {
                    let url = URL(fileURLWithPath: directory).appendingPathComponent("native-\(seed)-\(test.time).png")
                    let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil))
                    CGImageDestinationAddImage(destination, cgImage, nil)
                    XCTAssertTrue(CGImageDestinationFinalize(destination))
                }
                let expected = test.alphaRuns.flatMap { Array(repeating: $0[0], count: $0[1]) }
                XCTAssertEqual(expected.count, width * height)
                var absoluteDifference = 0, actualInk = 0
                for index in expected.indices {
                    let actual = Int(output[index * 4 + 3])
                    absoluteDifference += abs(actual - expected[index])
                    actualInk += actual
                }
                // Compare every alpha pixel. Sparse grids can overcount a
                // one-pixel edge shift when synthetic strokes align to the grid.
                let mean = Double(absoluteDifference) / Double(expected.count)
                XCTAssertLessThanOrEqual(mean, 1, "seed=\(seed) time=\(test.time) alpha MAE=\(mean)")
                XCTAssertEqual(Double(actualInk), Double(test.totalAlpha), accuracy: max(1, Double(test.totalAlpha) * 0.03), "seed=\(seed) time=\(test.time)")
            }
        }
    }
}
#endif
