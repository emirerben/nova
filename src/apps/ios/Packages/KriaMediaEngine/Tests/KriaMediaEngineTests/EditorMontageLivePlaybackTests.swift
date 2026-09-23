import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

/// KRI-164: a phone-rendered guided-story montage (job `d33dca56…`, item
/// `02eb2bf1…`) plays correctly in the iOS editor for about 2s, then goes
/// black for the rest while audio keeps playing. The exported MP4 for the
/// same job has no black frames — the failure is specific to the editor's
/// LIVE preview (a fresh `AVMutableComposition` + `RecipeVideoCompositor`
/// built by `NativeEditorRenderCompiler`, branded via `LivePreviewComposition`
/// with `.standard` options), not the server-compiled export recipe.
///
/// This fixture reproduces the real job's clip shape at the `KriaMediaEngine`
/// layer: 19 clips, all `rate == 1`, 18 crossfades at 0.133s/0.103s and a
/// final 0.0s cut, no track reuse (19 distinct sources), branded with the
/// real Kria outro. Values are taken verbatim from the job's
/// `guided_edit_revision.segments` (admin job-debug dump, 2026-09-23).
final class EditorMontageLivePlaybackTests: XCTestCase {
    private struct Segment {
        let outputStart: Double
        let outputEnd: Double
        let sourceStart: Double
        let sourceEnd: Double
        let transitionDuration: Double?
    }

    // segment_id, output_start_s, output_end_s, source_start_s, source_end_s, transition_duration_s (nil = cut)
    private static let segments: [Segment] = [
        .init(outputStart: 0.0, outputEnd: 0.8667, sourceStart: 3.4333, sourceEnd: 4.3, transitionDuration: 0.133),
        .init(outputStart: 0.7333, outputEnd: 1.6, sourceStart: 0.0, sourceEnd: 0.8667, transitionDuration: 0.133),
        .init(outputStart: 1.4667, outputEnd: 2.3333, sourceStart: 0.0, sourceEnd: 0.8667, transitionDuration: 0.133),
        .init(outputStart: 2.2, outputEnd: 3.0667, sourceStart: 1.6333, sourceEnd: 2.5, transitionDuration: 0.133),
        .init(outputStart: 2.9333, outputEnd: 3.8, sourceStart: 0.5667, sourceEnd: 1.4333, transitionDuration: 0.133),
        .init(outputStart: 3.6667, outputEnd: 4.5333, sourceStart: 0.8333, sourceEnd: 1.7, transitionDuration: 0.103),
        .init(outputStart: 4.4333, outputEnd: 4.7667, sourceStart: 1.5667, sourceEnd: 1.9, transitionDuration: 0.103),
        .init(outputStart: 4.6667, outputEnd: 5.5333, sourceStart: 3.4333, sourceEnd: 4.3, transitionDuration: 0.133),
        .init(outputStart: 5.4, outputEnd: 12.1, sourceStart: 9.8, sourceEnd: 16.5, transitionDuration: 0.133),
        .init(outputStart: 11.9667, outputEnd: 17.0333, sourceStart: 0.0, sourceEnd: 5.0667, transitionDuration: 0.133),
        .init(outputStart: 16.9, outputEnd: 23.9, sourceStart: 0.0, sourceEnd: 7.0, transitionDuration: 0.133),
        .init(outputStart: 23.7667, outputEnd: 24.6333, sourceStart: 3.8, sourceEnd: 4.6667, transitionDuration: 0.133),
        .init(outputStart: 24.5, outputEnd: 28.0, sourceStart: 5.5333, sourceEnd: 9.0333, transitionDuration: 0.133),
        .init(outputStart: 27.8667, outputEnd: 31.3667, sourceStart: 0.0, sourceEnd: 3.5, transitionDuration: 0.133),
        .init(outputStart: 31.2333, outputEnd: 33.4, sourceStart: 0.0, sourceEnd: 2.1667, transitionDuration: 0.133),
        .init(outputStart: 33.2667, outputEnd: 34.5333, sourceStart: 0.0, sourceEnd: 1.2667, transitionDuration: 0.133),
        .init(outputStart: 34.4, outputEnd: 36.7333, sourceStart: 0.0, sourceEnd: 2.3333, transitionDuration: 0.133),
        .init(outputStart: 36.6, outputEnd: 38.2667, sourceStart: 0.0, sourceEnd: 1.6667, transitionDuration: 0.133),
        .init(outputStart: 38.1333, outputEnd: 40.4333, sourceStart: 3.1667, sourceEnd: 5.4667, transitionDuration: nil),
    ]

