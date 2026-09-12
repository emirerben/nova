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
                          rotationDegrees: layer.rotationDegrees, runs: runs, effect: .fadeIn,
                          background: runs.isEmpty ? nil : layer.background)
    }

    func image(localTime: Double, settled: CIImage) throws -> CIImage {
        guard let content = layer.discreteReveal, let effect = PortableTextEffect(rawValue: layer.effect.rawValue) else { throw RecipeError.invalidTimeline }
        let sample: TextRevealSample
        if let phases = layer.animationPhases {
            let state = try phases.sample(time: localTime, duration: layer.end - layer.start)
            sample = TextRevealSample(visibleText: TextAnimationPhases.visiblePrefix(content.text, progress: state.revealProgress), showCursor: false, cursorStyle: .none)
        } else {
            sample = try TextRevealTiming.sample(effect: effect, text: content.text, localTime: localTime,
                                                start: layer.start, schedule: content.schedule, motion: layer.motion)
        }
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
        let geometry = try PortableTextVectorPainter(layer: NativeDiscreteRevealPainter.paintingLayer(layer, runs: boundingRuns),
            assetURLs: assetURLs, canvas: canvas)
        var bounds = geometry.bounds
        if let phases = layer.animationPhases {
            // A huge authored font can extend far outside the viewport. Retain
            // every pixel that any phase can bring onscreen, not an unbounded
            // offscreen bitmap (the settled and partial caches each own one).
            let minScale = (phases.entrance == .pop || phases.exit == .pop ? 0.49 : 1) * (phases.loop == .pulse ? 0.96 : 1)
            let maxScale = phases.loop == .pulse ? 1.04 : 1
            let maxX = phases.entrance == .slide || phases.exit == .slide ? 80.0 : 0
            let minY = phases.loop == .bounce ? -12.0 : phases.loop == .float ? -8.0 : 0
            let maxY = phases.loop == .float ? 8.0 : 0
            let anchor = geometry.anchor
            let angle = -layer.rotationDegrees * .pi / 180
            var visible = CGRect.null
            for scale in [minScale, maxScale] {
                for x in [0, maxX] {
                    for y in [minY, maxY] {
                        let dx = x * cos(angle) + y * sin(angle)
                        let dy = x * sin(angle) - y * cos(angle)
                        let inverse = CGAffineTransform(translationX: -anchor.x - dx, y: -anchor.y - dy)
                            .concatenating(CGAffineTransform(scaleX: 1 / scale, y: 1 / scale))
                            .concatenating(CGAffineTransform(translationX: anchor.x, y: anchor.y))
                        visible = visible.union(CGRect(origin: .zero, size: canvas).applying(inverse))
                    }
                }
            }
            bounds = bounds.intersection(visible.insetBy(dx: -2, dy: -2)).integral
        }
        let settled = try make(NativeDiscreteRevealPainter.paintingLayer(layer, runs: layer.runs), assetURLs: assetURLs,
                               canvas: canvas, maxBitmapBytes: maxBitmapBytes / 2, fixedBounds: bounds)
        let painter = NativeDiscreteRevealPainter(layer: layer, assetURLs: assetURLs, canvas: canvas, bounds: bounds, maxBitmapBytes: maxBitmapBytes / 2)
        return Self(image: settled.image, frame: bounds, start: layer.start, end: layer.end, animation: .none, selectionBounds: settled.selectionBounds, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), discreteReveal: painter)
    }
}
#endif
