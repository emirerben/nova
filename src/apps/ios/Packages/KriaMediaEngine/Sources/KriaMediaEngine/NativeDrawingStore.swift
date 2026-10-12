#if canImport(AVFoundation)
import CoreGraphics
import CoreImage
import Foundation

/// The compositor's drawings (KRI-589): one store per composition canvas, shared by preview and export.
///
/// A drawing whose ink is complete is rasterised once per (id, canvas) and reused for every later frame:
/// a fade only changes the opacity applied at composite time, so only the frames in the `.draw` reveal
/// window pay for a fresh Core Graphics raster.
final class NativeDrawingStore: @unchecked Sendable {
    let layers: [PortableDrawingLayer]
    let canvas: CGSize
    private let lock = NSLock()
    private var settled: [String: [NativeDrawingPainter.Pass]] = [:]

    init(layers: [PortableDrawingLayer], canvas: CGSize) {
        self.layers = layers
        self.canvas = canvas
    }

    /// Paints every drawing visible at `time` above `frame` (always the topmost thing in the frame).
    func compose(over initial: CIImage, at time: Double, canvas extent: CGRect) -> CIImage {
        var frame = initial
        for layer in NativeDrawingPainter.activeLayers(layers, at: time) {
            let alpha = NativeDrawingPainter.fadeAlpha(of: layer, at: time)
            guard alpha > 0 else { continue }
            for pass in passes(for: layer, at: time) {
                var ink = CIImage(cgImage: pass.image)
                    .transformed(by: CGAffineTransform(translationX: pass.origin.x, y: canvas.height - pass.origin.y - CGFloat(pass.image.height)))
                if alpha < 1 {
                    ink = ink.applyingFilter("CIColorMatrix", parameters: ["inputAVector": CIVector(x: 0, y: 0, z: 0, w: alpha)])
                }
                if pass.multiply {
                    frame = ink.applyingFilter("CIMultiplyBlendMode", parameters: [kCIInputBackgroundImageKey: frame]).cropped(to: extent)
                } else {
                    frame = ink.composited(over: frame).cropped(to: extent)
                }
            }
        }
        return frame
    }

    private func passes(for layer: PortableDrawingLayer, at time: Double) -> [NativeDrawingPainter.Pass] {
        let progress = NativeDrawingPainter.revealProgress(of: layer, at: time)
        guard progress >= 1 else { return NativeDrawingPainter.passes(for: layer, progress: progress, canvas: canvas) }
        lock.lock()
        if let cached = settled[layer.id] { lock.unlock(); return cached }
        lock.unlock()
        let rendered = NativeDrawingPainter.passes(for: layer, progress: 1, canvas: canvas)
        lock.lock(); settled[layer.id] = rendered; lock.unlock()
        return rendered
    }
}
#endif
