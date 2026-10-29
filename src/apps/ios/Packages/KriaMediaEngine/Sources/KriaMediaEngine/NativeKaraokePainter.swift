#if canImport(AVFoundation)
import Foundation
import CoreImage

/// Two bounded glyph images per word; seeking selects colors independently of
/// playback history. The draw order also preserves overlapping word shadows.
final class NativeKaraokePainter: @unchecked Sendable {
    struct Word {
        let start: Double
        let primary: RecipeTextLayer
        let highlighted: RecipeTextLayer
    }
    let activeOnly: Bool
    let words: [Word]
    let bounds: CGRect
    let bitmapBytes: Int
    init(words: [Word], bounds: CGRect, bitmapBytes: Int, activeOnly: Bool = false) {
        self.activeOnly = activeOnly
        self.words = words; self.bounds = bounds; self.bitmapBytes = bitmapBytes
    }
    func image(localTime: Double) -> CIImage {
        let extent = CGRect(origin: .zero, size: bounds.size)
        var image = CIImage(color: .clear).cropped(to: extent)
        let active = words.lastIndex(where: { localTime >= $0.start })
        for (index, word) in words.enumerated() {
            let highlighted = activeOnly ? active == index : localTime >= word.start
            let bitmap = highlighted ? word.highlighted : word.primary
            let placed = bitmap.image.transformed(by: CGAffineTransform(
                translationX: bitmap.frame.minX - bounds.minX, y: bitmap.frame.minY - bounds.minY))
            image = placed.composited(over: image).cropped(to: extent)
        }
        return image
    }
}

/// `KaraokeContent.singleWord`: one word on screen at a time (see the wire contract on `KaraokeContent`).
/// A word's bitmap is painted when it first becomes the active word and only the current one is kept, so a cue
/// activates for the cost of one word (like a one-word layer) instead of two bitmaps per word.
final class NativeSingleWordPainter: @unchecked Sendable {
    private let layer: PortableTextLayer
    private let content: KaraokeContent
    private let assetURLs: [String: URL]
    private let canvas: CGSize
    private let maxBitmapBytes: Int
    private let lock = NSLock()
    private var current: (index: Int, bitmap: RecipeTextLayer)?
    private(set) var bitmapBytes = 0

    init(layer: PortableTextLayer, content: KaraokeContent, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) {
        self.layer = layer; self.content = content; self.assetURLs = assetURLs; self.canvas = canvas; self.maxBitmapBytes = maxBitmapBytes
    }

    /// The index of the word shown at a layer-local time, or nil before the first word starts.
    func activeIndex(localTime: Double) -> Int? { content.starts.lastIndex(where: { localTime >= $0 }) }

    /// The layer-local window of the word shown at `localTime`. The layer's animation phases play on each
    /// word's window, because every word is its own caption.
    func window(localTime: Double) -> (start: Double, end: Double)? {
        activeIndex(localTime: localTime).map(window)
    }

    private func window(_ index: Int) -> (start: Double, end: Double) {
        (content.starts[index], index + 1 < content.starts.count ? content.starts[index + 1] : layer.end - layer.start)
    }

    private func bitmap(for index: Int) throws -> RecipeTextLayer {
        if let current, current.index == index { return current.bitmap }
        let run = layer.runs[index]
        let drawn = PositionedTextRun(text: run.text, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
            x: run.x, baselineY: run.baselineY, letterSpacing: run.letterSpacing, shaped: run.shaped,
            fill: content.highlight, stroke: run.stroke, strokeWidth: run.strokeWidth,
            blurLayers: run.blurLayers, glyphs: run.glyphs, fontVariations: run.fontVariations)
        var painting = NativeDiscreteRevealPainter.paintingLayer(layer, runs: [drawn])
        if let phases = layer.animationPhases, phases.entrance.isDesigned || phases.exit.isDesigned {
            let (start, end) = window(index)
            painting = PortableTextLayer(id: layer.id, start: layer.start + start, end: layer.start + end,
                anchorX: layer.anchorX, anchorY: layer.anchorY, rotationDegrees: layer.rotationDegrees, runs: [drawn],
                effect: .fadeIn, background: layer.background)
            painting.animationPhases = phases
        }
        let made = try RecipeTextLayer.make(painting, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes)
        current = (index, made)
        bitmapBytes = max(bitmapBytes, made.designed?.bitmapBytes ?? 0, Int(made.frame.width * made.frame.height * 4) * 2)
        return made
    }

