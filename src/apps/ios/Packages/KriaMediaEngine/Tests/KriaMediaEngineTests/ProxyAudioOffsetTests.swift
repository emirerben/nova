#if canImport(AVFoundation)
import XCTest
import AVFoundation
import Accelerate
@testable import KriaMediaEngine

/// KRI-374 (eng review finding 1): the song-aligner measures lip-sync offsets on the AAC analysis proxy,
/// but the render plays the full-quality original. A fixed proxy-vs-original audio shift (encoder priming,
/// `.mov` edit lists) would desync every lip-sync cut by that amount, and the proxy contract only checks
/// audio *presence* and duration. This runs the production `AVFoundationProxyGenerator` over an original
/// with an AAC click track and measures the audio onset offset between original and proxy.
@MainActor final class ProxyAudioOffsetTests: XCTestCase {
    /// Click onsets (seconds) written into the original's audio. Irregular so a periodic false match cannot hide a shift.
    private static let clickTimes: [Double] = [0.40, 0.95, 1.55, 2.05, 2.70]
    private static let duration: Double = 3.2
    private static let sampleRate: Double = 44_100
    /// Tolerance in seconds (eng review default). Override: KRI374_PROXY_OFFSET_TOLERANCE_S.
    private var tolerance: Double {
        ProcessInfo.processInfo.environment["KRI374_PROXY_OFFSET_TOLERANCE_S"].flatMap(Double.init) ?? 0.005
    }

    /// 5 ms of a 2 kHz burst with a fast attack: sharp enough to localize to well under a millisecond,
    /// broadband enough to survive AAC.
    private static func clickTrack() -> [Float] {
        var samples = [Float](repeating: 0, count: Int(duration * sampleRate))
        let length = Int(0.005 * sampleRate)
        for time in clickTimes {
            let start = Int((time * sampleRate).rounded())
            for index in 0..<length where start + index < samples.count {
                let envelope = Float(exp(-Double(index) / (0.0012 * sampleRate)))
                samples[start + index] = 0.8 * envelope * Float(sin(2 * .pi * 2_000 * Double(index) / sampleRate))
            }
        }
        return samples
    }

