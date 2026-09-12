import Foundation
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
import CoreText
import ImageIO

struct RecipeVideoLayer: @unchecked Sendable {
    let trackID: CMPersistentTrackID?
    let image: CIImage?
    var transform: CGAffineTransform
    let start: Double
    let end: Double
    let fadeIn: Double
    var transitionKind: Transition.Kind = .crossfade
    var look: SourceLook? = nil
    var isPrimary: Bool = true
    var clipID: String? = nil
    var naturalSize: CGSize? = nil
    var preferredTransform: CGAffineTransform = .identity
    var overlayAboveText: Bool = false
    var overlayPopIn: Bool = false
    var overlayPreserveAlpha: Bool? = nil
    var overlayCenter: CGPoint = .zero
    var visualPlacement: VisualMediaPlacement? = nil
    var visualOrder: Int = 0
    var overlayDissolve: NativeDissolveRenderer? = nil
}

struct RecipeTextLayer: @unchecked Sendable {
    let image: CIImage
    let frame: CGRect
    let start: Double
    let end: Double
    let animation: TextAnimation
    var selectionBounds: ResolvedTextSelectionBounds? = nil
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
        guard let font = CGFont(text.fontName as CFString) else { throw NativePreviewFeatureError("RecipeVideoCompositor-48") }
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
            throw NativePreviewFeatureError("RecipeVideoCompositor-68")
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
    let textStore: NativeTextLayerStore?
    let cameraPulses: [CameraPulse]
    let motionScenes: NativeMotionPainter?
    let canvas: CGRect
    let transparentBackground: Bool
    init(timeRange: CMTimeRange, layers: [RecipeVideoLayer], text: [RecipeTextLayer], canvas: CGSize, cameraPulses: [CameraPulse] = [], motionScenes: NativeMotionPainter? = nil, textStore: NativeTextLayerStore? = nil, transparentBackground: Bool = false) {
        self.transparentBackground = transparentBackground
        self.cameraPulses = cameraPulses
        self.motionScenes = motionScenes
        self.timeRange = timeRange
        self.layers = layers.enumerated().sorted { a, b in a.element.visualOrder == b.element.visualOrder ? a.offset < b.offset : a.element.visualOrder < b.element.visualOrder }.map(\.element)
        self.text = text
        self.textStore = textStore
        self.canvas = CGRect(origin: .zero, size: canvas)
        self.requiredSourceTrackIDs = layers.compactMap(\.trackID).map { NSNumber(value: $0) }
    }
    func activeText(at time: Double) throws -> [RecipeTextLayer] {
        guard let textStore else { return text.filter { time >= $0.start && time < $0.end } }
        return text.filter { $0.portable == nil && time >= $0.start && time < $0.end }
            + (try textStore.activeLayers(at: time))
    }
}

