#if canImport(AVFoundation)
import Foundation
import CoreImage

final class NativeGiantTitlePainter: @unchecked Sendable {
    private let layer: PortableTextLayer
    private let canvas: CGSize
    private let vector: PortableTextVectorPainter
    private let staggeredVector: PortableTextVectorPainter?
    private let maxBitmapBytes: Int
    private let assetURLs: [String: URL]
    private let lock = NSLock()
    private let imageContext = CIContext(options: [.cacheIntermediates: false,
        .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
        .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
    private var lastReveal: TextRevealSample?
    private var lastHighlights: [Bool]?
    private var lastVector: PortableTextVectorPainter?
    let bitmapBytes: Int

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws {
        guard layer.giantTitle != nil else { throw RecipeError.invalidTimeline }
        self.layer = layer; self.canvas = canvas; self.assetURLs = assetURLs
        vector = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas, outlineGlyphs: true)
        if let content = layer.staggered {
            let drawing = PortableTextLayer(id: layer.id, start: layer.start, end: layer.end, anchorX: layer.anchorX,
                anchorY: layer.anchorY, rotationDegrees: 0, runs: content.glyphs.map(\.run), effect: .fadeIn)
            staggeredVector = try PortableTextVectorPainter(layer: drawing, assetURLs: assetURLs, canvas: canvas, outlineGlyphs: true)
        } else { staggeredVector = nil }
        // Settled image + live foreground. Shadows add a cropped output and one
        // padded source mask, bounded by Skia's 128px source-mask clip outset.
        let hasShadows = layer.runs.contains { !$0.blurLayers.isEmpty } || !(layer.handwriting?.blurLayers.isEmpty ?? true)
        var maxBlur = 0.0
        if let content = layer.smoothReveal, let motion = layer.motion {
            // Blur decreases and camera scale increases. Endpoint products on
            // each interval conservatively bound their product between samples.
            for index in 0..<32 {
                let start = (layer.end - layer.start) * Double(index) / 32
                let end = (layer.end - layer.start) * Double(index + 1) / 32
                let blur = try TextMotionTiming.smoothType(text: content.text, localTime: start, motion: motion).blurPx
                let scale = try GiantTitleTiming.sample(localTime: end, duration: layer.end - layer.start).scale
                maxBlur = max(maxBlur, blur * scale)
            }
        }
        let padding = maxBlur > 0.01 ? ceil(maxBlur * 3) + 2 : 0
        let width = canvas.width + padding * 2, height = canvas.height + padding * 2
        let shadowBytes = hasShadows ? Int(width * height * 4 + (width + 256) * (height + 256) * 4) : 0
        let liveBytes = Int(width * height * (padding > 0 || layer.staggered != nil ? 8 : 4))
        bitmapBytes = Int(canvas.width * canvas.height * 4) + liveBytes + shadowBytes
        guard bitmapBytes <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
        self.maxBitmapBytes = maxBitmapBytes
    }

    var anchor: CGPoint { vector.anchor }
    func settledImage() throws -> CIImage {
        if let content = layer.handwriting {
            return try NativeGiantHandwritingPainter.image(content: content, canvas: canvas,
                bounds: CGRect(origin: .zero, size: canvas), rotation: vector.rotation, transform: .identity,
                progress: 1, opacity: 1, maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
        }
        return try vector.image(bounds: CGRect(origin: .zero, size: canvas), maxBitmapBytes: maxBitmapBytes)
    }

    private func visibleVector(localTime: Double) throws -> (PortableTextVectorPainter, Bool) {
        if let content = layer.karaoke {
            let highlighted = content.starts.map { localTime >= $0 }
            if !highlighted.contains(true) { return (vector, true) }
            lock.lock(); defer { lock.unlock() }
            if highlighted == lastHighlights, let lastVector { return (lastVector, false) }
            let runs = zip(layer.runs, highlighted).map { run, active in
                active ? PositionedTextRun(text: run.text, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
                    x: run.x, baselineY: run.baselineY, letterSpacing: run.letterSpacing, shaped: false,
                    fill: content.highlight, stroke: run.stroke, strokeWidth: run.strokeWidth,
                    blurLayers: run.blurLayers, glyphs: run.glyphs) : run
            }
            let colored = try PortableTextVectorPainter(layer: NativeDiscreteRevealPainter.paintingLayer(layer, runs: runs),
                assetURLs: assetURLs, canvas: canvas, outlineGlyphs: true)
            lastHighlights = highlighted; lastVector = colored
            return (colored, false)
        }
        guard let content = layer.discreteReveal,
              let effect = PortableTextEffect(rawValue: layer.effect.rawValue) else { return (vector, true) }
        let sample = try TextRevealTiming.sample(effect: effect, text: content.text, localTime: localTime,
            start: layer.start, schedule: content.schedule, motion: layer.motion)
        if sample.visibleText.unicodeScalars.elementsEqual(TextRevealTiming.normalized(content.text).unicodeScalars), !sample.showCursor {
            return (vector, true)
        }
        lock.lock(); defer { lock.unlock() }
        if sample == lastReveal, let lastVector { return (lastVector, false) }
        let runs = content.visibleRuns(layer.runs, sample: sample)
        let partial = try PortableTextVectorPainter(layer: NativeDiscreteRevealPainter.paintingLayer(layer, runs: runs),
            assetURLs: assetURLs, canvas: canvas, outlineGlyphs: true)
        lastReveal = sample; lastVector = partial
        return (partial, false)
    }

    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let theme = layer.giantTitle else { throw RecipeError.invalidTimeline }
        var (visible, complete) = try visibleVector(localTime: localTime)
        var runTransforms: [CGAffineTransform]?
        var runGroupAlphas: [Double]?
        if let content = layer.staggered, let partial = staggeredVector {
            let sample = try StaggeredSliceTiming.sample(text: content.text, localTime: localTime,
                duration: layer.end - layer.start, motion: layer.motion)
            if !sample.settled {
                visible = partial; complete = false
                runTransforms = []; runGroupAlphas = []
                for entry in content.glyphs {
                    let glyph = sample.lines[entry.logicalLine].glyphs[entry.glyphIndex]
                    let pivot = CGPoint(x: entry.pivotX, y: canvas.height - entry.pivotY)
                    runTransforms?.append(CGAffineTransform(translationX: -pivot.x, y: -pivot.y)
                        .concatenating(CGAffineTransform(rotationAngle: -glyph.rotateDeg * .pi / 180))
                        .concatenating(CGAffineTransform(translationX: pivot.x,
                            y: pivot.y - entry.run.fontSize * glyph.translateYEm)))
                    runGroupAlphas?.append(floor(min(255, max(0, 255 * glyph.opacity))) / 255)
                }
            }
        }
        let duration = layer.end - layer.start
        let state = try TextTransformTiming.sample(effect: layer.effect == .slideIn ? .static : PortableTextEffect(rawValue: layer.effect.rawValue)!,
            text: layer.handwriting?.text ?? layer.smoothReveal?.text ?? layer.discreteReveal?.text ?? layer.runs.map(\.text).joined(separator: "\n"), localTime: localTime, duration: duration,
            motion: layer.motion, fade: layer.fade)
        let wipe = try GiantTitleTiming.sample(localTime: localTime, duration: duration)
        let outputBounds = CGRect(origin: .zero, size: canvas)
        guard state.alpha * wipe.alpha > 0, state.revealProgress > 0 else {
            return CIImage(color: .clear).cropped(to: outputBounds)
        }
        if complete && state.alpha == 1 && wipe.scale == 1 && state.scale == 1 && state.xTranslate == 0 && state.yTranslate == 0 && state.revealProgress >= 1 {
            return Self.alpha(settled, state.alpha * wipe.alpha)
        }
        let anchor = vector.anchor
        let angle = -layer.rotationDegrees * .pi / 180
        let dx = state.xTranslate * cos(angle) + state.yTranslate * sin(angle)
        let dy = state.xTranslate * sin(angle) - state.yTranslate * cos(angle)
        let origin = CGPoint(x: theme.originX, y: canvas.height - theme.originY)
        let transform = CGAffineTransform(translationX: -anchor.x, y: -anchor.y)
            .concatenating(CGAffineTransform(scaleX: state.scale, y: state.scale))
            .concatenating(CGAffineTransform(translationX: anchor.x + dx, y: anchor.y + dy))
            .concatenating(CGAffineTransform(translationX: -origin.x, y: -origin.y))
            .concatenating(CGAffineTransform(scaleX: wipe.scale, y: wipe.scale))
            .concatenating(CGAffineTransform(translationX: origin.x, y: origin.y))
        var clip: CGPath?
        if let reveal = layer.revealBounds, state.revealProgress < 1 {
            let rect = CGRect(x: reveal.left, y: canvas.height - reveal.bottom,
                width: (reveal.right - reveal.left) * state.revealProgress, height: reveal.bottom - reveal.top)
            var clipTransform = vector.rotation.concatenating(transform)
            clip = CGPath(rect: rect, transform: &clipTransform)
        }
        var runClips: [CGPath?]?
        if let content = layer.smoothReveal {
            runClips = [CGPath?](repeating: nil, count: layer.runs.count)
            let clips = try content.clips(localTime: localTime, motion: layer.motion)
            for (line, rectangle) in zip(content.lines, clips) {
                guard let index = line.runIndex, let rectangle else { continue }
                let rect = CGRect(x: rectangle.left, y: canvas.height - rectangle.bottom,
                    width: max(0, rectangle.right - rectangle.left), height: rectangle.bottom - rectangle.top)
                var mapping = vector.rotation.concatenating(transform)
                runClips?[index] = CGPath(rect: rect, transform: &mapping)
            }
        }
        let blur = state.blurPx * hypot(transform.a, transform.b)
        let padding = blur > 0.01 ? ceil(blur * 3) + 2 : 0
        let drawingBounds = outputBounds.insetBy(dx: -padding, dy: -padding)
        var image: CIImage
        if let content = layer.handwriting {
            image = try NativeGiantHandwritingPainter.image(content: content, canvas: canvas,
                bounds: drawingBounds, rotation: vector.rotation, transform: transform,
                progress: state.revealProgress, opacity: state.alpha,
                maxBitmapBytes: maxBitmapBytes, imageContext: imageContext)
        } else {
            image = try visible.image(bounds: drawingBounds, maxBitmapBytes: maxBitmapBytes, transform: transform,
                clip: clip, runClips: runClips, runTransforms: runTransforms, runGroupAlphas: runGroupAlphas, opacity: state.alpha)
        }
        image = image.transformed(by: CGAffineTransform(translationX: drawingBounds.minX, y: drawingBounds.minY))
        if blur > 0.01 {
            image = image.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": blur])
            // Blur in the renderer's sRGB space before handing the layer to a
            // consumer, whose working color space may be linear (video fades).
            guard let blurred = imageContext.createCGImage(image, from: outputBounds) else { throw MediaEngineError.exportFailed }
            image = CIImage(cgImage: blurred)
        }
        return Self.alpha(image.cropped(to: outputBounds), wipe.alpha)
    }

    private static func alpha(_ image: CIImage, _ value: Double) -> CIImage {
        guard value < 1 else { return image }
        return image.applyingFilter("CIColorMatrix", parameters: [
            "inputAVector": CIVector(x: 0, y: 0, z: 0, w: value),
        ])
    }
}

extension RecipeTextLayer {
    static func makeGiantTitle(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        let painter = try NativeGiantTitlePainter(layer: layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes)
        let bounds = CGRect(origin: .zero, size: canvas)
        return Self(image: try painter.settledImage(), frame: bounds,
                    start: layer.start, end: layer.end, animation: .none, portable: layer,
                    portableAnchor: painter.anchor, giantTitle: painter)
    }
}
#endif
