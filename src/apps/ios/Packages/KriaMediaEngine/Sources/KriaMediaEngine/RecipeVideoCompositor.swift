import Foundation
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
import CoreText
import ImageIO

struct RecipeVideoLayer: @unchecked Sendable {
    let trackID: CMPersistentTrackID?
    let image: CIImage?
    let transform: CGAffineTransform
    let start: Double
    let end: Double
    let fadeIn: Double
}

struct RecipeTextLayer: @unchecked Sendable {
    let image: CIImage
    let frame: CGRect
    let start: Double
    let end: Double
    let animation: TextAnimation
    var portable: PortableTextLayer? = nil
    var portableAnchor: CGPoint = .zero
    var handwriting: NativeHandwritingPainter? = nil
    var discreteReveal: NativeDiscreteRevealPainter? = nil
    var smoothReveal: NativeSmoothRevealPainter? = nil
    var staggered: NativeStaggeredPainter? = nil
    var dissolve: NativeDissolveRenderer? = nil
    var karaoke: NativeKaraokePainter? = nil
    var giantTitle: NativeGiantTitlePainter? = nil

    static func make(_ text: TextTreatment, start: Double, end: Double, canvas: CGSize) throws -> Self {
        guard let font = CGFont(text.fontName as CFString) else { throw MediaEngineError.unsupportedCapability }
        let ctFont = CTFontCreateWithGraphicsFont(font, text.fontSize, nil, nil)
        var alignment = CTTextAlignment.center
        let paragraph = withUnsafePointer(to: &alignment) { pointer in
            var setting = CTParagraphStyleSetting(spec: .alignment, valueSize: MemoryLayout<CTTextAlignment>.size, value: pointer)
            return CTParagraphStyleCreate(&setting, 1)
        }
        let color = CGColor(red: text.colorRGBA[0], green: text.colorRGBA[1], blue: text.colorRGBA[2], alpha: text.colorRGBA[3])
        let string = NSAttributedString(string: text.text, attributes: [
            NSAttributedString.Key(kCTFontAttributeName as String): ctFont,
            NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
            NSAttributedString.Key(kCTParagraphStyleAttributeName as String): paragraph
        ])
        let setter = CTFramesetterCreateWithAttributedString(string)
        let width = ceil(canvas.width * 0.84)
        let measured = CTFramesetterSuggestFrameSizeWithConstraints(setter, CFRange(location: 0, length: 0), nil, CGSize(width: width, height: .greatestFiniteMagnitude), nil)
        let height = ceil(measured.height + text.fontSize * 0.25)
        guard height <= canvas.height * 0.8,
              let context = CGContext(data: nil, width: Int(width), height: Int(height), bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.unsupportedCapability
        }
        let bounds = CGRect(x: 0, y: 0, width: width, height: height)
        let frame = CTFramesetterCreateFrame(setter, CFRange(location: 0, length: 0), CGPath(rect: bounds, transform: nil), nil)
        CTFrameDraw(frame, context)
        guard let cgImage = context.makeImage() else { throw MediaEngineError.exportFailed }
        let y: Double
        switch text.anchor {
        case .top: y = canvas.height * 0.88 - height
        case .center: y = (canvas.height - height) / 2
        case .bottom: y = canvas.height * 0.12
        }
        return Self(image: CIImage(cgImage: cgImage), frame: CGRect(x: (canvas.width - width) / 2, y: y, width: width, height: height), start: start, end: end, animation: text.animation)
    }
}

final class RecipeVideoInstruction: NSObject, AVVideoCompositionInstructionProtocol, @unchecked Sendable {
    let timeRange: CMTimeRange
    let enablePostProcessing = true
    let containsTweening = true
    let requiredSourceTrackIDs: [NSValue]?
    let passthroughTrackID: CMPersistentTrackID = kCMPersistentTrackID_Invalid
    let layers: [RecipeVideoLayer]
    let text: [RecipeTextLayer]
    let canvas: CGRect
    init(timeRange: CMTimeRange, layers: [RecipeVideoLayer], text: [RecipeTextLayer], canvas: CGSize) {
        self.timeRange = timeRange
        self.layers = layers
        self.text = text
        self.canvas = CGRect(origin: .zero, size: canvas)
        self.requiredSourceTrackIDs = layers.compactMap(\.trackID).map { NSNumber(value: $0) }
    }
}

