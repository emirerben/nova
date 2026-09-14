#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// Cloud uses atrim/asetpts/delay/amix with no automatic normalization. Keep
/// native's real decoded preview and export PCM aligned at source-audio joins.
final class SourceAudioOverlapParityTests: XCTestCase {
    @MainActor func testOverlappingSourceAudioHasConstantGainsInPreviewAndExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory, "picture")
        let first = try await mux(picture, try tone(directory, "first", 431), directory.appendingPathComponent("first.mov"))
        let second = try await mux(picture, try tone(directory, "second", 719), directory.appendingPathComponent("second.mov"))
        let silent = try await video(directory, "silent")
        let urls = ["first": first, "second": second, "silent": silent]
        let clips = [
            TimelineClip(id: "outgoing", sourceAssetID: "first", sourceStart: 0.5, sourceDuration: 1.5, timelineStart: 0, volume: 1),
            TimelineClip(id: "incoming", sourceAssetID: "second", sourceStart: 0.75, sourceDuration: 1.5, timelineStart: 1,
                         transition: Transition(kind: .crossfade, duration: 0.5), volume: 1),
            TimelineClip(id: "silent", sourceAssetID: "silent", sourceStart: 0.25, sourceDuration: 0.5, timelineStart: 2.5, volume: 1),
            TimelineClip(id: "repeat", sourceAssetID: "first", sourceStart: 1.0, sourceDuration: 1, timelineStart: 3, volume: 1),
        ]
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160),
            assets: urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) },
            tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)],
            audio: AudioMixRecipe(originalVolume: 0.4))
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("overlap.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let firstPCM = try await decode(AVURLAsset(url: first), nil)
        let secondPCM = try await decode(AVURLAsset(url: second), nil)
        let firstExpected = try (0..<2).map { try amplitude(firstPCM, 1, 431, channel: $0) * 0.4 }
        let secondExpected = try (0..<2).map { try amplitude(secondPCM, 1, 719, channel: $0) * 0.4 }

        for (label, asset, mix) in [("preview", preview.preview.playerItem.asset, preview.preview.playerItem.audioMix),
                                    ("export", AVURLAsset(url: output) as AVAsset, nil)] {
            let pcm = try await decode(asset, mix)
            // These windows avoid transition boundaries. A and B must each be
            // present at the overlap with their un-faded 0.4 master gain.
            try assertBand(pcm, at: 0.5, first: firstExpected, second: nil, label: label)
            try assertBand(pcm, at: 1.25, first: firstExpected, second: secondExpected, label: label)
            try assertBand(pcm, at: 2.0, first: nil, second: secondExpected, label: label)
            try assertBand(pcm, at: 2.75, first: nil, second: nil, label: label)
            try assertBand(pcm, at: 3.5, first: firstExpected, second: nil, label: label)
        }
    }

    private func assertBand(_ pcm: [Float], at time: Double, first: [Double]?, second: [Double]?, label: String) throws {
        for channel in 0..<2 {
            let a = try amplitude(pcm, time, 431, channel: channel), b = try amplitude(pcm, time, 719, channel: channel)
            if let first { XCTAssertEqual(a, first[channel], accuracy: 0.02, "\(label) first tone at \(time)s channel \(channel)") }
            else { XCTAssertLessThan(a, 0.01, "\(label) first tone at \(time)s channel \(channel)") }
            if let second { XCTAssertEqual(b, second[channel], accuracy: 0.02, "\(label) second tone at \(time)s channel \(channel)") }
            else { XCTAssertLessThan(b, 0.01, "\(label) second tone at \(time)s channel \(channel)") }
        }
    }

    private func amplitude(_ pcm: [Float], _ time: Double, _ frequency: Double, channel: Int = 0) throws -> Double {
        let start = Int(time * 48_000) * 2, count = 7_200
        guard channel >= 0, channel < 2, start >= 0, start + count * 2 <= pcm.count else { throw MediaEngineError.exportFailed }
        var cosine = 0.0, sine = 0.0
        for index in 0..<count {
            let phase = 2 * Double.pi * frequency * Double(index) / 48_000
            let sample = Double(pcm[start + index * 2 + channel])
            cosine += sample * cos(phase); sine += sample * sin(phase)
        }
        return 2 * hypot(cosine, sine) / Double(count)
    }

    private func tone(_ directory: URL, _ name: String, _ frequency: Double) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4 * 48_000)); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * frequency / 48_000)) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func video(_ directory: URL, _ name: String) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
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
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false]); output.audioMix = mix; reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: 4 * 48_000 * 2)
        while let sample = output.copyNextSampleBuffer() { let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2; let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample)); var bytes = Data(count: CMBlockBufferGetDataLength(block)); XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr); bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } } }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
