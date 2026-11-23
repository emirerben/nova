import Foundation
import XCTest
import AVFoundation
import KriaMediaEngine

/// KRI-1581, opt-in: renders every `KRIA_INFORMATIVE_TEXT_E2E_DIR/cases/<design>/recipe.json` that
/// `scripts/ios/kri-1581-text-designs-e2e.py prepare` compiled from the Kyoto edit (designs A1, A2,
/// A3, B1, through the real resolver and phone compile) with the production exporter, to
/// `cases/<design>/phone.mp4`. The script's `score` step then compares one frame per teardown
/// mock-up still with the mock-up and with the cloud Skia burn.
///
/// Branding is off, because the mock-ups have no watermark or outro. The duration is the recipe's
/// own. Skipped unless `KRIA_INFORMATIVE_TEXT_E2E_DIR` is set (`documents:` prefix supported).
@MainActor final class DeviceInformativeTextRenderE2ETests: XCTestCase {
    func testRendersEveryPreparedDesign() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_INFORMATIVE_TEXT_E2E_DIR"] else { throw XCTSkip("Set KRIA_INFORMATIVE_TEXT_E2E_DIR to run") }
        let root: URL
        if path.hasPrefix("documents:") {
            root = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                .appendingPathComponent(String(path.dropFirst("documents:".count)))
        } else {
            root = URL(fileURLWithPath: path)
        }
        let cases = root.appendingPathComponent("cases")
        let names = try FileManager.default.contentsOfDirectory(atPath: cases.path).sorted().filter { !$0.hasPrefix(".") }
        XCTAssertFalse(names.isEmpty, "no cases under \(cases.path)")
        for name in names {
            let directory = cases.appendingPathComponent(name)
            let recipe = try RecipeJSON.decode(Data(contentsOf: directory.appendingPathComponent("recipe.json")))
            let paths = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: directory.appendingPathComponent("assets.json")))
            let decision = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: recipe.effectiveCapabilities)).decide(for: recipe)
            XCTAssertEqual(decision.route, .local, "\(name): \(decision.reason ?? "")")
            let output = directory.appendingPathComponent("phone.mp4")
            try? FileManager.default.removeItem(at: output)
            let state = directory.appendingPathComponent("state")
            try? FileManager.default.removeItem(at: state)
            let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: state), branding: .none)
                .export(recipe: recipe, assetURLs: paths.mapValues { URL(fileURLWithPath: $0) }, outputURL: output)
            XCTAssertEqual(checkpoint.status, .completed, name)
            let duration = try await AVURLAsset(url: output).load(.duration).seconds
            XCTAssertEqual(duration, TimelineMath.totalDuration(of: recipe), accuracy: 0.1, name)
        }
    }
}
