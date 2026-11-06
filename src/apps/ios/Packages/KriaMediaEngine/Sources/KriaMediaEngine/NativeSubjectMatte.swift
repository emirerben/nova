import Foundation
#if canImport(AVFoundation)
import CoreGraphics
import CoreImage
import CoreVideo
#if canImport(Vision)
import Vision
#endif

// Text behind people on the phone. The phone twin of the cloud's
// `app/pipeline/subject_matte.py` + `text_overlay_skia._apply_subject_mask`:
// a per-tick person mask multiplies a flagged text layer's alpha by (1 - mask),
// so the subject appears to stand in front of the text. Failure modes and the
// treatment each one gets: `docs/reviews/text-behind-subject-phone-failure-modes.md`.
//
// The mask is computed from the COMPOSITED canvas frame (video layers + motion
// scenes, before text), never from a source clip: the clip's crop, cover fit,
// transform, transition blend and camera pulse are already in those pixels, so
// the mask lives in canvas space by construction (failure mode F1).

/// One tick's person-confidence map, row-major, row 0 = top of the canvas, values 0...1.
public struct SubjectConfidenceMap: Equatable, Sendable {
    public let width: Int
    public let height: Int
    public let values: [Float]
    public init(width: Int, height: Int, values: [Float]) {
        precondition(values.count == width * height)
        self.width = width; self.height = height; self.values = values
    }

    /// A smaller map when this one has more than `maxPixels` samples (integer stride, no filtering).
    /// Keeps the treatment's connected-component pass bounded whatever size Vision answers with.
    func downsampled(maxPixels: Int) -> SubjectConfidenceMap {
        guard width * height > maxPixels, maxPixels > 0 else { return self }
        let stride = Int((Double(width * height) / Double(maxPixels)).squareRoot().rounded(.up))
        let w = max(1, width / stride), h = max(1, height / stride)
        var out = [Float](repeating: 0, count: w * h)
        for y in 0..<h { for x in 0..<w { out[y * w + x] = values[(y * stride) * width + x * stride] } }
        return SubjectConfidenceMap(width: w, height: h, values: out)
    }
}

/// The cloud's v3 mask treatment, ported as pure array math so it is testable on
/// synthetic frames without Vision: trailing temporal median -> hard cut at 0.40 ->
/// tiny-fragment drop. The feather is applied later, on the CIImage (`SubjectOcclusion`).
public enum SubjectMaskTreatment {
    public static let confidenceCut: Float = 0.4
    public static let medianWindow = 3
    /// Below this coverage nobody is in frame (F5); above it the mask swallowed the frame (F3).
    public static let minCoverage = 0.01
    public static let maxCoverage = 0.85
    /// Connected components smaller than this fraction of the frame are speckle / passers-by (F4).
    public static let tinyFragmentFraction = 0.002

    /// `history` holds the previous confidence maps (oldest first; at most `medianWindow - 1` are used).
    /// Returns a binary mask: 255 = subject, 0 = background.
    public static func treat(_ current: SubjectConfidenceMap, history: [SubjectConfidenceMap]) -> [UInt8] {
        let count = current.values.count
        let window = Array(history.suffix(medianWindow - 1).filter { $0.values.count == count }) + [current]
        var mask = [UInt8](repeating: 0, count: count)
        if window.count == 1 {
            for i in 0..<count where current.values[i] >= confidenceCut { mask[i] = 255 }
        } else {
            var samples = [Float](repeating: 0, count: window.count)
            for i in 0..<count {
                for (k, frame) in window.enumerated() { samples[k] = frame.values[i] }
                samples.sort()
                let median = window.count % 2 == 1 ? samples[window.count / 2] : (samples[window.count / 2 - 1] + samples[window.count / 2]) / 2
                if median >= confidenceCut { mask[i] = 255 }
            }
        }
        dropTinyFragments(&mask, width: current.width, height: current.height)
        return mask
    }

    public static func coverage(_ mask: [UInt8]) -> Double {
        guard !mask.isEmpty else { return 0 }
        var on = 0
        for value in mask where value != 0 { on += 1 }
        return Double(on) / Double(mask.count)
    }

