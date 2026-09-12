#if canImport(AVFoundation)
import AVFoundation
import CVPX
import Foundation
import ImageIO
import UniformTypeIdentifiers

/// Decode unsupported VP9 originals once, locally. The source is never changed;
/// the H.264/AAC file is a disposable editing cache, not a rendered edit.
public actor NativeVP9Source {
    public static let shared = NativeVP9Source()

    public func prepare(_ source: URL, cacheDirectory: URL, trace: (@Sendable (String) -> Void)? = nil) async throws -> URL {
        try Task.checkCancellation()
        trace?("image-probe")
        if let image = CGImageSourceCreateWithURL(source as CFURL, nil),
           let type = CGImageSourceGetType(image), UTType(type as String)?.conforms(to: .image) == true { return source }
        trace?("video-probe")
        let asset = AVURLAsset(url: source)
        guard let track = try await asset.loadTracks(withMediaType: .video).first else { return source }
        trace?("format-probe")
        let formats = try await track.load(.formatDescriptions)
        guard formats.contains(where: { CMFormatDescriptionGetMediaSubType($0) == 0x76703039 }) else { return source }
        trace?("fingerprint")
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: source)
        try FileManager.default.createDirectory(at: cacheDirectory, withIntermediateDirectories: true)
        let output = cacheDirectory.appendingPathComponent("vp9-v1-\(fingerprint.hex).mp4")
        if FileManager.default.fileExists(atPath: output.path) { return output }
        let temporary = cacheDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        defer { try? FileManager.default.removeItem(at: temporary) }
        trace?("conversion-start")
        let conversion = try await VP9Conversion(source: source, output: temporary)
        try await conversion.run()
        trace?("conversion-complete")
        try Task.checkCancellation()
        // Concurrent callers may have completed while this actor was suspended.
        if !FileManager.default.fileExists(atPath: output.path) {
            try FileManager.default.moveItem(at: temporary, to: output)
        }
        return output
    }
}

private final class VP9Conversion: @unchecked Sendable {
    let reader: AVAssetReader
    let writer: AVAssetWriter
    let video: AVAssetReaderTrackOutput
    let videoInput: AVAssetWriterInput
    let adaptor: AVAssetWriterInputPixelBufferAdaptor
    let audio: AVAssetReaderAudioMixOutput?
    let audioInput: AVAssetWriterInput?
    let decoder: OpaquePointer
    let width: Int
    let height: Int
    let duration: CMTime