/// One deterministic compositor is used by AVPlayer and AVAssetReader/Writer. Text is sampled at
/// composition time, so seeking and export cannot diverge through a wall-clock animation layer.
final class RecipeVideoCompositor: NSObject, AVVideoCompositing, @unchecked Sendable {
    let sourcePixelBufferAttributes: [String: any Sendable]? = [
        kCVPixelBufferPixelFormatTypeKey as String: [kCVPixelFormatType_32BGRA, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange]
    ]
    let requiredPixelBufferAttributesForRenderContext: [String: any Sendable] = [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        kCVPixelBufferIOSurfacePropertiesKey as String: [String: String]()
    ]
    private let context = CIContext(options: [.cacheIntermediates: false, .workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!, .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
    private let queue = DispatchQueue(label: "com.kria.recipe-compositor")
    func renderContextChanged(_ newRenderContext: AVVideoCompositionRenderContext) {}
    func startRequest(_ request: AVAsynchronousVideoCompositionRequest) {
        queue.async { [self] in
            autoreleasepool {
                guard let instruction = request.videoCompositionInstruction as? RecipeVideoInstruction,
                      let output = request.renderContext.newPixelBuffer() else {
                    request.finish(with: MediaEngineError.exportFailed)
                    return
                }
                let time = request.compositionTime.seconds
                var frame = CIImage(color: .black).cropped(to: instruction.canvas)
                for layer in instruction.layers where time >= layer.start && time < layer.end {
                    let source: CIImage
                    if let trackID = layer.trackID {
                        guard let buffer = request.sourceFrame(byTrackID: trackID) else {
                            request.finish(with: MediaEngineError.missingAsset(String(trackID)))
                            return
                        }
                        source = CIImage(cvPixelBuffer: buffer)
                    } else if let image = layer.image {
                        source = image
                    } else { continue }
                    let alpha = layer.fadeIn > 0 ? min(1, max(0, (time - layer.start) / layer.fadeIn)) : 1
                    let positioned = source.transformed(by: layer.transform)
                    if alpha < 1 {
                        // FFmpeg xfade blends encoded channel values. Keep the
                        // compositor's linear space for text, but perform this
                        // clip blend in encoded sRGB and convert back afterward.
                        frame = opacity(positioned.applyingFilter("CILinearToSRGBToneCurve"), alpha)
                            .composited(over: frame.applyingFilter("CILinearToSRGBToneCurve"))
                            .applyingFilter("CISRGBToneCurveToLinear")
                            .cropped(to: instruction.canvas)
                    } else {
                        frame = positioned.composited(over: frame).cropped(to: instruction.canvas)
                    }
                }
                for text in instruction.text where time >= text.start && time < text.end {
                    if let layer = text.portable {
                        do {
                            if let painter = text.giantTitle {
                                let image = try painter.image(localTime: time - text.start, settled: text.image)
                                frame = image.composited(over: frame).cropped(to: instruction.canvas)
                                continue
                            }
                            let state = try TextTransformTiming.sample(effect: PortableTextEffect(rawValue: layer.effect.rawValue)!,
                                text: layer.smoothReveal?.text ?? layer.discreteReveal?.text ?? layer.handwriting?.text ?? layer.runs.map(\.text).joined(separator: "\n"), localTime: time - text.start,
                                duration: text.end - text.start, motion: layer.motion, fade: layer.fade)
                            // The bitmap already contains rotation. Cloud translation occurs in
                            // the rotated coordinate system; scaling stays centered on its anchor.
                            let angle = -layer.rotationDegrees * .pi / 180
                            let dx = state.xTranslate * cos(angle) + state.yTranslate * sin(angle)
                            let dy = state.xTranslate * sin(angle) - state.yTranslate * cos(angle)
                            let transform = CGAffineTransform(translationX: text.frame.minX - text.portableAnchor.x,
                                                              y: text.frame.minY - text.portableAnchor.y)
                                .concatenating(CGAffineTransform(scaleX: state.scale, y: state.scale))
                                .concatenating(CGAffineTransform(translationX: text.portableAnchor.x + dx, y: text.portableAnchor.y + dy))
                            var image = state.revealProgress >= 1 ? text.image : try text.handwriting?.image(progress: state.revealProgress) ?? text.image
                            if let painter = text.discreteReveal { image = try painter.image(localTime: time - text.start, settled: text.image) }
                            if let painter = text.karaoke { image = painter.image(localTime: time - text.start) }
                            if let painter = text.dissolve { image = try painter.image(source: text.image, localTime: time - text.start, duration: text.end - text.start) }
                            if let painter = text.staggered { image = try painter.image(localTime: time - text.start, settled: text.image) }
                            if let painter = text.smoothReveal { image = try painter.image(localTime: time - text.start, settled: text.image) }
                            if state.blurPx > 0.01 { image = image.applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: state.blurPx]) }
                            if let bounds = layer.revealBounds, state.revealProgress < 1 {
                                guard state.revealProgress > 0 else { continue }
                                let reveal = CGRect(x: bounds.left, y: instruction.canvas.height - bounds.bottom,
                                    width: (bounds.right - bounds.left) * state.revealProgress, height: bounds.bottom - bounds.top)
                                let rotation = CGAffineTransform(translationX: -text.portableAnchor.x, y: -text.portableAnchor.y)
                                    .concatenating(CGAffineTransform(rotationAngle: angle))
                                    .concatenating(CGAffineTransform(translationX: text.portableAnchor.x - text.frame.minX,
                                                                  y: text.portableAnchor.y - text.frame.minY))
                                let mask = CIImage(color: .white).cropped(to: reveal).transformed(by: rotation)
                                image = image.applyingFilter("CIBlendWithAlphaMask", parameters: [
                                    kCIInputBackgroundImageKey: CIImage(color: .clear).cropped(to: image.extent),
                                    kCIInputMaskImageKey: mask
                                ]).cropped(to: image.extent)
                            }
                            frame = opacity(image.transformed(by: transform), state.alpha).composited(over: frame)
                        } catch {
                            request.finish(with: error)
                            return
                        }
                        continue
                    }
                    let elapsed = time - text.start
                    let duration = min(0.3, text.end - text.start)
                    let alpha = text.animation == .none ? 1 : min(1, elapsed / duration)
                    let scale = text.animation == .fadeScale ? 0.82 + 0.18 * min(1, elapsed / min(0.35, text.end - text.start)) : 1
                    let transform = CGAffineTransform(scaleX: scale, y: scale)
                        .concatenating(CGAffineTransform(translationX: text.frame.midX - text.frame.width * scale / 2,
                                                        y: text.frame.midY - text.frame.height * scale / 2))
                    frame = opacity(text.image.transformed(by: transform), alpha).composited(over: frame)
                }
                context.render(frame, to: output, bounds: instruction.canvas, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                request.finish(withComposedVideoFrame: output)
            }
        }
    }
    // Requests are serialized and each finishes exactly once. Draining the queue avoids leaving
    // requests outstanding while AVFoundation changes its render context after a seek.
    func cancelAllPendingVideoCompositionRequests() { queue.sync {} }
    private func opacity(_ image: CIImage, _ value: Double) -> CIImage {
        image.applyingFilter("CIColorMatrix", parameters: ["inputAVector": CIVector(x: 0, y: 0, z: 0, w: value)])
    }
}
#endif