    /// Intersection over union of two binary masks; 1 when both are empty (nothing moved).
    public static func iou(_ a: [UInt8], _ b: [UInt8]) -> Double {
        guard a.count == b.count else { return 0 }
        var inter = 0, union = 0
        for i in 0..<a.count {
            let x = a[i] != 0, y = b[i] != 0
            if x && y { inter += 1 }
            if x || y { union += 1 }
        }
        return union == 0 ? 1 : Double(inter) / Double(union)
    }

    /// Removes 4-connected components smaller than `tinyFragmentFraction` of the frame.
    public static func dropTinyFragments(_ mask: inout [UInt8], width: Int, height: Int) {
        guard width > 0, height > 0, mask.count == width * height else { return }
        let minimum = max(1, Int((Double(width * height) * tinyFragmentFraction).rounded(.up)))
        var visited = [Bool](repeating: false, count: mask.count)
        var stack: [Int] = []
        var component: [Int] = []
        for start in 0..<mask.count where mask[start] != 0 && !visited[start] {
            stack.removeAll(keepingCapacity: true); component.removeAll(keepingCapacity: true)
            stack.append(start); visited[start] = true
            while let index = stack.popLast() {
                component.append(index)
                let x = index % width, y = index / width
                if x > 0 { let n = index - 1; if mask[n] != 0 && !visited[n] { visited[n] = true; stack.append(n) } }
                if x < width - 1 { let n = index + 1; if mask[n] != 0 && !visited[n] { visited[n] = true; stack.append(n) } }
                if y > 0 { let n = index - width; if mask[n] != 0 && !visited[n] { visited[n] = true; stack.append(n) } }
                if y < height - 1 { let n = index + width; if mask[n] != 0 && !visited[n] { visited[n] = true; stack.append(n) } }
            }
            if component.count < minimum { for index in component { mask[index] = 0 } }
        }
    }
}

/// Streaming version of the cloud sanity gate (presence flips, large shape jumps) over a trailing
/// window. The cloud sees the whole clip before it decides; the phone decides while rendering, so a
/// verdict of "unstable" LATCHES for the rest of the cut: occlusion that blinks is worse than plain text (F2).
public struct SubjectMaskStability: Sendable {
    public static let windowSeconds = 1.5
    public static let maxPresenceFlips = 2
    public static let maxFlipsPerSecond = 0.75
    public static let largeJumpIoU = 0.5
    public static let maxLargeJumps = 3

    private var samples: [(time: Double, present: Bool, jump: Bool)] = []
    public private(set) var unstable = false
    public init() {}

    /// `iouWithPrevious` is nil for the first present frame after an absent one (no pair to compare).
    public mutating func observe(present: Bool, iouWithPrevious: Double?, at time: Double) {
        let jump = present && (iouWithPrevious.map { $0 < Self.largeJumpIoU } ?? false)
        samples.append((time, present, jump))
        samples.removeAll { time - $0.time > Self.windowSeconds }
        guard samples.count >= 2 else { return }
        var flips = 0
        for (a, b) in zip(samples, samples.dropFirst()) where a.present != b.present { flips += 1 }
        let span = max(samples.last!.time - samples.first!.time, 1.0 / 30.0)
        if flips > Self.maxPresenceFlips && Double(flips) / span > Self.maxFlipsPerSecond { unstable = true }
        if samples.filter(\.jump).count > Self.maxLargeJumps { unstable = true }
    }

    public mutating func reset() { samples.removeAll(); unstable = false }
}

/// The alpha-multiply itself, in canvas space. `mask` is a canvas-sized gray image: 1 = subject.
public enum SubjectOcclusion {
    /// Feather radius in mask pixels (the mask is ~270x480, so this is a few canvas pixels).
    public static let featherRadius = 1.0

    /// `text` is the text layer's bitmap AFTER its placement transform (canvas space).
    public static func apply(to text: CIImage, mask: CIImage) -> CIImage {
        let extent = text.extent
        guard !extent.isEmpty, !extent.isInfinite else { return text }
        // CIBlendWithMask keeps the input where the mask is white, so the keep-mask is (1 - person).
        let keep = mask.applyingFilter("CIColorInvert")
        return text.applyingFilter("CIBlendWithMask", parameters: [
            kCIInputBackgroundImageKey: CIImage(color: .clear).cropped(to: extent),
            kCIInputMaskImageKey: keep
        ]).cropped(to: extent)
    }

