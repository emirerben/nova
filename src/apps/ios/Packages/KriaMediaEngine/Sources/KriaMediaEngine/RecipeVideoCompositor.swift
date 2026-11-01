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
    var sourceCrop: NormalizedSourceRect? = nil
    var backdrop: ClipBackdrop? = nil
    /// Set only on a layer that exists to be a clip's `.media` backdrop: the pool visual id it draws. Such a layer
    /// is never composed onto the canvas itself; `composeVideoLayer` reads it under the clips that name it.
    var backdropAssetID: String? = nil
    /// Set only for an overlay clip with `overlayFadeIn`/`overlayFadeOut`.
    /// It scales opacity alone; positioning keeps using `transform`.
    var overlayFade: OverlayFadeWindow? = nil
    /// Set only when the recipe has a `speakerBackground` (KRI-1116).
    var speakerRole: SpeakerBackgroundRole? = nil
}

/// The two layers the speaker matte composes: the background replaces the speaker's room.
enum SpeakerBackgroundRole: Sendable { case background, speaker }

/// An overlay clip's fade across the window its layer actually occupies,
/// evaluated with the same curve as `VisualMediaPlacement` (see `fadeEnvelope`).
struct OverlayFadeWindow: Equatable, Sendable {
    let start: Double
    let end: Double
    let fadeIn: Bool
    let fadeOut: Bool

    /// `start`/`end` are the layer's own bounds, not `clip.duration`: a video
    /// card's track can end up to a source frame off its idealized length, and
    /// the fade-out must reach 0 where the layer stops drawing.
    init?(clip: TimelineClip, start: Double, end: Double) {
        guard clip.visualPlacement == nil, clip.overlayFadeIn == true || clip.overlayFadeOut == true else { return nil }
        self.start = start; self.end = end
        fadeIn = clip.overlayFadeIn == true; fadeOut = clip.overlayFadeOut == true
    }

