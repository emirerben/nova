@preconcurrency import AVFoundation
import CoreImage
import Metal
import QuartzCore
import SwiftUI
import UIKit

/// Receives raw camera frames from `TeleprompterCamera`'s video data output, on a queue of its own.
protocol LiveFrameConsumer: AnyObject, Sendable {
    /// `mirrored`: the preview must flip this frame to look like the selfie-mirrored camera preview.
    func consume(_ frame: CVPixelBuffer, mirrored: Bool)
    /// `capturedAt`: the frame's presentation time on the host clock (`CACurrentMediaTime` scale), nil when unknown.
    func consume(_ frame: CVPixelBuffer, mirrored: Bool, capturedAt: Double?)
    /// The capture session stopped or its graph was rebuilt. Drop anything that depends on the last frame.
    func sessionDidStop()
}

extension LiveFrameConsumer {
    func consume(_ frame: CVPixelBuffer, mirrored: Bool, capturedAt: Double?) { consume(frame, mirrored: mirrored) }
}

/// What picking the background frame reads from one item's video output. `AVPlayerItemVideoOutput` in the app; a test
/// scripts the looper's copies on its own clock with it.
protocol LoopedVideoOutput: AnyObject {
    func itemTime(forHostTime hostTimeInSeconds: CFTimeInterval) -> CMTime
    func hasNewPixelBuffer(forItemTime itemTime: CMTime) -> Bool
    func copyPixelBuffer(forItemTime itemTime: CMTime, itemTimeForDisplay outItemTimeForDisplay: UnsafeMutablePointer<CMTime>?) -> CVPixelBuffer?
}

extension AVPlayerItemVideoOutput: LoopedVideoOutput {}

/// Picks the frame to draw from the items a video background plays, at a given host time. Kept apart from the player so
/// the loop's timing can be driven frame by frame in a test instead of sampled from a live player (KRI-1379).
struct LoopedFramePicker {
    let orientation: CGImagePropertyOrientation
    private(set) var latest: CIImage?
    /// The size of the last decoded frame.
    private(set) var decoded: CGSize?

    init(orientation: CGImagePropertyOrientation) { self.orientation = orientation }

    /// Only the item on screen is read: the looper preloads the next copy, and its output reports that copy's frame 0 as
    /// new while this pass is still playing, which drew the first frame early and left a hole at the real loop point.
    /// Until the item on screen has a new frame, the last one stays.
    mutating func frame(onScreen current: AnyObject?, outputs: [(item: AnyObject, output: any LoopedVideoOutput)], hostTime: CFTimeInterval) -> CIImage? {
        guard let current, let output = outputs.first(where: { $0.item === current })?.output else { return latest }
        let time = output.itemTime(forHostTime: hostTime)
        if output.hasNewPixelBuffer(forItemTime: time), let buffer = output.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) {
            decoded = CGSize(width: CVPixelBufferGetWidth(buffer), height: CVPixelBufferGetHeight(buffer))
            latest = LiveBackgroundCompositor.coverFit(CIImage(cvPixelBuffer: buffer).oriented(orientation), into: LiveSpeakerMatteConfig.previewSize)
        }
        return latest
    }
}

/// A looping, muted video as the background. Decoded by `AVPlayerItemVideoOutput`, pulled one frame per camera frame.
final class LoopingVideoBackground: LiveBackgroundProviding, @unchecked Sendable {
    private let player: AVPlayer
    private let outputAttributes: [String: Any]
    private let lock = NSLock()
    private var picker: LoopedFramePicker
    /// The old way to loop (`loopedVideoBackground == false`).
    private var endObserver: NSObjectProtocol?
    /// The new way: `AVPlayerLooper` queues the next pass before this one ends, so there is no seek and no gap.
    private var looper: AVPlayerLooper?
    private var looperObservation: NSKeyValueObservation?
    /// One per item that can play: the looper rotates through a fixed few copies, and an output only sees the item it is on, so
    /// each copy has its own, attached before it starts and not when it becomes current (which lost its first frames).
    private var outputs: [(item: AnyObject, output: any LoopedVideoOutput)] = []

    enum VideoError: Error { case noVideoTrack, zeroDuration }

