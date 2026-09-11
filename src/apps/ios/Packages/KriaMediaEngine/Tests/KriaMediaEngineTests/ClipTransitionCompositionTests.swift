import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class ClipTransitionCompositionTests: XCTestCase {
    @MainActor func testCrossfadeMatchesCloudEncodedMidpoint() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let context = CIContext()
        var urls: [String: URL] = [:]
        for (name, color) in [("black", CIColor.black), ("white", CIColor.white)] {
            let url = directory.appendingPathComponent("\(name).png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: 64, height: 64)), to: url, format: .RGBA8, colorSpace: space)
            urls[name] = url
        }
        let recipe = EditRecipe(canvas: Canvas(width: 64, height: 64), assets: [MediaAsset(id: "black", relativePath: "black.png"), MediaAsset(id: "white", relativePath: "white.png")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [
            TimelineClip(id: "a", sourceAssetID: "black", sourceDuration: 2),
            TimelineClip(id: "b", sourceAssetID: "white", sourceDuration: 2, timelineStart: 1, transition: Transition(duration: 1))
        ])])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("transition.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let previewFrames = AVAssetImageGenerator(asset: preview.playerItem.asset)
        previewFrames.videoComposition = preview.playerItem.videoComposition
        let exportFrames = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        for (generator, tolerance) in [(previewFrames, 2.0), (exportFrames, 3.0)] {
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = .zero
            // FFmpeg xfade=fade with black/white YUV sources gives these encoded
            // RGB values. Disable color management when reading encoded bytes:
            // converting the AVFoundation Rec.709 image to sRGB changes mid-gray.
            for (time, expected) in [(0.5, 0.0), (1.2, 51.0), (1.5, 127.0), (1.8, 204.0), (2.5, 255.0), (1.2, 51.0)] {
                let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixel = [UInt8](repeating: 0, count: 4)
                context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixel, rowBytes: 4,
                               bounds: CGRect(x: 32, y: 32, width: 1, height: 1), format: .RGBA8, colorSpace: nil)
                for channel in pixel.prefix(3) { XCTAssertEqual(Double(channel), expected, accuracy: tolerance, "time \(time)") }
            }
        }
    }
}
#endif