    init(source: URL, output: URL) async throws {
        let asset = AVURLAsset(url: source)
        guard let track = try await asset.loadTracks(withMediaType: .video).first else { throw MediaEngineError.exportUnavailable }
        let size = try await track.load(.naturalSize)
        guard size.width > 0, size.height > 0, size.width <= 8192, size.height <= 8192,
              let decoder = kria_vpx_create(2) else { throw MediaEngineError.unsupportedCapability }
        self.decoder = decoder
        width = Int(size.width); height = Int(size.height)
        do {
            duration = try await asset.load(.duration)
            reader = try AVAssetReader(asset: asset)
            writer = try AVAssetWriter(outputURL: output, fileType: .mp4)
            video = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
            video.alwaysCopiesSampleData = false
            videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
                AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: min(40_000_000, max(4_000_000, width * height * 8)),
                    AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel]])
            videoInput.transform = try await track.load(.preferredTransform)
            adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: videoInput,
                sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                    kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
                    kCVPixelBufferIOSurfacePropertiesKey as String: [:]])
            let audioTracks = try await asset.loadTracks(withMediaType: .audio)
            if audioTracks.isEmpty { audio = nil; audioInput = nil }
            else {
                audio = AVAssetReaderAudioMixOutput(audioTracks: audioTracks, audioSettings: [
                    AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2,
                    AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false,
                    AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false])
                audioInput = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                    AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
                    AVNumberOfChannelsKey: 2, AVEncoderBitRateKey: 192_000])
            }
            guard reader.canAdd(video), writer.canAdd(videoInput) else { throw MediaEngineError.exportUnavailable }
            reader.add(video); writer.add(videoInput)
            if let audio, let audioInput {
                guard reader.canAdd(audio), writer.canAdd(audioInput) else { throw MediaEngineError.exportUnavailable }
                reader.add(audio); writer.add(audioInput)
            }
        } catch { kria_vpx_destroy(decoder); throw error }
    }

    deinit { kria_vpx_destroy(decoder) }

    func run() async throws {
        do {
            try Task.checkCancellation()
            let writing = writer.startWriting(); let reading = reader.startReading()
            guard writing, reading else { throw writer.error ?? reader.error ?? MediaEngineError.exportFailed }
            writer.startSession(atSourceTime: .zero)
            var videoDone = false
            var audioDone = audio == nil
            var frames = 0
            while !videoDone || !audioDone {
                try Task.checkCancellation()
                guard reader.status != .failed, writer.status != .failed else { throw reader.error ?? writer.error ?? MediaEngineError.exportFailed }
                var advanced = false
                if !videoDone, videoInput.isReadyForMoreMediaData {
                    try autoreleasepool {
                        if let sample = video.copyNextSampleBuffer() {
                            if try append(sample) { frames += 1 }
                        } else { videoDone = true; videoInput.markAsFinished() }
                    }
                    advanced = true
                }
                if !audioDone, let audio, let audioInput, audioInput.isReadyForMoreMediaData {
                    try autoreleasepool {
                        if let sample = audio.copyNextSampleBuffer() {
                            guard audioInput.append(sample) else { throw writer.error ?? MediaEngineError.exportFailed }
                        } else { audioDone = true; audioInput.markAsFinished() }
                    }
                    advanced = true
                }
                if !advanced { try await Task.sleep(for: .milliseconds(2)) }
            }
            guard frames > 0, reader.status != .failed else { throw reader.error ?? MediaEngineError.exportFailed }
            writer.endSession(atSourceTime: duration)
            await writer.finishWriting()
            guard writer.status == .completed else { throw writer.error ?? MediaEngineError.exportFailed }
        } catch { reader.cancelReading(); writer.cancelWriting(); throw error }
    }

    private func append(_ sample: CMSampleBuffer) throws -> Bool {
        guard CMSampleBufferGetNumSamples(sample) > 0 else { return false }
        guard let block = CMSampleBufferGetDataBuffer(sample) else { throw MediaEngineError.exportFailed }
        let length = CMBlockBufferGetDataLength(block)
        guard length > 0, length <= 64 * 1024 * 1024 else { throw MediaEngineError.unsupportedCapability }
        var data = Data(count: length)
        let decoded = data.withUnsafeMutableBytes { bytes -> Bool in
            guard let address = bytes.baseAddress,
                  CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: address) == noErr else { return false }
            return kria_vpx_decode(decoder, address.assumingMemoryBound(to: UInt8.self), length) == 0
        }
        guard decoded else { throw MediaEngineError.exportFailed }
        var w: UInt32 = 0, h: UInt32 = 0
        var y: UnsafePointer<UInt8>?, u: UnsafePointer<UInt8>?, v: UnsafePointer<UInt8>?
        var ys: Int32 = 0, us: Int32 = 0, vs: Int32 = 0, fullRange: Int32 = 0
        let status = kria_vpx_frame(decoder, &w, &h, &y, &u, &v, &ys, &us, &vs, &fullRange)
        if status == 0 { return false } // VP9 invisible reference frames have no display image.
        guard status == 1, Int(w) == width, Int(h) == height, let y, let u, let v,
              let pool = adaptor.pixelBufferPool else { throw MediaEngineError.unsupportedCapability }
        var created: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &created) == kCVReturnSuccess, let pixel = created else { throw MediaEngineError.exportFailed }
        CVPixelBufferLockBaseAddress(pixel, [])
        defer { CVPixelBufferUnlockBaseAddress(pixel, []) }
        guard let outY = CVPixelBufferGetBaseAddressOfPlane(pixel, 0)?.assumingMemoryBound(to: UInt8.self),
              let outUV = CVPixelBufferGetBaseAddressOfPlane(pixel, 1)?.assumingMemoryBound(to: UInt8.self) else { throw MediaEngineError.exportFailed }
        let outYS = CVPixelBufferGetBytesPerRowOfPlane(pixel, 0), outUVS = CVPixelBufferGetBytesPerRowOfPlane(pixel, 1)
        for row in 0..<height {
            if fullRange == 0 { memcpy(outY + row * outYS, y + row * Int(ys), width); continue }
            for col in 0..<width {
                let value = Int(y[row * Int(ys) + col])
                outY[row * outYS + col] = UInt8(16 + (value * 219 + 127) / 255)
            }
        }
        for row in 0..<((height + 1) / 2) {
            for col in 0..<((width + 1) / 2) {
                let a = Int(u[row * Int(us) + col]), b = Int(v[row * Int(vs) + col])
                outUV[row * outUVS + col * 2] = UInt8(fullRange == 0 ? a : 16 + (a * 224 + 127) / 255)
                outUV[row * outUVS + col * 2 + 1] = UInt8(fullRange == 0 ? b : 16 + (b * 224 + 127) / 255)
            }
        }
        guard adaptor.append(pixel, withPresentationTime: CMSampleBufferGetPresentationTimeStamp(sample)) else { throw writer.error ?? MediaEngineError.exportFailed }
        return true
    }
}
#endif
