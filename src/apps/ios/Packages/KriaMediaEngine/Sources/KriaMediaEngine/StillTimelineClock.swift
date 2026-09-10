import Foundation
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import ObjectiveC

/// AVFoundation needs a video track to drive a custom compositor for a pure
/// photo timeline. A single local black frame supplies its clock; the recipe
/// compositor still draws every visible pixel at the requested output time.
final class StillTimelineClock: NSObject, @unchecked Sendable {
    let url: URL
    private init(url: URL) { self.url = url }
    deinit { try? FileManager.default.removeItem(at: url) }

    @MainActor static func make() async throws -> StillTimelineClock {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("kria-still-clock-\(UUID().uuidString).mp4")
        let clock = StillTimelineClock(url: url)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 16, AVVideoHeightKey: 16
        ])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: 16, kCVPixelBufferHeightKey as String: 16
        ])
        guard writer.canAdd(input) else { throw MediaEngineError.exportUnavailable }
        writer.add(input)
        do {
            guard writer.startWriting() else { throw writer.error ?? MediaEngineError.exportFailed }
            writer.startSession(atSourceTime: .zero)
            while !input.isReadyForMoreMediaData {
                if writer.status == .failed { throw writer.error ?? MediaEngineError.exportFailed }
                try await Task.sleep(for: .milliseconds(2))
            }
            try Task.checkCancellation()
            guard let pool = adaptor.pixelBufferPool else { throw MediaEngineError.exportFailed }
            var optionalBuffer: CVPixelBuffer?
            guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &optionalBuffer) == kCVReturnSuccess,
                  let buffer = optionalBuffer else { throw MediaEngineError.exportFailed }
            CVPixelBufferLockBaseAddress(buffer, [])
            guard let address = CVPixelBufferGetBaseAddress(buffer) else {
                CVPixelBufferUnlockBaseAddress(buffer, [])
                throw MediaEngineError.exportFailed
            }
            address.initializeMemory(as: UInt8.self, repeating: 0, count: CVPixelBufferGetDataSize(buffer))
            CVPixelBufferUnlockBaseAddress(buffer, [])
            guard adaptor.append(buffer, withPresentationTime: .zero) else { throw writer.error ?? MediaEngineError.exportFailed }
            writer.endSession(atSourceTime: CMTime(value: 1, timescale: 30))
            input.markAsFinished()
            await writer.finishWriting()
            try Task.checkCancellation()
            guard writer.status == .completed else { throw writer.error ?? MediaEngineError.exportFailed }
            return clock
        } catch {
            writer.cancelWriting()
            throw error
        }
    }
}

@MainActor enum StillClockLifetime {
    private static var key: UInt8 = 0
    static func retain(_ clock: StillTimelineClock, on object: AnyObject) {
        objc_setAssociatedObject(object, &key, clock, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    }
}
#endif