    /// A binary mask (row 0 = top) as a canvas-sized, feathered, color-unmanaged gray CIImage.
    /// `.colorSpace: NSNull()` keeps the 0...1 values linear instead of re-curving them into the
    /// compositor's linear working space (F11).
    public static func maskImage(_ mask: [UInt8], width: Int, height: Int, canvas: CGRect) -> CIImage? {
        guard width > 0, height > 0, mask.count == width * height,
              let provider = CGDataProvider(data: Data(mask) as CFData),
              let gray = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: width,
                                 space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                                 provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent) else { return nil }
        var image = CIImage(cgImage: gray, options: [.colorSpace: NSNull()])
        if featherRadius > 0 {
            image = image.clampedToExtent().applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: featherRadius])
                .cropped(to: CGRect(x: 0, y: 0, width: width, height: height))
        }
        return image.transformed(by: CGAffineTransform(scaleX: canvas.width / CGFloat(width), y: canvas.height / CGFloat(height)))
            .transformed(by: CGAffineTransform(translationX: canvas.minX, y: canvas.minY))
    }
}

/// Produces one person-confidence map for a composited canvas frame. The Vision implementation is
/// the production one; tests inject a synthetic one through `NativeSubjectMatte.segmenterFactory`.
protocol NativePersonSegmenting: AnyObject {
    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap?
    /// The speaker matte's full-resolution 8-bit mask (row 0 = top). Defaults to `confidence` (`SpeakerMatte.swift`).
    func personMask(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) throws -> CVPixelBuffer?
}

/// Per-composition temporal state + sanity + rate limiting. Shared by every compositor instance a
/// composition spawns (AVPlayer, the exporter and `AVAssetImageGenerator` each create their own), so
/// it is locked. Every failure path answers "no mask" and the text paints plain: the export never
/// fails because of occlusion (F13).
final class NativeSubjectMatte: @unchecked Sendable {
    /// Segmentation ticks per second; frames between ticks hold the last mask (F7).
    static let sampleRate = 15.0
    /// A time jump larger than this is a seek or a cut, never playback: history is reset (F6).
    static let continuityWindow = 0.25
    /// Longest side of the frame handed to the segmenter.
    static let inputMaxSide = 480
    static let treatmentMaxPixels = 270 * 480

    /// Test hook: replaces the Vision segmenter for every matte created afterwards.
    nonisolated(unsafe) static var segmenterFactory: () -> NativePersonSegmenting? = {
        #if canImport(Vision)
        return VisionPersonSegmenter()
        #else
        return nil
        #endif
    }

    struct Statistics: Equatable, Sendable {
        var ticks = 0
        var heldFrames = 0
        var masked = 0
        var fallbacks = 0
        var resets = 0
    }

    private let lock = NSLock()
    private let segmenter: NativePersonSegmenting?
    private var cutKey: String?
    private var lastTime: Double?
    private var lastTick: Int?
    private var held: CIImage?
    private var history: [SubjectConfidenceMap] = []
    private var previousMask: [UInt8]?
    private var stability = SubjectMaskStability()
    private(set) var statistics = Statistics()

    init(segmenter: NativePersonSegmenting? = NativeSubjectMatte.segmenterFactory()) {
        self.segmenter = segmenter
    }

    /// The canvas-space mask for this frame, or nil when the text should paint plain.
    func mask(for frame: CIImage, at time: Double, canvas: CGRect, cutKey: String) -> CIImage? {
        lock.lock(); defer { lock.unlock() }
        guard let segmenter else { return nil }
        if cutKey != self.cutKey || (lastTime.map { abs(time - $0) > Self.continuityWindow } ?? false) {
            self.cutKey = cutKey; history.removeAll(); previousMask = nil; stability.reset(); held = nil; lastTick = nil
            statistics.resets += 1
        }
        lastTime = time
        let tick = Int((time * Self.sampleRate).rounded(.down))
        if tick == lastTick { statistics.heldFrames += 1; return held }
        lastTick = tick
        statistics.ticks += 1
        held = nil
        let map: SubjectConfidenceMap
        do {
            guard let answer = try segmenter.confidence(for: frame, canvas: canvas) else { statistics.fallbacks += 1; return nil }
            map = answer.downsampled(maxPixels: Self.treatmentMaxPixels)
        } catch {
            statistics.fallbacks += 1
            return nil
        }
        let treated = SubjectMaskTreatment.treat(map, history: history)
        history.append(map)
        if history.count > SubjectMaskTreatment.medianWindow - 1 { history.removeFirst(history.count - (SubjectMaskTreatment.medianWindow - 1)) }
        let coverage = SubjectMaskTreatment.coverage(treated)
        let present = coverage >= SubjectMaskTreatment.minCoverage
        let iou = present ? previousMask.map { SubjectMaskTreatment.iou($0, treated) } : nil
        stability.observe(present: present, iouWithPrevious: iou, at: time)
        previousMask = present ? treated : nil
        guard present, coverage <= SubjectMaskTreatment.maxCoverage, !stability.unstable,
              let image = SubjectOcclusion.maskImage(treated, width: map.width, height: map.height, canvas: canvas) else {
            statistics.fallbacks += 1
            return nil
        }
        held = image
        statistics.masked += 1
        return image
    }
}

