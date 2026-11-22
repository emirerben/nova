#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// Synthesized media and band-level measurement for the export level tests
/// (`TalkingMusicDuckLevelTests`). Each test frequency completes a whole number of
/// cycles in the 0.15 s analysis window, so two tones never leak into each other's bin.
enum LevelTestMedia {
    static let sampleRate = 48_000.0
    static let analysisSeconds = 0.15

    static func tone(_ directory: URL, _ name: String, _ frequency: Double, amplitude: Double, seconds: Int) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(Double(seconds) * sampleRate))); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(amplitude * sin(Double(index) * 2 * .pi * frequency / sampleRate)) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: sampleRate, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor static func blackVideo(_ directory: URL, _ name: String, seconds: Int) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<(seconds * 30) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw)
            let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, [])
            memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed); return url
    }

    @MainActor static func mux(_ video: URL, _ audio: URL, _ output: URL) async throws -> URL {
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

    /// Interleaved stereo Float32 PCM of the exported mix, `seconds` long.
    @MainActor static func decode(_ asset: AVAsset, seconds: Int) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: sampleRate, AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false])
        reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: seconds * Int(sampleRate) * 2)
        while let sample = output.copyNextSampleBuffer() {
            let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * sampleRate).rounded()) * 2
            let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample)); var bytes = Data(count: CMBlockBufferGetDataLength(block))
            XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr)
            bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } }
        }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }

    /// Single-bin DFT amplitude of the left channel over the 0.15 s that starts at `time`.
    static func amplitude(_ pcm: [Float], _ time: Double, _ frequency: Double) throws -> Double {
        let start = Int(time * sampleRate) * 2, count = Int(analysisSeconds * sampleRate)
        guard start >= 0, start + count * 2 <= pcm.count else { throw MediaEngineError.exportFailed }
        var cosine = 0.0, sine = 0.0
        for index in 0..<count {
            let phase = 2 * Double.pi * frequency * Double(index) / sampleRate
            let sample = Double(pcm[start + index * 2])
            cosine += sample * cos(phase); sine += sample * sin(phase)
        }
        return 2 * hypot(cosine, sine) / Double(count)
    }

    static func mean(_ pcm: [Float], _ times: [Double], _ frequency: Double) throws -> Double {
        try times.map { try amplitude(pcm, $0, frequency) }.reduce(0, +) / Double(times.count)
    }

    static func decibels(_ ratio: Double) -> Double { 20 * log10(ratio) }
}
#endif
