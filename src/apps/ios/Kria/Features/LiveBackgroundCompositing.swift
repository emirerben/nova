import CoreImage
import CoreVideo
import ImageIO
import Metal
import UIKit
import Vision

/// KRI-1119: the live camera preview of the Talk-to-camera green screen.
///
/// Mirrors the matte settings the engine lane renders with (`KriaMediaEngine` speaker matte, KRI-1116), and the
/// numbers measured on an iPhone 13 Pro in `docs/reviews/speaker-background-failure-modes.md` (Phase 0 spike).
/// The engine's render is the source of truth. Change a value here only together with that config.
enum LiveSpeakerMatteConfig {
    /// `.balanced` on every incoming frame: 30 fps, about 25 ms per frame on the device. `.accurate` dropped the live
    /// preview to 10 fps. NEVER use it live.
    static let qualityLevel: VNGeneratePersonSegmentationRequest.QualityLevel = .balanced
    /// Vision's native output: 384x512 for `.balanced`, kept as an 8-bit mask and scaled up in Core Image.
    static let maskPixelFormat = kCVPixelFormatType_OneComponent8
    /// The short side of the frame Vision is given. It matches the mask's own short side (384), so the mask loses nothing
    /// to the smaller input. Against the mask of the full 1080x1920 frame, over 150 frames of a moving speaker on macOS, the
    /// mask from a 384-wide input overlaps 0.995 (median) and one from 216 wide 0.991 with a 5th percentile of 0.949.
    static let segmentationShortSide = 384
    /// Soft mask, never a hard cut: alpha = smoothstep(low, high, confidence).
    static let curveLow: Float = 0.35
    static let curveHigh: Float = 0.65
    /// The size the spike drew at (540x960 drawable). The layer is scaled up by the view.
    static let previewSize = CGSize(width: 540, height: 960)
}

/// The soft-edge curve, as a function and as the 256-entry lookup Core Image applies on the GPU.
enum SpeakerMatteCurve {
    static func alpha(_ confidence: Float, low: Float = LiveSpeakerMatteConfig.curveLow,
                      high: Float = LiveSpeakerMatteConfig.curveHigh) -> Float {
        let t = min(1, max(0, (confidence - low) / (high - low)))
        return t * t * (3 - 2 * t)
    }

    /// A 256x1 gray gradient: pixel `i` holds `alpha(i / 255)`. Untagged, so Core Image reads it as data.
    static func lookupImage() -> CIImage {
        var bytes = [UInt8](repeating: 255, count: 256 * 4)
        for i in 0..<256 {
            let value = UInt8((alpha(Float(i) / 255) * 255).rounded())
            bytes[i * 4] = value; bytes[i * 4 + 1] = value; bytes[i * 4 + 2] = value
        }
        return CIImage(bitmapData: Data(bytes), bytesPerRow: 256 * 4, size: CGSize(width: 256, height: 1),
                       format: .RGBA8, colorSpace: nil)
    }
}

/// The selfie rule: the plain camera preview is mirrored, so the live preview must be too, whatever the data output's
/// connection really delivers. A connection that reports `isVideoMirrored == true` already hands over mirrored pixels,
/// so flipping again would undo it. `isVideoMirrored = false` was requested at attach time but may not take effect,
/// which is why this is read from the connection at frame time.
enum LivePreviewOrientation {
    static func previewMirrored(connectionIsMirrored: Bool) -> Bool { !connectionIsMirrored }
}

/// Pure compositing: the camera frame over a background, through a soft mask. Rendering to a texture, and the
/// vertical flip that needs, belongs to the presenter. The output is upright in Core Image's y-up space.
enum LiveBackgroundCompositor {
    static let curve = SpeakerMatteCurve.lookupImage()

    /// Wraps Vision's mask without copying it. `colorSpace: NSNull()` keeps the values as data: a tagged gray image would
    /// be linearised on load and the curve would act on the wrong numbers.
    static func maskImage(_ mask: CVPixelBuffer) -> CIImage {
        CIImage(cvPixelBuffer: mask, options: [.colorSpace: NSNull()])
    }

    static func softAlpha(_ mask: CIImage) -> CIImage {
        mask.applyingFilter("CIColorMap", parameters: ["inputGradientImage": curve])
    }

    static func coverFit(_ image: CIImage, into canvas: CGSize) -> CIImage {
        let e = image.extent
        guard e.width > 0, e.height > 0 else { return image }
        let scale = max(canvas.width / e.width, canvas.height / e.height)
        let scaled = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let dx = (canvas.width - scaled.extent.width) / 2 - scaled.extent.minX
        let dy = (canvas.height - scaled.extent.height) / 2 - scaled.extent.minY
        return scaled.transformed(by: CGAffineTransform(translationX: dx, y: dy)).cropped(to: CGRect(origin: .zero, size: canvas))
    }

