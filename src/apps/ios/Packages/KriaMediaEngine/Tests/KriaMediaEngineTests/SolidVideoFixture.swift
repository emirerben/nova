#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation

/// A solid-colour H.264 clip with a quiet AAC tone, written and muxed the way `TalkingHeadCutawayCompositionTests` does.
/// `videoStartOffset` muxes the video track later than the audio (a leading empty edit), the shape of
/// an original whose picture starts ~21 ms after its sound (KRI-811).
enum SolidVideoFixture {
    @MainActor static func make(directory: URL, name: String, color: CGColor, seconds: Double, size: (Int, Int) = (96, 160), fps: Int32 = 30, videoStartOffset: Double = 0) async throws -> URL {
        let (width, height) = size
        let (w, h) = (width, height)
        let videoURL = directory.appendingPathComponent("\(name)-video.mp4")
        let writer = try AVAssetWriter(outputURL: videoURL, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: w, AVVideoHeightKey: h])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h])
        writer.add(input)
        XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<Int((seconds * Double(fps)).rounded(.up)) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: w, height: h, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color); context.fill(CGRect(x: 0, y: 0, width: w, height: h))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
        }
        input.markAsFinished(); await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        let audioURL = directory.appendingPathComponent("\(name)-audio.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000)))
        pcm.frameLength = pcm.frameCapacity
        for index in 0..<Int(pcm.frameLength) { pcm.floatChannelData![0][index] = 0.2 * Float(sin(Double(index) * 2 * .pi * 440 / 48_000)) }
        do {
            let file = try AVAudioFile(forWriting: audioURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
            try file.write(from: pcm)
        }
        let composition = AVMutableComposition()
        for (url, kind) in [(videoURL, AVMediaType.video), (audioURL, AVMediaType.audio)] {
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: kind)
            let track = try XCTUnwrap(tracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            let at = kind == .video ? CMTime(seconds: videoStartOffset, preferredTimescale: 1_000_000) : .zero
            try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: at)
        }
        let output = directory.appendingPathComponent("\(name).mov")
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        exporter.outputURL = output; exporter.outputFileType = .mov
        await exporter.export()
        XCTAssertEqual(exporter.status, .completed)
        return output
    }
}
#endif