    func alpha(at time: Double) -> Double {
        VisualMediaPlacement.fadeEnvelope(at: time, windowStart: start, windowEnd: end, fadeIn: fadeIn, fadeOut: fadeOut)
    }
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
    var designed: NativeDesignedTextPainter? = nil
    var handwriting: NativeHandwritingPainter? = nil
    var discreteReveal: NativeDiscreteRevealPainter? = nil
    var smoothReveal: NativeSmoothRevealPainter? = nil
    var staggered: NativeStaggeredPainter? = nil
    var dissolve: NativeDissolveRenderer? = nil
    var karaoke: NativeKaraokePainter? = nil
    var singleWord: NativeSingleWordPainter? = nil
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

/// Instruction time ranges for a set of layers, tiled on the composition's 1/60000 s clock.
///
/// Boundaries are de-duplicated AFTER quantizing to CMTime, never as Doubles: a hard cut's end is
/// computed (`timelineStart + duration`) while the next clip's start is a server literal, and the
/// two can differ by one ulp (44.132999999999996 vs 44.133). Both Doubles used to survive the
/// boundary Set and round to the same CMTime, which produced a zero-length instruction.
enum RecipeInstructionTiming {
    static func time(_ seconds: Double) -> CMTime { CMTime(value: Int64((seconds * 60_000).rounded()), timescale: 60_000) }
    static func tiledRanges(total: Double, layers: [RecipeVideoLayer]) -> [(range: CMTimeRange, layers: [RecipeVideoLayer])] {
        let spans = layers.map { (layer: $0, start: time($0.start), end: time($0.end)) }
        let boundaries = Set([CMTime.zero, time(total)] + spans.flatMap { [$0.start, $0.end] }).sorted()
        return zip(boundaries, boundaries.dropFirst()).map { start, end in
            (CMTimeRange(start: start, end: end), spans.filter { $0.start < end && $0.end > start }.map(\.layer))
        }
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
    /// KRI-589 freehand drawings: painted last, above footage, motion, text and above-text media.
    let drawings: NativeDrawingStore?
    /// Text behind people: one per composition (shared across the compositor instances the player,
    /// exporter and image generator each create), present only when a text layer is flagged.
    let subjectMatte: NativeSubjectMatte?
    /// Talk-to-camera green screen: one per composition, present only when the recipe has a background.
    let speakerMatte: SpeakerMatte?
    let canvas: CGRect
    let transparentBackground: Bool
    init(timeRange: CMTimeRange, layers: [RecipeVideoLayer], text: [RecipeTextLayer], canvas: CGSize, cameraPulses: [CameraPulse] = [], motionScenes: NativeMotionPainter? = nil, textStore: NativeTextLayerStore? = nil, drawings: NativeDrawingStore? = nil, transparentBackground: Bool = false, subjectMatte: NativeSubjectMatte? = nil, speakerMatte: SpeakerMatte? = nil) {
        self.transparentBackground = transparentBackground
        self.drawings = drawings
        self.subjectMatte = subjectMatte
        self.speakerMatte = speakerMatte
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
    /// The person mask for `scene` (the composited frame below the text), or nil when no active text layer
    /// is flagged or the matte declines (nobody in frame, frame swallowed, unstable, segmenter error).
    func subjectOcclusion(scene: CIImage, activeText: [RecipeTextLayer], time: Double) -> CIImage? {
        guard let subjectMatte, activeText.contains(where: { $0.portable?.behindSubject == true }) else { return nil }
        // Keyed by the primary clip(s) under this frame: a hard cut resets the temporal state (F6).
        let key = layers.filter { $0.isPrimary && $0.trackID != nil && time >= $0.start && time < $0.end }
            .map { $0.clipID ?? String($0.trackID ?? -1) }.joined(separator: "|")
        return subjectMatte.mask(for: scene, at: time, canvas: canvas, cutKey: key)
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
                let belowText = instruction.layers.filter { time >= $0.start && time < $0.end && !$0.overlayAboveText }
                do { frame = try composeBelowText(belowText, onto: frame, instruction: instruction, request: request, time: time) }
                catch { request.finish(with: error); return }
                if let motion = instruction.motionScenes {
                    do { frame = try motion.image(at: time).composited(over: frame) }
                    catch { request.finish(with: error); return }
                }
                let activeText: [RecipeTextLayer]
                do {
                    activeText = try instruction.activeText(at: time)
                } catch { request.finish(with: error); return }
                let occlusion = instruction.subjectOcclusion(scene: frame, activeText: activeText, time: time)
                do { frame = try Self.paintText(activeText, over: frame, canvas: instruction.canvas, time: time, occlusion: occlusion) }
                catch { request.finish(with: error); return }
                for layer in instruction.layers where time >= layer.start && time < layer.end && layer.overlayAboveText {
                    do { frame = try composeVideoLayer(layer, onto: frame, instruction: instruction, request: request, time: time) }
                    catch { request.finish(with: error); return }
                }
                if let drawings = instruction.drawings { frame = drawings.compose(over: frame, at: time, canvas: instruction.canvas) }
                context.render(frame, to: output, bounds: instruction.canvas, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                request.finish(withComposedVideoFrame: output)
            }
        }
    }
    /// The layers under the text. With a speaker background the order is: layers sorted below the first
    /// speaker layer, the background, the speaker layer(s) times the person mask, then every other layer
    /// (cutaways, fullscreen and overlay cards, fills) exactly as without a background, so nothing above the
    /// speaker is ever matted (S8). The background shows only under an active speaker clip, so an ending
    /// clip on the same track is untouched.
    private func composeBelowText(_ candidates: [RecipeVideoLayer], onto canvas: CIImage, instruction: RecipeVideoInstruction,
                                  request: AVAsynchronousVideoCompositionRequest, time: Double) throws -> CIImage {
        let layers = candidates.filter { $0.backdropAssetID == nil }
        var frame = canvas
        func compose(_ layer: RecipeVideoLayer) throws { frame = try composeVideoLayer(layer, onto: frame, instruction: instruction, request: request, time: time) }
        guard let matte = instruction.speakerMatte, let background = layers.first(where: { $0.speakerRole == .background }),
              let first = layers.firstIndex(where: { $0.speakerRole == .speaker }) else {
            for layer in layers where layer.speakerRole != .background { try compose(layer) }
            return frame
        }
        for layer in layers[..<first] where layer.speakerRole == nil { try compose(layer) }
        let speakers = layers[first...].filter { $0.speakerRole == .speaker }
        var speaker = CIImage(color: .clear).cropped(to: instruction.canvas)
        for layer in speakers { speaker = try composeVideoLayer(layer, onto: speaker, instruction: instruction, request: request, time: time) }
        let key = speakers.map { $0.clipID ?? String($0.trackID ?? -1) }.joined(separator: "|")
        switch matte.answer(for: speaker, at: time, canvas: instruction.canvas, clipKey: key) {
        case .cutout(let mask):
            try compose(background)
            let cut = SpeakerMatte.cutOut(speaker, mask: mask, canvas: instruction.canvas, config: matte.config) ?? speaker
            frame = cut.composited(over: frame).cropped(to: instruction.canvas)
        case .backgroundOnly:
            try compose(background)
        case .original:
            for layer in speakers { try compose(layer) }
        }
        for layer in layers[first...] where layer.speakerRole == nil { try compose(layer) }
        return frame
    }

    /// The layer's own picture for this request: its track's frame (graded when it carries a look) or its image.
    private func sourceImage(of layer: RecipeVideoLayer, request: AVAsynchronousVideoCompositionRequest) throws -> CIImage? {
        if let trackID = layer.trackID {
            guard let buffer = request.sourceFrame(byTrackID: trackID) else {
                throw MediaEngineError.missingAsset(String(trackID))
            }
            // FFmpeg's SDR effects operate on decoded channel values.
            // Preserve those values through the sRGB output pipeline;
            // otherwise Core Image converts the Rec.709 transfer curve
            // again and visibly brightens the source before any effect.
            // The buffer's YCbCr matrix still controls YUV decoding.
            let graded = layer.look == .goldenHour ? try GoldenHourGrade.apply(to: buffer) : buffer
            return CIImage(cvPixelBuffer: graded, options: [.colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
        }
        return layer.image
    }

    /// The canvas-sized pool photo or video a clip's `.media` backdrop shows, cover-fit like a fullscreen cutaway.
    /// nil (nothing drawn) when no backdrop layer carries `poolID`, which a built composition never lacks.
    private func mediaBackdropImage(_ poolID: String, instruction: RecipeVideoInstruction,
                                    request: AVAsynchronousVideoCompositionRequest, time: Double) throws -> CIImage? {
        guard let backdropLayer = instruction.layers.first(where: { $0.backdropAssetID == poolID }),
              let source = try sourceImage(of: backdropLayer, request: request) else { return nil }
        return Self.positionedSource(source, layer: backdropLayer, canvas: instruction.canvas, time: time).cropped(to: instruction.canvas)
    }

    private func composeVideoLayer(_ layer: RecipeVideoLayer, onto background: CIImage,
                                   instruction: RecipeVideoInstruction, request: AVAsynchronousVideoCompositionRequest,
                                   time: Double) throws -> CIImage {
        var frame = background
        guard let source = try sourceImage(of: layer, request: request) else { return frame }
        var alpha = layer.visualPlacement?.alpha(at: time) ?? (layer.fadeIn > 0 ? min(1, max(0, (time - layer.start) / layer.fadeIn)) : 1)
        if layer.visualPlacement == nil, let fade = layer.overlayFade { alpha *= fade.alpha(at: time) }
        var positioned = Self.positionedSource(source, layer: layer, canvas: instruction.canvas, time: time)
        if let backdrop = layer.backdrop {
            let under: CIImage?
            if case .media(let poolID) = backdrop {
                under = try mediaBackdropImage(poolID, instruction: instruction, request: request, time: time)
            } else {
                under = Self.backdropImage(backdrop, source: source, layer: layer, canvas: instruction.canvas, time: time)
            }
            if let under { positioned = positioned.composited(over: under).cropped(to: instruction.canvas) }
        }
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

    /// `occlusion`: the canvas-space person mask (1 = subject) for this frame; a layer with
    /// `behindSubject` keeps its alpha only where the mask is 0 (`SubjectOcclusion.apply`). nil paints plain.
    static func paintText(_ texts: [RecipeTextLayer], over initial: CIImage, canvas: CGRect, time: Double, occlusion: CIImage? = nil) throws -> CIImage {
        var frame = initial
        for text in texts {
            if let layer = text.portable {
                do {
                    if let painter = text.giantTitle {
                        let image = try painter.image(localTime: time - text.start, settled: text.image)
                        frame = image.composited(over: frame).cropped(to: canvas)
                        continue
                    }
                    let word = text.singleWord?.window(localTime: time - text.start)
                    let state = try layer.animationPhases?.sample(time: time - text.start - (word?.start ?? 0),
                        duration: word.map { $0.end - $0.start } ?? text.end - text.start) ?? TextTransformTiming.sample(effect: PortableTextEffect(rawValue: layer.effect.rawValue)!,
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
                    if let painter = text.singleWord { image = try painter.image(localTime: time - text.start) }
                    if let painter = text.dissolve { image = try painter.image(source: text.image, localTime: time - text.start, duration: text.end - text.start) }
                    if let painter = text.staggered { image = try painter.image(localTime: time - text.start, settled: text.image) }
                    if let painter = text.smoothReveal { image = try painter.image(localTime: time - text.start, settled: text.image) }
                    if let painter = text.designed { image = try painter.image(localTime: time - text.start, settled: image) }
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
                    var placed = image.transformed(by: transform)
                    // Text behind people: the multiply happens AFTER placement, in canvas space, where the
                    // mask lives (F1). No mask this frame = plain text, never a missing layer (F3, F13).
                    if layer.behindSubject == true, let occlusion { placed = SubjectOcclusion.apply(to: placed, mask: occlusion) }
                    frame = opacity(placed, state.alpha).composited(over: frame)
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

    /// Crop coordinates are normalized, top-left origin, in UPRIGHT (display) space: the
    /// frame after the track's preferred transform / EXIF orientation. This matches the
    /// cloud `reframe.py` crop and the crop editor. `layer.preferredTransform` is the
    /// Core Image (bottom-left) form of that transform. Map the upright rectangle back
    /// into the encoded frame before cropping, then preserve the existing transform's
    /// user zoom, rotation and translation while replacing its full-source cover fit with
    /// a fit of the selected rectangle.
    static func positionedSource(_ source: CIImage, layer: RecipeVideoLayer, canvas: CGRect, time: Double) -> CIImage {
        var image = source
        var transform = layer.transform
        if let crop = layer.sourceCrop {
            let full = source.extent
            let rectangle = uprightCropToEncodedRect(crop: crop, extent: full, preferredTransform: layer.preferredTransform)
            image = source.cropped(to: rectangle)
            if layer.visualPlacement == nil {
                let orientedFull = full.applying(layer.preferredTransform)
                let orientedCrop = rectangle.applying(layer.preferredTransform)
                let oldCover = max(canvas.width / orientedFull.width, canvas.height / orientedFull.height)
                let newCover = max(canvas.width / orientedCrop.width, canvas.height / orientedCrop.height)
                let correction = CGAffineTransform(translationX: -rectangle.midX, y: -rectangle.midY)
                    .concatenating(CGAffineTransform(scaleX: newCover / oldCover, y: newCover / oldCover))
                    .concatenating(CGAffineTransform(translationX: full.midX, y: full.midY))
                transform = correction.concatenating(transform)
            }
        }
        if layer.overlayPreserveAlpha == false {
            image = image.applyingFilter("CIColorMatrix", parameters: [
                "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 0),
                "inputBiasVector": CIVector(x: 0, y: 0, z: 0, w: 1)
            ]).cropped(to: image.extent)
        }
        return layer.visualPlacement?.position(image, preferred: layer.preferredTransform, canvas: canvas,
            time: time, clipStart: layer.start, clipEnd: layer.end) ?? image.transformed(by: transform)
    }

    /// Converts a normalized, top-left-origin crop in upright space into a Core Image
    /// (bottom-left-origin) rectangle in the encoded frame `extent`. `preferredTransform`
    /// maps encoded -> upright in Core Image coordinates; mirrored and quarter-turn
    /// transforms are handled by mapping the upright rectangle through its inverse.
    static func uprightCropToEncodedRect(crop: NormalizedSourceRect, extent: CGRect, preferredTransform: CGAffineTransform) -> CGRect {
        let upright = extent.applying(preferredTransform)
        let uprightRect = CGRect(x: upright.minX + upright.width * crop.x,
            y: upright.maxY - upright.height * (crop.y + crop.height),
            width: upright.width * crop.width, height: upright.height * crop.height)
        let t = preferredTransform
        guard !t.isIdentity, abs(t.a * t.d - t.b * t.c) > 1e-9 else { return uprightRect }
        return uprightRect.applying(preferredTransform.inverted())
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
