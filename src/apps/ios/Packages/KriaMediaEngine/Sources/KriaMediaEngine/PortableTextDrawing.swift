#if canImport(AVFoundation)
import Foundation
import AVFoundation
import CoreText
import CoreImage

extension TextInk {
    /// Core Image color matrices operate on straight color. Set a constant tint
    /// and multiply coverage only in alpha; premultiplying RGB here darkens glows twice.
    func tint(mask: CIImage) -> CIImage {
        mask.applyingFilter("CIColorMatrix", parameters: [
            "inputRVector": CIVector(x: 0, y: 0, z: 0, w: 0),
            "inputGVector": CIVector(x: 0, y: 0, z: 0, w: 0),
            "inputBVector": CIVector(x: 0, y: 0, z: 0, w: 0),
            "inputAVector": CIVector(x: 0, y: 0, z: 0, w: alpha),
            "inputBiasVector": CIVector(x: red, y: green, z: blue, w: 0),
        ])
    }
    var cgColor: CGColor { CGColor(colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, components: [red, green, blue, alpha])! }
}

extension RecipeTextLayer {
    /// Baselines and tracking are authored in output pixels. No device-specific
    /// wrap, auto-shrink, font lookup, or substitution occurs here.
    static func make(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int = 64 * 1024 * 1024, fixedBounds: CGRect? = nil) throws -> Self {
        if layer.giantTitle != nil { return try makeGiantTitle(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes) }
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
                    selectionBounds: painter.selectionBounds, portable: layer, portableAnchor: painter.anchor)
    }
}
#endif
