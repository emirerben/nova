import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
import ImageIO

/// A clip's `dim` rendered through the real compositor (the preview composition read back as BGRA) and the live
/// preview's in-place update. Failure modes: the multiply runs in linear light (50% would come out near 73%); the
/// backdrop around a tilted clip stays bright; an unset dim changes a pixel or a byte of the recipe; a slider drag
/// forces a full rebuild or draws something a rebuild would not; a dim reaches an overlay clip.
final class ClipDimCompositionTests: XCTestCase {
    private let width = 90, height = 160
    private let scene = [200, 120, 40], backdropHex = "#E91E8C", backdropRGB = [233, 30, 140]

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func writeScene(in directory: URL) throws -> URL {
        var pixels = [UInt8](repeating: 255, count: width * height * 4)
        for index in 0..<(width * height) { for channel in 0..<3 { pixels[index * 4 + channel] = UInt8(scene[channel]) } }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        let image = try XCTUnwrap(CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let url = directory.appendingPathComponent("scene.png")
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return url
    }

    private func recipe(dim: Double?, rotation: Double = 0, backdrop: ClipBackdrop? = nil) -> EditRecipe {
        EditRecipe(canvas: Canvas(width: width, height: height), assets: [MediaAsset(id: "scene", relativePath: "scene")], tracks: [
            TimelineTrack(id: "photos", kind: .video, clips: [
                TimelineClip(id: "clip", sourceAssetID: "scene", sourceDuration: 1,
                             transform: MediaTransform(rotationDegrees: rotation), backdrop: backdrop, dim: dim)
            ])
        ])
    }

    private struct Frame {
        let bytes: [UInt8], width: Int
        var height: Int { bytes.count / 4 / width }
        func pixel(_ x: Int, _ y: Int) -> [Int] { [Int(bytes[(y * width + x) * 4 + 2]), Int(bytes[(y * width + x) * 4 + 1]), Int(bytes[(y * width + x) * 4])] }
    }

    @MainActor private func frame(of item: AVPlayerItem) async throws -> Frame {
        let reader = try AVAssetReader(asset: item.asset)
        let tracks = try await item.asset.loadTracks(withMediaType: .video)
        let output = AVAssetReaderVideoCompositionOutput(videoTracks: tracks, videoSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        output.videoComposition = item.videoComposition
        reader.add(output)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.5, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
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

    @MainActor private func previewFrame(_ recipe: EditRecipe, urls: [String: URL]) async throws -> Frame {
        try await frame(of: AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls).playerItem)
    }

    private func assertClose(_ actual: [Int], _ expected: [Int], within tolerance: Int, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        let delta = zip(actual, expected).map { abs($0 - $1) }.max() ?? 0
        XCTAssertLessThanOrEqual(delta, tolerance, "\(message): \(actual) vs \(expected)", file: file, line: line)
    }

    @MainActor func testADimMultipliesTheEncodedPixelsAndAFullDimIsBlack() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let plain = try await previewFrame(recipe(dim: nil), urls: urls)
        assertClose(plain.pixel(45, 80), scene, within: 2, "undimmed")
        for strength in [0.25, 0.5, 0.75] {
            let dimmed = try await previewFrame(recipe(dim: strength), urls: urls)
            assertClose(dimmed.pixel(45, 80), scene.map { Int((Double($0) * (1 - strength)).rounded()) }, within: 2, "dim \(strength)")
        }
        let black = try await previewFrame(recipe(dim: 1), urls: urls)
        assertClose(black.pixel(45, 80), [0, 0, 0], within: 1, "dim 1 is black")
    }

    @MainActor func testTheBackdropAroundATiltedClipDimsWithIt() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let dimmed = try await previewFrame(recipe(dim: 0.5, rotation: 30, backdrop: .color(backdropHex)), urls: urls)
        assertClose(dimmed.pixel(2, 2), backdropRGB.map { Int((Double($0) * 0.5).rounded()) }, within: 2, "the backdrop corner")
        assertClose(dimmed.pixel(45, 80), scene.map { Int((Double($0) * 0.5).rounded()) }, within: 2, "the clip centre")
    }

