#if canImport(AVFoundation)
import Foundation
import AVFoundation
import CoreText
import CoreImage

private extension TextInk {
    var cgColor: CGColor { CGColor(colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, components: [red, green, blue, alpha])! }
}

extension RecipeTextLayer {
    /// Baselines and tracking are authored in output pixels. No device-specific
    /// wrap, auto-shrink, font lookup, or substitution occurs here.
    static func make(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int = 64 * 1024 * 1024) throws -> Self {
        struct Run { let line: CTLine; let stroke: CTLine?; let mask: CTLine; let origin: CGPoint; let blurs: [TextBlurLayer]; let gradient: TextGradient? }
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
            var maskAttributes = attributes
            maskAttributes[NSAttributedString.Key(kCTForegroundColorAttributeName as String)] = CGColor(gray: 1, alpha: 1)
            let mask = CTLineCreateWithAttributedString(NSAttributedString(string: run.text, attributes: maskAttributes))
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
            let inkBounds = CTLineGetImageBounds(mask, nil).offsetBy(dx: origin.x, dy: origin.y)
            for blur in run.blurLayers {
                bounds = bounds.union(inkBounds.offsetBy(dx: blur.dx, dy: -blur.dy)
                    .insetBy(dx: -ceil(3 * blur.sigma) - 2, dy: -ceil(3 * blur.sigma) - 2).applying(rotation))
            }
            runs.append(Run(line: line, stroke: stroke, mask: mask, origin: origin, blurs: run.blurLayers, gradient: run.gradient))
        }
        // Moving/scaling text can enter the canvas from an offscreen position.
        // Keep its complete bitmap, still subject to the aggregate memory budget.
        if layer.motion == nil && layer.runs.allSatisfy({ $0.blurLayers.isEmpty }) { bounds = bounds.intersection(CGRect(origin: .zero, size: canvas)) }
        bounds = bounds.integral
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0,
              bounds.width * bounds.height * 4 <= Double(maxBitmapBytes),
              let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height), bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.unsupportedCapability
        }
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        let imageContext = CIContext(options: [.cacheIntermediates: false])
        for run in runs {
            if !run.blurs.isEmpty {
                guard let maskContext = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height), bitsPerComponent: 8,
                    bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
                    throw MediaEngineError.exportFailed
                }
                maskContext.translateBy(x: -bounds.minX, y: -bounds.minY)
                maskContext.concatenate(rotation)
                maskContext.textPosition = run.origin
                CTLineDraw(run.mask, maskContext)
                guard let maskImage = maskContext.makeImage() else { throw MediaEngineError.exportFailed }
                for blur in run.blurs {
                    let angle = -layer.rotationDegrees * .pi / 180
                    let dx = blur.dx * cos(angle) + blur.dy * sin(angle)
                    let dy = blur.dx * sin(angle) - blur.dy * cos(angle)
                    var shadow = CIImage(cgImage: maskImage).applyingFilter("CIColorMatrix", parameters: [
                        "inputRVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.red * blur.color.alpha),
                        "inputGVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.green * blur.color.alpha),
                        "inputBVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.blue * blur.color.alpha),
                        "inputAVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.alpha)
                    ])
                    if blur.sigma > 0 { shadow = shadow.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": blur.sigma]) }
                    shadow = shadow.transformed(by: CGAffineTransform(translationX: dx, y: dy))
                    guard let cgShadow = imageContext.createCGImage(shadow, from: CGRect(origin: .zero, size: bounds.size)) else {
                        throw MediaEngineError.exportFailed
                    }
                    context.draw(cgShadow, in: bounds)
                }
            }
            context.saveGState()
            context.concatenate(rotation)
            context.textPosition = run.origin
            if let stroke = run.stroke { CTLineDraw(stroke, context) }
            context.textPosition = run.origin
            if let gradient = run.gradient {
                context.setTextDrawingMode(.clip)
                CTLineDraw(run.mask, context)
                guard let cgGradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                    colors: gradient.stops.map { $0.color.cgColor } as CFArray, locations: gradient.stops.map { CGFloat($0.position) }) else {
                    throw MediaEngineError.unsupportedCapability
                }
                context.drawLinearGradient(cgGradient,
                    start: CGPoint(x: gradient.startX, y: canvas.height - gradient.startY),
                    end: CGPoint(x: gradient.endX, y: canvas.height - gradient.endY),
                    options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
            } else { CTLineDraw(run.line, context) }
            context.restoreGState()
        }
        guard let image = context.makeImage() else { throw MediaEngineError.exportFailed }
        return Self(image: CIImage(cgImage: image), frame: bounds, start: layer.start, end: layer.end, animation: .none, portable: layer, portableAnchor: anchor)
    }
}
#endif
