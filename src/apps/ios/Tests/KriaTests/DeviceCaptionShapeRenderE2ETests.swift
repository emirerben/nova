import Foundation
import XCTest
import AVFoundation
import KriaMediaEngine

/// Qendresa item 12 (caption shape), opt-in: renders every
/// `KRIA_CAPTION_SHAPE_E2E_DIR/cases/<name>/recipe.json` that
/// `scripts/ios/qendresa-caption-shape-e2e.py prepare` compiled with the real
/// phone Talking caption chain (`q1_before`, `q1_after`, `q2_before`,
/// `q2_after`) through the production exporter on the simulator, to
/// `cases/<name>/phone.mp4`. The same script's `score` step then runs her kit's
/// scorer over those movies.
///
/// Branding: the app's real export paths (`NativeEditorSession` download,
/// `DeviceRenderSessions`) use `AVFoundationLocalExporter`'s default
/// `.standard` (watermark + outro tail), so this does too; the kit scorer is
/// run with `--ignore-tail-s 1.6` for the tail. The duration assertion adds the
/// bundled outro's own length to the recipe duration.
/// Skipped unless `KRIA_CAPTION_SHAPE_E2E_DIR` is set (`documents:` prefix supported).
@MainActor final class DeviceCaptionShapeRenderE2ETests: XCTestCase {
    func testRendersEveryPreparedCaptionShapeCase() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_CAPTION_SHAPE_E2E_DIR"] else { throw XCTSkip("Set KRIA_CAPTION_SHAPE_E2E_DIR to run") }
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
