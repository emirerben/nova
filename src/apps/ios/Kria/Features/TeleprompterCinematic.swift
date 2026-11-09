@preconcurrency import AVFoundation
#if canImport(Cinematic)
import Cinematic
#endif
import Metal
import OSLog
import UIKit
import VideoToolbox

/// KRI-1160: native Cinematic recording in the teleprompter.
///
/// Apple's Cinematic Video capture API (iOS 26) records an ordinary-looking movie plus a disparity (depth) track and a
/// metadata track. Only Apple's own apps draw the blur from those tracks: every other player, our upload, and the
/// on-device render show the sharp source. So a Cinematic take is "baked" right after it is recorded (Cinematic
/// framework's `CNRenderingSession`, the focus the camera chose while filming) into an ordinary movie that every later
/// step already handles. Standard takes never pass through any of this.
enum TeleprompterCinematic {
    enum Availability: Equatable, Sendable {
        case available
        case needsNewerIOS
        case unsupportedCamera

        /// Nil when available. Always says the take stays standard, so the fallback is never a surprise.
        var creatorMessage: String? {
            switch self {
            case .available: nil
            case .needsNewerIOS: "Cinematic needs iOS 26 or later. Your takes record in standard mode."
            case .unsupportedCamera: "This iPhone’s front camera can’t record Cinematic. Your takes record in standard mode."
            }
        }
    }

    static let preferenceKey = "teleprompter.cinematic.enabled"

    static func availability() -> Availability {
        guard #available(iOS 26.0, *) else { return .needsNewerIOS }
        return frontCinematicCamera() != nil ? .available : .unsupportedCamera
    }

    /// The first front camera with a Cinematic format, and the format to record with: 1080p first (the standard path
    /// records 1080p), then 30 fps, then 8-bit SDR (what the rest of the pipeline expects).
    @available(iOS 26.0, *)
    nonisolated static func frontCinematicCamera() -> (device: AVCaptureDevice, format: AVCaptureDevice.Format)? {
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInTrueDepthCamera, .builtInWideAngleCamera, .builtInUltraWideCamera],
            mediaType: .video, position: .front)
        for device in discovery.devices {
            let formats = device.formats.filter(\.isCinematicVideoCaptureSupported)
            if let format = formats.min(by: { rank($0) < rank($1) }) { return (device, format) }
        }
        return nil
    }

    @available(iOS 26.0, *)
    private nonisolated static func rank(_ format: AVCaptureDevice.Format) -> Int {
        let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
        let is1080p = dimensions.width == 1_920 && dimensions.height == 1_080
        let range = format.videoFrameRateRangeForCinematicVideo
        let has30 = range.map { $0.minFrameRate <= 30 && $0.maxFrameRate >= 30 } ?? false
        let subtype = CMFormatDescriptionGetMediaSubType(format.formatDescription)
        let eightBit = subtype == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange || subtype == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
        // Lower is better; resolution outranks frame rate, which outranks bit depth.
        return (is1080p ? 0 : 4) + (has30 ? 0 : 2) + (eightBit ? 0 : 1)
    }

    /// Bit rate of the baked movie (`CinematicBake`).
    static let bakedBitsPerSecond = 16_000_000

    /// Free space a bake needs before it starts: the baked movie's size, half as much again, and slack for the container.
    static func bytesNeededToBake(seconds: Double) -> Int64 {
        Int64((max(seconds, 0) * Double(bakedBitsPerSecond) / 8 * 1.5).rounded(.up)) + 50_000_000
    }

    /// Turns Cinematic on for an input already in a committed (not yet running) session. Returns whether it is on.
    /// Every property is checked before it is set: AVFoundation raises an uncatchable exception otherwise.
    @available(iOS 26.0, *)
    nonisolated static func enable(on input: AVCaptureDeviceInput, format: AVCaptureDevice.Format) -> Bool {
        let device = input.device
        guard device.formats.contains(format), (try? device.lockForConfiguration()) != nil else { return false }
        device.activeFormat = format
        device.unlockForConfiguration()
        guard input.isCinematicVideoCaptureSupported else { return false }
        input.isCinematicVideoCaptureEnabled = true
        if let range = format.videoFrameRateRangeForCinematicVideo, range.minFrameRate <= 30, range.maxFrameRate >= 30,
           (try? device.lockForConfiguration()) != nil {
            let duration = CMTime(value: 1, timescale: 30)
            device.activeVideoMinFrameDuration = duration
            device.activeVideoMaxFrameDuration = duration
            device.unlockForConfiguration()
        }
        return input.isCinematicVideoCaptureEnabled
    }
}