    /// Builds a video-only composition so no audio can ever reach the session, whatever the source holds.
    @MainActor init(url: URL, tuning: LiveBackgroundTuning = .current) async throws {
        let asset = AVURLAsset(url: url)
        guard let track = try await asset.loadTracks(withMediaType: .video).first else { throw VideoError.noVideoTrack }
        let duration = try await asset.load(.duration)
        guard duration.seconds.isFinite, duration.seconds > 0 else { throw VideoError.zeroDuration }
        let composition = AVMutableComposition()
        let picture = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
        try picture?.insertTimeRange(CMTimeRange(start: .zero, duration: duration), of: track, at: .zero)
        let orientation = Self.orientation(of: try await track.load(.preferredTransform))
        picker = LoopedFramePicker(orientation: orientation)
        var attributes: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        if tuning.drawnSizeVideoDecode,
           let size = Self.decodeSize(natural: try await track.load(.naturalSize), orientation: orientation) {
            attributes[kCVPixelBufferWidthKey as String] = Int(size.width)
            attributes[kCVPixelBufferHeightKey as String] = Int(size.height)
        }
        outputAttributes = attributes
        let item = AVPlayerItem(asset: composition)
        if tuning.loopedVideoBackground {
            let queue = AVQueuePlayer()
            let looper = AVPlayerLooper(player: queue, templateItem: item)
            self.looper = looper
            player = queue
            looperObservation = looper.observe(\.status, options: [.initial, .new]) { [weak self] looper, _ in
                if looper.status == .ready { self?.attachOutputs(to: looper.loopingPlayerItems) }
            }
        } else {
            let output = AVPlayerItemVideoOutput(pixelBufferAttributes: attributes)
            outputs = [(item, output)]
            item.add(output)
            player = AVPlayer(playerItem: item)
            player.actionAtItemEnd = .none
            endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak player] _ in
                player?.seek(to: .zero)
                player?.play()
            }
        }
        player.isMuted = true
        player.play()
    }

    deinit { stop() }

    private func attachOutputs(to items: [AVPlayerItem]) {
        lock.lock(); defer { lock.unlock() }
        guard outputs.isEmpty else { return }
        outputs = items.map { item in
            let output = AVPlayerItemVideoOutput(pixelBufferAttributes: outputAttributes)
            item.add(output)
            return (item, output)
        }
    }

    func frame() -> CIImage? {
        lock.lock(); defer { lock.unlock() }
        return picker.frame(onScreen: player.currentItem, outputs: outputs, hostTime: CACurrentMediaTime())
    }

    /// How many looper items have an output waiting on them, and how many items the looper plays. For tests.
    var outputCount: Int { lock.lock(); defer { lock.unlock() }; return outputs.count }
    var loopedItemCount: Int { looper?.loopingPlayerItems.count ?? 0 }

    /// The size of the last decoded frame. For tests.
    var decodedSize: CGSize? { lock.lock(); defer { lock.unlock() }; return picker.decoded }

    /// The size to decode a track of `natural` size at so the preview's cover fit draws it at 1:1, or nil when the track
    /// is already no larger. Decoding at the size that is drawn spares the decoder, the copy and the Core Image read from a
    /// full-resolution BGRA frame 30 times a second.
    static func decodeSize(natural: CGSize, orientation: CGImagePropertyOrientation) -> CGSize? {
        let turned = [.left, .right, .leftMirrored, .rightMirrored].contains(orientation)
        let shown = turned ? CGSize(width: natural.height, height: natural.width) : natural
        let canvas = LiveSpeakerMatteConfig.previewSize
        let scale = max(canvas.width / shown.width, canvas.height / shown.height)
        guard scale.isFinite, scale > 0, scale < 1 else { return nil }
        func even(_ value: CGFloat) -> CGFloat { max(2, (value / 2).rounded(.up) * 2) }
        return CGSize(width: even(natural.width * scale), height: even(natural.height * scale))
    }

    func setActive(_ active: Bool) {
        if active { player.play() } else { player.pause() }
    }

    /// Seeks to the very first frame and plays on, looping as before. Zero tolerance, so the first frame is frame 0 and not
    /// the nearest keyframe. Until the seek lands the previous frame stays on screen.
    func restart() {
        player.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero) { [weak player] _ in player?.play() }
    }

    /// Where the loop is, in seconds. For the loop test only.
    var playbackSeconds: Double { player.currentTime().seconds }

    func stop() {
        looper?.disableLooping()
        looperObservation?.invalidate(); looperObservation = nil
        player.pause()
        if let endObserver { NotificationCenter.default.removeObserver(endObserver); self.endObserver = nil }
    }

    /// The common 90-degree cases of a track's `preferredTransform`, as an image orientation.
    static func orientation(of transform: CGAffineTransform) -> CGImagePropertyOrientation {
        switch (transform.a, transform.b, transform.c, transform.d) {
        case (0, 1, -1, 0): .right
        case (0, -1, 1, 0): .left
        case (-1, 0, 0, -1): .down
        default: .up
        }
    }
}