    @MainActor func testAnUnsetDimRendersAndEncodesExactlyAsBefore() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let plain = recipe(dim: nil)
        XCTAssertFalse(String(decoding: try RecipeJSON.encoder().encode(plain), as: UTF8.self).contains("\"dim\""))
        XCTAssertFalse(plain.effectiveCapabilities.contains(.clipDim))
        XCTAssertTrue(recipe(dim: 0.5).effectiveCapabilities.contains(.clipDim))
        let decoded = try RecipeJSON.decoder().decode(EditRecipe.self, from: RecipeJSON.encoder().encode(recipe(dim: 0.5)))
        XCTAssertEqual(decoded.tracks[0].clips[0].dim, 0.5)
        let first = try await previewFrame(plain, urls: urls)
        let second = try await previewFrame(plain, urls: urls)
        XCTAssertEqual(first.bytes, second.bytes)
    }

    @MainActor func testLivePreviewTakesADimEditWithoutARebuild() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let live = try await LivePreviewComposition(recipe: recipe(dim: nil), assetURLs: urls)
        try live.updateText(recipe: recipe(dim: 0.6))
        let updated = try await frame(of: live.preview.playerItem)
        let rebuilt = try await previewFrame(recipe(dim: 0.6), urls: urls)
        XCTAssertEqual(updated.bytes, rebuilt.bytes)
        try live.updateText(recipe: recipe(dim: nil))
        let undone = try await frame(of: live.preview.playerItem)
        let plain = try await previewFrame(recipe(dim: nil), urls: urls)
        XCTAssertEqual(undone.bytes, plain.bytes)
    }

    /// The speaker's person mask is found on the undimmed picture: `GreenPersonSegmenter` only sees a person brighter
    /// than green 150, so a 50% dimmed speaker (green about 128) would lose its cut-out if the mask read the dimmed frame.
    @MainActor func testADimmedSpeakerKeepsItsCutOutOverTheBackground() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let canvas = Canvas(width: 120, height: 200)
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (canvas.width, canvas.height), seconds: 2)
        var scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        scene.recipe.tracks[0].clips[0].dim = 0.5
        try scene.recipe.validate()
        let frame = try await KriaMediaEngineTests.previewFrame(scene.recipe, urls: scene.urls, at: 1)
        let person = frame.rgb(fx: 0.5, fy: 0.6)
        XCTAssertEqual(person[1], 128, accuracy: 12, "the speaker is kept and dimmed: \(person)")
        XCTAssertLessThan(max(person[0], person[2]), 30, "the speaker is still green, not the background: \(person)")
        XCTAssertTrue(isColor(frame.rgb(fx: 0.04, fy: 0.04), SpeakerColor.background), "the background is not dimmed: \(frame.rgb(fx: 0.04, fy: 0.04))")
    }

    func testADimOnAnOverlayClipOrOutsideZeroToOneIsRefused() throws {
        let overlay = EditRecipe(canvas: Canvas(width: width, height: height), assets: [MediaAsset(id: "scene", relativePath: "scene")], tracks: [
            TimelineTrack(id: "photos", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "scene", sourceDuration: 1)]),
            TimelineTrack(id: "cards", kind: .overlay, clips: [TimelineClip(id: "card", sourceAssetID: "scene", sourceDuration: 1, dim: 0.5)]),
        ])
        XCTAssertThrowsError(try overlay.validate())
        for bad in [0, -0.1, 1.5, .nan] { XCTAssertThrowsError(try recipe(dim: bad).validate(), "dim \(bad)") }
        XCTAssertNoThrow(try recipe(dim: 1).validate())
    }
}
#endif
