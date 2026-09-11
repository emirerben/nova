#if canImport(AVFoundation)
import Foundation
import CoreImage

final class NativeGiantTitlePainter: @unchecked Sendable {
    private let layer: PortableTextLayer
    private let canvas: CGSize
    private let vector: PortableTextVectorPainter
    private let maxBitmapBytes: Int
    private let assetURLs: [String: URL]
    private let lock = NSLock()
    private var lastReveal: TextRevealSample?
    private var lastVector: PortableTextVectorPainter?
    let bitmapBytes: Int

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws {
        guard layer.giantTitle != nil else { throw RecipeError.invalidTimeline }
        self.layer = layer; self.canvas = canvas; self.assetURLs = assetURLs
        vector = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas, outlineGlyphs: true)
        // Settled image + live foreground. Shadows add a cropped output and one
        // padded source mask, bounded by Skia's 128px source-mask clip outset.
        let hasShadows = layer.runs.contains { !$0.blurLayers.isEmpty }
        let shadowBytes = hasShadows ? Int(canvas.width * canvas.height * 4 + (canvas.width + 256) * (canvas.height + 256) * 4) : 0
        bitmapBytes = Int(canvas.width * canvas.height * 8) + shadowBytes
        guard bitmapBytes <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
        self.maxBitmapBytes = maxBitmapBytes
    }

    var anchor: CGPoint { vector.anchor }
    func settledImage() throws -> CIImage {
        try vector.image(bounds: CGRect(origin: .zero, size: canvas), maxBitmapBytes: maxBitmapBytes)
    }

    private func visibleVector(localTime: Double) throws -> (PortableTextVectorPainter, Bool) {
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
        let (visible, complete) = try visibleVector(localTime: localTime)
        let duration = layer.end - layer.start
        let state = try TextTransformTiming.sample(effect: layer.effect == .slideIn ? .static : PortableTextEffect(rawValue: layer.effect.rawValue)!,
            text: layer.discreteReveal?.text ?? layer.runs.map(\.text).joined(separator: "\n"), localTime: localTime, duration: duration,
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
        let image = try visible.image(bounds: outputBounds, maxBitmapBytes: maxBitmapBytes, transform: transform, clip: clip, opacity: state.alpha)
        return Self.alpha(image, wipe.alpha)
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
