#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// KRI-184. Every clip owns an audio track. With no volume set before the first
/// cut point AVAudioMix renders that track at unity, so a muted montage leaked a
/// few milliseconds of each new clip ("a weird sound at every cut"), and a kept
/// clip stepped 0 -> full on its first sample (click). Decodes the real preview
/// mix and the real export.
final class AudioEdgeEnvelopeTests: XCTestCase {
    private let cut = 1.0

    @MainActor func testMutedClipsEmitNoEnergyAtAnyCut() async throws {
        for (label, pcm) in try await render(originalVolume: 0) {
            XCTAssertLessThan(rms(pcm, 0, 0.05), 0.002, "\(label) start")
            XCTAssertLessThan(rms(pcm, cut - 0.02, cut + 0.05), 0.002, "\(label) cut")
            XCTAssertLessThan(rms(pcm, 0, 2), 0.002, "\(label) whole timeline")
        }
    }

    @MainActor func testKeptClipsFadeAtTheirEdgesAndPlayAtFullLevelBetween() async throws {
        for (label, pcm) in try await render(originalVolume: 1) {
            let steady = rms(pcm, 0.5, 0.6)
            XCTAssertGreaterThan(steady, 0.25, "\(label) tone is present")
            // Both sides of a hard cut ramp instead of stepping.
            XCTAssertLessThan(rms(pcm, cut - 0.001, cut), 0.3 * steady, "\(label) outgoing edge")
            XCTAssertLessThan(rms(pcm, cut, cut + 0.001), 0.3 * steady, "\(label) incoming edge")
            XCTAssertLessThan(rms(pcm, 0, 0.001), 0.3 * steady, "\(label) first clip start")
            // The fade is a few tens of ms: the rest of the clip is untouched.
            XCTAssertEqual(rms(pcm, cut + 0.07, cut + 0.17), steady, accuracy: 0.03 * steady, "\(label) after cut")
            XCTAssertEqual(rms(pcm, cut - 0.17, cut - 0.07), steady, accuracy: 0.03 * steady, "\(label) before cut")
        }
    }

    @MainActor private func render(originalVolume: Double) async throws -> [(String, [Float])] {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory, "picture")
        let first = try await mux(picture, try tone(directory, "first", 431), directory.appendingPathComponent("first.mov"))
        let second = try await mux(picture, try tone(directory, "second", 431), directory.appendingPathComponent("second.mov"))
        let urls = ["first": first, "second": second]
        let clips = [
            TimelineClip(id: "a", sourceAssetID: "first", sourceStart: 0.5, sourceDuration: 1, timelineStart: 0, volume: 1),
            TimelineClip(id: "b", sourceAssetID: "second", sourceStart: 0.5, sourceDuration: 1, timelineStart: cut, volume: 1),
        ]
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160),
            assets: urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) },
            tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)],
            audio: AudioMixRecipe(originalVolume: originalVolume))
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("edge.mp4")
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
