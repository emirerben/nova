import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

/// KRI-1139: a clip's `.media` backdrop (a Visuals-pool photo or video) rendered through the real compositor
/// (the AVPlayer path and the exporter) and read back as pixels.
final class ClipBackdropMediaCompositionTests: XCTestCase {
    private let width = 180, height = 320
    private let pool = "pool-1"
    private let quadrants: [[Int]] = [[220, 40, 40], [40, 200, 60], [50, 60, 230], [230, 210, 40]]

    private func writePNG(to url: URL, color: (Int, Int) -> [UInt8]) throws {
        var pixels = [UInt8](repeating: 255, count: width * height * 4)
        for y in 0..<height { for x in 0..<width {
            let value = color(x, y)
            for channel in 0..<3 { pixels[(y * width + x) * 4 + channel] = value[channel] }
        } }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        let image = try XCTUnwrap(CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    private func scene(in directory: URL) throws -> URL {
        let url = directory.appendingPathComponent("scene.png")
        try writePNG(to: url) { _, _ in [128, 128, 128] }
        return url
    }

    /// A canvas-shaped photo: top-left, top-right, bottom-left, bottom-right quadrants of `quadrants`.
    private func backdropPhoto(in directory: URL) throws -> URL {
        let url = directory.appendingPathComponent("backdrop.png")
        try writePNG(to: url) { x, y in
            self.quadrants[(y < self.height / 2 ? 0 : 2) + (x < self.width / 2 ? 0 : 1)].map { UInt8($0) }
        }
        return url
    }

    private func recipe(media: URL, kind: VisualMediaKind, scene sceneURL: URL, rotation: Double, scale: Double = 1,
                        duration: Double = 1, backdrop: Bool = true) throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let assetID = "visual-" + pool
        let sceneFingerprint = try SHA256Fingerprinter().fingerprint(file: sceneURL)
        let mediaFingerprint = try SHA256Fingerprinter().fingerprint(file: media)
        let recipe = EditRecipe(
            schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height),
            assets: [MediaAsset(id: "scene", relativePath: "scene", fingerprint: sceneFingerprint),
                     MediaAsset(id: assetID, relativePath: assetID, fingerprint: mediaFingerprint)],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [
                TimelineClip(id: "tilted", sourceAssetID: "scene", sourceDuration: duration,
                             transform: MediaTransform(scale: scale, rotationDegrees: rotation), backdrop: backdrop ? .media(pool) : nil)
            ])],
            assetManifest: RenderAssetManifest(assets: [
                RenderAssetReference(id: "scene", fingerprint: try RenderFingerprint(sceneFingerprint), source: .original(mediaID: "scene")),
                RenderAssetReference(id: assetID, fingerprint: try RenderFingerprint(mediaFingerprint),
                                     source: .visual(visualID: pool, generation: "1", mediaKind: kind)),
            ]))
        try recipe.validate()
        return (recipe, ["scene": sceneURL, assetID: media])
    }

    private func assertClose(_ actual: [Int], _ expected: [Int], within tolerance: Int, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        let delta = zip(actual, expected).map { abs($0 - $1) }.max() ?? 0
        XCTAssertLessThanOrEqual(delta, tolerance, "\(message): \(actual) vs \(expected)", file: file, line: line)
    }

    /// The compositor's own BGRA output at `seconds`, read without any colour conversion (as `ClipTiltCompositionTests` does).
    private struct Frame {
        let bytes: [UInt8], width: Int
        var height: Int { bytes.count / 4 / width }
        func rgb(_ x: Int, _ y: Int) -> [Int] { [Int(bytes[(y * width + x) * 4 + 2]), Int(bytes[(y * width + x) * 4 + 1]), Int(bytes[(y * width + x) * 4])] }
        var corners: [[Int]] { [rgb(4, 4), rgb(width - 5, 4), rgb(4, height - 5), rgb(width - 5, height - 5)] }
    }

    private let bgra: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]

    private func firstFrame(of output: AVAssetReaderOutput, reader: AVAssetReader) throws -> Frame {
        XCTAssertTrue(reader.startReading(), "\(String(describing: reader.error))")
        let sample = try XCTUnwrap(output.copyNextSampleBuffer())
        let buffer = try XCTUnwrap(CMSampleBufferGetImageBuffer(sample))
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let (w, h, stride) = (CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer))
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(buffer)).assumingMemoryBound(to: UInt8.self)
        var bytes = [UInt8](repeating: 0, count: w * h * 4)
        for row in 0..<h { bytes.withUnsafeMutableBufferPointer { $0.baseAddress!.advanced(by: row * w * 4).update(from: base.advanced(by: row * stride), count: w * 4) } }
        return Frame(bytes: bytes, width: w)
    }

    @MainActor private func frame(of item: AVPlayerItem, at seconds: Double) async throws -> Frame {
        let reader = try AVAssetReader(asset: item.asset)
        let tracks = try await item.asset.loadTracks(withMediaType: .video)
        let output = AVAssetReaderVideoCompositionOutput(videoTracks: tracks, videoSettings: bgra)
        output.videoComposition = item.videoComposition
        reader.add(output)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: seconds, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        return try firstFrame(of: output, reader: reader)
    }

    @MainActor private func frame(_ recipe: EditRecipe, urls: [String: URL], at seconds: Double) async throws -> Frame {
        try await frame(of: AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls).playerItem, at: seconds)
    }

    @MainActor func testPhotoBackdropFillsTheCornersOfATiltedClip() async throws {
        let directory = try speakerTestDirectory(self)
        let built = try recipe(media: try backdropPhoto(in: directory), kind: .image, scene: try scene(in: directory), rotation: 30)
        XCTAssertTrue(built.recipe.effectiveCapabilities.isSuperset(of: [.clipTilt, .clipBackdropMedia, .stillImages]))
        let frame = try await frame(built.recipe, urls: built.urls, at: 0.5)
        for (index, corner) in frame.corners.enumerated() { assertClose(corner, quadrants[index], within: 6, "corner \(index) is the backdrop photo's pixel") }
        assertClose(frame.rgb(width / 2, height / 2), [128, 128, 128], within: 6, "the clip stays in front of the backdrop")
    }

    @MainActor func testPhotoBackdropFillsTheBarsOfAnUntiltedLetterboxedClip() async throws {
        let directory = try speakerTestDirectory(self)
        let built = try recipe(media: try backdropPhoto(in: directory), kind: .image, scene: try scene(in: directory), rotation: 0, scale: 0.5)
        let frame = try await frame(built.recipe, urls: built.urls, at: 0.5)
        for (index, corner) in frame.corners.enumerated() { assertClose(corner, quadrants[index], within: 6, "bar corner \(index)") }
        assertClose(frame.rgb(width / 2, height / 2), [128, 128, 128], within: 6, "the clip is drawn over the backdrop")
    }

    @MainActor func testBackdropNeverShowsThroughAClipThatCoversTheCanvas() async throws {
        let directory = try speakerTestDirectory(self)
        let photo = try backdropPhoto(in: directory), sceneURL = try scene(in: directory)
        let with = try recipe(media: photo, kind: .image, scene: sceneURL, rotation: 0)
        let without = try recipe(media: photo, kind: .image, scene: sceneURL, rotation: 0, backdrop: false)
        let framed = try await frame(with.recipe, urls: with.urls, at: 0.5)
        let bare = try await frame(without.recipe, urls: without.urls, at: 0.5)
        for (x, y) in [(4, 4), (width - 5, 4), (4, height - 5), (width / 2, height / 2), (width - 5, height - 5)] {
            XCTAssertEqual(framed.rgb(x, y), bare.rgb(x, y), "pixel (\(x), \(y))")
        }
    }

    @MainActor func testVideoBackdropAdvancesAndLoopsAndExportsWithoutItsAudio() async throws {
        let directory = try speakerTestDirectory(self)
        let palette: [[Double]] = [[0.9, 0.1, 0.1], [0.1, 0.8, 0.2]]
        let video = try await writeSpeakerTestVideo(directory: directory, name: "backdrop", size: (width, height), seconds: 1,
                                                    toneAmplitude: 0.5) { context, index in
            let color = palette[index < 15 ? 0 : 1]
            context.setFillColor(CGColor(red: color[0], green: color[1], blue: color[2], alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: self.width, height: self.height))
        }
        let built = try recipe(media: video, kind: .video, scene: try scene(in: directory), rotation: 30, duration: 2.5)
        XCTAssertTrue(built.recipe.effectiveCapabilities.contains(.visualVideos))
        let early = try await frame(built.recipe, urls: built.urls, at: 0.25)
        let later = try await frame(built.recipe, urls: built.urls, at: 0.75)
        let looped = try await frame(built.recipe, urls: built.urls, at: 1.25)
        let loopedLater = try await frame(built.recipe, urls: built.urls, at: 1.75)
        func isRed(_ pixel: [Int]) -> Bool { pixel[0] > pixel[1] + 120 && pixel[0] > pixel[2] + 120 }
        func isGreen(_ pixel: [Int]) -> Bool { pixel[1] > pixel[0] + 120 && pixel[1] > pixel[2] + 80 }
        XCTAssertTrue(isRed(early.corners[0]), "first half of the video: \(early.corners[0])")
        XCTAssertTrue(isGreen(later.corners[0]), "a later frame at a later time: \(later.corners[0])")
        XCTAssertTrue(isRed(looped.corners[0]), "the video loops back to its first frame: \(looped.corners[0])")
        XCTAssertTrue(isGreen(loopedLater.corners[0]), "and plays on from there: \(loopedLater.corners[0])")

        let output = directory.appendingPathComponent("tilt.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: built.recipe, assetURLs: built.urls, outputURL: output)
        let exported = AVURLAsset(url: output)
        let audioTracks = try await exported.loadTracks(withMediaType: .audio)
        if !audioTracks.isEmpty {
            let peak = try await peakAudio(output)
            XCTAssertLessThan(peak, 0.01, "the backdrop video's 0.5 tone never reaches the export")
        }
        let reader = try AVAssetReader(asset: exported)
        let videoTracks = try await exported.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(videoTracks.first)
        let trackOutput = AVAssetReaderTrackOutput(track: track, outputSettings: bgra)
        reader.add(trackOutput)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.75, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        let exportedFrame = try firstFrame(of: trackOutput, reader: reader)
        XCTAssertTrue(isGreen(exportedFrame.corners[0]), "the exported frame shows the backdrop video: \(exportedFrame.corners[0])")
    }

    @MainActor func testLivePreviewKeepsAMediaBackdropAndRebuildsWhenItIsNew() async throws {
        let directory = try speakerTestDirectory(self)
        let photo = try backdropPhoto(in: directory), sceneURL = try scene(in: directory)
        let tilted = try recipe(media: photo, kind: .image, scene: sceneURL, rotation: 30)
        let live = try await LivePreviewComposition(recipe: tilted.recipe, assetURLs: tilted.urls)
        let turned = try recipe(media: photo, kind: .image, scene: sceneURL, rotation: -20)
        try live.updateText(recipe: turned.recipe)
        let updated = try await frame(turned.recipe, urls: turned.urls, at: 0.5)
        for (index, corner) in updated.corners.enumerated() { assertClose(corner, quadrants[index], within: 6, "corner \(index)") }

        let plain = try await LivePreviewComposition(recipe: try recipe(media: photo, kind: .image, scene: sceneURL, rotation: 30, backdrop: false).recipe,
                                                     assetURLs: tilted.urls)
        XCTAssertThrowsError(try plain.updateText(recipe: tilted.recipe), "adding a media backdrop needs the full rebuild")
    }

    func testMediaBackdropNeedsAManifestVisualAndAV2Recipe() throws {
        func clip(_ backdrop: ClipBackdrop) -> TimelineClip { TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 1, backdrop: backdrop) }
        let track = { (backdrop: ClipBackdrop) in [TimelineTrack(id: "video", kind: .video, clips: [clip(backdrop)])] }
        let fingerprint = RenderFingerprint(sha256: String(repeating: "b", count: 64), byteCount: 50)
        let visual = RenderAssetReference(id: "visual-pool-1", fingerprint: fingerprint, source: .visual(visualID: "pool-1", generation: "1"))
        let original = RenderAssetReference(id: "a", fingerprint: fingerprint, source: .original(mediaID: "a"))
        let print = AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 50)
        let assets = [MediaAsset(id: "a", relativePath: "a", fingerprint: print), MediaAsset(id: "visual-pool-1", relativePath: "visual-pool-1", fingerprint: print)]
        XCTAssertNoThrow(try EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: assets, tracks: track(.media("pool-1")),
                                        assetManifest: RenderAssetManifest(assets: [original, visual])).validate())
        XCTAssertThrowsError(try EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: assets, tracks: track(.media("other")),
                                            assetManifest: RenderAssetManifest(assets: [original, visual])).validate(), "the pool id must be in the manifest")
        XCTAssertThrowsError(try EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: assets, tracks: track(.media("pool-1"))).validate())
        XCTAssertThrowsError(try EditRecipe(assets: assets, tracks: track(.media("pool-1"))).validate(), "V1 has no Visuals pool")
        XCTAssertEqual(CapabilityNegotiator(provider: DefaultRendererCapabilities()).decide(for: EditRecipe(
            schemaVersion: 2, rendererVersion: "kria-ios-2", assets: assets, tracks: track(.media("pool-1")),
            assetManifest: RenderAssetManifest(assets: [original, visual]))).missingCapabilities.contains(.clipBackdropMedia), true)
    }
}
#endif
