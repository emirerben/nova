#if canImport(AVFoundation)
import Foundation
import CoreImage

final class NativeStaggeredPainter: @unchecked Sendable {
    let layer: PortableTextLayer
    let glyphs: [RecipeTextLayer?]
    let bounds: CGRect
    let canvas: CGSize
    let bitmapBytes: Int
    init(layer: PortableTextLayer, glyphs: [RecipeTextLayer?], bounds: CGRect, canvas: CGSize, bitmapBytes: Int) {
        self.layer = layer; self.glyphs = glyphs; self.bounds = bounds; self.canvas = canvas; self.bitmapBytes = bitmapBytes
    }
    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let content = layer.staggered else { throw RecipeError.invalidTimeline }
        let state = try StaggeredSliceTiming.sample(text: content.text, localTime: localTime, duration: layer.end - layer.start, motion: layer.motion)
        if state.settled { return settled }
        let extent = CGRect(origin: .zero, size: bounds.size)
        var result = CIImage(color: .clear).cropped(to: extent)
        for (entry, bitmap) in zip(content.glyphs, glyphs) {
            guard let bitmap else { continue }
            let glyph = state.lines[entry.logicalLine].glyphs[entry.glyphIndex]
            guard glyph.opacity > 0.001 else { continue }
            let pivot = CGPoint(x: entry.pivotX, y: canvas.height - entry.pivotY)
            let transform = CGAffineTransform(translationX: bitmap.frame.minX - pivot.x, y: bitmap.frame.minY - pivot.y)
                .concatenating(CGAffineTransform(rotationAngle: -glyph.rotateDeg * .pi / 180))
                .concatenating(CGAffineTransform(translationX: pivot.x - bounds.minX,
                    y: pivot.y - bounds.minY - entry.run.fontSize * glyph.translateYEm))
            let alpha = floor(min(255, max(0, 255 * glyph.opacity))) / 255
            let image = bitmap.image.transformed(by: transform).applyingFilter("CIColorMatrix", parameters: [
                "inputAVector": CIVector(x: 0, y: 0, z: 0, w: alpha)
            ])
            result = image.composited(over: result).cropped(to: extent)
        }
        return result
    }
}

extension RecipeTextLayer {
    static func makeStaggered(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        guard let content = layer.staggered else { throw RecipeError.invalidTimeline }
        try content.validate()
        let settled = try make(NativeDiscreteRevealPainter.paintingLayer(layer, runs: layer.runs), assetURLs: assetURLs,
                               canvas: canvas, maxBitmapBytes: maxBitmapBytes / 2)
        var bounds = settled.frame
        var glyphs: [RecipeTextLayer?] = []
        var retainedBytes = 0
        for entry in content.glyphs {
            if entry.run.text.allSatisfy(\.isWhitespace) { glyphs.append(nil); continue }
            // Production's partial stage applies only each glyph's rotation.
            // The settled branch uses the full overlay rotation instead.
            let drawing = PortableTextLayer(id: layer.id, start: layer.start, end: layer.end, anchorX: layer.anchorX, anchorY: layer.anchorY,
                                            rotationDegrees: 0, runs: [entry.run], effect: .fadeIn)
            let reserved = retainedBytes + Int(bounds.width * bounds.height * 4) * 2
            let bitmap = try make(drawing, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes - reserved)
            retainedBytes += Int(bitmap.frame.width * bitmap.frame.height * 4)
            let pivot = CGPoint(x: entry.pivotX, y: canvas.height - entry.pivotY)
            // A circumscribed circle contains every intermediate rotation; the
            // vertical expansion covers all entrance translations, not just endpoints.
            let radius = hypot(max(abs(bitmap.frame.minX - pivot.x), abs(bitmap.frame.maxX - pivot.x)),
                               max(abs(bitmap.frame.minY - pivot.y), abs(bitmap.frame.maxY - pivot.y)))
            bounds = bounds.union(CGRect(x: pivot.x - radius, y: pivot.y - radius - entry.run.fontSize * 0.18,
                                         width: 2 * radius, height: 2 * radius + entry.run.fontSize * 0.18)).integral
            guard retainedBytes + Int(bounds.width * bounds.height * 4) * 2 <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
            glyphs.append(bitmap)
        }
        let extent = CGRect(origin: .zero, size: bounds.size)
        let full = settled.image.transformed(by: CGAffineTransform(translationX: settled.frame.minX - bounds.minX,
                                                                 y: settled.frame.minY - bounds.minY))
            .composited(over: CIImage(color: .clear).cropped(to: extent)).cropped(to: extent)
        // Retain the existing settled backing image, while reserving a complete
        // final canvas and one partial composite for the lazy image graph.
        let bytes = retainedBytes + Int(bounds.width * bounds.height * 4) * 2
        let painter = NativeStaggeredPainter(layer: layer, glyphs: glyphs, bounds: bounds, canvas: canvas, bitmapBytes: bytes)
        return Self(image: full, frame: bounds, start: layer.start, end: layer.end, animation: .none, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), staggered: painter)
    }
}
#endif