    /// Distinct saturated colors so a black frame (or a wrong/missing source
    /// frame) is unambiguous against real picture content, cycling if there
    /// are more clips than colors.
    private static let colors: [CGColor] = [
        CGColor(red: 1, green: 0, blue: 0, alpha: 1), CGColor(red: 0, green: 1, blue: 0, alpha: 1),
        CGColor(red: 0, green: 0.3, blue: 1, alpha: 1), CGColor(red: 1, green: 1, blue: 0, alpha: 1),
        CGColor(red: 1, green: 0, blue: 1, alpha: 1), CGColor(red: 0, green: 1, blue: 1, alpha: 1),
        CGColor(red: 1, green: 0.5, blue: 0, alpha: 1),
    ]

    @MainActor func testRealJobShapedMontagePlaysThroughALivePlayerAfterBranding() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        var assets: [MediaAsset] = []
        var references: [RenderAssetReference] = []
        var urls: [String: URL] = [:]
        var clips: [TimelineClip] = []
        for (index, segment) in Self.segments.enumerated() {
            let id = "source-\(index)"
            let color = Self.colors[index % Self.colors.count]
            // Enough decodable frames to cover the referenced source window
            // with margin, at the recipe's own frame rate.
            let url = try await makeColorVideo(directory: directory, name: id, color: color,
                durationSeconds: segment.sourceEnd + 0.2, fps: 30)
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
            assets.append(MediaAsset(id: id, relativePath: id, fingerprint: fingerprint))
            references.append(RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: id)))
            urls[id] = url
            // `transition_after`/`transition_duration_s` on segment i describes
            // the transition INTO segment i+1 (its own compiled transition
            // dissolves the outgoing frame as the next clip's incoming frame
            // fades in) — mirrors `NativeEditorRenderCompiler.swift`'s
            // `clip.transition = Transition(duration: previous.transitionDurationS…)`
            // taken from `clips[index - 1]`, not the clip's own segment.
            let incomingTransition = index > 0 ? Self.segments[index - 1].transitionDuration.map { Transition(kind: .crossfade, duration: $0) } : nil
            clips.append(TimelineClip(id: "clip-\(index)", sourceAssetID: id,
                sourceStart: segment.sourceStart, sourceDuration: segment.sourceEnd - segment.sourceStart,
                timelineStart: segment.outputStart, rate: 1, transition: incomingTransition))
        }
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160),
            assets: assets, tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)],
            assetManifest: RenderAssetManifest(assets: references))
        try recipe.validate()

        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls, branding: .standard)
        let item = live.preview.playerItem
        let composition = try XCTUnwrap(item.videoComposition)
        let assetDuration = try await item.asset.load(.duration).seconds
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(composition.instructions.last as? AVVideoCompositionInstructionProtocol)
            .timeRange.end.seconds, assetDuration - 0.01,
            "instructions must cover the whole (branded) asset or AVPlayer renders nothing")

        let output = AVPlayerItemVideoOutput(pixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        item.add(output)
        let player = AVPlayer(playerItem: item)
        player.isMuted = true
        player.play()

        // Sample real presentation times across the whole branded timeline,
        // including well past the user-reported "~2s" black point and into
        // the outro tail, and confirm a fresh frame arrives at each.
        var lastGoodFrameCount = 0
        var missed: [Double] = []
        let checkpoints = stride(from: 0.5, to: assetDuration - 0.1, by: 1.0).map { $0 }
        for target in checkpoints {
            let deadline = Date().addingTimeInterval(6)
            var sawFrame = false
            while Date() < deadline {
                let time = item.currentTime()
                if time.seconds >= target, output.hasNewPixelBuffer(forItemTime: time),
                   output.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) != nil {
                    sawFrame = true
                    lastGoodFrameCount += 1
                    break
                }
                try await Task.sleep(for: .milliseconds(20))
            }
            if !sawFrame { missed.append(target) }
        }
        player.pause()
        XCTAssertNotEqual(item.status, .failed, "live item entered .failed: \(String(describing: item.error))")
        XCTAssertTrue(missed.isEmpty, "no fresh frame arrived at checkpoint(s) \(missed) of \(checkpoints) — reproduces KRI-164")
        XCTAssertGreaterThan(lastGoodFrameCount, checkpoints.count - 1)
    }

    @MainActor private func makeColorVideo(directory: URL, name: String, color: CGColor, durationSeconds: Double, fps: Int32) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        let frameCount = Int(ceil(durationSeconds * Double(fps)))
        for index in 0..<frameCount {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 96, height: 160, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
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
