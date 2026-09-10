#if canImport(AVFoundation)
import Foundation
import CoreImage

/// Immutable line bitmaps. Each mask is applied before compositing, so a line's
/// shadow cannot leak through an adjacent line's reveal rectangle.
final class NativeSmoothRevealPainter: @unchecked Sendable {
    let layer: PortableTextLayer
    let lines: [RecipeTextLayer]
    let bounds: CGRect
    let canvas: CGSize
    let bitmapBytes: Int
    init(layer: PortableTextLayer, lines: [RecipeTextLayer], bounds: CGRect, canvas: CGSize, bitmapBytes: Int) {
        self.layer = layer; self.lines = lines; self.bounds = bounds; self.canvas = canvas; self.bitmapBytes = bitmapBytes
    }
    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let content = layer.smoothReveal else { throw RecipeError.invalidTimeline }
        let clips = try content.clips(localTime: localTime, motion: layer.motion)
        if clips.allSatisfy({ $0 == nil }) { return settled }
        let extent = CGRect(origin: .zero, size: bounds.size)
        var result = CIImage(color: .clear).cropped(to: extent)
        let anchor = CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY)
        let rotation = CGAffineTransform(translationX: -anchor.x, y: -anchor.y)
            .concatenating(CGAffineTransform(rotationAngle: -layer.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: anchor.x - bounds.minX, y: anchor.y - bounds.minY))
        for (line, clip) in zip(content.lines, clips) {
            guard let index = line.runIndex else { continue }
            let painted = lines[index]
            var image = painted.image.transformed(by: CGAffineTransform(translationX: painted.frame.minX - bounds.minX,
                                                                         y: painted.frame.minY - bounds.minY))
            if let clip {
                if clip.right <= clip.left { continue }
                let rect = CGRect(x: clip.left, y: canvas.height - clip.bottom, width: clip.right - clip.left, height: clip.bottom - clip.top)
                let mask = CIImage(color: .white).cropped(to: rect).transformed(by: rotation)
                image = image.applyingFilter("CIBlendWithAlphaMask", parameters: [
                    kCIInputBackgroundImageKey: CIImage(color: .clear).cropped(to: image.extent), kCIInputMaskImageKey: mask
                ]).cropped(to: image.extent)
            }
            result = image.composited(over: result).cropped(to: extent)
        }
        return result
    }
}

extension RecipeTextLayer {
    static func makeSmoothReveal(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        guard let content = layer.smoothReveal else { throw RecipeError.invalidTimeline }
        try content.validate(runs: layer.runs)
        let settled = try make(NativeDiscreteRevealPainter.paintingLayer(layer, runs: layer.runs), assetURLs: assetURLs,
                               canvas: canvas, maxBitmapBytes: maxBitmapBytes / 2)
        // Reserve the settled bitmap and a full-size partial composite, plus
        // the separately retained line bitmaps. Reject before further allocation.
        var bytes = Int(settled.image.extent.width * settled.image.extent.height * 4) * 2
        var lines: [RecipeTextLayer] = []
        for run in layer.runs {
            let line = try make(NativeDiscreteRevealPainter.paintingLayer(layer, runs: [run]), assetURLs: assetURLs,
                                canvas: canvas, maxBitmapBytes: maxBitmapBytes - bytes)
            bytes += Int(line.image.extent.width * line.image.extent.height * 4)
            lines.append(line)
        }
        let painter = NativeSmoothRevealPainter(layer: layer, lines: lines, bounds: settled.frame, canvas: canvas, bitmapBytes: bytes)
        return Self(image: settled.image, frame: settled.frame, start: layer.start, end: layer.end, animation: .none, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), smoothReveal: painter)
    }
}
#endif
