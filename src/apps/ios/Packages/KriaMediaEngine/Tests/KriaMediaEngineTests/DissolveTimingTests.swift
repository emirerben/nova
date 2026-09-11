import Foundation
import XCTest
@testable import KriaMediaEngine

final class DissolveTimingTests: XCTestCase {
    private struct Fixture: Decodable { let timing: [Timing]; let particles: [Particles] }
    private struct Timing: Decodable { let preset: DissolveTiming.Preset; let cap: Bool; let duration: Double; let time: Double; let state: DissolveSample }
    private struct Particles: Decodable { let preset: DissolveTiming.Preset; let seed: UInt32; let sourceAlpha: UInt8; let progress: Double; let cells: [Cell] }
    private struct Cell: Decodable { let x: UInt32; let y: UInt32; let alpha: UInt8 }
    func testTimingAndParticleAlphaMatchProduction() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_dissolve_timing.json").standardizedFileURL
        let fixture = try RecipeJSON.decoder().decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertEqual(fixture.timing.count, 96)
        XCTAssertEqual(fixture.particles.count, 96)
        for test in fixture.timing {
            let sample = try DissolveTiming.sample(localTime: test.time, duration: test.duration, preset: test.preset, capToWebkit: test.cap)
            XCTAssertEqual(sample.linearProgress, test.state.linearProgress, accuracy: 1e-10)
            XCTAssertEqual(sample.progress, test.state.progress, accuracy: 1e-10)
            XCTAssertEqual(sample.alpha, test.state.alpha, accuracy: 1e-10)
            XCTAssertEqual(sample.displacementScale, test.state.displacementScale, accuracy: 1e-8)
            XCTAssertEqual(sample.transformScale, test.state.transformScale, accuracy: 1e-10)
        }
        for test in fixture.particles {
            for cell in test.cells {
                XCTAssertEqual(try DissolveTiming.particleAlpha(sourceAlpha: test.sourceAlpha, cellX: cell.x, cellY: cell.y,
                    seed: test.seed, progress: test.progress, preset: test.preset), cell.alpha, "preset=\(test.preset) seed=\(test.seed) p=\(test.progress) cell=\(cell.x),\(cell.y)")
            }
        }
    }
}
