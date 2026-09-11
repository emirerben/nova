import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class GoldenHourCompositionTests: XCTestCase {
    @MainActor func testMovingPreviewExportAndBackwardSeek() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_LOOK_FIXTURE_DIR"] else {
            throw XCTSkip("Set KRIA_LOOK_FIXTURE_DIR to production look references")
        }
        let directory = URL(fileURLWithPath: path)
        let urls = ["source": directory.appendingPathComponent("source.mp4")]
        let recipe = EditRecipe(canvas: Canvas(width: 160, height: 96),
            assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "a", sourceAssetID: "source", sourceDuration: 2, look: .goldenHour)
            ])])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.goldenHourLook))
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("native-golden.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let previewFrames = AVAssetImageGenerator(asset: preview.playerItem.asset)
        previewFrames.videoComposition = preview.playerItem.videoComposition
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        let expected = AVAssetImageGenerator(asset: AVURLAsset(url: directory.appendingPathComponent("golden.mp4")))
        let context = CIContext()
        func pixels(_ frame: CGImage) -> [UInt8] {
            var values = [UInt8](repeating: 0, count: 160 * 96 * 4)
            context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &values, rowBytes: 640,
                bounds: CGRect(x: 0, y: 0, width: 160, height: 96), format: .RGBA8, colorSpace: nil)
            return values
        }
        var metrics: [[String: Any]] = []
        var passthrough = recipe
        passthrough.tracks[0].clips[0].look = nil
        let baseline = try await AVPlayerPreviewComposer().makePreview(recipe: passthrough,
            assetURLs: ["source": directory.appendingPathComponent("golden.mp4")])
        let baselineFrames = AVAssetImageGenerator(asset: baseline.playerItem.asset)
        let yuvComposition = try XCTUnwrap(baseline.playerItem.videoComposition?.mutableCopy() as? AVMutableVideoComposition)
        yuvComposition.customVideoCompositorClass = RecipeLookVideoCompositor.self
        baselineFrames.videoComposition = yuvComposition
        baselineFrames.requestedTimeToleranceBefore = .zero
        baselineFrames.requestedTimeToleranceAfter = .zero
        for (generator, stage) in [(previewFrames, "preview"), (exported, "export")] {
            for frames in [generator, expected] { frames.requestedTimeToleranceBefore = .zero; frames.requestedTimeToleranceAfter = .zero }
            for seconds in [0.0, 0.2, 0.7, 1.2, 1.8, 0.2] {
                let at = CMTime(seconds: seconds, preferredTimescale: 600)
                let frame = try await generator.image(at: at).image
                let cloud = try await expected.image(at: at).image
                let a = pixels(frame), b = pixels(cloud)
                let baselinePixels = pixels(try await baselineFrames.image(at: at).image)
                let baselineError = b.indices.filter { $0 % 4 != 3 }.reduce(0.0) { $0 + abs(Double(baselinePixels[$1]) - Double(b[$1])) } / Double(160 * 96 * 3)
                let error = a.indices.filter { $0 % 4 != 3 }.reduce(0.0) { $0 + abs(Double(a[$1]) - Double(b[$1])) } / Double(160 * 96 * 3)
                print("GOLDEN_PARITY \(stage) \(seconds) mae=\(error) baseline=\(baselineError)")
                metrics.append(["stage": stage, "time": seconds, "mean_rgb_error": error, "passthrough_rgb_error": baselineError])
                // Core Image NV12 upsampling differs from ImageGenerator's RGB
                // path by up to 5.17 levels on these saturated 160px edges even
                // with no effect. Bound that baseline independently, and allow
                // the grade+cloud re-encode at most half a level beyond it.
                XCTAssertLessThan(baselineError, 5.5)
                XCTAssertLessThan(error, 6)
                XCTAssertLessThan(error - baselineError, 0.5)
                if seconds == 0.7 {
                    for (name, image) in [(stage, frame), ("cloud", cloud)] {
                        try context.writePNGRepresentation(of: CIImage(cgImage: image), to: directory.appendingPathComponent("golden-\(name).png"), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    }
                }
            }
        }
        try JSONSerialization.data(withJSONObject: metrics, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("moving-look-report.json"))
        var resized = recipe
        resized.canvas.width = 320
        do {
            _ = try await AVPlayerPreviewComposer().makePreview(recipe: resized, assetURLs: urls)
            XCTFail("Resizing before a look must remain unsupported")
        } catch { XCTAssertEqual(error as? MediaEngineError, .unsupportedCapability) }
    }
}
#endif
