import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class ColoredClipTransitionTests: XCTestCase {
    @MainActor func testMovingVideoAgainstProductionFFmpeg() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_TRANSITION_VIDEO_FIXTURE_DIR"] else {
            throw XCTSkip("Set KRIA_TRANSITION_VIDEO_FIXTURE_DIR to production-generated moving video references")
        }
        let directory = URL(fileURLWithPath: path)
        let urls = ["a": directory.appendingPathComponent("a.mp4"), "b": directory.appendingPathComponent("b.mp4")]
        let context = CIContext()
        var metrics: [[String: Any]] = []
        func pixels(_ frame: CGImage) -> [UInt8] {
            var pixels = [UInt8](repeating: 0, count: 160 * 96 * 4)
            context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: 640,
                           bounds: CGRect(x: 0, y: 0, width: 160, height: 96), format: .RGBA8, colorSpace: nil)
            return pixels
        }
        for (name, kind) in [("fade", Transition.Kind.crossfade), ("fadeblack", .fadeBlack), ("fadewhite", .fadeWhite), ("wipeleft", .wipeLeft), ("wiperight", .wipeRight)] {
            let recipe = EditRecipe(canvas: Canvas(width: 160, height: 96), assets: urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) },
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [
                    TimelineClip(id: "a", sourceAssetID: "a", sourceDuration: 2),
                    TimelineClip(id: "b", sourceAssetID: "b", sourceDuration: 2, timelineStart: 1, transition: Transition(kind: kind, duration: 1))
                ])])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let output = directory.appendingPathComponent("native-\(name).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let previewFrames = AVAssetImageGenerator(asset: preview.playerItem.asset)
            previewFrames.videoComposition = preview.playerItem.videoComposition
            let exportFrames = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            let expected = AVAssetImageGenerator(asset: AVURLAsset(url: directory.appendingPathComponent("\(name).mp4")))
            for (actual, stage) in [(previewFrames, "preview"), (exportFrames, "export")] {
                for generator in [actual, expected] { generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero }
                for time in [0.5, 1.2, 1.5, 1.8, 2.5, 1.2] {
                    let at = CMTime(seconds: time, preferredTimescale: 600)
                    let actualFrame = try await actual.image(at: at).image
                    let expectedFrame = try await expected.image(at: at).image
                    let a = pixels(actualFrame)
                    let b = pixels(expectedFrame)
                    if time == 1.5 {
                        for (label, frame) in [(stage, actualFrame), ("cloud", expectedFrame)] {
                            try context.writePNGRepresentation(of: CIImage(cgImage: frame), to: directory.appendingPathComponent("\(name)-\(label).png"), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                        }
                    }
                    let error = a.indices.filter { $0 % 4 != 3 }.reduce(0.0) { $0 + abs(Double(a[$1]) - Double(b[$1])) } / Double(160 * 96 * 3)
                    print("CLIP_VIDEO_PARITY \(name) \(stage) \(time) mae=\(error)")
                    metrics.append(["transition": name, "stage": stage, "time": time, "mean_rgb_error": error])
                    // The native H.264 path resamples saturated 4:2:0 edges again;
                    // its no-transition baseline is already ~5 channel levels here.
                    // Preview stays stricter to catch source transfer-curve changes.
                    XCTAssertLessThan(error, stage == "preview" ? 5 : 7, "\(name) \(stage) at \(time)")
                }
            }
        }
        try JSONSerialization.data(withJSONObject: metrics, options: [.prettyPrinted, .sortedKeys])
            .write(to: directory.appendingPathComponent("colored-report.json"))
    }
}
#endif
