#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// KRI-139: the phone mix matches the cloud's audio chain. A music bed fades in
/// and out, a voiceover fades out over the cloud's 0.5 s, the footage bed ducks
/// under the voice (`sidechaincompress`), and the export lands on the loudness
/// target (`loudnorm`). Every check decodes the real preview mix and the real
/// export, like `AudioEdgeEnvelopeTests`.
final class AudioMixParityTests: XCTestCase {
    private let length = 3.0

    // MARK: Fades

    @MainActor func testMusicBedFadesInAndOutAtBothEnds() async throws {
        let bed = TimelineClip(id: "bed", sourceAssetID: "bed", sourceDuration: length, audioFadeIn: 0.5, audioFadeOut: 0.5)
        for (label, pcm) in try await render(audioClip: bed, originalVolume: 0) {
            let steady = rms(pcm, 1.2, 1.6)
            XCTAssertGreaterThan(steady, 0.25, "\(label) bed is present")
            XCTAssertLessThan(rms(pcm, 0, 0.03), 0.1 * steady, "\(label) starts silent")
            XCTAssertEqual(rms(pcm, 0.2, 0.3) / steady, 0.5, accuracy: 0.15, "\(label) halfway up the fade-in")
            XCTAssertEqual(rms(pcm, 0.6, 0.9), steady, accuracy: 0.05 * steady, "\(label) full level once faded in")
            XCTAssertEqual(rms(pcm, length - 0.3, length - 0.2) / steady, 0.5, accuracy: 0.15, "\(label) halfway down the fade-out")
            XCTAssertLessThan(rms(pcm, length - 0.03, length), 0.1 * steady, "\(label) ends silent")
        }
    }

    @MainActor func testVoiceFadesOutOverTheCloudsHalfSecondOnly() async throws {
        let voice = TimelineClip(id: "voice", sourceAssetID: "bed", sourceDuration: length, audioFadeOut: 0.5)
        for (label, pcm) in try await render(audioClip: voice, originalVolume: 0) {
            let steady = rms(pcm, 1.2, 1.6)
            // No authored fade-in: only the 25 ms declick edge at the start.
            XCTAssertEqual(rms(pcm, 0.06, 0.2), steady, accuracy: 0.05 * steady, "\(label) starts at full level")
            XCTAssertEqual(rms(pcm, length - 0.8, length - 0.55), steady, accuracy: 0.05 * steady, "\(label) untouched before the fade")
            XCTAssertEqual(rms(pcm, length - 0.3, length - 0.2) / steady, 0.5, accuracy: 0.15, "\(label) halfway down")
            XCTAssertLessThan(rms(pcm, length - 0.03, length), 0.1 * steady, "\(label) ends silent")
        }
    }

    // MARK: Ducking

    @MainActor func testFootageDucksUnderTheVoiceAndRecoversInItsPauses() async throws {
        // Voice speaks 0.5-1.5 s, then pauses; the footage bed plays throughout.
        let voice = TimelineClip(id: "voice", sourceAssetID: "bed", sourceDuration: length)
        for (label, pcm) in try await render(audioClip: voice, originalVolume: 0.6, duck: true, voiceGate: 0.5..<1.5, voiceFrequency: 1_000) {
            let before = tone(pcm, 431, 0.1, 0.4), during = tone(pcm, 431, 0.9, 1.3), after = tone(pcm, 431, 2.2, 2.8)
            XCTAssertGreaterThan(before, 0.05, "\(label) bed audible before the voice")
            // threshold 0.03, ratio 8 on a 0.5 key: about -19 dB, i.e. ~0.11x.
            XCTAssertLessThan(during, 0.2 * before, "\(label) bed ducks under the voice")
            XCTAssertEqual(after, before, accuracy: 0.1 * before, "\(label) bed recovers in the pause")
        }
    }

    func testDuckEnvelopeMatchesTheSidechainGainComputer() {
        // Reference: ffmpeg 8 `sidechaincompress=threshold=0.03:ratio=8:attack=15:
        // release=300:makeup=1` on a 0.25 431 Hz bed keyed by a 0.25 1 kHz sine
        // leaves the bed at 0.159x its RMS over the second second.
        let sampleRate = 48_000.0
        let samples = 2 * Int(sampleRate)
        let key = (0..<samples).map { Float(abs(0.25 * sin(Double($0) * 2 * .pi * 1_000 / sampleRate))) }
        let gains = AudioDuckEnvelope.Compressor().gains(forKey: key, sampleRate: sampleRate)
        var power = 0.0
        for index in Int(sampleRate)..<samples { let bed = 0.25 * sin(Double(index) * 2 * .pi * 431 / sampleRate) * Double(gains[index]); power += bed * bed }
        let ratio = (power / sampleRate).squareRoot() / (0.25 / 2.squareRoot())
        XCTAssertEqual(ratio, 0.159, accuracy: 0.012)
        XCTAssertEqual(AudioDuckEnvelope.Compressor().gains(forKey: [Float](repeating: 0.001, count: 4_800), sampleRate: sampleRate).last, 1, "below the knee stays at unity")
    }

