import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class SplitScreenCompositionTests: XCTestCase {
    @MainActor func testMixedPanelsGapsClocksAndFullScreenBoundariesInPreviewAndExport() async throws {
        let retained = ProcessInfo.processInfo.environment["KRIA_SPLIT_SCREEN_ARTIFACTS"]
        let directory = retained.map { URL(fileURLWithPath: $0).appendingPathComponent(UUID().uuidString) }
            ?? FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { if retained == nil { try? FileManager.default.removeItem(at: directory) } }
        if retained != nil { print("Split-screen artifacts: \(directory.path)") }
        let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1)
        let green = CGColor(red: 0, green: 1, blue: 0, alpha: 1)
        let video = try await SolidVideoFixture.make(directory: directory, name: "clock", color: red, seconds: 3,
            frameColor: { $0 < 30 ? red : green })
        let photo = directory.appendingPathComponent("blue.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .blue).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let urls = ["video": video, "photo": photo]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { RenderAssetReference(id: $0.id,
            fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id)) })
        for count in [2, 3] {
            var tracks = [TimelineTrack(id: "ordinary", kind: .video, clips: [
                TimelineClip(id: "before", sourceAssetID: "photo", sourceDuration: 0.5),
                TimelineClip(id: "after", sourceAssetID: "photo", sourceDuration: 0.5, timelineStart: 2.5)
            ]), TimelineTrack(id: "top", kind: .video, clips: [
                TimelineClip(id: "top", sourceAssetID: "video", sourceStart: 1, sourceDuration: 1, timelineStart: 0.5,
                    transform: MediaTransform(scale: 2, rotationDegrees: 25), volume: 0.7,
                    splitScreen: .init(sectionID: "s", panelIndex: 0))
            ]), TimelineTrack(id: "bottom", kind: .video, clips: [
                TimelineClip(id: "bottom", sourceAssetID: "photo", sourceDuration: 1.5, timelineStart: 1,
                    splitScreen: .init(sectionID: "s", panelIndex: count - 1))
            ])]
            if count == 3 { tracks.append(TimelineTrack(id: "middle", kind: .video, clips: [
                TimelineClip(id: "middle", sourceAssetID: "video", sourceDuration: 0.5, timelineStart: 0.5,
                    splitScreen: .init(sectionID: "s", panelIndex: 1))])) }
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160),
                assets: assets, tracks: tracks, assetManifest: manifest,
                splitScreenSections: [.init(id: "s", start: 0.5, end: 2.5, panelWeights: Array(repeating: 1, count: count), backgroundColor: "#FFFFFF")])
            try recipe.validate()
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let audioTracks = try await preview.playerItem.asset.loadTracks(withMediaType: .audio)
            XCTAssertFalse(audioTracks.isEmpty, "Panel video must retain source audio")
            let output = directory.appendingPathComponent("split-\(count).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-\(count)")), branding: .none)
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let live = AVAssetImageGenerator(asset: preview.playerItem.asset)
            live.videoComposition = preview.playerItem.videoComposition
            for generator in [live, AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                for (time, y, channel) in [(0.0, 150, 2), (0.5, 150, 1), (1.0, 10, 2), (2.5, 150, 2)] {
                    let cg = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                    let pixel = rgba(CIImage(cgImage: cg), x: 48, y: y)
                    XCTAssertGreaterThan(pixel[channel], 210, "time \(time), panels \(count)")
                    XCTAssertLessThan(pixel[(channel + 1) % 3], 45)
                }
                for (time, y) in [(0.5, 10), (2.0, 150)] {
                    let cg = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                    XCTAssertTrue(rgba(CIImage(cgImage: cg), x: 48, y: y).prefix(3).allSatisfy { $0 > 210 }, "Gap must show white, even beside rotated media")
                }
            }
        }
    }

    private func rgba(_ image: CIImage, x: Int, y: Int) -> [UInt8] {
        var pixel = [UInt8](repeating: 0, count: 4)
        CIContext().render(image, toBitmap: &pixel, rowBytes: 4, bounds: CGRect(x: x, y: y, width: 1, height: 1),
            format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return pixel
    }
}
#endif
