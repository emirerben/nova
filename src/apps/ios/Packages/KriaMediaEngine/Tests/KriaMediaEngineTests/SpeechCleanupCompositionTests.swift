import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

/// A speech-cleanup "clean" render keeps several non-contiguous ranges from
/// ONE recorded take (the pauses/retakes it detected are the gaps between
/// them) and lays them back-to-back on the timeline. There is no dedicated
/// "speech cleanup" recipe shape -- it is a plain multi-clip `TimelineTrack`
/// referencing the same `sourceAssetID` several times with different
/// `sourceStart`/`sourceDuration` windows. These tests prove the existing
/// composer handles that pattern (schema compatibility for whatever recipe
/// the server's speech-cleanup pipeline emits): the composed duration and
/// instruction tiling match what three independently-cut clips would produce,
/// no different because they happen to share one source asset.
///
/// No sample recipe was found at the scratchpad path a backend implementer
/// may drop one at (`cleaned_subtitled_recipe.json`); the recipe below is
/// built directly against `TimelineClip`'s public schema instead.
final class SpeechCleanupCompositionTests: XCTestCase {
    /// [0-1.2], [1.8-3.0], [3.5-5.0] of one 5.5s source, laid back-to-back
    /// with no timeline gap and no transition -- the simplest "clean" cut.
    @MainActor func testKeepSegmentsFromTheSameSourceComposeBackToBackWithNoGaps() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = try await makeVideo(directory: directory, name: "take", color: CGColor(red: 0, green: 0, blue: 1, alpha: 1), seconds: 5.5)

        let keepWindows: [(start: TimeInterval, end: TimeInterval)] = [(0, 1.2), (1.8, 3.0), (3.5, 5.0)]
        var clips: [TimelineClip] = []
        var timelineCursor: TimeInterval = 0
        for (index, window) in keepWindows.enumerated() {
            let duration = window.end - window.start
            clips.append(TimelineClip(id: "keep-\(index)", sourceAssetID: "take", sourceStart: window.start, sourceDuration: duration, timelineStart: timelineCursor))
            timelineCursor += duration
        }
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "take", relativePath: "take.mp4")], tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)])

        let expectedTotalDuration = keepWindows.reduce(0) { $0 + ($1.end - $1.start) } // 1.2 + 1.2 + 1.5 = 3.9
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), expectedTotalDuration, accuracy: 0.001)

        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["take": url])
        let composition = try XCTUnwrap(preview.playerItem.videoComposition)
        XCTAssertEqual(composition.instructions.count, clips.count, "one instruction per keep segment when none overlap via a transition")
        var cursor = CMTime.zero
        for instruction in composition.instructions {
            XCTAssertEqual(instruction.timeRange.start, cursor, "keep segments must tile with no gap or overlap")
            XCTAssertGreaterThan(instruction.timeRange.duration, .zero, "a zero-length instruction invalidates the whole composition")
            cursor = instruction.timeRange.end
        }
        XCTAssertEqual(cursor.seconds, expectedTotalDuration, accuracy: 0.01)

        // Renders end to end -- proves the composition AVFoundation actually
        // built (not just the instruction bookkeeping above) is sampleable
        // across every keep segment, including right after each cut.
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        for offset in stride(from: 0.05, to: expectedTotalDuration, by: 0.5) {
            _ = try await generator.image(at: CMTime(seconds: offset, preferredTimescale: 600)).image
        }
    }

    /// The same three keep segments, but with a short crossfade over each cut
    /// instead of a hard cut -- the total duration shortens by one transition
    /// duration per overlap (the standard "overlap the timeline placement by
    /// the transition's duration" convention, per `ClipTransitionCompositionTests`),
    /// and the composition still tiles with no gap and renders end to end.
    @MainActor func testKeepSegmentsFromTheSameSourceComposeWithAShortCrossfadeBetweenCuts() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = try await makeVideo(directory: directory, name: "take", color: CGColor(red: 0, green: 0, blue: 1, alpha: 1), seconds: 5.5)

        let keepWindows: [(start: TimeInterval, end: TimeInterval)] = [(0, 1.2), (1.8, 3.0), (3.5, 5.0)]
        let crossfade = 0.2
        var clips: [TimelineClip] = []
        var timelineCursor: TimeInterval = 0
        for (index, window) in keepWindows.enumerated() {
            let duration = window.end - window.start
            let start = index == 0 ? timelineCursor : timelineCursor - crossfade
            let transition = index == 0 ? nil : Transition(kind: .crossfade, duration: crossfade)
            clips.append(TimelineClip(id: "keep-\(index)", sourceAssetID: "take", sourceStart: window.start, sourceDuration: duration, timelineStart: start, transition: transition))
            timelineCursor = start + duration
        }
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "take", relativePath: "take.mp4")], tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)])

        let rawTotal = keepWindows.reduce(0) { $0 + ($1.end - $1.start) }
        let expectedTotalDuration = rawTotal - crossfade * Double(keepWindows.count - 1) // 3.9 - 0.4 = 3.5
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), expectedTotalDuration, accuracy: 0.001)

        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["take": url])
        let composition = try XCTUnwrap(preview.playerItem.videoComposition)
        var cursor = CMTime.zero
        for instruction in composition.instructions {
            XCTAssertEqual(instruction.timeRange.start, cursor, "instructions must still tile with no gap once crossfades are involved")
            XCTAssertGreaterThan(instruction.timeRange.duration, .zero, "a zero-length instruction invalidates the whole composition")
            cursor = instruction.timeRange.end
        }
        XCTAssertEqual(cursor.seconds, expectedTotalDuration, accuracy: 0.01)

        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        for offset in stride(from: 0.05, to: expectedTotalDuration, by: 0.4) {
            _ = try await generator.image(at: CMTime(seconds: offset, preferredTimescale: 600)).image
        }
    }

    @MainActor private func makeVideo(directory: URL, name: String, color: CGColor, seconds: Double, fps: Int32 = 30) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        let frameCount = Int((seconds * Double(fps)).rounded(.up))
        for index in 0..<frameCount {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 96, height: 160, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color)
            context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        return url
    }
}
#endif
