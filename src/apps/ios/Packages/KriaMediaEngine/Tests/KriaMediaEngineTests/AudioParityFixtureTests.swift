#if canImport(AVFoundation)
import XCTest
@testable import KriaMediaEngine

/// Renders the recipes `scripts/ios/phone-audio-parity.py prepare` compiled with
/// the real phone compilers through the production exporter, so `compare` can
/// measure them against the cloud's mix of the same edit (KRI-139). Opt-in:
/// skipped unless `KRIA_AUDIO_PARITY_DIR` names a prepared directory.
final class AudioParityFixtureTests: XCTestCase {
    @MainActor func testRendersEveryPreparedCase() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_AUDIO_PARITY_DIR"] else { throw XCTSkip("Set KRIA_AUDIO_PARITY_DIR to run") }
        let cases = URL(fileURLWithPath: path).appendingPathComponent("cases")
        let names = try FileManager.default.contentsOfDirectory(atPath: cases.path).sorted()
        XCTAssertFalse(names.isEmpty)
        for name in names {
            let directory = cases.appendingPathComponent(name)
            let recipe = try RecipeJSON.decode(Data(contentsOf: directory.appendingPathComponent("recipe.json")))
            let paths = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: directory.appendingPathComponent("assets.json")))
            // The compilers' own capability set must route this recipe to the device.
            let decision = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: recipe.effectiveCapabilities)).decide(for: recipe)
            XCTAssertEqual(decision.route, .local, "\(name): \(decision.reason ?? "")")
            let output = directory.appendingPathComponent("phone.mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
                .export(recipe: recipe, assetURLs: paths.mapValues { URL(fileURLWithPath: $0) }, outputURL: output)
            XCTAssertTrue(FileManager.default.fileExists(atPath: output.path), name)
        }
    }
}
#endif
