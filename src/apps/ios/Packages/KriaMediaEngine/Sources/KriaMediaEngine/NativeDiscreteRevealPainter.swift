#if canImport(AVFoundation)
import Foundation
import CoreImage

/// Keeps one partial bitmap, replaced only when text or cursor visibility changes.
/// Settled frames reuse the complete bitmap owned by RecipeTextLayer.
final class NativeDiscreteRevealPainter: @unchecked Sendable {
    let layer: PortableTextLayer
    let canvas: CGSize
    let bounds: CGRect
    let assetURLs: [String: URL]
    let maxBitmapBytes: Int
    private let lock = NSLock()
    private var lastSample: TextRevealSample?
    private var lastImage: CIImage?

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, bounds: CGRect, maxBitmapBytes: Int) {
        self.layer = layer; self.assetURLs = assetURLs; self.canvas = canvas; self.bounds = bounds; self.maxBitmapBytes = maxBitmapBytes
    }

    static func paintingLayer(_ layer: PortableTextLayer, runs: [PositionedTextRun]) -> PortableTextLayer {
        // A non-static marker preserves offscreen ink bounds. The compositor
        // applies the original motion once, after this local bitmap is painted.
        PortableTextLayer(id: layer.id, start: layer.start, end: layer.end, anchorX: layer.anchorX, anchorY: layer.anchorY,
                          rotationDegrees: layer.rotationDegrees, runs: runs, effect: .fadeIn)
    }

    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let content = layer.discreteReveal, let effect = PortableTextEffect(rawValue: layer.effect.rawValue) else { throw RecipeError.invalidTimeline }
        let sample = try TextRevealTiming.sample(effect: effect, text: content.text, localTime: localTime,
                                                start: layer.start, schedule: content.schedule, motion: layer.motion)
        if sample.visibleText.unicodeScalars.elementsEqual(TextRevealTiming.normalized(content.text).unicodeScalars), !sample.showCursor { return settled }
        lock.lock(); defer { lock.unlock() }
        if sample == lastSample, let image = lastImage { return image }
        let runs = content.visibleRuns(layer.runs, sample: sample)
        let image = try RecipeTextLayer.make(Self.paintingLayer(layer, runs: runs), assetURLs: assetURLs, canvas: canvas,
                                            maxBitmapBytes: maxBitmapBytes, fixedBounds: bounds).image
        lastSample = sample; lastImage = image
        return image
    }
}

extension RecipeTextLayer {
    static func makeDiscreteReveal(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        guard let content = layer.discreteReveal else { throw RecipeError.invalidTimeline }
        try content.validate(runs: layer.runs)
        var boundingRuns = layer.runs
        for line in content.lines {
            for offset in [line.cursorOffsets.min()!, line.cursorOffsets.max()!] {
                let cursor = line.cursorRun
                boundingRuns.append(cursor.replacing(text: cursor.text, x: cursor.x + offset, glyphs: cursor.glyphs))
            }
        }
        let bounds = try make(NativeDiscreteRevealPainter.paintingLayer(layer, runs: boundingRuns), assetURLs: assetURLs,
                              canvas: canvas, maxBitmapBytes: maxBitmapBytes / 2).frame
        let settled = try make(NativeDiscreteRevealPainter.paintingLayer(layer, runs: layer.runs), assetURLs: assetURLs,
                               canvas: canvas, maxBitmapBytes: maxBitmapBytes / 2, fixedBounds: bounds)
        let painter = NativeDiscreteRevealPainter(layer: layer, assetURLs: assetURLs, canvas: canvas, bounds: bounds, maxBitmapBytes: maxBitmapBytes / 2)
        return Self(image: settled.image, frame: bounds, start: layer.start, end: layer.end, animation: .none, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), discreteReveal: painter)
    }
}
#endif