/// How one bake ended. The take's persisted reading of it is `CinematicBakeState`.
enum CinematicTakeOutcome: String, Codable, Sendable {
    /// The Cinematic look is baked into the movie.
    case applied
    /// The look could not be baked; the take stays standard.
    case standard
}

#if canImport(Cinematic)
/// Replaces a just-recorded Cinematic movie, in place, with an ordinary movie that has the Cinematic look baked in.
enum CinematicTakeFinisher {
    private static let log = Logger(subsystem: "com.kria.app", category: "teleprompter")

    static func isCinematic(movieAt url: URL) async -> Bool {
        await CNAssetInfo.isCinematic(asset: AVURLAsset(url: url))
    }

    /// Nil: the movie is not Cinematic and was not touched. Otherwise the movie at `url` was replaced by a verified
    /// ordinary movie (`.applied`), or by the sharp source alone when baking failed (`.standard`). The original stays
    /// in place until a verified replacement exists, so a failure here never loses the take.
    ///
    /// Throws `CancellationError`, with the movie untouched, when the task is cancelled or the system takes the GPU away
    /// (the app left the foreground). Neither is a failed bake: the caller starts over later, so neither falls back to
    /// the plain copy.
    static func finish(movieAt url: URL, progress: (@Sendable (Double) -> Void)? = nil) async throws -> CinematicTakeOutcome? {
        let asset = AVURLAsset(url: url)
        guard await CNAssetInfo.isCinematic(asset: asset) else { return nil }
        let staging = CinematicStaging.shared.reserve()
        defer { CinematicStaging.shared.release(staging) }
        do {
            let info = try await CNAssetInfo(asset: asset)
            let length = try await asset.load(.duration).seconds
            guard hasRoomToBake(seconds: length, in: staging.deletingLastPathComponent()) else { throw CinematicBakeError.notEnoughStorage }
            try await CinematicBake(asset: asset, info: info).run(to: staging, progress: progress)
            try Task.checkCancellation()
            try await verify(staging, against: asset)
            _ = try FileManager.default.replaceItemAt(url, withItemAt: staging)
            return .applied
        } catch is CancellationError {
            throw CancellationError()
        } catch CinematicBakeError.interrupted {
            throw CancellationError()
        } catch {
            if await shouldPause(after: error) { throw CancellationError() }
            log.error("cinematic bake failed: \(error.localizedDescription, privacy: .public)")
        }
        try Task.checkCancellation()
        do {
            try? FileManager.default.removeItem(at: staging)
            try await standardCopy(of: asset, to: staging)
            try await verify(staging, against: asset)
            _ = try FileManager.default.replaceItemAt(url, withItemAt: staging)
        } catch is CancellationError {
            // A cancelled export throws this too; the movie on disk is still the raw one, so it must not be called standard.
            throw CancellationError()
        } catch {
            if await shouldPause(after: error) { throw CancellationError() }
            // The raw Cinematic movie stays: its first video track is the sharp, playable source.
            log.error("cinematic standard copy failed: \(error.localizedDescription, privacy: .public)")
        }
        try Task.checkCancellation()
        return .standard
    }

