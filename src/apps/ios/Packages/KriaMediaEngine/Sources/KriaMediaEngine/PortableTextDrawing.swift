#if canImport(AVFoundation)
import Foundation
import AVFoundation
import CoreText
import CoreImage

private extension TextInk {
    var cgColor: CGColor { CGColor(red: red, green: green, blue: blue, alpha: alpha) }
}

extension RecipeTextLayer {
    /// Baselines and tracking are authored in output pixels. No device-specific
    /// wrap, auto-shrink, font lookup, or substitution occurs here.
    static func make(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int = 64 * 1024 * 1024) throws -> Self {
        struct Run { let line: CTLine; let stroke: CTLine?; let origin: CGPoint }
        let anchor = CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY)
        let rotation = CGAffineTransform(translationX: -anchor.x, y: -anchor.y)
            .concatenating(CGAffineTransform(rotationAngle: -layer.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: anchor.x, y: anchor.y))
        var runs: [Run] = []
        var bounds = CGRect.null
        for run in layer.runs {
            guard run.shaped else { throw MediaEngineError.unsupportedCapability }
            guard let url = assetURLs[run.fontAssetID],
                  let provider = CGDataProvider(url: url as CFURL), let graphicsFont = CGFont(provider) else {
                throw MediaEngineError.missingAsset(run.fontAssetID)
            }
            let font = CTFontCreateWithGraphicsFont(graphicsFont, run.fontSize, nil, nil)
            let attributes: [NSAttributedString.Key: Any] = [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorAttributeName as String): run.fill.cgColor,
                NSAttributedString.Key(kCTKernAttributeName as String): run.letterSpacing,
                NSAttributedString.Key(kCTLigatureAttributeName as String): run.shaped ? 1 : 0
            ]
            let line = CTLineCreateWithAttributedString(NSAttributedString(string: run.text, attributes: attributes))
            var strokeAttributes = attributes
            strokeAttributes[NSAttributedString.Key(kCTStrokeColorAttributeName as String)] = run.stroke.cgColor
            strokeAttributes[NSAttributedString.Key(kCTStrokeWidthAttributeName as String)] = 100 * run.strokeWidth / run.fontSize
            let stroke = run.strokeWidth > 0 ? CTLineCreateWithAttributedString(NSAttributedString(string: run.text, attributes: strokeAttributes)) : nil
            // Fallback changes typography and is not licensed by this manifest.
            for item in CTLineGetGlyphRuns(line) as! [CTRun] {
                let resolved = (CTRunGetAttributes(item) as NSDictionary)[kCTFontAttributeName] as! CTFont
                guard CFEqual(CTFontCopyGraphicsFont(resolved, nil), graphicsFont) else {
                    throw MediaEngineError.unsupportedCapability
                }
                var glyphs = [CGGlyph](repeating: 0, count: CTRunGetGlyphCount(item))
                CTRunGetGlyphs(item, CFRange(location: 0, length: 0), &glyphs)
                guard !glyphs.contains(0) else { throw MediaEngineError.unsupportedCapability }
            }
            let origin = CGPoint(x: run.x, y: canvas.height - run.baselineY)
            var box = CTLineGetImageBounds(line, nil).offsetBy(dx: origin.x, dy: origin.y)
            box = box.insetBy(dx: -run.strokeWidth - 2, dy: -run.strokeWidth - 2).applying(rotation)
            bounds = bounds.union(box)
            runs.append(Run(line: line, stroke: stroke, origin: origin))
        }
        bounds = bounds.intersection(CGRect(origin: .zero, size: canvas)).integral
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0,
              bounds.width * bounds.height * 4 <= Double(maxBitmapBytes),
              let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height), bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.unsupportedCapability
        }
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        context.concatenate(rotation)
        for run in runs {
            context.textPosition = run.origin
            if let stroke = run.stroke { CTLineDraw(stroke, context) }
            context.textPosition = run.origin
            CTLineDraw(run.line, context)
        }
        guard let image = context.makeImage() else { throw MediaEngineError.exportFailed }
        return Self(image: CIImage(cgImage: image), frame: bounds, start: layer.start, end: layer.end, animation: .none)
    }
}
#endif
