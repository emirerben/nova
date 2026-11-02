import XCTest
@testable import KriaMediaEngine

#if canImport(AVFoundation)
@preconcurrency import AVFoundation

/// Parent KRI-1151: a Talking edit pauses the creator's take for another clip. The recipe is
/// `clip-0` (take, 0.3-1.3) / `insert-0` (other clip, 1 s, its own sound) / `clip-1` (take,
/// 1.3-2.3): a different clip between two clips of one source, which the engine already plays.
/// This pins the real preview mix and the real export: the insert's tone plays only in its own
/// window (silent when its volume is 0), the take's room noise plays around it with no hole at
/// the joins, and the take resumes where it stopped.
final class TalkingInsertClipPCMTests: XCTestCase {
    private let sampleRate = 48_000.0

    func testNoAudioCutHandlesAreBorrowedAcrossAnInsert() {
        let clips = [
            TimelineClip(id: "clip-0", sourceAssetID: "take", sourceStart: 0.3, sourceDuration: 1, timelineStart: 0),
            TimelineClip(id: "insert-0", sourceAssetID: "talk", sourceStart: 0, sourceDuration: 1, timelineStart: 1),
            TimelineClip(id: "clip-1", sourceAssetID: "take", sourceStart: 1.3, sourceDuration: 1, timelineStart: 2),
        ]
        XCTAssertTrue(AudioCutHandles.plan([TimelineTrack(id: "main", kind: .video, clips: clips)]).isEmpty)
    }

    @MainActor func testTheInsertPlaysItsOwnSoundAndTheTakeResumesAroundIt() async throws {
        for (label, pcm, seconds) in try await render(insertVolume: 1) {
            XCTAssertEqual(seconds, 3, accuracy: 0.1, "\(label) the video is the take plus the insert")
            let noise = rms(pcm, 0.3, 0.9), tone = rms(pcm, 1.2, 1.8)
            XCTAssertGreaterThan(noise, 0.05, "\(label) take before the insert")
            XCTAssertGreaterThan(tone, 3 * noise, "\(label) the insert's own sound is loud inside its window")
            XCTAssertEqual(rms(pcm, 2.2, 2.8), noise, accuracy: 0.35 * noise, "\(label) take resumes")
            XCTAssertLessThan(rms(pcm, 0.0, 0.001), 0.3 * noise, "\(label) first edge declicks")
            // Each edge keeps the engine's ordinary ~25 ms declick, nothing longer: the take is
            // at full level right up to it and the insert is at full level right after it.
            XCTAssertGreaterThan(rms(pcm, 0.92, 0.97), 0.7 * noise, "\(label) take runs up to the join")
            XCTAssertGreaterThan(rms(pcm, 1.03, 1.08), 0.7 * tone, "\(label) insert is at level right after the join")
            XCTAssertGreaterThan(rms(pcm, 1.92, 1.97), 0.7 * tone, "\(label) insert runs up to its end")
            XCTAssertGreaterThan(rms(pcm, 2.03, 2.08), 0.7 * noise, "\(label) take is back right after the insert")
        }
    }

    @MainActor func testAMutedInsertIsSilentAndTheTakeStillResumes() async throws {
        for (label, pcm, _) in try await render(insertVolume: 0) {
            let noise = rms(pcm, 0.3, 0.9)
            XCTAssertLessThan(rms(pcm, 1.1, 1.9), 0.1 * noise, "\(label) muted insert plays nothing")
            XCTAssertEqual(rms(pcm, 2.2, 2.8), noise, accuracy: 0.35 * noise, "\(label) take resumes")
        }
    }

    @MainActor private func render(insertVolume: Double) async throws -> [(String, [Float], Double)] {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory)
        let take = try await mux(picture, try audio(directory, "noise.m4a", seconds: 3, tone: false), directory.appendingPathComponent("take.mov"))
        let talk = try await mux(picture, try audio(directory, "tone.m4a", seconds: 3, tone: true), directory.appendingPathComponent("talk.mov"))
        let clips = [
            TimelineClip(id: "clip-0", sourceAssetID: "take", sourceStart: 0.3, sourceDuration: 1, timelineStart: 0, volume: 1),
            TimelineClip(id: "insert-0", sourceAssetID: "talk", sourceStart: 0, sourceDuration: 1, timelineStart: 1, volume: insertVolume),
            TimelineClip(id: "clip-1", sourceAssetID: "take", sourceStart: 1.3, sourceDuration: 1, timelineStart: 2, volume: 1),
        ]
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "take", relativePath: "take"), MediaAsset(id: "talk", relativePath: "talk")],
            tracks: [TimelineTrack(id: "subtitled", kind: .video, clips: clips)], audio: AudioMixRecipe(originalVolume: 1))
        let urls = ["take": take, "talk": talk]
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("insert.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        if let keep = ProcessInfo.processInfo.environment["KRIA_INSERT_PROOF_DIR"] {
            try? FileManager.default.createDirectory(atPath: keep, withIntermediateDirectories: true)
            try? FileManager.default.removeItem(atPath: "\(keep)/insert-volume-\(Int(insertVolume)).mp4")
            try? FileManager.default.copyItem(at: output, to: URL(fileURLWithPath: "\(keep)/insert-volume-\(Int(insertVolume)).mp4"))
        }
        let exported = AVURLAsset(url: output)
        return [("preview", try await decode(preview.preview.playerItem.asset, preview.preview.playerItem.audioMix), try await preview.preview.playerItem.asset.load(.duration).seconds),
                ("export", try await decode(exported, nil), try await exported.load(.duration).seconds)]
    }

    private func rms(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * sampleRate).rounded()) * 2, end = min(Int((to * sampleRate).rounded()) * 2, pcm.count)
        guard start >= 0, end > start else { return 0 }
        var sum = 0.0
        for index in start..<end { sum += Double(pcm[index]) * Double(pcm[index]) }
        return (sum / Double(end - start)).squareRoot()
    }

    /// Seeded white noise (the take's room tone) or a 1 kHz tone (the inserted clip's sound).
    private func audio(_ directory: URL, _ name: String, seconds: Int, tone: Bool) throws -> URL {
        let url = directory.appendingPathComponent(name)
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000))); buffer.frameLength = buffer.frameCapacity
        var state: UInt64 = 0x9E37_79B9_7F4A_7C15
        for index in 0..<Int(buffer.frameLength) {
            state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
            let white = Double(state >> 11) / Double(1 << 53) * 2 - 1
            buffer.floatChannelData![0][index] = tone ? Float(0.5 * sin(2 * .pi * 1_000 * Double(index) / sampleRate)) : Float(white * 0.2)
        }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func video(_ directory: URL) async throws -> URL {
        let url = directory.appendingPathComponent("picture.mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<90 {
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