    /// Read after the failure, on the main actor: `.inactive` counts as away too, since the failure can land during the
    /// slide to the background, before the scene phase change cancels this task.
    private static func shouldPause(after error: Error) async -> Bool {
        let active = await MainActor.run { UIApplication.shared.applicationState == .active }
        return CinematicBakeInterruption.shouldPause(after: error, taskCancelled: Task.isCancelled, appActive: active)
    }

    private static func hasRoomToBake(seconds: Double, in directory: URL) -> Bool {
        guard let free = try? directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
            .volumeAvailableCapacityForImportantUsage else { return true }
        return free >= TeleprompterCinematic.bytesNeededToBake(seconds: seconds)
    }

    /// The sharp source video and the audio, without the depth and metadata tracks.
    private static func standardCopy(of asset: AVURLAsset, to output: URL) async throws {
        let info = try await CNAssetInfo(asset: asset)
        let composition = AVMutableComposition()
        let duration = try await asset.load(.duration)
        let range = CMTimeRange(start: .zero, duration: duration)
        let source = info.cinematicVideoTrack
        guard let video = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid) else {
            throw CinematicBakeError.couldNotWrite
        }
        try video.insertTimeRange(try await source.load(.timeRange), of: source, at: .zero)
        video.preferredTransform = try await source.load(.preferredTransform)
        for track in try await asset.loadTracks(withMediaType: .audio) {
            let audio = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
            try audio?.insertTimeRange(range.intersection(try await track.load(.timeRange)), of: track, at: .zero)
        }
        guard let export = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough) else {
            throw CinematicBakeError.couldNotWrite
        }
        export.metadata = try await CinematicBake.ordinaryMetadata(of: asset)
        try await export.export(to: output, as: .mov)
    }

    /// One video track, every audio track, and the source's length. Anything else and the original is kept.
    private static func verify(_ output: URL, against source: AVURLAsset) async throws {
        let result = AVURLAsset(url: output)
        guard try await result.load(.isPlayable), !(await CNAssetInfo.isCinematic(asset: result)) else {
            throw CinematicBakeError.verificationFailed("not an ordinary playable movie")
        }
        let videos = try await result.loadTracks(withMediaType: .video)
        let audios = try await result.loadTracks(withMediaType: .audio)
        let sourceAudios = try await source.loadTracks(withMediaType: .audio)
        guard videos.count == 1, audios.count == sourceAudios.count else {
            throw CinematicBakeError.verificationFailed("\(videos.count) video / \(audios.count) audio tracks")
        }
        let length = try await result.load(.duration).seconds
        let sourceLength = try await source.load(.duration).seconds
        guard length.isFinite, abs(length - sourceLength) < 0.25 else {
            throw CinematicBakeError.verificationFailed("length \(length) s vs \(sourceLength) s")
        }
    }
}

enum CinematicBakeError: LocalizedError {
    case noGPU
    case couldNotRead(String)
    case couldNotWrite
    case renderFailed(String)
    case verificationFailed(String)
    case notEnoughStorage
    /// The system refused GPU work because the app left the foreground.
    case interrupted

    var errorDescription: String? {
        switch self {
        case .noGPU: "no Metal device"
        case let .couldNotRead(detail): "could not read the Cinematic tracks: \(detail)"
        case .couldNotWrite: "could not write the movie"
        case let .renderFailed(detail): "render failed: \(detail)"
        case let .verificationFailed(detail): "verification failed: \(detail)"
        case .notEnoughStorage: "not enough free storage to bake"
        case .interrupted: "the system stopped GPU work"
        }
    }
}

/// One bake: reads the source, disparity and Cinematic metadata in step, renders each frame with the focus the
/// camera chose, and writes an ordinary HEVC movie with the source's orientation and audio. Runs off the main actor;
/// AVFoundation's pull callbacks drive it, so its state is confined to their serial queues.
final class CinematicBake: @unchecked Sendable {
    private let asset: AVURLAsset
    private let info: CNAssetInfo
    private let videoQueue = DispatchQueue(label: "app.kria.cinematic-bake.video")
    private let audioQueue = DispatchQueue(label: "app.kria.cinematic-bake.audio")

