import Foundation
import XCTest
import AVFoundation
import KriaMediaEngine
@testable import Kria

/// Qendresa item 21 (clip text look): renders every prepared before/after case.
/// `scripts/ios/qendresa-clip-text-look-e2e.py prepare` writes
/// `KRIA_CLIP_TEXT_LOOK_E2E_DIR/cases/<name>/{recipe.json,assets.json}`; each case is
/// exported through the production `AVFoundationLocalExporter` (default branding: watermark
/// plus outro) to `cases/<name>/phone.mp4`, which `score` then checks.
@MainActor final class DeviceClipTextLookRenderE2ETests: XCTestCase {
    func testRendersEveryPreparedClipTextLookCase() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_CLIP_TEXT_LOOK_E2E_DIR"] else {
            throw XCTSkip("Set KRIA_CLIP_TEXT_LOOK_E2E_DIR to run")
        }
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
        var tail = 0.0
        if let outro = KriaBranding.outroURL() {
            tail = try await AVURLAsset(url: outro).load(.duration).seconds
        }
        for name in names {
            let directory = cases.appendingPathComponent(name)
            guard FileManager.default.fileExists(atPath: directory.appendingPathComponent("recipe.json").path) else { continue }
            let recipe = try RecipeJSON.decode(Data(contentsOf: directory.appendingPathComponent("recipe.json")))
            let paths = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: directory.appendingPathComponent("assets.json")))
            let decision = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: recipe.effectiveCapabilities)).decide(for: recipe)
            XCTAssertEqual(decision.route, .local, "\(name): \(decision.reason ?? "")")
            let output = directory.appendingPathComponent("phone.mp4")
            try? FileManager.default.removeItem(at: output)
            let state = directory.appendingPathComponent("state")
            try? FileManager.default.removeItem(at: state)
            let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: state))
                .export(recipe: recipe, assetURLs: paths.mapValues { URL(fileURLWithPath: $0) }, outputURL: output)
            XCTAssertEqual(checkpoint.status, .completed, name)
            XCTAssertTrue(FileManager.default.fileExists(atPath: output.path), name)
            let duration = try await AVURLAsset(url: output).load(.duration).seconds
            XCTAssertEqual(duration, TimelineMath.totalDuration(of: recipe) + tail, accuracy: 0.1, name)
        }
    }
}
