import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class GoldenHourCompositionTests: XCTestCase {
    @MainActor func testMovingPreviewExportAndBackwardSeek() async throws {
        try await verifyMoving(width: 160, height: 96, suffix: "")
    }

    @MainActor func testFullCanvasMovingPreviewAndExport() async throws {
        try await verifyMoving(width: 1080, height: 1920, suffix: "-full")
    }

    @MainActor private func verifyMoving(width: Int, height: Int, suffix: String) async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_LOOK_FIXTURE_DIR"] else {
            throw XCTSkip("Set KRIA_LOOK_FIXTURE_DIR to production look references")
        }
        let directory = URL(fileURLWithPath: path)
        let urls = ["source": directory.appendingPathComponent("source\(suffix).mp4")]
        let recipe = EditRecipe(canvas: Canvas(width: width, height: height),
            assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "a", sourceAssetID: "source", sourceDuration: 2, look: .goldenHour)
            ])])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.goldenHourLook))
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("native-golden\(suffix).mp4")
        let exportStart = Date()
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        print("GOLDEN_EXPORT \(width)x\(height) seconds=\(Date().timeIntervalSince(exportStart))")
        let previewFrames = AVAssetImageGenerator(asset: preview.playerItem.asset)
        previewFrames.videoComposition = preview.playerItem.videoComposition
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        let expected = AVAssetImageGenerator(asset: AVURLAsset(url: directory.appendingPathComponent("golden\(suffix).mp4")))
        let context = CIContext()
        func pixels(_ frame: CGImage) -> [UInt8] {
            var values = [UInt8](repeating: 0, count: width * height * 4)
            context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &values, rowBytes: width * 4,
                bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
            return values
        }
        var metrics: [[String: Any]] = []
        var passthrough = recipe
        passthrough.tracks[0].clips[0].look = nil
        let baseline = try await AVPlayerPreviewComposer().makePreview(recipe: passthrough,
            assetURLs: ["source": directory.appendingPathComponent("golden\(suffix).mp4")])
        let baselineFrames = AVAssetImageGenerator(asset: baseline.playerItem.asset)
        let yuvComposition = try XCTUnwrap(baseline.playerItem.videoComposition?.mutableCopy() as? AVMutableVideoComposition)
        yuvComposition.customVideoCompositorClass = RecipeLookVideoCompositor.self
        baselineFrames.videoComposition = yuvComposition
        baseline.playerItem.videoComposition = yuvComposition
        let baselineURL = directory.appendingPathComponent("passthrough-golden\(suffix).mp4")
        if FileManager.default.fileExists(atPath: baselineURL.path) { try FileManager.default.removeItem(at: baselineURL) }
        let baselineWriter = try await RecipeWriter(preview: baseline, outputURL: baselineURL, bitrate: 8_000_000)
        try await baselineWriter.run(progress: nil)
        let baselineExport = AVAssetImageGenerator(asset: AVURLAsset(url: baselineURL))
        baselineExport.requestedTimeToleranceBefore = .zero
        baselineExport.requestedTimeToleranceAfter = .zero
        baselineFrames.requestedTimeToleranceBefore = .zero
        baselineFrames.requestedTimeToleranceAfter = .zero
        for (generator, stage) in [(previewFrames, "preview"), (exported, "export")] {
            for frames in [generator, expected] { frames.requestedTimeToleranceBefore = .zero; frames.requestedTimeToleranceAfter = .zero }
            for seconds in [0.0, 0.2, 0.7, 1.2, 1.8, 0.2] {
                let at = CMTime(seconds: seconds, preferredTimescale: 600)
                let frame = try await generator.image(at: at).image
                let cloud = try await expected.image(at: at).image
                let a = pixels(frame), b = pixels(cloud)
                let baselineGenerator = stage == "preview" ? baselineFrames : baselineExport
                let baselinePixels = pixels(try await baselineGenerator.image(at: at).image)
                let baselineError = b.indices.filter { $0 % 4 != 3 }.reduce(0.0) { $0 + abs(Double(baselinePixels[$1]) - Double(b[$1])) } / Double(width * height * 3)
                let error = a.indices.filter { $0 % 4 != 3 }.reduce(0.0) { $0 + abs(Double(a[$1]) - Double(b[$1])) } / Double(width * height * 3)
                print("GOLDEN_PARITY \(stage) \(seconds) mae=\(error) baseline=\(baselineError)")
                metrics.append(["stage": stage, "time": seconds, "mean_rgb_error": error, "passthrough_rgb_error": baselineError])
                // Core Image NV12 upsampling differs from ImageGenerator's RGB
                // path by up to 5.17 levels on these saturated 160px edges even
                // with no effect. Bound that baseline independently, and allow
                // the grade+cloud re-encode at most half a level beyond it.
                XCTAssertLessThan(baselineError, width > 160 ? 2.25 : 5.5)
                XCTAssertLessThan(error, width > 160 ? 2.5 : 6)
                XCTAssertLessThan(error - baselineError, 0.5)
                if seconds == 0.7 {
                    for (name, image) in [(stage, frame), ("cloud", cloud)] {
                        try context.writePNGRepresentation(of: CIImage(cgImage: image), to: directory.appendingPathComponent("golden\(suffix)-\(name).png"), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    }
                }
            }
        }
        try JSONSerialization.data(withJSONObject: metrics, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("moving-look\(suffix)-report.json"))
        var resized = recipe
        resized.canvas.width = 320
        do {
            _ = try await AVPlayerPreviewComposer().makePreview(recipe: resized, assetURLs: urls)
            XCTFail("Resizing before a look must remain unsupported")
        } catch { XCTAssertEqual(error as? MediaEngineError, .unsupportedCapability) }
    }
}
#endif
