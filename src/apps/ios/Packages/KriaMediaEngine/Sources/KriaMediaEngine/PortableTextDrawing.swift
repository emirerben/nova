#if canImport(AVFoundation)
import Foundation
import AVFoundation
import CoreText
import CoreImage

extension TextInk {
    var cgColor: CGColor { CGColor(colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, components: [red, green, blue, alpha])! }
}

extension RecipeTextLayer {
    /// Baselines and tracking are authored in output pixels. No device-specific
    /// wrap, auto-shrink, font lookup, or substitution occurs here.
    static func make(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int = 64 * 1024 * 1024, fixedBounds: CGRect? = nil) throws -> Self {
        if layer.karaoke != nil { return try makeKaraoke(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
        if layer.effect == .dissolveOut { return try makeDissolve(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
        if layer.staggered != nil { return try makeStaggered(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
        if layer.smoothReveal != nil { return try makeSmoothReveal(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
        if layer.handwriting != nil { return try makeHandwriting(layer, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
        if layer.discreteReveal != nil { return try makeDiscreteReveal(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
        let painter = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas)
        let bounds = fixedBounds ?? painter.bounds
        return Self(image: try painter.image(bounds: bounds, maxBitmapBytes: maxBitmapBytes),
                    frame: bounds, start: layer.start, end: layer.end, animation: .none,
                    portable: layer, portableAnchor: painter.anchor)
    }
}
#endif