    init(asset: AVURLAsset, info: CNAssetInfo) {
        self.asset = asset
        self.info = info
    }

    func run(to output: URL, progress: (@Sendable (Double) -> Void)? = nil) async throws {
        try Task.checkCancellation()
        let sessionAttributes = try await CNRenderingSession.Attributes(asset: asset)
        // The script carries the focus decisions the camera made while filming; without it the metadata's own
        // per-frame focus is used, which is what the live preview showed.
        let script = try? await CNScript(asset: asset)
        guard let device = MTLCreateSystemDefaultDevice(), let commandQueue = device.makeCommandQueue() else { throw CinematicBakeError.noGPU }
        // Identity: frames are rendered in the source's own orientation and the writer carries its transform, so no
        // pixel is rotated here.
        let session = CNRenderingSession(commandQueue: commandQueue, sessionAttributes: sessionAttributes,
                                         preferredTransform: .identity, quality: .export)

        let sourceFormat = Self.pick(CNRenderingSession.sourcePixelFormatTypes)
        let destinationFormat = Self.pick(CNRenderingSession.destinationPixelFormatTypes)
        let size = info.naturalSize
        let reader = try AVAssetReader(asset: asset)
        let videoOutput = AVAssetReaderTrackOutput(track: info.cinematicVideoTrack, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: sourceFormat,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
            kCVPixelBufferMetalCompatibilityKey as String: true,
        ])
        let disparityOutput = AVAssetReaderTrackOutput(track: info.cinematicDisparityTrack, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_DisparityFloat16,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
            kCVPixelBufferMetalCompatibilityKey as String: true,
        ])
        let metadataOutput = AVAssetReaderTrackOutput(track: info.cinematicMetadataTrack, outputSettings: nil)
        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        let audioOutputs = audioTracks.map { AVAssetReaderTrackOutput(track: $0, outputSettings: nil) }
        for output in [videoOutput, disparityOutput, metadataOutput] + audioOutputs {
            output.alwaysCopiesSampleData = false
            guard reader.canAdd(output) else { throw CinematicBakeError.couldNotRead("reader rejected an output") }
            reader.add(output)
        }

        let writer = try AVAssetWriter(outputURL: output, fileType: .mov)
        writer.metadata = try await Self.ordinaryMetadata(of: asset)
        var compression: [String: Any] = [AVVideoAverageBitRateKey: TeleprompterCinematic.bakedBitsPerSecond, AVVideoExpectedSourceFrameRateKey: 30]
        if Self.isTenBit(destinationFormat) { compression[AVVideoProfileLevelKey] = kVTProfileLevel_HEVC_Main10_AutoLevel }
        var videoSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.hevc,
            AVVideoWidthKey: Int(size.width), AVVideoHeightKey: Int(size.height),
            AVVideoCompressionPropertiesKey: compression,
        ]
        if let color = try await Self.colorProperties(of: info.cinematicVideoTrack) { videoSettings[AVVideoColorPropertiesKey] = color }
        let videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        videoInput.expectsMediaDataInRealTime = false
        videoInput.transform = info.preferredTransform
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: videoInput, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: destinationFormat,
            kCVPixelBufferWidthKey as String: Int(size.width), kCVPixelBufferHeightKey as String: Int(size.height),
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
            kCVPixelBufferMetalCompatibilityKey as String: true,
        ])
        guard writer.canAdd(videoInput) else { throw CinematicBakeError.couldNotWrite }
        writer.add(videoInput)
        var audioInputs: [AVAssetWriterInput] = []
        for track in audioTracks {
            let hint = try await track.load(.formatDescriptions).first
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: nil, sourceFormatHint: hint)
            input.expectsMediaDataInRealTime = false
            guard writer.canAdd(input) else { throw CinematicBakeError.couldNotWrite }
            writer.add(input)
            audioInputs.append(input)
        }

        guard reader.startReading() else { throw CinematicBakeError.couldNotRead(reader.error?.localizedDescription ?? "unknown") }
        guard writer.startWriting() else { throw CinematicBakeError.couldNotWrite }
        writer.startSession(atSourceTime: .zero)

        let frames = FrameRenderer(session: session, script: script, sessionAttributes: sessionAttributes,
                                   disparity: SampleAligner(output: disparityOutput) { CMSampleBufferGetImageBuffer($0) },
                                   metadata: SampleAligner(output: metadataOutput) {
                                       CNRenderingSession.FrameAttributes(sampleBuffer: $0, sessionAttributes: sessionAttributes)
                                   })
        let length = try await asset.load(.duration).seconds
        let cancelled = BakeCancel()
        let report = ProgressThrottle(total: length, send: progress)
        let failure: BakeFailure.Reason? = await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                let group = DispatchGroup()
                let state = BakeFailure()
                group.enter()
                pump(videoInput, on: videoQueue, group: group) {
                    // Cancelling finishes the inputs the ordinary way; the writer is cancelled once every pump has left.
                    if cancelled.isSet { return .finished }
                    guard let sample = videoOutput.copyNextSampleBuffer() else { return .finished }
                    guard let image = CMSampleBufferGetImageBuffer(sample) else { return .skipped }
                    let time = CMSampleBufferGetPresentationTimeStamp(sample)
                    guard let pool = adaptor.pixelBufferPool else { state.set("no destination pool"); return .failed }
                    switch frames.render(image, at: time, pool: pool) {
                    case let .success(buffer):
                        guard adaptor.append(buffer, withPresentationTime: time) else { state.set("append video"); return .failed }
                        report.frame(at: time)
                        return .appended
                    case let .failure(failure):
                        state.set(failure.reason, leftForeground: failure.leftForeground); return .failed
                    }
                }
                for (input, output) in zip(audioInputs, audioOutputs) {
                    group.enter()
                    pump(input, on: audioQueue, group: group) {
                        if cancelled.isSet { return .finished }
                        guard let sample = output.copyNextSampleBuffer() else { return .finished }
                        return input.append(sample) ? .appended : .failed
                    }
                }
                group.notify(queue: videoQueue) { continuation.resume(returning: state.value) }
            }
        } onCancel: {
            cancelled.set()
        }
        if cancelled.isSet {
            reader.cancelReading()
            writer.cancelWriting()
            throw CancellationError()
        }
        if let failure {
            reader.cancelReading()
            writer.cancelWriting()
            if failure.leftForeground { throw CinematicBakeError.interrupted }
            throw CinematicBakeError.renderFailed(failure.text)
        }
        if reader.status == .failed {
            writer.cancelWriting()
            if CinematicBakeInterruption.isSystemInterruption(reader.error) { throw CinematicBakeError.interrupted }
            throw CinematicBakeError.couldNotRead(reader.error?.localizedDescription ?? "unknown")
        }
        await writer.finishWriting()
        if writer.status != .completed, CinematicBakeInterruption.isSystemInterruption(writer.error) { throw CinematicBakeError.interrupted }
        guard writer.status == .completed else { throw CinematicBakeError.renderFailed(writer.error?.localizedDescription ?? "writer") }
    }

    fileprivate enum Step { case appended, skipped, finished, failed }

    /// Pulls samples while the input wants them. Finished or failed: marks the input finished and leaves the group once.
    private func pump(_ input: AVAssetWriterInput, on queue: DispatchQueue, group: DispatchGroup, next: @escaping () -> Step) {
        let done = BakeFlag()
        let source = StepSource(next)
        input.requestMediaDataWhenReady(on: queue) {
            guard !done.value else { return }
            while input.isReadyForMoreMediaData {
                switch source.next() {
                case .appended, .skipped: continue
                case .finished, .failed:
                    done.value = true
                    input.markAsFinished()
                    group.leave()
                    return
                }
            }
        }
    }

    /// Movie-level metadata without the Cinematic intent, so Photos and other apps treat the result as ordinary.
    static func ordinaryMetadata(of asset: AVAsset) async throws -> [AVMetadataItem] {
        try await asset.load(.metadata).filter { item in
            !(item.identifier?.rawValue.localizedCaseInsensitiveContains("cinematic") ?? false)
        }
    }

    /// 8-bit 4:2:0 when offered (the capture format is chosen 8-bit); otherwise whatever Cinematic supports first.
    private static func pick(_ formats: [OSType]) -> OSType {
        let preferred: [OSType] = [kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, kCVPixelFormatType_420YpCbCr8BiPlanarFullRange]
        return preferred.first(where: formats.contains)
            ?? formats.first { $0 != kCVPixelFormatType_DisparityFloat16 && $0 != kCVPixelFormatType_DisparityFloat32 }
            ?? kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
    }

    private static func isTenBit(_ format: OSType) -> Bool {
        format == kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange || format == kCVPixelFormatType_420YpCbCr10BiPlanarFullRange
    }

    /// The source's own colour tags, so the baked movie looks like what was filmed.
    private static func colorProperties(of track: AVAssetTrack) async throws -> [String: String]? {
        guard let description = try await track.load(.formatDescriptions).first,
              let primaries = CMFormatDescriptionGetExtension(description, extensionKey: kCMFormatDescriptionExtension_ColorPrimaries) as? String,
              let transfer = CMFormatDescriptionGetExtension(description, extensionKey: kCMFormatDescriptionExtension_TransferFunction) as? String,
              let matrix = CMFormatDescriptionGetExtension(description, extensionKey: kCMFormatDescriptionExtension_YCbCrMatrix) as? String
        else { return nil }
        return [AVVideoColorPrimariesKey: primaries, AVVideoTransferFunctionKey: transfer, AVVideoYCbCrMatrixKey: matrix]
    }
}