    /// The active word at its absolute canvas position (the layer's frame is the whole canvas).
    func image(localTime: Double) throws -> CIImage {
        lock.lock(); defer { lock.unlock() }
        guard let index = activeIndex(localTime: localTime) else { return CIImage.empty() }
        let made = try bitmap(for: index)
        let image = try made.designed?.image(localTime: localTime - window(index).start, settled: made.image) ?? made.image
        return image.transformed(by: CGAffineTransform(translationX: made.frame.minX, y: made.frame.minY))
    }
}

extension RecipeTextLayer {
    static func makeSingleWord(_ layer: PortableTextLayer, content: KaraokeContent, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        let painter = NativeSingleWordPainter(layer: layer, content: content, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes)
        // Paint the first word now so a layer that cannot be painted fails at build time, and to size the cache.
        _ = try painter.image(localTime: content.starts.first ?? 0)
        return Self(image: CIImage.empty(), frame: CGRect(origin: .zero, size: canvas), start: layer.start, end: layer.end,
                    animation: .none, selectionBounds: try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas).selectionBounds,
                    portable: layer, portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), singleWord: painter)
    }

    static func makeKaraoke(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        guard let content = layer.karaoke else { throw RecipeError.invalidTimeline }
        try content.validate(runs: layer.runs)
        if content.singleWord == true {
            return try makeSingleWord(layer, content: content, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: maxBitmapBytes)
        }
        var words: [NativeKaraokePainter.Word] = []
        var retained = 0
        var bounds = CGRect.null
        for (run, start) in zip(layer.runs, content.starts) {
            let highlight = PositionedTextRun(text: run.text, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
                x: run.x, baselineY: run.baselineY, letterSpacing: run.letterSpacing, shaped: run.shaped,
                fill: content.highlight, stroke: run.stroke, strokeWidth: run.strokeWidth,
                blurLayers: run.blurLayers, glyphs: run.glyphs, fontVariations: run.fontVariations)
            var bitmaps: [RecipeTextLayer] = []
            for drawingRun in [run, highlight] {
                let drawing = NativeDiscreteRevealPainter.paintingLayer(layer, runs: [drawingRun])
                let reserve = bounds.isNull ? 0 : Int(bounds.width * bounds.height * 8)
                let bitmap = try make(drawing, assetURLs: assetURLs, canvas: canvas,
                                      maxBitmapBytes: maxBitmapBytes - retained - reserve)
                retained += Int(bitmap.frame.width * bitmap.frame.height * 4)
                bounds = bounds.union(bitmap.frame).integral
                guard retained + Int(bounds.width * bounds.height * 8) <= maxBitmapBytes else {
                    throw NativePreviewFeatureError("NativeKaraokePainter-57")
                }
                bitmaps.append(bitmap)
            }
            words.append(.init(start: start, primary: bitmaps[0], highlighted: bitmaps[1]))
        }
        let painter = NativeKaraokePainter(words: words, bounds: bounds,
                                           bitmapBytes: retained + Int(bounds.width * bounds.height * 8), activeOnly: content.activeOnly == true)
        return Self(image: painter.image(localTime: 0), frame: bounds, start: layer.start, end: layer.end,
                    animation: .none, selectionBounds: try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas).selectionBounds, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), karaoke: painter)
    }
}
#endif