/// Counts what the preview did with each frame, for tests and for the device check.
struct LiveBackgroundStatistics: Equatable, Sendable {
    var framesConsumed = 0
    var framesMatted = 0
    /// Frames drawn without a matte because Vision threw or no background was ready (S25).
    var framesRaw = 0
    /// Frames not drawn at all because the mask for them is still being computed (the screen keeps its last picture).
    var framesAwaitingMask = 0
    /// How many frames older than the picture drawn last the mask under it was (0 when matting is on the display path).
    var lastMaskOffsetFrames: Int?
}

/// Draws the creator in front of the chosen background, on the camera's frame queue, into pooled IOSurface-backed pixel
/// buffers shown as a layer's contents.
///
/// Why pixel buffers and not a `CAMetalLayer` drawable (device finding 2026-10-31): the first version flipped the image
/// vertically before `CIContext.render(_:to: MTLTexture...)`, because Core Image's origin is bottom-left. The simulator
/// (macOS Core Image) read back upright with that flip, but the iPhone 13 Pro showed the whole composite upside down, so
/// the two platforms disagree about the texture's origin. A pixel buffer has no such ambiguity: Core Image writes the top
/// of the image to row 0 on both, exactly as `CIImage(cvPixelBuffer:)` reads it back. So no flip is applied anywhere and
/// the readback tests below pin the same rows the screen shows.
/// The preview is a convenience: it never touches the recording, and any failure shows the raw frame instead (S25).
final class LiveBackgroundPreview: NSObject, LiveFrameConsumer, @unchecked Sendable {
    let layer = CALayer()
    private let segmenter: any PersonSegmenting
    private let queue: (any MTLCommandQueue)?
    private let context: CIContext
    private let lock = NSLock()
    private var provider: (any LiveBackgroundProviding)?
    private var providerActive = true
    private var stats = LiveBackgroundStatistics()
    private let tuning: LiveBackgroundTuning
    private let segmentationQueue = DispatchQueue(label: "app.kria.live-background.segmentation", qos: .userInitiated)
    private var frameIndex = 0
    /// One segmentation at a time. A frame that arrives while one runs is not queued behind it: the first frame after it
    /// finishes is the one segmented, so no mask is computed from a frame that was already old when it started.
    private var segmenting = false
    /// Bumped when the session stops, so a mask still being computed for the old session is thrown away.
    private var generation = 0
    private var newestMask: SegmentedMask?
    private var segmentationFailed = false

    private struct SegmentedMask {
        let buffer: CVPixelBuffer
        let frameIndex: Int
        let capturedAt: Double?
        let frameSize: CGSize
        /// The orientation of the pixels it was computed from. The composite mirrors camera and mask together, so a mask
        /// only fits frames delivered the same way.
        let mirrored: Bool
    }

    /// The oldest mask worn over a newer picture, in seconds: two frames at 30 fps and a little jitter. The offset is never
    /// below one frame (the mask comes from the frame before), and past two a moving hand is visibly out of its outline.
    private static let maxMaskOffset = 0.075

    /// False where there is no Metal device: the screen then keeps the plain camera preview.
    var isAvailable: Bool { queue != nil }

