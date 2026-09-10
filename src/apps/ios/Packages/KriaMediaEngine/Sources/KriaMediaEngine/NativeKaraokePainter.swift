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
    let words: [Word]
    let bounds: CGRect
    let bitmapBytes: Int
    init(words: [Word], bounds: CGRect, bitmapBytes: Int) {
        self.words = words; self.bounds = bounds; self.bitmapBytes = bitmapBytes
    }
    func image(localTime: Double) -> CIImage {
        let extent = CGRect(origin: .zero, size: bounds.size)
        var image = CIImage(color: .clear).cropped(to: extent)
        for word in words {
            let bitmap = localTime >= word.start ? word.highlighted : word.primary
            let placed = bitmap.image.transformed(by: CGAffineTransform(
                translationX: bitmap.frame.minX - bounds.minX, y: bitmap.frame.minY - bounds.minY))
            image = placed.composited(over: image).cropped(to: extent)
        }
        return image
    }
}

extension RecipeTextLayer {
    static func makeKaraoke(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        guard let content = layer.karaoke else { throw RecipeError.invalidTimeline }
        try content.validate(runs: layer.runs)
        var words: [NativeKaraokePainter.Word] = []
        var retained = 0
        var bounds = CGRect.null
        for (run, start) in zip(layer.runs, content.starts) {
            let highlight = PositionedTextRun(text: run.text, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
                x: run.x, baselineY: run.baselineY, letterSpacing: run.letterSpacing, shaped: false,
                fill: content.highlight, stroke: run.stroke, strokeWidth: run.strokeWidth,
                blurLayers: run.blurLayers, glyphs: run.glyphs)
            var bitmaps: [RecipeTextLayer] = []
            for drawingRun in [run, highlight] {
                let drawing = NativeDiscreteRevealPainter.paintingLayer(layer, runs: [drawingRun])
                let reserve = bounds.isNull ? 0 : Int(bounds.width * bounds.height * 8)
                let bitmap = try make(drawing, assetURLs: assetURLs, canvas: canvas,
                                      maxBitmapBytes: maxBitmapBytes - retained - reserve)
                retained += Int(bitmap.frame.width * bitmap.frame.height * 4)
                bounds = bounds.union(bitmap.frame).integral
                guard retained + Int(bounds.width * bounds.height * 8) <= maxBitmapBytes else {
                    throw MediaEngineError.unsupportedCapability
                }
                bitmaps.append(bitmap)
            }
            words.append(.init(start: start, primary: bitmaps[0], highlighted: bitmaps[1]))
        }
        let painter = NativeKaraokePainter(words: words, bounds: bounds,
                                           bitmapBytes: retained + Int(bounds.width * bounds.height * 8))
        return Self(image: painter.image(localTime: 0), frame: bounds, start: layer.start, end: layer.end,
                    animation: .none, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), karaoke: painter)
    }
}
#endif