#if canImport(Vision)
/// `VNGeneratePersonSegmentationRequest` on a downscaled sRGB render of the canvas frame.
final class VisionPersonSegmenter: NativePersonSegmenting {
    private let context = CIContext(options: [.cacheIntermediates: false,
                                              .workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!,
                                              .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
    private var pool: CVPixelBufferPool?
    private var poolSize: (Int, Int) = (0, 0)

    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
        guard let buffer = render(frame, canvas: canvas, maxSide: NativeSubjectMatte.inputMaxSide),
              let mask = try segment(buffer, quality: .balanced) else { return nil }
        CVPixelBufferLockBaseAddress(mask, .readOnly); defer { CVPixelBufferUnlockBaseAddress(mask, .readOnly) }
        guard CVPixelBufferGetPixelFormatType(mask) == kCVPixelFormatType_OneComponent8,
              let base = CVPixelBufferGetBaseAddress(mask) else { return nil }
        let w = CVPixelBufferGetWidth(mask), h = CVPixelBufferGetHeight(mask), stride = CVPixelBufferGetBytesPerRow(mask)
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        var values = [Float](repeating: 0, count: w * h)
        for y in 0..<h { for x in 0..<w { values[y * w + x] = Float(bytes[y * stride + x]) / 255 } }
        return SubjectConfidenceMap(width: w, height: h, values: values)
    }

    /// Vision's buffer as is: no `[Float]` copy and no downsample (the speaker matte scales it in Core Image).
    func personMask(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) throws -> CVPixelBuffer? {
        guard let buffer = render(frame, canvas: canvas, maxSide: config.inputMaxSide) else { return nil }
        return try segment(buffer, quality: config.quality.vision)
    }

    private func render(_ frame: CIImage, canvas: CGRect, maxSide: Int) -> CVPixelBuffer? {
        let scale = Double(maxSide) / Double(max(canvas.width, canvas.height))
        let width = max(16, Int((canvas.width * scale).rounded())), height = max(16, Int((canvas.height * scale).rounded()))
        guard let buffer = makeBuffer(width: width, height: height) else { return nil }
        let scaled = frame.cropped(to: canvas)
            .transformed(by: CGAffineTransform(translationX: -canvas.minX, y: -canvas.minY))
            .transformed(by: CGAffineTransform(scaleX: CGFloat(width) / canvas.width, y: CGFloat(height) / canvas.height))
        context.render(scaled, to: buffer, bounds: CGRect(x: 0, y: 0, width: width, height: height), colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return buffer
    }

    private func segment(_ buffer: CVPixelBuffer, quality: VNGeneratePersonSegmentationRequest.QualityLevel) throws -> CVPixelBuffer? {
        let request = VNGeneratePersonSegmentationRequest()
        request.qualityLevel = quality
        request.outputPixelFormat = kCVPixelFormatType_OneComponent8
        try VNImageRequestHandler(cvPixelBuffer: buffer, options: [:]).perform([request])
        return request.results?.first?.pixelBuffer
    }

    private func makeBuffer(width: Int, height: Int) -> CVPixelBuffer? {
        if pool == nil || poolSize != (width, height) {
            var created: CVPixelBufferPool?
            let attributes: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
                kCVPixelBufferIOSurfacePropertiesKey as String: [String: String]()
            ]
            CVPixelBufferPoolCreate(nil, nil, attributes as CFDictionary, &created)
            pool = created; poolSize = (width, height)
        }
        guard let pool else { return nil }
        var buffer: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer)
        return buffer
    }
}
#endif
#endif
