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

/// Draws the creator in front of the chosen background, on the camera's frame queue, into a `CAMetalLayer`.
/// The preview is a convenience: it never touches the recording, and any failure shows the raw frame instead (S25).
final class LiveBackgroundPreview: NSObject, LiveFrameConsumer, @unchecked Sendable {
    let layer = CAMetalLayer()
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
        let device = MTLCreateSystemDefaultDevice()
        queue = device?.makeCommandQueue()
        context = LiveBackgroundCompositor.makeContext(commandQueue: queue)
        super.init()
        let size = LiveSpeakerMatteConfig.previewSize
        layer.device = device
        layer.pixelFormat = .bgra8Unorm
        layer.framebufferOnly = false
        layer.drawableSize = size
        layer.maximumDrawableCount = 3
        layer.contentsGravity = .resizeAspect
    }

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
        let image = LiveBackgroundCompositor.compose(
            frame: CIImage(cvPixelBuffer: frame),
            mask: matte.map(LiveBackgroundCompositor.maskImage),
            background: background ?? CIImage(color: .black).cropped(to: CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize)),
            canvas: LiveSpeakerMatteConfig.previewSize, mirrored: mirrored)
        lock.lock()
        if matte != nil { stats.framesMatted += 1 } else { stats.framesRaw += 1 }
        lock.unlock()
        present(image)
    }

    private func present(_ image: CIImage) {
        guard let queue, let drawable = layer.nextDrawable(), let commands = queue.makeCommandBuffer() else { return }
        let size = LiveSpeakerMatteConfig.previewSize
        // Core Image's origin is bottom-left and a texture's is top-left.
        let upright = image.transformed(by: CGAffineTransform(a: 1, b: 0, c: 0, d: -1, tx: 0, ty: size.height))
        context.render(upright, to: drawable.texture, commandBuffer: commands, bounds: CGRect(origin: .zero, size: size),
                       colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        commands.present(drawable)
        commands.commit()
    }
}

/// Replaces `TeleprompterCameraPreview` while a background is active. Front-camera mirroring is baked into the
/// composite per frame, decided from the delivering connection (preview only); the recorded movie is never touched.
struct LiveBackgroundPreviewView: UIViewRepresentable {
    let preview: LiveBackgroundPreview

    func makeUIView(context: Context) -> HostView { HostView(layer: preview.layer) }
    func updateUIView(_ uiView: HostView, context: Context) {}

    final class HostView: UIView {
        private let metalLayer: CAMetalLayer
        init(layer metalLayer: CAMetalLayer) {
            self.metalLayer = metalLayer
            super.init(frame: .zero)
            backgroundColor = .black
            layer.addSublayer(metalLayer)
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
        override func layoutSubviews() {
            super.layoutSubviews()
            CATransaction.begin(); CATransaction.setDisableActions(true)
            metalLayer.frame = bounds
            CATransaction.commit()
        }
    }
}