    func testSimplifyKeepsEveryCorner() {
        let ramp: [(time: Double, gain: Double)] = (0...100).map { index in
            let t = Double(index) / 100
            return (t, t < 0.5 ? 1 : max(0.2, 1 - (t - 0.5) * 4))
        }
        let simplified = AudioDuckEnvelope.simplify(ramp, tolerance: 0.001)
        XCTAssertLessThan(simplified.count, 8)
        let envelope = AudioDuckEnvelope(points: simplified)
        for point in ramp { XCTAssertEqual(envelope.gain(at: point.time), point.gain, accuracy: 0.002) }
    }

    func testDuckingNeedsTheVerifiedCapabilityInsteadOfAHardRefusal() {
        var recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2)])])
        recipe.audio.duckOriginalDuringMusic = true
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.audioDucking))
        let unverified = CapabilityNegotiator().decide(for: recipe)
        XCTAssertEqual(unverified.route, .cloud)
        XCTAssertEqual(unverified.missingCapabilities, [.audioDucking])
        let verified = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: recipe.effectiveCapabilities)).decide(for: recipe)
        XCTAssertEqual(verified.route, .local)
    }

    // MARK: Loudness

    func testMeterReadsASineAtItsBS1770Level() {
        // A -20 dBFS 997 Hz sine on both channels is -20 LUFS by definition.
        var meter = LoudnessMeter()
        meter.consume(interleavedStereo: (0..<(3 * 48_000)).flatMap { index -> [Float] in
            let value = Float(0.1 * sin(Double(index) * 2 * .pi * 997 / 48_000)); return [value, value]
        })
        XCTAssertEqual(try XCTUnwrap(meter.integratedLUFS), -20, accuracy: 0.1)
        XCTAssertNil(LoudnessMeter().integratedLUFS, "no blocks, no reading")
    }

    func testLimiterHoldsTheTruePeakCeiling() {
        var meter = LoudnessMeter()
        meter.consume(interleavedStereo: [Float](repeating: 0.01, count: 2 * 48_000))
        var normalizer = try! XCTUnwrap(LoudnessNormalizer(target: -14, meter: meter))
        var pcm: [Int16] = (0..<9_600).map { $0.isMultiple(of: 97) ? 30_000 : 400 }
        normalizer.process(&pcm)
        let ceiling = Int16(LoudnessNormalizer.ceiling * 32_768) + 1
        XCTAssertLessThanOrEqual(pcm.map { abs(Int($0)) }.max() ?? 0, Int(ceiling))
    }

    @MainActor func testExportLandsOnTheTargetFromAQuietAndALoudMix() async throws {
        for amplitude in [0.03, 0.9] {
            let pcm = try await exportOnly(footageAmplitude: amplitude, target: -14)
            var meter = LoudnessMeter(); meter.consume(interleavedStereo: pcm)
            // The cloud's loudnorm lands within about 1 LU of its target; match that.
            XCTAssertEqual(try XCTUnwrap(meter.integratedLUFS), -14, accuracy: 1, "amplitude \(amplitude)")
            XCTAssertLessThanOrEqual(meter.samplePeak, LoudnessNormalizer.ceiling + 0.02, "amplitude \(amplitude)")
        }
    }

    @MainActor func testNoTargetLeavesTheExportAtItsMixedLevel() async throws {
        let pcm = try await exportOnly(footageAmplitude: 0.03, target: nil)
        var meter = LoudnessMeter(); meter.consume(interleavedStereo: pcm)
        XCTAssertLessThan(try XCTUnwrap(meter.integratedLUFS), -25)
    }

    // MARK: Wire

    func testNewFieldsRoundTripAndOldPayloadsStillDecode() throws {
        var recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .audio, clips: [
            TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2, audioFadeIn: 0.5, audioFadeOut: 0.25)])])
        recipe.audio.targetLUFS = -14
        let data = try JSONEncoder().encode(recipe)
        XCTAssertEqual(try JSONDecoder().decode(EditRecipe.self, from: data), recipe)
        let legacy = try JSONDecoder().decode(AudioMixRecipe.self, from: Data(#"{"musicVolume":1,"originalVolume":1,"fadeIn":0,"fadeOut":0,"duckOriginalDuringMusic":false}"#.utf8))
        XCTAssertNil(legacy.targetLUFS)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(legacy), as: UTF8.self).contains("targetLufs"))
        recipe.audio.targetLUFS = 3
        XCTAssertThrowsError(try recipe.validate())
    }

    // MARK: Rendering

    @MainActor private func render(audioClip: TimelineClip, originalVolume: Double, duck: Bool = false,
                                   voiceGate: Range<Double>? = nil, voiceFrequency: Double = 431) async throws -> [(String, [Float])] {
        let directory = try scratch()
        defer { try? FileManager.default.removeItem(at: directory) }
        let footage = try await mux(try await video(directory), try tone(directory, "footage", 431, amplitude: 0.5), directory.appendingPathComponent("footage.mov"))
        let urls = ["footage": footage, "bed": try tone(directory, "bed", voiceFrequency, amplitude: 0.5, gate: voiceGate)]
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160),
            assets: urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) },
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "f", sourceAssetID: "footage", sourceDuration: length)]),
                     TimelineTrack(id: "audio", kind: .audio, clips: [audioClip])],
            audio: AudioMixRecipe(originalVolume: originalVolume, duckOriginalDuringMusic: duck))
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("mix.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        return [("preview", try await decode(preview.playerItem.asset, preview.playerItem.audioMix)),
                ("export", try await decode(AVURLAsset(url: output), nil))]
    }

    @MainActor private func exportOnly(footageAmplitude: Double, target: Double?) async throws -> [Float] {
        let directory = try scratch()
        defer { try? FileManager.default.removeItem(at: directory) }
        let footage = try await mux(try await video(directory), try tone(directory, "footage", 431, amplitude: footageAmplitude), directory.appendingPathComponent("footage.mov"))
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "footage", relativePath: "footage")],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "f", sourceAssetID: "footage", sourceDuration: length)])],
            audio: AudioMixRecipe(targetLUFS: target))
        let output = directory.appendingPathComponent("loud.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: ["footage": footage], outputURL: output)
        return try await decode(AVURLAsset(url: output), nil)
    }

    private func scratch() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// RMS over both channels of the interleaved 48 kHz stereo decode.
    private func rms(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()) * 2, end = min(Int((to * 48_000).rounded()) * 2, pcm.count)
        guard start >= 0, end > start else { return 0 }
        var sum = 0.0
        for index in start..<end { sum += Double(pcm[index]) * Double(pcm[index]) }
        return (sum / Double(end - start)).squareRoot()
    }

    /// Amplitude of one frequency in the left channel (Goertzel), so the footage
    /// bed can be measured while the voice plays on another frequency.
    private func tone(_ pcm: [Float], _ frequency: Double, _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()), end = min(Int((to * 48_000).rounded()), pcm.count / 2)
        let coefficient = 2 * cos(2 * .pi * frequency / 48_000)
        var s1 = 0.0, s2 = 0.0
        for index in start..<end { let s0 = Double(pcm[index * 2]) + coefficient * s1 - s2; s2 = s1; s1 = s0 }
        let power = s1 * s1 + s2 * s2 - coefficient * s1 * s2
        return 2 * power.squareRoot() / Double(end - start)
    }

    private func tone(_ directory: URL, _ name: String, _ frequency: Double, amplitude: Double, gate: Range<Double>? = nil) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4 * 48_000)); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) {
            let time = Double(index) / 48_000
            let on = gate.map { $0.contains(time) } ?? true
            buffer.floatChannelData![0][index] = on ? Float(amplitude * sin(time * 2 * .pi * frequency)) : 0
        }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func video(_ directory: URL) async throws -> URL {
        let url = directory.appendingPathComponent("picture.mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<120 { while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }; var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw); let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, []); memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, []); XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30))) }
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
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough)); exporter.outputURL = output; exporter.outputFileType = .mov; await exporter.export(); XCTAssertEqual(exporter.status, .completed, "\(String(describing: exporter.error))"); return output
    }

    @MainActor private func decode(_ asset: AVAsset, _ mix: AVAudioMix?) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: floatPCMSettings(channels: 2)); output.audioMix = mix; reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: Int(length * 48_000) * 2)
        while let sample = output.copyNextSampleBuffer() {
            let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2
            for (index, value) in try interleavedFloats(sample).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value }
        }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