    /// The camera over the background. A nil mask is the raw frame alone (the S25 fallback). Mirroring applies to the
    /// camera and its mask, never to the background.
    /// `mirrored` is decided per frame from the connection that delivered it (`LivePreviewOrientation`), never assumed.
    /// Only the horizontal axis is ever flipped: Core Image's origin is bottom-left and an upright frame stays upright.
    static func compose(frame: CIImage, mask: CIImage?, background: CIImage, canvas: CGSize,
                        mirrored: Bool) -> CIImage {
        let e = frame.extent
        let scale = max(canvas.width / e.width, canvas.height / e.height)
        var fit = CGAffineTransform(scaleX: scale, y: scale)
        let fitted = CGSize(width: e.width * scale, height: e.height * scale)
        fit = fit.concatenating(CGAffineTransform(translationX: (canvas.width - fitted.width) / 2 - e.minX * scale,
                                                  y: (canvas.height - fitted.height) / 2 - e.minY * scale))
        let flip = mirrored ? CGAffineTransform(a: -1, b: 0, c: 0, d: 1, tx: canvas.width, ty: 0) : .identity
        let bounds = CGRect(origin: .zero, size: canvas)
        let camera = frame.transformed(by: fit).transformed(by: flip).cropped(to: bounds)
        guard let mask else { return camera }
        let m = mask.extent
        let maskFit = CGAffineTransform(scaleX: fitted.width / m.width, y: fitted.height / m.height)
            .concatenating(CGAffineTransform(translationX: (canvas.width - fitted.width) / 2 - m.minX * fitted.width / m.width,
                                             y: (canvas.height - fitted.height) / 2 - m.minY * fitted.height / m.height))
        let alpha = softAlpha(mask).transformed(by: maskFit).transformed(by: flip).cropped(to: bounds)
        return camera.applyingFilter("CIBlendWithRedMask", parameters: [
            kCIInputBackgroundImageKey: background, kCIInputMaskImageKey: alpha,
        ])
    }

    static func makeContext(commandQueue: (any MTLCommandQueue)? = nil) -> CIContext {
        let options: [CIContextOption: Any] = [
            .cacheIntermediates: false,
            .workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!,
            .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
        ]
        if let commandQueue { return CIContext(mtlCommandQueue: commandQueue, options: options) }
        return CIContext(options: options)
    }
}

/// Person segmentation at the live settings. A seam so a test can make the matte fail.
protocol PersonSegmenting: Sendable {
    func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer
}

/// Scales a camera frame down to `LiveSpeakerMatteConfig.segmentationShortSide` for Vision, keeping the aspect and the
/// pixel rows (no flip, no colour management: Vision reads values, not a picture). A frame that is already small enough is
/// returned as it is, and so is any frame the scaler cannot render, which only costs time. One caller at a time.
final class SegmentationInputScaler: @unchecked Sendable {
    private let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull(), .cacheIntermediates: false])
    private var pool: (size: CGSize, buffers: CVPixelBufferPool)?

    /// The size to render a `frame`-sized image at, or nil when it is not larger than the target. Even sides.
    static func targetSize(for frame: CGSize, shortSide: Int = LiveSpeakerMatteConfig.segmentationShortSide) -> CGSize? {
        let scale = CGFloat(shortSide) / min(frame.width, frame.height)
        guard scale < 1 else { return nil }
        func even(_ value: CGFloat) -> CGFloat { max(2, (value / 2).rounded() * 2) }
        return CGSize(width: even(frame.width * scale), height: even(frame.height * scale))
    }

    func scaled(_ frame: CVPixelBuffer) -> CVPixelBuffer {
        let source = CGSize(width: CVPixelBufferGetWidth(frame), height: CVPixelBufferGetHeight(frame))
        guard let size = Self.targetSize(for: source), let buffers = buffers(for: size) else { return frame }
        let aux: [CFString: Any] = [kCVPixelBufferPoolAllocationThresholdKey: 3]
        var output: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(nil, buffers, aux as CFDictionary, &output) == kCVReturnSuccess,
              let output else { return frame }
        let image = CIImage(cvPixelBuffer: frame)
            .transformed(by: CGAffineTransform(scaleX: size.width / source.width, y: size.height / source.height), highQualityDownsample: true)
        context.render(image, to: output)
        return output
    }

    private func buffers(for size: CGSize) -> CVPixelBufferPool? {
        if let pool, pool.size == size { return pool.buffers }
        let attributes: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: Int(size.width), kCVPixelBufferHeightKey: Int(size.height),
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [CFString: Any],
            kCVPixelBufferMetalCompatibilityKey: true,
        ]
        var created: CVPixelBufferPool?
        CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &created)
        pool = created.map { (size, $0) }
        return created
    }
}

