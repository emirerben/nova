import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class ClipTransitionCompositionTests: XCTestCase {
    @MainActor func testFadesAndWipesMatchCloudFramesInPreviewAndExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        var urls: [String: URL] = [:]
        for (name, color) in [("gray", CIColor(red: 128.0/255, green: 128.0/255, blue: 128.0/255)), ("white", CIColor.white)] {
            let url = directory.appendingPathComponent("\(name).png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: 16, height: 16)), to: url, format: .RGBA8, colorSpace: space)
            urls[name] = url
        }
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_clip_transitions.json").standardizedFileURL
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as? [String: Any])
        let rows = try XCTUnwrap(document["rows"] as? [[String: Any]])
        for (name, kind) in [("fadeblack", Transition.Kind.fadeBlack), ("fadewhite", .fadeWhite), ("wipeleft", .wipeLeft), ("wiperight", .wipeRight)] {
            let recipe = EditRecipe(canvas: Canvas(width: 16, height: 16), assets: [MediaAsset(id: "gray", relativePath: "gray.png"), MediaAsset(id: "white", relativePath: "white.png")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "a", sourceAssetID: "gray", sourceDuration: 2),
                TimelineClip(id: "b", sourceAssetID: "white", sourceDuration: 2, timelineStart: 1, transition: Transition(kind: kind, duration: 1))
            ])])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let output = directory.appendingPathComponent("\(name).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let previewFrames = AVAssetImageGenerator(asset: preview.playerItem.asset)
            previewFrames.videoComposition = preview.playerItem.videoComposition
            let exportFrames = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            for (generator, tolerance) in [(previewFrames, 3.0), (exportFrames, 5.0)] {
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                for row in rows.filter({ $0["effect"] as? String == name }).reversed() {
                    let progress = try XCTUnwrap(row["progress"] as? Double)
                    let expected = try XCTUnwrap(row["rgb_row"] as? [Double])
                    let frame = try await generator.image(at: CMTime(seconds: 1 + progress, preferredTimescale: 600)).image
                    var pixels = [UInt8](repeating: 0, count: 16 * 4)
                    context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: 64,
                                   bounds: CGRect(x: 0, y: 8, width: 16, height: 1), format: .RGBA8, colorSpace: nil)
                    for x in 0..<16 {
                        for channel in 0..<3 { XCTAssertEqual(Double(pixels[x*4+channel]), expected[x*3+channel], accuracy: tolerance, "\(name) \(progress) x=\(x)") }
                    }
                }
            }
        }
    }

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
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
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

    /// A single flat-color asset for the KRI-163-follow-up tests below, which
    /// only need to know whether `makePreview` throws -- not what it renders.
    @MainActor private func grayAsset() throws -> (directory: URL, url: URL) {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let url = directory.appendingPathComponent("gray.png")
        try context.writePNGRepresentation(of: CIImage(color: CIColor(red: 0.5, green: 0.5, blue: 0.5))
            .cropped(to: CGRect(x: 0, y: 0, width: 16, height: 16)), to: url, format: .RGBA8, colorSpace: space)
        return (directory, url)
    }

    // KRI-163 follow-up: a recipe built directly by this compositor (the
    // native editor's own local preview/export) has no refit awareness of
    // its own, unlike the server's phone-plan compiler -- an authored fade
    // can arrive here with only some of its requested overlap actually
    // available. These three tests pin the same clamp/cut/refuse behavior
    // `phone_guided_plan.py`'s equivalent server-side fix already has,
    // applied at this shared choke point instead.

    @MainActor func testCrossfadeShortensWhenThePreviousClipRunsShort() async throws {
        let (directory, url) = try grayAsset()
        defer { try? FileManager.default.removeItem(at: directory) }
        let recipe = EditRecipe(canvas: Canvas(width: 16, height: 16), assets: [MediaAsset(id: "gray", relativePath: "gray.png")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [
            // "a" ends at 1.95 (a 0.05s refit shrink off a nominal 2s clip);
            // "b" starts at 1.7, expecting a 0.3s fade -- only 0.25s of real
            // overlap exists. Must compile with the fade shortened, not throw.
            TimelineClip(id: "a", sourceAssetID: "gray", sourceDuration: 1.95),
            TimelineClip(id: "b", sourceAssetID: "gray", sourceDuration: 2, timelineStart: 1.7, transition: Transition(duration: 0.3)),
        ])])
        _ = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["gray": url])
    }

    @MainActor func testCrossfadeDropsToACutWhenLessThanAFrameOfOverlapRemains() async throws {
        let (directory, url) = try grayAsset()
        defer { try? FileManager.default.removeItem(at: directory) }
        let recipe = EditRecipe(canvas: Canvas(width: 16, height: 16), assets: [MediaAsset(id: "gray", relativePath: "gray.png")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [
            // Only 0.01s of overlap remains -- under one frame at 30fps, too
            // little to render any crossfade at all. Dropped to a hard cut
            // rather than refused outright.
            TimelineClip(id: "a", sourceAssetID: "gray", sourceDuration: 1.71),
            TimelineClip(id: "b", sourceAssetID: "gray", sourceDuration: 2, timelineStart: 1.7, transition: Transition(duration: 0.3)),
        ])])
        _ = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["gray": url])
    }

    @MainActor func testCrossfadeStillThrowsOnAGenuineGap() async throws {
        let (directory, url) = try grayAsset()
        defer { try? FileManager.default.removeItem(at: directory) }
        let recipe = EditRecipe(canvas: Canvas(width: 16, height: 16), assets: [MediaAsset(id: "gray", relativePath: "gray.png")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [
            // "a" ends at 1.0, entirely before "b" starts (1.7) -- a real
            // gap, not a rounding-margin shortfall the clamp can absorb.
            TimelineClip(id: "a", sourceAssetID: "gray", sourceDuration: 1),
            TimelineClip(id: "b", sourceAssetID: "gray", sourceDuration: 2, timelineStart: 1.7, transition: Transition(duration: 0.3)),
        ])])
        do {
            _ = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["gray": url])
            XCTFail("expected invalidTimeline")
        } catch {
            XCTAssertEqual(error as? RecipeError, .invalidTimeline)
        }
    }
}
#endif