/// Renders one Cinematic frame. Touched only from the video pump's serial queue.
private final class FrameRenderer: @unchecked Sendable {
    private let session: CNRenderingSession
    private let script: CNScript?
    private let disparity: SampleAligner<CVPixelBuffer>
    private let metadata: SampleAligner<CNRenderingSession.FrameAttributes>

    init(session: CNRenderingSession, script: CNScript?, sessionAttributes: CNRenderingSession.Attributes,
         disparity: SampleAligner<CVPixelBuffer>, metadata: SampleAligner<CNRenderingSession.FrameAttributes>) {
        self.session = session
        self.script = script
        self.disparity = disparity
        self.metadata = metadata
    }

    func render(_ image: CVPixelBuffer, at time: CMTime, pool: CVPixelBufferPool) -> Result<CVPixelBuffer, FrameFailure> {
        guard let depth = disparity.value(at: time) else { return .failure(FrameFailure("no disparity at \(time.seconds) s")) }
        guard var attributes = metadata.value(at: time) else { return .failure(FrameFailure("no metadata at \(time.seconds) s")) }
        if let script {
            if let frame = script.frame(at: time, tolerance: CMTime(value: 1, timescale: 60)) { attributes.focusDisparity = frame.focusDisparity }
            attributes.fNumber = script.fNumber
        }
        var destination: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &destination) == kCVReturnSuccess, let destination,
              let commands = session.commandQueue.makeCommandBuffer() else { return .failure(FrameFailure("no destination buffer")) }
        guard session.encodeRender(to: commands, frameAttributes: attributes, sourceImage: image, sourceDisparity: depth,
                                   destinationImage: destination) else { return .failure(FrameFailure("encodeRender refused \(time.seconds) s")) }
        commands.commit()
        commands.waitUntilCompleted()
        if let error = commands.error {
            // Metal refuses GPU work once the app has left the foreground. That is the app being suspended, not a bad take.
            let denied = (error as NSError).domain == MTLCommandBufferErrorDomain
                && (error as NSError).code == MTLCommandBufferError.Code.notPermitted.rawValue
            return .failure(FrameFailure(error.localizedDescription, leftForeground: denied))
        }
        return .success(destination)
    }
}

