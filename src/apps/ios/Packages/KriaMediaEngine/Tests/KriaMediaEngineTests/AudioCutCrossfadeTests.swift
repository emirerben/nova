import XCTest
@testable import KriaMediaEngine

/// Speech-cleanup cuts used to declick each side to silence, so the room tone
/// (rain, traffic) dropped 10-20 dB for ~30 ms at every cut and the edit
/// sounded jumpy. Same-source cuts now borrow a little removed audio and
/// crossfade across the join.
final class AudioCutCrossfadeTests: XCTestCase {
    private func clip(_ id: String, _ asset: String = "take", source: Double, duration: Double, at start: Double) -> TimelineClip {
        TimelineClip(id: id, sourceAssetID: asset, sourceStart: source, sourceDuration: duration, timelineStart: start)
    }

    private func plan(_ clips: [TimelineClip], kind: TimelineTrack.Kind = .video) -> [String: AudioCutHandles] {
        AudioCutHandles.plan([TimelineTrack(id: "main", kind: kind, clips: clips)])
    }

    func testSameSourceCutBorrowsAudioOnBothSides() {
        let handles = plan([clip("a", source: 0.3, duration: 1, at: 0), clip("b", source: 2.0, duration: 1, at: 1), clip("c", source: 3.5, duration: 1, at: 2)])
        XCTAssertEqual(handles["a"], AudioCutHandles(lead: 0, tail: audioCutCrossfadeHandle))
        XCTAssertEqual(handles["b"], AudioCutHandles(lead: audioCutCrossfadeHandle, tail: audioCutCrossfadeHandle))
        XCTAssertEqual(handles["c"], AudioCutHandles(lead: audioCutCrossfadeHandle, tail: 0))
    }

    func testHandleStaysInsideHalfTheRemovedSpanAndAQuarterOfEachClip() {
        let narrowGap = plan([clip("a", source: 0, duration: 1, at: 0), clip("b", source: 1.02, duration: 1, at: 1)])
        XCTAssertEqual(narrowGap["b"]?.lead ?? 0, 0.01, accuracy: 0.000_001)
        let shortClip = plan([clip("a", source: 0, duration: 0.04, at: 0), clip("b", source: 1, duration: 1, at: 0.04)])
        XCTAssertEqual(shortClip["a"]?.tail ?? 0, 0.01, accuracy: 0.000_001)
    }

    func testOnlyRemovedSourceMaterialIsEverBorrowed() {
        // Continuous source: nothing was removed, so there is no join to hide.
        XCTAssertTrue(plan([clip("a", source: 0, duration: 1, at: 0), clip("b", source: 1, duration: 1, at: 1)]).isEmpty)
        // Backwards jump: the "removed" audio would be a kept word.
        XCTAssertTrue(plan([clip("a", source: 2, duration: 1, at: 0), clip("b", source: 0, duration: 1, at: 1)]).isEmpty)
        // A different take has no shared room tone to crossfade.
        XCTAssertTrue(plan([clip("a", source: 0, duration: 1, at: 0), clip("b", "other", source: 2, duration: 1, at: 1)]).isEmpty)
        // A timeline gap is not a cut.
        XCTAssertTrue(plan([clip("a", source: 0, duration: 1, at: 0), clip("b", source: 2, duration: 1, at: 1.5)]).isEmpty)
        // Overlays keep their own edges. Audio tracks crossfade same-source
        // excerpts since KRI-282 (see SpeechExcerptAudioTests).
        XCTAssertTrue(plan([clip("a", source: 0, duration: 1, at: 0), clip("b", source: 2, duration: 1, at: 1)], kind: .overlay).isEmpty)
    }

    func testAuthoredTransitionsFadesRatesAndHoldsKeepTheirOwnEdges() {
        var transitioned = clip("b", source: 2, duration: 1, at: 0.8); transitioned.transition = Transition(kind: .crossfade, duration: 0.2)
        var fadedIn = clip("b", source: 2, duration: 1, at: 1); fadedIn.audioFadeIn = 0.5
        var fast = clip("b", source: 2, duration: 1, at: 1); fast.rate = 2
        var held = clip("a", source: 0, duration: 1, at: 0); held.holdDuration = 0.5
        let base = clip("a", source: 0, duration: 1, at: 0)
        XCTAssertTrue(plan([base, transitioned]).isEmpty)
        XCTAssertTrue(plan([base, fadedIn]).isEmpty)
        XCTAssertTrue(plan([base, fast]).isEmpty)
        XCTAssertTrue(plan([held, clip("b", source: 2, duration: 1, at: 1.5)]).isEmpty)
    }

