#if canImport(AVFoundation)
import Foundation
import CoreImage
import XCTest
@testable import KriaMediaEngine

final class DissolveWarpTests: XCTestCase {
    private struct Fixture: Decodable { let width: Int; let height: Int; let cases: [Case] }
    private struct Case: Decodable { let seed: UInt32; let scale: Double; let pixels: [Pixel] }
    private struct Pixel: Decodable { let x: Int; let y: Int; let rgba: [Int] }

    func testMetalWarpMatchesProductionPixels() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_dissolve_warp.json").standardizedFileURL
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        let width = fixture.width, height = fixture.height
        var source = [UInt8](repeating: 0, count: width * height * 4)
        for y in 0..<height {
            for x in 0..<width {
                let i = (y * width + x) * 4
                source[i] = UInt8(x % 251); source[i + 1] = UInt8(y % 251)
                source[i + 2] = UInt8((x + y) % 251); source[i + 3] = 255
            }
        }
        let input = CIImage(bitmapData: Data(source), bytesPerRow: width * 4, size: CGSize(width: width, height: height), format: .RGBA8, colorSpace: nil)
        let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
        XCTAssertEqual(fixture.cases.count, 12)
        for seed in Set(fixture.cases.map(\.seed)) {
            let warp = try NativeDissolveWarp(width: width, height: height, seed: seed, maxBitmapBytes: width * height * 4)
            for test in fixture.cases where test.seed == seed {
                let output = try warp.image(source: input, scale: test.scale)
                var pixels = [UInt8](repeating: 0, count: source.count)
                context.render(output, toBitmap: &pixels, rowBytes: width * 4, bounds: warp.extent, format: .RGBA8, colorSpace: nil)
                for pixel in test.pixels {
                    let offset = (pixel.y * width + pixel.x) * 4
                    for channel in 0..<4 {
                        XCTAssertEqual(Int(pixels[offset + channel]), pixel.rgba[channel], "seed=\(seed) scale=\(test.scale) pixel=\(pixel.x),\(pixel.y) channel=\(channel)")
                    }
                }
            }
        }
    }
}
#endif