    private func pcmBuffer(_ samples: ArraySlice<Float>, firstSample: Int) throws -> CMSampleBuffer {
        var description = AudioStreamBasicDescription(
            mSampleRate: Self.sampleRate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked, mBytesPerPacket: 4, mFramesPerPacket: 1,
            mBytesPerFrame: 4, mChannelsPerFrame: 1, mBitsPerChannel: 32, mReserved: 0)
        var format: CMAudioFormatDescription?
        XCTAssertEqual(CMAudioFormatDescriptionCreate(allocator: nil, asbd: &description, layoutSize: 0, layout: nil, magicCookieSize: 0, magicCookie: nil, extensions: nil, formatDescriptionOut: &format), noErr)
        let byteCount = samples.count * 4
        var block: CMBlockBuffer?
        XCTAssertEqual(CMBlockBufferCreateWithMemoryBlock(allocator: nil, memoryBlock: nil, blockLength: byteCount, blockAllocator: nil, customBlockSource: nil, offsetToData: 0, dataLength: byteCount, flags: 0, blockBufferOut: &block), noErr)
        let blockBuffer = try XCTUnwrap(block)
        _ = samples.withUnsafeBytes { CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: blockBuffer, offsetIntoDestination: 0, dataLength: byteCount) }
        var buffer: CMSampleBuffer?
        let pts = CMTime(value: CMTimeValue(firstSample), timescale: CMTimeScale(Self.sampleRate))
        XCTAssertEqual(CMAudioSampleBufferCreateReadyWithPacketDescriptions(allocator: nil, dataBuffer: blockBuffer, formatDescription: try XCTUnwrap(format), sampleCount: samples.count, presentationTimeStamp: pts, packetDescriptions: nil, sampleBufferOut: &buffer), noErr)
        return try XCTUnwrap(buffer)
    }

    /// An H.264 + AAC `.mov` like the iPhone camera writes: 30 fps video with the click track as AAC audio.
    private func makeOriginal(in directory: URL) async throws -> URL {
        let url = directory.appendingPathComponent("original.mov")
        let width = 160, height = 90, fps = 30.0
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let video = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: video, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        let audio = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: Self.sampleRate, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 96_000,
        ])
        writer.add(video); writer.add(audio)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        let frames = Int(Self.duration * fps)
        let timescale: Int32 = 60_000
        let samples = Self.clickTrack()
        var nextFrame = 0, cursor = 0
        // Feed whichever input is ready: AVAssetWriter interleaves audio and video, so filling one
        // track to the end first would stall it waiting for the other.
        while nextFrame < frames || cursor < samples.count {
            var progressed = false
            if nextFrame < frames, video.isReadyForMoreMediaData {
                var pixels: CVPixelBuffer?
                CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &pixels)
                let pixel = try XCTUnwrap(pixels)
                CVPixelBufferLockBaseAddress(pixel, [])
                memset(CVPixelBufferGetBaseAddress(pixel), Int32(nextFrame % 200), CVPixelBufferGetBytesPerRow(pixel) * height)
                CVPixelBufferUnlockBaseAddress(pixel, [])
                XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(Double(nextFrame) / fps * Double(timescale)), timescale: timescale)))
                nextFrame += 1; progressed = true
                if nextFrame == frames { video.markAsFinished() }
            }
            if cursor < samples.count, audio.isReadyForMoreMediaData {
                let end = min(cursor + 4_096, samples.count)
                XCTAssertTrue(audio.append(try pcmBuffer(samples[cursor..<end], firstSample: cursor)))
                cursor = end; progressed = true
                if cursor == samples.count { audio.markAsFinished() }
            }
            if !progressed { try await Task.sleep(for: .milliseconds(2)) }
            XCTAssertNotEqual(writer.status, .failed, String(describing: writer.error))
            if writer.status == .failed { break }
        }
        writer.endSession(atSourceTime: CMTime(seconds: Self.duration, preferredTimescale: timescale))
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed, String(describing: writer.error))
        return url
    }

    /// Decodes the first audio track to 48 kHz mono float PCM on the movie timeline. Returns the samples and the
    /// presentation time (seconds) of the first one, so an edit-list or priming shift is part of the measurement.
    private func decodeAudio(_ url: URL) async throws -> (samples: [Float], start: Double) {
        let asset = AVURLAsset(url: url)
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        let track = try XCTUnwrap(tracks.first, "no audio track in \(url.lastPathComponent)")
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false,
        ])
        reader.add(output)
        XCTAssertTrue(reader.startReading())
        var samples: [Float] = []
        var start: Double?
        while let buffer = output.copyNextSampleBuffer() {
            if start == nil { start = CMSampleBufferGetPresentationTimeStamp(buffer).seconds }
            guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(block)
            var chunk = [Float](repeating: 0, count: length / 4)
            chunk.withUnsafeMutableBytes { _ = CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }
            samples.append(contentsOf: chunk)
        }
        XCTAssertEqual(reader.status, .completed, String(describing: reader.error))
        return (samples, start ?? 0)
    }

    /// Per-click lag (seconds) of `proxy` relative to `original`, by cross-correlating a window around each click.
    private func clickOffsets(original: (samples: [Float], start: Double), proxy: (samples: [Float], start: Double)) -> [Double] {
        let rate = 48_000.0
        let half = Int(0.020 * rate), maxLag = Int(0.100 * rate)
        var offsets: [Double] = []
        for time in Self.clickTimes {
            let centerA = Int(((time - original.start) * rate).rounded())
            let reference = Array(original.samples[max(0, centerA - half)..<min(original.samples.count, centerA + half)])
            let searchStart = max(0, Int(((time - proxy.start) * rate).rounded()) - half - maxLag)
            let searchEnd = min(proxy.samples.count, Int(((time - proxy.start) * rate).rounded()) + half + maxLag)
            guard reference.count > 64, searchEnd - searchStart > reference.count else { continue }
            let window = Array(proxy.samples[searchStart..<searchEnd])
            let lags = window.count - reference.count + 1
            var correlation = [Float](repeating: 0, count: lags)
            vDSP_conv(window, 1, reference, 1, &correlation, 1, vDSP_Length(lags), vDSP_Length(reference.count))
            guard let best = correlation.indices.max(by: { correlation[$0] < correlation[$1] }) else { continue }
            // Parabolic refinement to sub-sample precision.
            var fractional = 0.0
            if best > 0, best < lags - 1 {
                let a = Double(correlation[best - 1]), b = Double(correlation[best]), c = Double(correlation[best + 1])
                let denominator = a - 2 * b + c
                if denominator != 0 { fractional = 0.5 * (a - c) / denominator }
            }
            // `window[best]` aligns the proxy with the start of `reference` (original sample `centerA - half`).
            let proxyStartInOriginalTimeline = proxy.start + (Double(searchStart + best) + fractional) / rate
            let originalStart = original.start + Double(max(0, centerA - half)) / rate
            offsets.append(proxyStartInOriginalTimeline - originalStart)
        }
        return offsets
    }

    /// The measurement must be able to see a shift at all: a copy of the click track delayed by a known
    /// amount has to read back as that amount, or a "0 ms" result above would prove nothing.
    func testMeasurementDetectsAKnownShift() {
        let samples = Self.clickTrack()
        let rate = 48_000.0
        // Resample the 44.1 kHz fixture to the 48 kHz analysis rate by nearest sample (clicks are sparse and sharp).
        let resampled = (0..<Int(Self.duration * rate)).map { samples[min(samples.count - 1, Int(Double($0) * Self.sampleRate / rate))] }
        for shiftMs in [-12.0, 3.0, 25.0] {
            let shift = Int(shiftMs / 1000 * rate)
            var delayed = [Float](repeating: 0, count: resampled.count)
            for index in resampled.indices where index + shift >= 0 && index + shift < delayed.count { delayed[index + shift] = resampled[index] }
            let offsets = clickOffsets(original: (resampled, 0), proxy: (delayed, 0)).sorted()
            XCTAssertEqual(offsets.count, Self.clickTimes.count)
            XCTAssertEqual(offsets[offsets.count / 2] * 1000, shiftMs, accuracy: 0.1, "measured \(offsets.map { $0 * 1000 })")
        }
    }

    func testProxyAudioOnsetMatchesOriginalWithinTolerance() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("kri374-offset-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let original = try await makeOriginal(in: directory)
        let proxyURL = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360))
            .makeProxy(for: original, destination: directory.appendingPathComponent("proxy.mp4"))
        if let keep = ProcessInfo.processInfo.environment["KRI374_KEEP_DIR"] {
            try? FileManager.default.createDirectory(atPath: keep, withIntermediateDirectories: true)
            try? FileManager.default.copyItem(at: original, to: URL(fileURLWithPath: keep).appendingPathComponent("original.mov"))
            try? FileManager.default.copyItem(at: proxyURL, to: URL(fileURLWithPath: keep).appendingPathComponent("proxy.mp4"))
        }
        let originalAudio = try await decodeAudio(original)
        let proxyAudio = try await decodeAudio(proxyURL)
        let offsets = clickOffsets(original: originalAudio, proxy: proxyAudio)
        XCTAssertEqual(offsets.count, Self.clickTimes.count, "every click must be measurable in both files")
        let sorted = offsets.sorted()
        let median = sorted[sorted.count / 2]
        let spread = (sorted.last ?? 0) - (sorted.first ?? 0)
        print("KRI374_PROXY_AUDIO_SAMPLES original=\(originalAudio.samples.count) proxy=\(proxyAudio.samples.count) (48 kHz mono)")
        print("KRI374_PROXY_AUDIO_OFFSET_MS median=\(String(format: "%+.3f", median * 1000)) min=\(String(format: "%+.3f", (sorted.first ?? 0) * 1000)) max=\(String(format: "%+.3f", (sorted.last ?? 0) * 1000)) spread=\(String(format: "%.3f", spread * 1000)) streamStart(original)=\(String(format: "%.4f", originalAudio.start))s streamStart(proxy)=\(String(format: "%.4f", proxyAudio.start))s tolerance=\(tolerance * 1000)ms")
        XCTAssertLessThanOrEqual(abs(median), tolerance, "proxy audio is shifted \(median * 1000) ms from the original; set song_alignment_proxy_offset_s")
        XCTAssertLessThanOrEqual(spread, tolerance, "proxy audio drifts across the clip by \(spread * 1000) ms")
    }
}
#endif
