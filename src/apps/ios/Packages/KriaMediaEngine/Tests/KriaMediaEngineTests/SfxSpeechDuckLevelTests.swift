#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// Level check for the server-side SFX-under-speech duck (KRI-181 follow-up).
///
/// The phone renderer has no loudness or sidechain stage, so a Talking edit's
/// sound effect plays at its raw level over the speaker's own audio. Catalog
/// effects are mastered to -14 LUFS (`scripts/sfx_library/master.py`), while a
/// phone talk-to-camera recording sits around -23 dBFS. This test models those
/// levels with two tones: the speaker clip's own audio at 400 Hz and a catalog
/// effect at 2 kHz on the `sfx` audio track, then exports through the real
/// native exporter and measures each band inside and around the effect window.
///
/// `serverDuckGain` must equal `SFX_SPEECH_DUCK_GAIN` in
/// `src/apps/api/app/pipeline/phone_subtitled_plan.py`; the Python test
/// `test_ios_level_test_pins_the_server_duck_gain` reads this file to keep
/// them in lockstep.
final class SfxSpeechDuckLevelTests: XCTestCase {
    static let serverDuckGain = 0.35
    private static let speechHz = 400.0, effectHz = 2_000.0
    // Sine peak amplitudes: 0.1 is about -23 dBFS RMS, 0.28 about -14 dBFS RMS.
    private static let speechAmplitude = 0.1, effectAmplitude = 0.28
    private static let effectStart = 1.0, effectDuration = 1.0

    @MainActor func testFullVolumeEffectDrownsSpeechButServerDuckKeepsItIntelligible() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory, "picture")
        let speaker = try await mux(picture, try tone(directory, "speech", Self.speechHz, amplitude: Self.speechAmplitude, seconds: 4),
                                    directory.appendingPathComponent("speaker.mov"))
        let effect = try tone(directory, "effect", Self.effectHz, amplitude: Self.effectAmplitude, seconds: 1)
        let urls = ["speaker": speaker, "effect": effect]

        let fullVolume = try await measure(effectVolume: 1.0, urls: urls, directory: directory)
        let ducked = try await measure(effectVolume: Self.serverDuckGain, urls: urls, directory: directory)

        // The problem this duck exists for: at full volume the effect sits
        // well above the speaker (about +9 dB).
        XCTAssertGreaterThan(fullVolume.effectInWindow, 2 * fullVolume.speechInWindow,
                             "a full-volume effect should dominate the speech band without the duck")
        for (label, level) in [("full volume", fullVolume), ("ducked", ducked)] {
            // The speaker is never lowered: speech-band level inside the
            // effect window matches the surrounding speech within 1 dB.
            XCTAssertGreaterThan(level.speechAround, 0.05, "\(label): speech must be audible around the effect")
            XCTAssertEqual(20 * log10(level.speechInWindow / level.speechAround), 0, accuracy: 1,
                           "\(label): speech band inside the effect window must match the surrounding speech")
            XCTAssertLessThan(level.effectAround, 0.01, "\(label): the effect must be silent outside its window")
        }
        // With the duck the effect lands no more than 1 dB above the speaker.
        let speechToEffectDB = 20 * log10(ducked.speechInWindow / ducked.effectInWindow)
        XCTAssertGreaterThan(speechToEffectDB, -1, "ducked effect must not drown the speech band")
        XCTAssertEqual(ducked.effectInWindow / fullVolume.effectInWindow, Self.serverDuckGain, accuracy: 0.03)
    }

    private struct Levels { var speechInWindow: Double; var speechAround: Double; var effectInWindow: Double; var effectAround: Double }

    @MainActor private func measure(effectVolume: Double, urls: [String: URL], directory: URL) async throws -> Levels {
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160),
            assets: urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) },
            tracks: [
                TimelineTrack(id: "subtitled", kind: .video, clips: [
                    TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceStart: 0, sourceDuration: 3, timelineStart: 0)]),
                TimelineTrack(id: "sfx", kind: .audio, clips: [
                    TimelineClip(id: "sfx-beat", sourceAssetID: "effect", sourceStart: 0, sourceDuration: Self.effectDuration,
                                 timelineStart: Self.effectStart, volume: effectVolume)]),
            ],
            audio: AudioMixRecipe(originalVolume: 1))
        let output = directory.appendingPathComponent("duck-\(effectVolume).mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-\(effectVolume)")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let pcm = try await decode(AVURLAsset(url: output))
        // Windows sit clear of the effect's edges and the AAC priming.
        let inside = [1.2, 1.5], around = [0.4, 2.4]
        func mean(_ times: [Double], _ hz: Double) throws -> Double { try times.map { try amplitude(pcm, $0, hz) }.reduce(0, +) / Double(times.count) }
        return Levels(speechInWindow: try mean(inside, Self.speechHz), speechAround: try mean(around, Self.speechHz),
                      effectInWindow: try mean(inside, Self.effectHz), effectAround: try mean(around, Self.effectHz))
    }

    /// Single-bin DFT amplitude over 0.15 s of the left channel. Both test
    /// frequencies complete a whole number of cycles in that window, so the
    /// two bands do not leak into each other.
    private func amplitude(_ pcm: [Float], _ time: Double, _ frequency: Double) throws -> Double {
        let start = Int(time * 48_000) * 2, count = 7_200
        guard start >= 0, start + count * 2 <= pcm.count else { throw MediaEngineError.exportFailed }
        var cosine = 0.0, sine = 0.0
        for index in 0..<count {
            let phase = 2 * Double.pi * frequency * Double(index) / 48_000
            let sample = Double(pcm[start + index * 2])
            cosine += sample * cos(phase); sine += sample * sin(phase)
        }
        return 2 * hypot(cosine, sine) / Double(count)
    }

    private func tone(_ directory: URL, _ name: String, _ frequency: Double, amplitude: Double, seconds: Int) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000))); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(amplitude * sin(Double(index) * 2 * .pi * frequency / 48_000)) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func video(_ directory: URL, _ name: String) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<120 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw)
            let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, [])
            memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed); return url
    }

    @MainActor private func mux(_ video: URL, _ audio: URL, _ output: URL) async throws -> URL {
        let composition = AVMutableComposition()
        for (url, kind) in [(video, AVMediaType.video), (audio, AVMediaType.audio)] {
            // Keep the asset alive: a track whose asset deallocated fails to insert (-11800).
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: kind)
            let track = try XCTUnwrap(tracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: .zero)
        }
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        exporter.outputURL = output; exporter.outputFileType = .mov; await exporter.export()
        XCTAssertEqual(exporter.status, .completed, "\(String(describing: exporter.error))"); return output
    }

    @MainActor private func decode(_ asset: AVAsset) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false])
        reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: 4 * 48_000 * 2)
        while let sample = output.copyNextSampleBuffer() {
            let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2
            let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample)); var bytes = Data(count: CMBlockBufferGetDataLength(block))
            XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr)
            bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } }
        }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
