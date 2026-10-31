import Foundation
#if canImport(AVFoundation)
import CoreGraphics
import CoreImage
import CoreVideo
#if canImport(Vision)
import Vision
#endif

// Talk-to-camera green screen (KRI-1116): the speaker layer, cut out by a soft per-frame person
// mask, over a picture or looping video. A different contract from the text matte
// (`NativeSubjectMatte`): it segments the speaker layer alone, keeps the soft confidence, and on a
// failed frame holds the last good answer instead of giving up. Failure modes and their treatments:
// `docs/reviews/speaker-background-failure-modes.md`.

/// Every tunable of the speaker matte; a device measurement changes one line here. iPhone 13 Pro, 60 s
/// export (KRI-1116): `.balanced` every frame 52 s, `.accurate` every frame 171 s (over the 120 s budget),
/// `.accurate` at 15 Hz 91 s. `.accurate` and a lower `maskRate` stay selectable.
struct SpeakerMatteConfig: Equatable, Sendable {
    enum Quality: Sendable { case accurate, balanced, fast }
    /// `.balanced` answers a 384x512 mask whatever the input size, `.accurate` a 1512x2016 one. Either is
    /// drawn at its native size, scaled in Core Image, never stride-downsampled.
    var quality: Quality = .balanced
    /// Longest side of the speaker layer handed to the segmenter. A larger input changes neither
    /// Vision's mask nor its time, only the cost of rendering the input.
    var inputMaxSide = 960
    /// `smoothstep(edgeLow, edgeHigh)` maps confidence to alpha: crisp but anti-aliased (S1). The curve, not
    /// the mask resolution, is what removes the halo; 0.35...0.65 on `.balanced` came closest to `.accurate`
    /// in the device montage.
    var edgeLow: Float = 0.35
    var edgeHigh: Float = 0.65
    /// Segmentations per second; nil segments every output frame. 15 Hz with a hold visibly steps
    /// and a mask cross-fade ghosts a fast hand, so the shipping value is every frame (S2).
    var maskRate: Double? = nil
    /// A failed or unreliable frame keeps the last good answer this long, then renders without the
    /// background (S3, S5).
    var holdGrace = 0.5
    /// A larger time jump is a seek, never playback, and drops the held answer (S7).
    var continuityWindow = 0.25
    /// Under this person coverage nobody is in frame (S4); over the maximum the mask swallowed the frame (S5).
    var minCoverage = 0.01
    var maxCoverage = 0.9
    /// Coverage and fragments are read from a stride-sampled copy of at most this many pixels.
    /// The mask that is drawn is never downsampled.
    var gateMaxPixels = 270 * 480

    static let shipping = SpeakerMatteConfig()
}

/// One per composition, shared by every compositor instance it spawns (player, exporter, image
/// generator), so it is locked. Temporal state is keyed by the active speaker clip(s).
final class SpeakerMatte: @unchecked Sendable {
    enum Answer {
        /// The background, then the speaker multiplied by this canvas-space mask (1 = speaker).
        case cutout(CIImage)
        /// Nobody in frame: the background alone (S4).
        case backgroundOnly
        /// No reliable mask: the frame renders as if the edit had no background (S3).
        case original
    }

    /// Test hook: replaces the Vision segmenter for every matte created afterwards.
    nonisolated(unsafe) static var segmenterFactory: () -> NativePersonSegmenting? = {
        #if canImport(Vision)
        return VisionPersonSegmenter()
        #else
        return nil
        #endif
    }

    /// Test hook: the config every matte created afterwards uses. Production changes `SpeakerMatteConfig.shipping`.
    nonisolated(unsafe) static var defaultConfig = SpeakerMatteConfig.shipping

    let config: SpeakerMatteConfig
    private let lock = NSLock()
    private let segmenter: NativePersonSegmenting?
    private var clipKey: String?
    private var lastTime: Double?
    private var tick: (index: Int, answer: Answer)?
    private var lastGood: (answer: Answer, time: Double)?

    init(segmenter: NativePersonSegmenting? = SpeakerMatte.segmenterFactory(), config: SpeakerMatteConfig = SpeakerMatte.defaultConfig) {
        self.segmenter = segmenter
        self.config = config
    }

    /// `speaker` is the speaker layer alone, after its transform, over a transparent canvas.
    func answer(for speaker: CIImage, at time: Double, canvas: CGRect, clipKey: String) -> Answer {
        lock.lock(); defer { lock.unlock() }
        // A cut to another speaker clip or a seek must never inherit the previous silhouette (S7).
        if clipKey != self.clipKey || (lastTime.map { abs(time - $0) > config.continuityWindow } ?? false) {
            self.clipKey = clipKey; lastGood = nil; tick = nil
        }
        lastTime = time
        let index = config.maskRate.map { Int((time * $0 + 0.001).rounded(.down)) }
        if let index, let tick, tick.index == index { return tick.answer }
        let answer = segment(speaker, at: time, canvas: canvas)
        if let index { tick = (index, answer) }
        return answer
    }

    private func segment(_ speaker: CIImage, at time: Double, canvas: CGRect) -> Answer {
        if let buffer = try? segmenter?.personMask(for: speaker, canvas: canvas, config: config),
           let reading = SpeakerMaskReading(buffer, config: config) {
            if reading.coverage < config.minCoverage {
                lastGood = (.backgroundOnly, time)
                return .backgroundOnly
            }
            if reading.coverage <= config.maxCoverage {
                let answer = Answer.cutout(reading.mask(canvas: canvas))
                lastGood = (answer, time)
                return answer
            }
        }
        if let lastGood, abs(time - lastGood.time) <= config.holdGrace { return lastGood.answer }
        return .original
    }

