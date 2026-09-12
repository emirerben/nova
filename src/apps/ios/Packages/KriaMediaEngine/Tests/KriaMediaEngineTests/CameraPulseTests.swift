import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
#endif

final class CameraPulseTests: XCTestCase {
    func testPulseBoundariesAndOverlapCapAreIndependentOfSeekOrder() throws {
        let pulse = CameraPulse(id: "one", start: 1, end: 2, intensity: 0.08)
        let other = CameraPulse(id: "two", start: 1, end: 2, intensity: 0.08)
        try pulse.validate()
        XCTAssertEqual(CameraPulse.scale(pulses: [pulse], time: 1, dimension: 1080), 1)
        XCTAssertEqual(CameraPulse.scale(pulses: [pulse], time: 1.5, dimension: 1080), 1166.0 / 1080)
        XCTAssertEqual(CameraPulse.scale(pulses: [pulse, other], time: 1.5, dimension: 1080), 1208.0 / 1080)
        XCTAssertEqual(CameraPulse.scale(pulses: [pulse], time: 2, dimension: 1080), 1)
        XCTAssertThrowsError(try CameraPulse(id: "bad", start: 1, end: 0, intensity: 0.04).validate())
    }

    #if canImport(AVFoundation)
    @MainActor func testCameraCropMatchesPreviewAndExportAtBoundariesAndBackwardSeeks() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let canvas = CGRect(x: 0, y: 0, width: 200, height: 200)
        let stripe = CIImage(color: .white).cropped(to: CGRect(x: 0, y: 0, width: 8, height: 200))
        let picture = stripe.composited(over: CIImage(color: .black).cropped(to: canvas)).cropped(to: canvas)
        let photo = directory.appendingPathComponent("stripe.png")
        let context = CIContext()
        try context.writePNGRepresentation(of: picture, to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: photo)
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 200, height: 200),
            assets: [MediaAsset(id: "photo", relativePath: "photo", fingerprint: fingerprint)],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 3)])],
            assetManifest: RenderAssetManifest(assets: [RenderAssetReference(id: "photo", fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "photo"))]),
            cameraPulses: [CameraPulse(id: "pulse", start: 0.5, end: 1.5, intensity: 0.08)])
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: ["photo": photo])
        let output = directory.appendingPathComponent("camera.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: ["photo": photo], outputURL: output)
        let native = AVAssetImageGenerator(asset: preview.preview.playerItem.asset)
        native.videoComposition = preview.preview.playerItem.videoComposition
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        var comparisons: [[Int]] = []
        for generator in [native, exported] {
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = .zero
            var counts: [Int] = []
            for time in [0.0, 0.5, 1.0, 1.5, 1.0, 0.0] {
                let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: 200 * 200 * 4)
                context.render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 800, bounds: canvas,
                    format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                counts.append(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 }.count)
            }
            XCTAssertEqual(counts[0], counts[1])
            XCTAssertLessThan(counts[2], counts[0] / 3)
            XCTAssertEqual(counts[3], counts[0])
            XCTAssertEqual(counts[4], counts[2])
            XCTAssertEqual(counts[5], counts[0])
            comparisons.append(counts)
        }
        for (previewCount, exportCount) in zip(comparisons[0], comparisons[1]) {
            XCTAssertLessThanOrEqual(abs(previewCount - exportCount), 200)
        }
        var edited = recipe
        edited.cameraPulses = []
        let item = preview.preview.playerItem
        let source = item.asset
        try preview.updateText(recipe: edited)
        XCTAssertTrue(preview.preview.playerItem === item)
        XCTAssertTrue(item.asset === source)
    }
    #endif
}