    func testWideningMovesSourceAndTimelineTogether() {
        let widened = clip("b", source: 2, duration: 1, at: 1).widened(by: AudioCutHandles(lead: 0.025, tail: 0.02))
        XCTAssertEqual(widened.sourceStart, 1.975, accuracy: 0.000_001)
        XCTAssertEqual(widened.timelineStart, 0.975, accuracy: 0.000_001)
        XCTAssertEqual(widened.sourceDuration, 1.045, accuracy: 0.000_001)
    }
}

#if canImport(AVFoundation)
@preconcurrency import AVFoundation

/// Decodes the real preview mix and the real export of a two-cut take whose
/// room tone is steady noise, with a loud burst inside the removed span.
final class AudioCutCrossfadePCMTests: XCTestCase {
    private let cut = 1.0

    @MainActor func testRoomToneRunsThroughACleanupCutWithoutADip() async throws {
        for (label, pcm) in try await render() {
            let steady = rms(pcm, 0.4, 0.8)
            XCTAssertGreaterThan(steady, 0.05, "\(label) room tone is present")
            XCTAssertGreaterThan(rms(pcm, cut - 0.015, cut + 0.015), 0.75 * steady, "\(label) the cut keeps the room tone")
            for start in stride(from: cut - 0.04, to: cut + 0.04, by: 0.005) {
                XCTAssertGreaterThan(rms(pcm, start, start + 0.005), 0.55 * steady, "\(label) no hole at \(start)")
            }
            // The borrowed audio stays at the edges of the removed span: the
            // burst in its middle never plays.
            XCTAssertLessThan(rms(pcm, cut - 0.05, cut + 0.05), 1.3 * steady, "\(label) removed burst stays out")
            XCTAssertEqual(rms(pcm, 1.2, 1.6), steady, accuracy: 0.15 * steady, "\(label) after the cut")
            // The take's own first edge still declicks.
            XCTAssertLessThan(rms(pcm, 0, 0.001), 0.3 * steady, "\(label) first clip start")
        }
    }

    @MainActor private func render() async throws -> [(String, [Float])] {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let take = try await mux(try await video(directory), try noise(directory), directory.appendingPathComponent("take.mov"))
        // Keep [0.3, 1.3] and [2.0, 3.0]; [1.3, 2.0] was removed.
        let clips = [
            TimelineClip(id: "keep-0", sourceAssetID: "take", sourceStart: 0.3, sourceDuration: 1, timelineStart: 0, volume: 1),
            TimelineClip(id: "keep-1", sourceAssetID: "take", sourceStart: 2.0, sourceDuration: 1, timelineStart: cut, volume: 1),
        ]
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "take", relativePath: "take")],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)], audio: AudioMixRecipe(originalVolume: 1))
        let urls = ["take": take]
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("cut.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        return [("preview", try await decode(preview.preview.playerItem.asset, preview.preview.playerItem.audioMix)),
                ("export", try await decode(AVURLAsset(url: output), nil))]
    }

    /// RMS over both channels of the interleaved 48 kHz stereo decode.
    private func rms(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()) * 2, end = min(Int((to * 48_000).rounded()) * 2, pcm.count)
        guard start >= 0, end > start else { return 0 }
        var sum = 0.0
        for index in start..<end { sum += Double(pcm[index]) * Double(pcm[index]) }
        return (sum / Double(end - start)).squareRoot()
    }

    /// Seeded white noise at a steady level, loud only well inside the removed span.
    private func noise(_ directory: URL) throws -> URL {
        let url = directory.appendingPathComponent("noise.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4 * 48_000)); buffer.frameLength = buffer.frameCapacity
        var state: UInt64 = 0x9E37_79B9_7F4A_7C15
        for index in 0..<Int(buffer.frameLength) {
            state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
            let white = Double(state >> 11) / Double(1 << 53) * 2 - 1
            let seconds = Double(index) / 48_000
            buffer.floatChannelData![0][index] = Float(white * (seconds > 1.4 && seconds < 1.9 ? 0.9 : 0.2))
        }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func video(_ directory: URL) async throws -> URL {
        let url = directory.appendingPathComponent("picture.mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<120 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var raw: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw)
            let pixel = try XCTUnwrap(raw)
            CVPixelBufferLockBaseAddress(pixel, [])
            memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed); return url
    }

    @MainActor private func mux(_ video: URL, _ audio: URL, _ output: URL) async throws -> URL {
        let composition = AVMutableComposition()
        for (url, kind) in [(video, AVMediaType.video), (audio, AVMediaType.audio)] {
            // The asset must outlive the insert, or the track's reads fail.
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: kind)
            let track = try XCTUnwrap(tracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: .zero)
        }
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        exporter.outputURL = output; exporter.outputFileType = .mov
        await exporter.export()
        XCTAssertEqual(exporter.status, .completed, "\(String(describing: exporter.error))"); return output
    }

    @MainActor private func decode(_ asset: AVAsset, _ mix: AVAudioMix?) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false])
        output.audioMix = mix; reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: 4 * 48_000 * 2)
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