/// One deterministic compositor is used by AVPlayer and AVAssetReader/Writer. Text is sampled at
/// composition time, so seeking and export cannot diverge through a wall-clock animation layer.
class RecipeVideoCompositor: NSObject, AVVideoCompositing, @unchecked Sendable {
    var sourcePixelBufferAttributes: [String: any Sendable]? { [
        kCVPixelBufferPixelFormatTypeKey as String: [kCVPixelFormatType_32BGRA, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange]
    ] }
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
                var frame = CIImage(color: instruction.transparentBackground ? .clear : .black).cropped(to: instruction.canvas)
                for layer in instruction.layers where time >= layer.start && time < layer.end && !layer.overlayAboveText {
                    do { frame = try composeVideoLayer(layer, onto: frame, instruction: instruction, request: request, time: time) }
                    catch { request.finish(with: error); return }
                }
                if let motion = instruction.motionScenes {
                    do { frame = try motion.image(at: time).composited(over: frame) }
                    catch { request.finish(with: error); return }
                }
                let activeText: [RecipeTextLayer]
                do {
                    activeText = try instruction.activeText(at: time)
                } catch { request.finish(with: error); return }
                do { frame = try Self.paintText(activeText, over: frame, canvas: instruction.canvas, time: time) }
                catch { request.finish(with: error); return }
                for layer in instruction.layers where time >= layer.start && time < layer.end && layer.overlayAboveText {
                    do { frame = try composeVideoLayer(layer, onto: frame, instruction: instruction, request: request, time: time) }
                    catch { request.finish(with: error); return }
                }
                context.render(frame, to: output, bounds: instruction.canvas, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                request.finish(withComposedVideoFrame: output)
            }
        }
    }
    private func composeVideoLayer(_ layer: RecipeVideoLayer, onto background: CIImage,
                                   instruction: RecipeVideoInstruction, request: AVAsynchronousVideoCompositionRequest,
                                   time: Double) throws -> CIImage {
        var frame = background
        let source: CIImage
        if let trackID = layer.trackID {
            guard let buffer = request.sourceFrame(byTrackID: trackID) else {
                throw MediaEngineError.missingAsset(String(trackID))
            }
            // FFmpeg's SDR effects operate on decoded channel values.
            // Preserve those values through the sRGB output pipeline;
            // otherwise Core Image converts the Rec.709 transfer curve
            // again and visibly brightens the source before any effect.
            // The buffer's YCbCr matrix still controls YUV decoding.
            do {
                let graded = layer.look == .goldenHour ? try GoldenHourGrade.apply(to: buffer) : buffer
                source = CIImage(cvPixelBuffer: graded, options: [.colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
            } catch {
                throw error
            }
        } else if let image = layer.image {
            source = image
        } else { return frame }
        let alpha = layer.visualPlacement?.alpha(at: time) ?? (layer.fadeIn > 0 ? min(1, max(0, (time - layer.start) / layer.fadeIn)) : 1)
        var paintSource = source
        if layer.overlayPreserveAlpha == false {
            paintSource = source.applyingFilter("CIColorMatrix", parameters: [
                "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 0),
                "inputBiasVector": CIVector(x: 0, y: 0, z: 0, w: 1)
            ]).cropped(to: source.extent)
        }
        var positioned = layer.visualPlacement?.position(paintSource, preferred: layer.preferredTransform, canvas: instruction.canvas, time: time, clipStart: layer.start, clipEnd: layer.end) ?? paintSource.transformed(by: layer.transform)
        if layer.overlayPopIn {
            let scale = 0.82 + 0.18 * min(1, max(0, (time - layer.start) / 0.18))
            positioned = positioned.transformed(by: CGAffineTransform(translationX: -layer.overlayCenter.x, y: -layer.overlayCenter.y))
                .transformed(by: CGAffineTransform(scaleX: scale, y: scale))
                .transformed(by: CGAffineTransform(translationX: layer.overlayCenter.x, y: layer.overlayCenter.y))
        }
        if let dissolve = layer.overlayDissolve {
            positioned = try dissolve.image(source: positioned.cropped(to: instruction.canvas), localTime: floor((time - layer.start) * 30 + 0.000_001) / 30, duration: layer.end - layer.start)
        }
        if layer.isPrimary && !instruction.cameraPulses.isEmpty {
            let sx = CameraPulse.scale(pulses: instruction.cameraPulses, time: time, dimension: Int(instruction.canvas.width))
            let sy = CameraPulse.scale(pulses: instruction.cameraPulses, time: time, dimension: Int(instruction.canvas.height))
            positioned = positioned.cropped(to: instruction.canvas)
                .transformed(by: CGAffineTransform(translationX: -instruction.canvas.midX, y: -instruction.canvas.midY))
                .transformed(by: CGAffineTransform(scaleX: sx, y: sy))
                .transformed(by: CGAffineTransform(translationX: instruction.canvas.midX, y: instruction.canvas.midY))
        }
        if alpha < 1 && layer.transitionKind != .crossfade {
            do {
                frame = try NativeClipTransitionPainter.image(incoming: positioned, outgoing: frame, kind: layer.transitionKind, progress: alpha, canvas: instruction.canvas)
            } catch {
                throw error
            }
        } else if alpha < 1 {
            // FFmpeg xfade blends encoded channel values. Keep the
            // compositor's linear space for text, but perform this
            // clip blend in encoded sRGB and convert back afterward.
            frame = Self.opacity(positioned.applyingFilter("CILinearToSRGBToneCurve"), alpha)
                .composited(over: frame.applyingFilter("CILinearToSRGBToneCurve"))
                .applyingFilter("CISRGBToneCurveToLinear")
                .cropped(to: instruction.canvas)
        } else {
            frame = positioned.composited(over: frame).cropped(to: instruction.canvas)
        }
        return frame
    }

    static func paintText(_ texts: [RecipeTextLayer], over initial: CIImage, canvas: CGRect, time: Double) throws -> CIImage {
        var frame = initial
        for text in texts {
            if let layer = text.portable {
                do {
                    if let painter = text.giantTitle {
                        let image = try painter.image(localTime: time - text.start, settled: text.image)
                        frame = image.composited(over: frame).cropped(to: canvas)
                        continue
                    }
                    let state = try layer.animationPhases?.sample(time: time - text.start, duration: text.end - text.start) ?? TextTransformTiming.sample(effect: PortableTextEffect(rawValue: layer.effect.rawValue)!,
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
                        let reveal = CGRect(x: bounds.left, y: canvas.height - bounds.bottom,
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
                } catch { throw error }
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
        return frame
    }

    // Requests are serialized and each finishes exactly once. Draining the queue avoids leaving
    // requests outstanding while AVFoundation changes its render context after a seek.
    func cancelAllPendingVideoCompositionRequests() { queue.sync {} }
    private static func opacity(_ image: CIImage, _ value: Double) -> CIImage {
        image.applyingFilter("CIColorMatrix", parameters: ["inputAVector": CIVector(x: 0, y: 0, z: 0, w: value)])
    }
}

/// A look requires original YUV samples, before RGB clipping or resampling.
/// Leave decoder negotiation for recipes without looks unchanged.
final class RecipeLookVideoCompositor: RecipeVideoCompositor, @unchecked Sendable {
    override var sourcePixelBufferAttributes: [String: any Sendable]? { [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
    ] }
}
#endif
