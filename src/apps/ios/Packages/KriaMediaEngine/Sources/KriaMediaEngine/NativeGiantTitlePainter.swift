#if canImport(AVFoundation)
import Foundation
import CoreImage

struct NativeGiantTitlePainter: @unchecked Sendable {
    private let layer: PortableTextLayer
    private let canvas: CGSize
    private let vector: PortableTextVectorPainter
    private let maxBitmapBytes: Int
    let bitmapBytes: Int

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws {
        guard layer.giantTitle != nil else { throw RecipeError.invalidTimeline }
        self.layer = layer; self.canvas = canvas
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

    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let theme = layer.giantTitle else { throw RecipeError.invalidTimeline }
        let duration = layer.end - layer.start
        let state = try TextTransformTiming.sample(effect: layer.effect == .slideIn ? .static : PortableTextEffect(rawValue: layer.effect.rawValue)!,
            text: layer.runs.map(\.text).joined(separator: "\n"), localTime: localTime, duration: duration,
            motion: layer.motion, fade: layer.fade)
        let wipe = try GiantTitleTiming.sample(localTime: localTime, duration: duration)
        let outputBounds = CGRect(origin: .zero, size: canvas)
        guard state.alpha * wipe.alpha > 0, state.revealProgress > 0 else {
            return CIImage(color: .clear).cropped(to: outputBounds)
        }
        if state.alpha == 1 && wipe.scale == 1 && state.scale == 1 && state.xTranslate == 0 && state.yTranslate == 0 && state.revealProgress >= 1 {
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
        let image = try vector.image(bounds: outputBounds, maxBitmapBytes: maxBitmapBytes, transform: transform, clip: clip, opacity: state.alpha)
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