private struct FrameFailure: Error {
    let reason: String
    let leftForeground: Bool
    init(_ reason: String, leftForeground: Bool = false) {
        self.reason = reason
        self.leftForeground = leftForeground
    }
}

/// Follows a side track (disparity, metadata) alongside the video: the latest sample at or before a video frame's time,
/// or the first one when the side track starts a little late. Samples that do not convert are skipped.
private final class SampleAligner<Value>: @unchecked Sendable {
    private let output: AVAssetReaderOutput
    private let convert: (CMSampleBuffer) -> Value?
    private var current: (time: CMTime, value: Value)?
    private var next: (time: CMTime, value: Value)?
    private var started = false
    private static var tolerance: CMTime { CMTime(value: 1, timescale: 1_000) }

    init(output: AVAssetReaderOutput, convert: @escaping (CMSampleBuffer) -> Value?) {
        self.output = output
        self.convert = convert
    }

    func value(at time: CMTime) -> Value? {
        if !started {
            started = true
            current = pull()
            next = pull()
        }
        let limit = CMTimeAdd(time, Self.tolerance)
        while let candidate = next, CMTimeCompare(candidate.time, limit) <= 0 {
            current = candidate
            next = pull()
        }
        return current?.value
    }

    private func pull() -> (time: CMTime, value: Value)? {
        while let sample = output.copyNextSampleBuffer() {
            if let value = convert(sample) { return (CMSampleBufferGetPresentationTimeStamp(sample), value) }
        }
        return nil
    }
}

