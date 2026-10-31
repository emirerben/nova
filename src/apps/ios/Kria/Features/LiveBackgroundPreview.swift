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
    /// The capture session stopped or its graph was rebuilt. Drop anything that depends on the last frame.
    func sessionDidStop()
}

/// A looping, muted video as the background. Decoded by `AVPlayerItemVideoOutput`, pulled one frame per camera frame.
final class LoopingVideoBackground: LiveBackgroundProviding, @unchecked Sendable {
    private let player: AVPlayer
    private let output: AVPlayerItemVideoOutput
    private let orientation: CGImagePropertyOrientation
    private let lock = NSLock()
    private var latest: CIImage?
    private var endObserver: NSObjectProtocol?

    enum VideoError: Error { case noVideoTrack, zeroDuration }

    /// Builds a video-only composition so no audio can ever reach the session, whatever the source holds.
    @MainActor init(url: URL) async throws {
        let asset = AVURLAsset(url: url)
        guard let track = try await asset.loadTracks(withMediaType: .video).first else { throw VideoError.noVideoTrack }
        let duration = try await asset.load(.duration)
        guard duration.seconds.isFinite, duration.seconds > 0 else { throw VideoError.zeroDuration }
        let composition = AVMutableComposition()
        let picture = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
        try picture?.insertTimeRange(CMTimeRange(start: .zero, duration: duration), of: track, at: .zero)
        orientation = Self.orientation(of: try await track.load(.preferredTransform))
        output = AVPlayerItemVideoOutput(pixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        let item = AVPlayerItem(asset: composition)
        item.add(output)
        player = AVPlayer(playerItem: item)
        player.isMuted = true
        player.actionAtItemEnd = .none
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak player] _ in
            player?.seek(to: .zero)
            player?.play()
        }
        player.play()
    }

    deinit { stop() }

    func frame() -> CIImage? {
        let time = output.itemTime(forHostTime: CACurrentMediaTime())
        lock.lock(); defer { lock.unlock() }
        if output.hasNewPixelBuffer(forItemTime: time), let buffer = output.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) {
            latest = LiveBackgroundCompositor.coverFit(CIImage(cvPixelBuffer: buffer).oriented(orientation), into: LiveSpeakerMatteConfig.previewSize)
        }
        return latest
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

    /// False where there is no Metal device: the screen then keeps the plain camera preview.
    var isAvailable: Bool { queue != nil }

    init(segmenter: any PersonSegmenting = VisionPersonSegmenter()) {
        self.segmenter = segmenter
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
        lock.lock(); providerActive = false; let current = provider; lock.unlock()
        current?.setActive(false)
    }

    func consume(_ frame: CVPixelBuffer, mirrored: Bool) {
        lock.lock()
        stats.framesConsumed += 1
        let resume = !providerActive ? provider : nil
        providerActive = true
        lock.unlock()
        resume?.setActive(true)
        lock.lock(); let background = provider?.frame(); lock.unlock()
        var matte: CVPixelBuffer?
        if background != nil {
            do { matte = try segmenter.mask(for: frame) } catch { matte = nil }
        }
        let image = composite(frame: frame, matte: matte, background: background, mirrored: mirrored)
        lock.lock()
        if matte != nil { stats.framesMatted += 1 } else { stats.framesRaw += 1 }
        lock.unlock()
        present(image)
    }

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

    private func present(_ image: CIImage) {
        guard let buffer = render(image), let surface = CVPixelBufferGetIOSurface(buffer)?.takeUnretainedValue() else { return }
        lock.lock()
        recent.append(buffer)
        if recent.count > Self.ringSize { recent.removeFirst(recent.count - Self.ringSize) }
        lock.unlock()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        layer.contents = surface
        CATransaction.commit()
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
