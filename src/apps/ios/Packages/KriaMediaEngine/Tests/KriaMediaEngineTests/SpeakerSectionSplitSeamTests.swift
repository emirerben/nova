#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// KRI-1558: a Talking edit changes the speaker's place and size per section by splitting the one take
/// into back-to-back pieces of the SAME source with nothing removed between them, each piece carrying
/// its own `speakerFrameOverride`. The split must be invisible and inaudible: the voice keeps its level
/// through the join, the layout changes on the boundary frame with nothing in between, and the speaker's
/// cut-out does not restart at the join (one bad mask right after it must not show the room).
final class SpeakerSectionSplitSeamTests: XCTestCase {
    private let split = 1.0
    private let duration = 2.0
    private let canvas = Canvas(width: 90, height: 160)
    private let corner = SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80)

    /// The take as one clip, or as two pieces joined at `split` with nothing removed in the source.
    private func take(_ pieces: (SpeakerFrameOverride?, SpeakerFrameOverride?)?, whole: SpeakerFrameOverride? = nil) -> [TimelineClip] {
        guard let pieces else {
            return [TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceDuration: duration, speakerFrameOverride: whole)]
        }
        return [
            TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceStart: 0, sourceDuration: split, timelineStart: 0,
                         speakerFrameOverride: pieces.0),
            TimelineClip(id: "clip-1", sourceAssetID: "speaker", sourceStart: split, sourceDuration: duration - split,
                         timelineStart: split, speakerFrameOverride: pieces.1),
        ]
    }

    @MainActor private func scene(speaker: URL, background: URL, clips: [TimelineClip]) throws -> (recipe: EditRecipe, urls: [String: URL]) {
        var scene = try speakerRecipe(canvas: canvas, speaker: speaker, duration: duration,
                                      background: SpeakerTestBackground(url: background), audio: AudioMixRecipe(originalVolume: 1))
        scene.recipe.tracks = [TimelineTrack(id: "subtitled", kind: .video, clips: clips)]
        try scene.recipe.validate()
        return scene
    }

    // MARK: voice

    @MainActor func testTheVoiceKeepsItsLevelThroughASplitWithNothingRemoved() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (90, 160), seconds: duration, toneAmplitude: 0.5)
        let background = try solidSpeakerTestImage(directory: directory)
        let whole = try scene(speaker: speaker, background: background, clips: take(nil))
        let pieces = try scene(speaker: speaker, background: background,
                               clips: take((nil, SpeakerFrameOverride(mode: .custom, frame: corner))))
        let reference = try await exportedPCM(whole, directory: directory, name: "whole")
        for (label, pcm) in [("export", try await exportedPCM(pieces, directory: directory, name: "pieces")),
                             ("preview", try await previewPCM(pieces))] {
            var worst = (ratio: Double.infinity, at: 0.0)
            for start in stride(from: split - 0.05, to: split + 0.05, by: 0.005) {
                let expected = rms(reference, start, start + 0.005)
                XCTAssertGreaterThan(expected, 0.1, "\(label) the take is audible at \(start)")
                let ratio = rms(pcm, start, start + 0.005) / expected
                if ratio < worst.ratio { worst = (ratio, start) }
            }
            // A hard join that is not sample-continuous clicks: one sample-to-sample step far larger than the tone's own.
            let step = largestStep(pcm, split - 0.01, split + 0.01), ownStep = largestStep(reference, split - 0.01, split + 0.01)
            print("KRI-1558 seam \(label): lowest level next to the split = \(String(format: "%.2f", worst.ratio)) of the unsplit take at \(String(format: "%.3f", worst.at)) s; largest step \(String(format: "%.4f", step)) vs \(String(format: "%.4f", ownStep)) unsplit")
            XCTAssertGreaterThan(worst.ratio, 0.8, "\(label) the voice dips to \(worst.ratio) of its level at \(worst.at) s")
            XCTAssertLessThan(step, 1.5 * ownStep, "\(label) the join clicks")
        }
    }

    // MARK: picture

    @MainActor func testTheLayoutChangesOnTheBoundaryFrameWithNothingInBetween() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (90, 160), seconds: duration)
        let background = try solidSpeakerTestImage(directory: directory)
        let pieces = try scene(speaker: speaker, background: background,
                               clips: take((nil, SpeakerFrameOverride(mode: .custom, frame: corner))))
        let output = try await exportSpeakerRecipe(pieces.recipe, urls: pieces.urls, directory: directory)
        for index in 25..<36 {
            let time = Double(index) / 30 + 0.001
            let expected = time < split ? "full" : "corner"
            let exported = layout(try await fileFrame(output, at: time))
            let previewed = layout(try await previewFrame(pieces.recipe, urls: pieces.urls, at: time))
            XCTAssertEqual(exported, expected, "export frame \(index)")
            XCTAssertEqual(previewed, expected, "preview frame \(index)")
        }
    }

    // MARK: cut-out continuity

    /// Vision misses a frame now and then. Inside one clip the last good mask covers it (`holdGrace`); a split
    /// with nothing removed is the same shot, so it must cover it there too instead of showing the room.
    @MainActor func testABadMaskRightAfterTheSplitHoldsTheLastGoodCutOut() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        // Frame 30 (the first frame after the split) reads as "person everywhere": coverage over the ceiling.
        let speaker = try await writeSpeakerTestVideo(directory: directory, name: "speaker", size: (90, 160), seconds: duration) { context, index in
            context.setFillColor(index == 30 ? SpeakerColor.person : SpeakerColor.room)
            context.fill(CGRect(x: 0, y: 0, width: 90, height: 160))
            context.setFillColor(SpeakerColor.person)
            context.fill(CGRect(x: 90 * 0.3, y: 0, width: 90 * 0.4, height: 160 * 0.75))
        }
        let background = try solidSpeakerTestImage(directory: directory)
        let inCorner = SpeakerFrameOverride(mode: .custom, frame: corner)
        let whole = try scene(speaker: speaker, background: background, clips: take(nil, whole: inCorner))
        let pieces = try scene(speaker: speaker, background: background, clips: take((inCorner, inCorner)))
        let time = split + 0.001
        // Inside the corner box but beside the person: the background when cut out, the room/frame when not.
        let beside = (fx: 0.60, fy: 0.85)
        let wholeFrame = try await fileFrame(try await exportSpeakerRecipe(whole.recipe, urls: whole.urls, directory: directory, name: "whole"), at: time)
        XCTAssertTrue(isColor(wholeFrame.rgb(fx: beside.fx, fy: beside.fy), SpeakerColor.background), "one clip holds the last good mask")
        let splitFrame = try await fileFrame(try await exportSpeakerRecipe(pieces.recipe, urls: pieces.urls, directory: directory, name: "pieces"), at: time)
        XCTAssertTrue(isColor(splitFrame.rgb(fx: beside.fx, fy: beside.fy), SpeakerColor.background),
                      "the split shows the uncut frame: \(splitFrame.rgb(fx: beside.fx, fy: beside.fy))")
    }

    // MARK: helpers

    /// "full": the person in the middle over the background. "corner": the small bottom-right speaker.
    /// Anything else is a frame that belongs to neither layout.
    private func layout(_ frame: SpeakerTestFrame) -> String {
        let middle = frame.rgb(fx: 0.5, fy: 0.6), corner = frame.rgb(fx: 0.76, fy: 0.85), beside = frame.rgb(fx: 0.60, fy: 0.85)
        if isColor(middle, SpeakerColor.person) && isColor(corner, SpeakerColor.background) { return "full" }
        if isColor(middle, SpeakerColor.background) && isColor(corner, SpeakerColor.person)
            && isColor(beside, SpeakerColor.background) { return "corner" }
        return "neither: middle \(middle) corner \(corner) beside \(beside)"
    }

    @MainActor private func exportedPCM(_ scene: (recipe: EditRecipe, urls: [String: URL]), directory: URL, name: String) async throws -> [Float] {
        try await decode(AVURLAsset(url: try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: name)), nil)
    }

    @MainActor private func previewPCM(_ scene: (recipe: EditRecipe, urls: [String: URL])) async throws -> [Float] {
        let preview = try await LivePreviewComposition(recipe: scene.recipe, assetURLs: scene.urls)
        return try await decode(preview.preview.playerItem.asset, preview.preview.playerItem.audioMix)
    }

    /// RMS over both channels of the interleaved 48 kHz stereo decode.
    private func rms(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()) * 2, end = min(Int((to * 48_000).rounded()) * 2, pcm.count)
        guard start >= 0, end > start else { return 0 }
        var sum = 0.0
        for index in start..<end { sum += Double(pcm[index]) * Double(pcm[index]) }
        return (sum / Double(end - start)).squareRoot()
    }

    /// The largest jump between consecutive samples of one channel.
    private func largestStep(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()) * 2, end = min(Int((to * 48_000).rounded()) * 2, pcm.count - 2)
        var largest = 0.0
        for index in stride(from: max(0, start), to: end, by: 1) { largest = max(largest, Double(abs(pcm[index + 2] - pcm[index]))) }
        return largest
    }

    @MainActor private func decode(_ asset: AVAsset, _ mix: AVAudioMix?) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false])
        output.audioMix = mix; reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: Int(duration + 1) * 48_000 * 2)
        while let sample = output.copyNextSampleBuffer() {
            let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2
            let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample))
            var bytes = Data(count: CMBlockBufferGetDataLength(block))
            XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr)
            bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } }
        }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
