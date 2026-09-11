import Foundation
import XCTest
@testable import KriaMediaEngine

final class DissolveNoiseTests: XCTestCase {
    private struct Fixture: Decodable { let cases: [Case]; let maps: [Map] }
    private struct Map: Decodable { let seed: UInt32; let pixels: [Pixel] }
    private struct Case: Decodable { let seed: UInt32; let frequency: Float; let pixels: [Pixel] }
    private struct Pixel: Decodable { let x: Int; let y: Int; let rgba: [Int] }

    func testNoiseMatchesProductionRasterSamples() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_dissolve_noise.json").standardizedFileURL
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertEqual(fixture.cases.count, 15)
        for test in fixture.cases {
            let noise = DissolveNoise(seed: test.seed)
            for pixel in test.pixels {
                let actual = noise.pixel(x: pixel.x, y: pixel.y, frequency: test.frequency)
                for channel in 0..<4 {
                    XCTAssertEqual(Double(actual[channel] * 255), Double(pixel.rgba[channel]), accuracy: 1,
                        "seed=\(test.seed) frequency=\(test.frequency) pixel=\(pixel.x),\(pixel.y) channel=\(channel)")
                }
            }
        }
        XCTAssertEqual(fixture.maps.count, 5)
        for test in fixture.maps {
            let noise = DissolveNoise(seed: test.seed)
            for pixel in test.pixels {
                let actual = noise.textMapPixel(x: pixel.x, y: pixel.y)
                for channel in 0..<4 {
                    XCTAssertEqual(Double(actual[channel] * 255), Double(pixel.rgba[channel]), accuracy: 1,
                        "map seed=\(test.seed) pixel=\(pixel.x),\(pixel.y) channel=\(channel)")
                }
            }
        }
    }
}