    private static let kernel: CIColorKernel? = try? CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        [[stitchable]] float4 kriaSpeakerCutout(sample_t speaker, sample_t mask, float low, float high) {
            return speaker * smoothstep(low, high, mask.r);
        }
        }}
        """).first as? CIColorKernel

    /// The speaker layer with its alpha multiplied by the mask through the edge curve. nil only if the
    /// kernel failed to build; the caller then draws the speaker unmasked, which is the original look.
    static func cutOut(_ speaker: CIImage, mask: CIImage, canvas: CGRect, config: SpeakerMatteConfig) -> CIImage? {
        kernel?.apply(extent: canvas, arguments: [speaker, mask, config.edgeLow, config.edgeHigh])
    }
}

/// The checks on one segmenter answer. Coverage and tiny fragments come from a stride-sampled copy,
/// while the drawn mask stays the segmenter's full-resolution buffer: the text matte's 192x256 copy
/// is what made its edges blocky.
struct SpeakerMaskReading {
    let coverage: Double
    private let buffer: CVPixelBuffer
    private let step: Int
    /// 0 over dropped fragments (dilated one cell), 255 elsewhere; nil when nothing was dropped (S6).
    private let keep: (values: [UInt8], width: Int, height: Int)?

    init?(_ buffer: CVPixelBuffer, config: SpeakerMatteConfig) {
        guard CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_OneComponent8 else { return nil }
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer)?.assumingMemoryBound(to: UInt8.self) else { return nil }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer), rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        guard width > 0, height > 0 else { return nil }
        let step = max(1, Int((Double(width * height) / Double(max(1, config.gateMaxPixels))).squareRoot().rounded(.up)))
        let w = max(1, width / step), h = max(1, height / step)
        let threshold = UInt8(((config.edgeLow + config.edgeHigh) / 2 * 255).rounded())
        var cells = [UInt8](repeating: 0, count: w * h)
        for y in 0..<h {
            let row = base + y * step * rowBytes
            for x in 0..<w where row[x * step] >= threshold { cells[y * w + x] = 255 }
        }
        var kept = cells
        SubjectMaskTreatment.dropTinyFragments(&kept, width: w, height: h)
        if kept == cells {
            keep = nil
        } else {
            var values = [UInt8](repeating: 255, count: w * h)
            for index in cells.indices where cells[index] != 0 && kept[index] == 0 {
                let x = index % w, y = index / w
                for ny in max(0, y - 1)...min(h - 1, y + 1) {
                    for nx in max(0, x - 1)...min(w - 1, x + 1) where kept[ny * w + nx] == 0 { values[ny * w + nx] = 0 }
                }
            }
            keep = (values, w, h)
        }
        coverage = SubjectMaskTreatment.coverage(kept)
        self.buffer = buffer
        self.step = step
    }

    /// The mask in canvas space, color-unmanaged so 0.5 stays 0.5 through the linear working space.
    /// Clamped before scaling so the canvas border keeps the mask's edge value instead of fading to 0.
    func mask(canvas: CGRect) -> CIImage {
        let width = CGFloat(CVPixelBufferGetWidth(buffer)), height = CGFloat(CVPixelBufferGetHeight(buffer))
        var mask = CIImage(cvPixelBuffer: buffer, options: [.colorSpace: NSNull()]).clampedToExtent()
            .transformed(by: CGAffineTransform(scaleX: canvas.width / width, y: canvas.height / height))
        if let keep, let provider = CGDataProvider(data: Data(keep.values) as CFData),
           let gray = CGImage(width: keep.width, height: keep.height, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: keep.width,
                              space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                              provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent) {
            let cell = CGFloat(step)
            let keepImage = CIImage(cgImage: gray, options: [.colorSpace: NSNull()]).clampedToExtent()
                .transformed(by: CGAffineTransform(scaleX: canvas.width * cell / width, y: canvas.height * cell / height))
            mask = mask.applyingFilter("CIMultiplyCompositing", parameters: [kCIInputBackgroundImageKey: keepImage])
        }
        return mask.transformed(by: CGAffineTransform(translationX: canvas.minX, y: canvas.minY)).cropped(to: canvas)
    }
}

extension NativePersonSegmenting {
    /// A synthetic segmenter answers a confidence map; the speaker matte reads it as the same 8-bit,
    /// row-0-on-top buffer Vision answers, so tests drive the production mask path.
    func personMask(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) throws -> CVPixelBuffer? {
        try confidence(for: frame, canvas: canvas)?.pixelBuffer()
    }
}

extension SubjectConfidenceMap {
    func pixelBuffer() -> CVPixelBuffer? {
        var created: CVPixelBuffer?
        let attributes: [String: Any] = [kCVPixelBufferIOSurfacePropertiesKey as String: [String: String]()]
        guard CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_OneComponent8, attributes as CFDictionary, &created) == kCVReturnSuccess,
              let buffer = created else { return nil }
        CVPixelBufferLockBaseAddress(buffer, []); defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let base = CVPixelBufferGetBaseAddress(buffer)?.assumingMemoryBound(to: UInt8.self) else { return nil }
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        for y in 0..<height {
            for x in 0..<width { base[y * rowBytes + x] = UInt8((min(1, max(0, values[y * width + x])) * 255).rounded()) }
        }
        return buffer
    }
}

#if canImport(Vision)
extension SpeakerMatteConfig.Quality {
    var vision: VNGeneratePersonSegmentationRequest.QualityLevel {
        switch self {
        case .accurate: .accurate
        case .balanced: .balanced
        case .fast: .fast
        }
    }
}
#endif
#endif