    init(segmenter: any PersonSegmenting = VisionPersonSegmenter(), tuning: LiveBackgroundTuning = .current) {
        self.segmenter = segmenter
        self.tuning = tuning
        queue = MTLCreateSystemDefaultDevice()?.makeCommandQueue()
        context = LiveBackgroundCompositor.makeContext(commandQueue: queue)
        let size = LiveSpeakerMatteConfig.previewSize
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: Int(size.width), kCVPixelBufferHeightKey: Int(size.height),
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
            kCVPixelBufferMetalCompatibilityKey: true,
        ]
        var pool: CVPixelBufferPool?
        CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &pool)
        self.pool = pool
        super.init()
        layer.contentsGravity = .resizeAspect
        layer.isOpaque = true
    }

    private let pool: CVPixelBufferPool?
    /// The last few frames, kept alive so a surface the layer may still be showing is never recycled underneath it.
    private var recent: [CVPixelBuffer] = []
    private static let ringSize = 3

    var statistics: LiveBackgroundStatistics { lock.lock(); defer { lock.unlock() }; return stats }

    func setBackground(_ background: (any LiveBackgroundProviding)?) {
        lock.lock()
        let old = provider
        provider = background
        providerActive = true
        lock.unlock()
        old?.stop()
    }

    /// Matting stops with the session, so a video background stops decoding too. The first frame of the next session
    /// resumes it.
    /// The recording just started: a video background begins again from its first frame.
    func restartBackground() {
        lock.lock(); let current = provider; providerActive = true; lock.unlock()
        current?.restart()
    }

    func sessionDidStop() {
        lock.lock()
        providerActive = false
        generation += 1
        newestMask = nil
        segmentationFailed = false
        let current = provider
        lock.unlock()
        current?.setActive(false)
    }

    func consume(_ frame: CVPixelBuffer, mirrored: Bool) { consume(frame, mirrored: mirrored, capturedAt: nil) }

    func consume(_ frame: CVPixelBuffer, mirrored: Bool, capturedAt: Double?) {
        Perf.measure("talk.live.consume") {
            lock.lock()
            stats.framesConsumed += 1
            frameIndex += 1
            let index = frameIndex
            let resume = !providerActive ? provider : nil
            providerActive = true
            lock.unlock()
            resume?.setActive(true)
            lock.lock(); let background = provider?.frame(); lock.unlock()
            let matte: Matte
            if background == nil {
                matte = .raw
            } else if tuning.asyncSegmentation {
                matte = newestMatte(for: frame, index: index, mirrored: mirrored, capturedAt: capturedAt)
            } else {
                let buffer = Perf.measure("talk.live.segment") { try? segmenter.mask(for: frame) }
                matte = buffer.map { .mask($0, offsetFrames: 0, offsetMilliseconds: 0) } ?? .raw
            }
            Perf.measure("talk.live.render") {
                if case .awaiting = matte {
                    lock.lock(); stats.framesAwaitingMask += 1; lock.unlock()
                    return
                }
                var mask: CVPixelBuffer?
                if case let .mask(buffer, _, _) = matte { mask = buffer }
                let image = composite(frame: frame, matte: mask, background: background, mirrored: mirrored)
                lock.lock()
                if case let .mask(_, frames, _) = matte { stats.framesMatted += 1; stats.lastMaskOffsetFrames = frames } else { stats.framesRaw += 1 }
                lock.unlock()
                present(image, capturedAt: capturedAt)
                if case let .mask(_, frames, milliseconds) = matte {
                    Perf.sample("talk.live.maskOffset", milliseconds: milliseconds, "frames=\(frames)")
                }
            }
        }
    }

    private enum Matte {
        case mask(CVPixelBuffer, offsetFrames: Int, offsetMilliseconds: Double)
        /// No mask: the camera frame alone, because there is no background yet or segmentation failed (S25).
        case raw
        /// The mask in flight is not ready and the one before it is too old to wear: hold the last picture.
        case awaiting
    }

    /// The camera frame is drawn at once with the newest mask that fits it when that mask is at most `maxMaskOffset` older,
    /// and this frame is segmented in the background when nothing else is. A mask any older would be a visible ghost (the
    /// leading edge of a hand cut off, the trailing edge showing the room), so then the frame is segmented before it is
    /// drawn, as the previous path did, unless a segmentation is already running: that one is waited for by holding the picture.
    private func newestMatte(for frame: CVPixelBuffer, index: Int, mirrored: Bool, capturedAt: Double?) -> Matte {
        let size = CGSize(width: CVPixelBufferGetWidth(frame), height: CVPixelBufferGetHeight(frame))
        lock.lock()
        let busy = segmenting, startedIn = generation, failed = segmentationFailed
        var usable: (mask: SegmentedMask, frames: Int, milliseconds: Double)?
        if let newest = newestMask, newest.frameSize == size, newest.mirrored == mirrored {
            let frames = index - newest.frameIndex
            let milliseconds = capturedAt.flatMap { now in newest.capturedAt.map { max(0, (now - $0) * 1000) } } ?? Double(frames) * 1000 / 30
            if milliseconds / 1000 <= Self.maxMaskOffset { usable = (newest, frames, milliseconds) }
        }
        if !busy { segmenting = true }
        lock.unlock()
        if let usable {
            if !busy { segmentationQueue.async { [self] in _ = segment(frame, index: index, size: size, mirrored: mirrored, capturedAt: capturedAt, startedIn: startedIn) } }
            return .mask(usable.mask.buffer, offsetFrames: usable.frames, offsetMilliseconds: usable.milliseconds)
        }
        if busy { return failed ? .raw : .awaiting }
        return segmentationQueue.sync { segment(frame, index: index, size: size, mirrored: mirrored, capturedAt: capturedAt, startedIn: startedIn) }.map { .mask($0.buffer, offsetFrames: 0, offsetMilliseconds: 0) } ?? .raw
    }

    /// Segments `frame` on the calling thread and keeps the mask for later frames unless the session stopped meanwhile.
    private func segment(_ frame: CVPixelBuffer, index: Int, size: CGSize, mirrored: Bool, capturedAt: Double?, startedIn: Int) -> SegmentedMask? {
        let buffer = Perf.measure("talk.live.segment") { try? segmenter.mask(for: frame) }
        let fresh = buffer.map { SegmentedMask(buffer: $0, frameIndex: index, capturedAt: capturedAt, frameSize: size, mirrored: mirrored) }
        lock.lock()
        segmenting = false
        if generation == startedIn { newestMask = fresh; segmentationFailed = fresh == nil }
        lock.unlock()
        return fresh
    }

    /// Blocks until the segmentation in flight, if any, has finished. For tests.
    func waitForSegmentation() { segmentationQueue.sync {} }

    /// The composite exactly as `consume` draws it, in Core Image space. Split out so a test can send it through the same
    /// render step as the screen.
    func composite(frame: CVPixelBuffer, matte: CVPixelBuffer?, background: CIImage?, mirrored: Bool) -> CIImage {
        LiveBackgroundCompositor.compose(
            frame: CIImage(cvPixelBuffer: frame),
            mask: matte.map(LiveBackgroundCompositor.maskImage),
            background: background ?? CIImage(color: .black).cropped(to: CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize)),
            canvas: LiveSpeakerMatteConfig.previewSize, mirrored: mirrored)
    }

    /// The one place an image becomes pixel rows: Core Image's top row goes to row 0 of the buffer, which is the top row the
    /// layer shows. No flip.
    func render(_ image: CIImage) -> CVPixelBuffer? {
        guard let pool else { return nil }
        let aux: [CFString: Any] = [kCVPixelBufferPoolAllocationThresholdKey: Self.ringSize + 1]
        var buffer: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(nil, pool, aux as CFDictionary, &buffer) == kCVReturnSuccess,
              let buffer else { return nil }
        context.render(image, to: buffer, bounds: CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize),
                       colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return buffer
    }

    private func present(_ image: CIImage, capturedAt: Double?) {
        guard let buffer = render(image), let surface = CVPixelBufferGetIOSurface(buffer)?.takeUnretainedValue() else { return }
        lock.lock()
        recent.append(buffer)
        if recent.count > Self.ringSize { recent.removeFirst(recent.count - Self.ringSize) }
        lock.unlock()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        layer.contents = surface
        CATransaction.commit()
        Perf.count("talk.live.presented")
        // Stops at the layer-contents hand-off, so the compositor and the panel add to what the creator actually sees.
        if let capturedAt { Perf.sample("talk.live.captureToLayer", milliseconds: (CACurrentMediaTime() - capturedAt) * 1000) }
    }
}

/// Replaces `TeleprompterCameraPreview` while a background is active. Front-camera mirroring is baked into the
/// composite per frame, decided from the delivering connection (preview only); the recorded movie is never touched.
struct LiveBackgroundPreviewView: UIViewRepresentable {
    let preview: LiveBackgroundPreview

    func makeUIView(context: Context) -> HostView { HostView(layer: preview.layer) }
    func updateUIView(_ uiView: HostView, context: Context) {}

    final class HostView: UIView {
        private let contentLayer: CALayer
        init(layer contentLayer: CALayer) {
            self.contentLayer = contentLayer
            super.init(frame: .zero)
            backgroundColor = .black
            layer.addSublayer(contentLayer)
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
        override func layoutSubviews() {
            super.layoutSubviews()
            CATransaction.begin(); CATransaction.setDisableActions(true)
            contentLayer.frame = bounds
            CATransaction.commit()
        }
    }
}