private final class BakeFailure: @unchecked Sendable {
    struct Reason: Sendable {
        let text: String
        let leftForeground: Bool
    }

    private let lock = NSLock()
    private var reason: Reason?
    var value: Reason? { lock.lock(); defer { lock.unlock() }; return reason }
    func set(_ text: String, leftForeground: Bool = false) {
        lock.lock(); if reason == nil { reason = Reason(text: text, leftForeground: leftForeground) }; lock.unlock()
    }
}

/// Set from the task's cancellation handler (any thread), read by both pumps.
private final class BakeCancel: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return flag }
    func set() { lock.lock(); flag = true; lock.unlock() }
}

/// Reports the fraction of the source rendered, at most once per percent. Only called from the video pump's queue.
private final class ProgressThrottle: @unchecked Sendable {
    private let total: Double
    private let send: (@Sendable (Double) -> Void)?
    private var last = -1.0

    init(total: Double, send: (@Sendable (Double) -> Void)?) {
        self.total = total
        self.send = send
    }

    func frame(at time: CMTime) {
        guard let send, total.isFinite, total > 0 else { return }
        let fraction = min(1, max(0, time.seconds / total))
        guard fraction - last >= 0.01 else { return }
        last = fraction
        send(fraction)
    }
}

/// Only touched on its input's serial queue.
private final class BakeFlag: @unchecked Sendable {
    var value = false
}

/// A pump's sample source; called only on its input's serial queue.
private final class StepSource: @unchecked Sendable {
    let next: () -> CinematicBake.Step
    init(_ next: @escaping () -> CinematicBake.Step) { self.next = next }
}
#else
/// The simulator SDK has no Cinematic framework, and no simulator camera records Cinematic: nothing to bake.
enum CinematicTakeFinisher {
    static func isCinematic(movieAt url: URL) async -> Bool { false }
    static func finish(movieAt url: URL, progress: (@Sendable (Double) -> Void)? = nil) async throws -> CinematicTakeOutcome? { nil }
}
#endif