/// Used only from the frame queue, one frame at a time.
final class VisionPersonSegmenter: PersonSegmenting, @unchecked Sendable {
    /// Kept only for the previous behaviour (`LiveBackgroundTuning.freshSegmentationRequest == false`).
    private let reusedRequest: VNGeneratePersonSegmentationRequest?
    /// Nil keeps handing Vision the camera's full frame (`LiveBackgroundTuning.reducedSegmentationInput == false`).
    private let scaler: SegmentationInputScaler?

    init(tuning: LiveBackgroundTuning = .current) {
        reusedRequest = tuning.freshSegmentationRequest ? nil : Self.makeRequest()
        scaler = tuning.reducedSegmentationInput ? SegmentationInputScaler() : nil
    }

    private static func makeRequest() -> VNGeneratePersonSegmentationRequest {
        let request = VNGeneratePersonSegmentationRequest()
        request.qualityLevel = LiveSpeakerMatteConfig.qualityLevel
        request.outputPixelFormat = LiveSpeakerMatteConfig.maskPixelFormat
        return request
    }

    enum SegmentationError: Error { case noMask }

    /// A request per frame, as the export does, so no state can carry a mask from an earlier frame into this one.
    func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer {
        let request = reusedRequest ?? Self.makeRequest()
        let input = scaler?.scaled(frame) ?? frame
        try VNImageRequestHandler(cvPixelBuffer: input, orientation: .up, options: [:]).perform([request])
        guard let mask = request.results?.first?.pixelBuffer else { throw SegmentationError.noMask }
        // A reused request may write its next answer into this same buffer while the preview still draws it.
        return reusedRequest == nil ? mask : try Self.copy(of: mask)
    }

    private static func copy(of mask: CVPixelBuffer) throws -> CVPixelBuffer {
        var copy: CVPixelBuffer?
        guard CVPixelBufferCreate(nil, CVPixelBufferGetWidth(mask), CVPixelBufferGetHeight(mask), CVPixelBufferGetPixelFormatType(mask),
                                  [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &copy) == kCVReturnSuccess, let copy else {
            throw SegmentationError.noMask
        }
        CVPixelBufferLockBaseAddress(mask, .readOnly); CVPixelBufferLockBaseAddress(copy, [])
        defer { CVPixelBufferUnlockBaseAddress(copy, []); CVPixelBufferUnlockBaseAddress(mask, .readOnly) }
        let rows = CVPixelBufferGetHeight(mask), source = CVPixelBufferGetBytesPerRow(mask), target = CVPixelBufferGetBytesPerRow(copy)
        guard let from = CVPixelBufferGetBaseAddress(mask), let to = CVPixelBufferGetBaseAddress(copy) else { throw SegmentationError.noMask }
        for row in 0..<rows { memcpy(to + row * target, from + row * source, min(source, target)) }
        return copy
    }
}

/// Where the live preview gets the background from, one frame at a time, already cover-fit to
/// `LiveSpeakerMatteConfig.previewSize`.
protocol LiveBackgroundProviding: Sendable {
    func frame() -> CIImage?
    /// Pauses or resumes decoding while the camera session is stopped or running again.
    func setActive(_ active: Bool)
    /// A video begins again from its first frame (source time 0) and keeps looping. A photo ignores it. Called at the moment
    /// a recording really starts, so what the creator sees while filming is how the edit plays it.
    func restart()
    /// Final: releases the player and its observers.
    func stop()
}

/// A photo, decoded and downscaled once. HEIC and EXIF orientation are handled by ImageIO.
final class StillBackground: LiveBackgroundProviding, @unchecked Sendable {
    private let image: CIImage

    init(url: URL) throws {
        let canvas = LiveSpeakerMatteConfig.previewSize
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: Int(max(canvas.width, canvas.height)),
        ]
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            throw CocoaError(.fileReadCorruptFile)
        }
        image = LiveBackgroundCompositor.coverFit(CIImage(cgImage: cgImage), into: canvas)
    }

    func frame() -> CIImage? { image }
    func setActive(_ active: Bool) {}
    func restart() {}
    func stop() {}
}
