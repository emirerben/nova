import Foundation
#if canImport(AVFoundation)
@preconcurrency import AVFoundation

/// Confined to one export task. Explicit codec settings avoid relying on an export preset's
/// device-dependent codec choice (the public contract is H.264/AAC, not HEVC passthrough).
final class RecipeWriter: @unchecked Sendable {
    let reader: AVAssetReader
    let writer: AVAssetWriter
    let video: AVAssetReaderVideoCompositionOutput
    let videoInput: AVAssetWriterInput
    let audio: AVAssetReaderAudioMixOutput?
    let audioInput: AVAssetWriterInput
    let duration: Double

    @MainActor init(preview: PreviewComposition, outputURL: URL, bitrate: Int) async throws {
        let asset = preview.playerItem.asset
        duration = preview.description.duration
        guard duration.isFinite, duration > 0, duration <= 1800 else { throw RecipeError.invalidTimeline }
        reader = try AVAssetReader(asset: asset)
        writer = try AVAssetWriter(outputURL: outputURL, fileType: .mp4)
        video = AVAssetReaderVideoCompositionOutput(videoTracks: try await asset.loadTracks(withMediaType: .video), videoSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        video.videoComposition = preview.playerItem.videoComposition
        video.alwaysCopiesSampleData = false
        videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoColorPropertiesKey: [AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2, AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2, AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2],
            AVVideoWidthKey: preview.description.canvas.width,
            AVVideoHeightKey: preview.description.canvas.height,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: bitrate, AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel]
        ])
        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        if audioTracks.isEmpty {
            audio = nil
        } else {
            let output = AVAssetReaderAudioMixOutput(audioTracks: audioTracks, audioSettings: [
                AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2,
                AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false
            ])
            output.audioMix = preview.playerItem.audioMix
            output.alwaysCopiesSampleData = false
            audio = output
        }
        audioInput = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 2, AVEncoderBitRateKey: 128_000
        ])
        guard reader.canAdd(video), writer.canAdd(videoInput), writer.canAdd(audioInput) else { throw MediaEngineError.exportUnavailable }
        reader.add(video); writer.add(videoInput); writer.add(audioInput)
        if let audio {
            guard reader.canAdd(audio) else { throw MediaEngineError.exportUnavailable }
            reader.add(audio)
        }
        writer.shouldOptimizeForNetworkUse = true
    }

    func run(progress: (@Sendable (Double) -> Void)?) async throws {
        do {
            try Task.checkCancellation()
            guard writer.startWriting(), reader.startReading() else { throw writer.error ?? reader.error ?? MediaEngineError.exportFailed }
            writer.startSession(atSourceTime: .zero)
            var videoDone = false
            var audioDone = false
            var silenceFrame: Int64 = 0
            let silenceFrames = Int64((duration * 48_000).rounded())
            var lastProgress = -1.0
            while !videoDone || !audioDone {
                try Task.checkCancellation()
                if reader.status == .failed || writer.status == .failed { throw reader.error ?? writer.error ?? MediaEngineError.exportFailed }
                var advanced = false
                if !videoDone && videoInput.isReadyForMoreMediaData {
                    try autoreleasepool {
                        if let sample = video.copyNextSampleBuffer() {
                            guard videoInput.append(sample) else { throw writer.error ?? MediaEngineError.exportFailed }
                            let value = min(0.99, max(0, CMSampleBufferGetPresentationTimeStamp(sample).seconds / duration))
                            if value - lastProgress >= 0.01 { progress?(value); lastProgress = value }
                        } else {
                            videoDone = true; videoInput.markAsFinished()
                        }
                    }
                    advanced = true
                }
                if !audioDone, audioInput.isReadyForMoreMediaData {
                    try autoreleasepool {
                        let sample: CMSampleBuffer?
                        if let audio {
                            sample = audio.copyNextSampleBuffer()
                        } else if silenceFrame < silenceFrames {
                            let count = Int(min(1024, silenceFrames - silenceFrame))
                            sample = try Self.silence(startFrame: silenceFrame, frames: count)
                            silenceFrame += Int64(count)
                        } else { sample = nil }
                        if let sample {
                            guard audioInput.append(sample) else { throw writer.error ?? MediaEngineError.exportFailed }
                        } else {
                            audioDone = true; audioInput.markAsFinished()
                        }
                    }
                    advanced = true
                }
                if !advanced { try await Task.sleep(for: .milliseconds(2)) }
            }
            guard reader.status == .completed else { throw reader.error ?? MediaEngineError.exportFailed }
            try Task.checkCancellation()
            await writer.finishWriting()
            try Task.checkCancellation()
            guard writer.status == .completed else { throw writer.error ?? MediaEngineError.exportFailed }
        } catch {
            reader.cancelReading(); writer.cancelWriting()
            throw error
        }
    }

    /// Bounded PCM chunks: silent projects still produce the H.264/AAC contract.
    private static func silence(startFrame: Int64, frames: Int) throws -> CMSampleBuffer {
        var stream = AudioStreamBasicDescription(mSampleRate: 48_000, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
            mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
            mChannelsPerFrame: 2, mBitsPerChannel: 16, mReserved: 0)
        var format: CMAudioFormatDescription?
        guard CMAudioFormatDescriptionCreate(allocator: kCFAllocatorDefault, asbd: &stream,
            layoutSize: 0, layout: nil, magicCookieSize: 0, magicCookie: nil,
            extensions: nil, formatDescriptionOut: &format) == noErr, let format else {
            throw MediaEngineError.exportUnavailable
        }
        var block: CMBlockBuffer?
        let bytes = frames * 4
        guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil,
            blockLength: bytes, blockAllocator: kCFAllocatorDefault, customBlockSource: nil,
            offsetToData: 0, dataLength: bytes, flags: kCMBlockBufferAssureMemoryNowFlag,
            blockBufferOut: &block) == noErr, let block,
              CMBlockBufferFillDataBytes(with: 0, blockBuffer: block, offsetIntoDestination: 0, dataLength: bytes) == noErr else {
            throw MediaEngineError.exportUnavailable
        }
        var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: 48_000),
            presentationTimeStamp: CMTime(value: startFrame, timescale: 48_000), decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        var size = 4
        guard CMSampleBufferCreateReady(allocator: kCFAllocatorDefault, dataBuffer: block,
            formatDescription: format, sampleCount: frames, sampleTimingEntryCount: 1,
            sampleTimingArray: &timing, sampleSizeEntryCount: 1, sampleSizeArray: &size,
            sampleBufferOut: &sample) == noErr, let sample else { throw MediaEngineError.exportUnavailable }
        return sample
    }
}
#endif
