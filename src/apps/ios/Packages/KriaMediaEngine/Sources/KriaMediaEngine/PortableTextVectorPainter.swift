#if canImport(AVFoundation)
import Foundation
import CoreText
import CoreImage

/// Retains resolved font/glyph geometry so vector text can be painted at output scale.
struct PortableTextVectorPainter: @unchecked Sendable {
    private let layer: PortableTextLayer
    private let canvas: CGSize
    private let runs: [Run]
    private let imageContext = CIContext(options: [.cacheIntermediates: false, .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
    let anchor: CGPoint
    let rotation: CGAffineTransform
    let bounds: CGRect
    struct Run {
        let line: CTLine; let stroke: CTLine?; let mask: CTLine; let origin: CGPoint
        let blurs: [TextBlurLayer]; let gradient: TextGradient?
        let font: CTFont; let glyphs: [CGGlyph]?; let positions: [CGPoint]
        let fill: CGColor; let strokeColor: CGColor; let strokeWidth: Double
        let outline: CGPath?
        func draw(_ context: CGContext, mode: CGTextDrawingMode = .fill, isMask: Bool = false, opacity: Double = 1) {
            if let outline {
                context.setFillColor(isMask ? CGColor(gray: 1, alpha: 1) : fill.copy(alpha: floor(fill.alpha * opacity * 255) / 255)!)
                context.setStrokeColor(strokeColor.copy(alpha: floor(strokeColor.alpha * opacity * 255) / 255)!)
                context.setLineWidth(strokeWidth)
                context.setLineJoin(.round)
                context.addPath(outline)
                if mode == .clip { context.clip() }
                else if mode == .stroke { context.strokePath() }
                else { context.fillPath() }
                return
            }
            context.setTextDrawingMode(mode)
            context.textPosition = origin
            if let glyphs {
                context.textPosition = .zero
                context.setFillColor(isMask ? CGColor(gray: 1, alpha: 1) : fill)
                context.setStrokeColor(strokeColor)
                context.setLineWidth(strokeWidth)
                context.setLineJoin(.round)
                CTFontDrawGlyphs(font, glyphs, positions, glyphs.count, context)
            } else if mode == .stroke {
                if let stroke { CTLineDraw(stroke, context) }
            } else { CTLineDraw(isMask ? mask : line, context) }
        }
    }

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, outlineGlyphs: Bool = false) throws {
        self.layer = layer
        self.canvas = canvas
        anchor = CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY)
        rotation = CGAffineTransform(translationX: -anchor.x, y: -anchor.y)
            .concatenating(CGAffineTransform(rotationAngle: -layer.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: anchor.x, y: anchor.y))
        var runs: [Run] = []
        var bounds = CGRect.null
        for run in layer.runs {
            guard run.shaped || run.glyphs != nil else { throw MediaEngineError.unsupportedCapability }
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
            for item in (run.glyphs == nil ? CTLineGetGlyphRuns(line) as! [CTRun] : []) {
                let resolved = (CTRunGetAttributes(item) as NSDictionary)[kCTFontAttributeName] as! CTFont
                guard CFEqual(CTFontCopyGraphicsFont(resolved, nil), graphicsFont) else {
                    throw MediaEngineError.unsupportedCapability
                }
                var glyphs = [CGGlyph](repeating: 0, count: CTRunGetGlyphCount(item))
                CTRunGetGlyphs(item, CFRange(location: 0, length: 0), &glyphs)
                guard !glyphs.contains(0) else { throw MediaEngineError.unsupportedCapability }
            }
            let origin = CGPoint(x: run.x, y: canvas.height - run.baselineY)
            var inkBounds = CTLineGetImageBounds(line, nil).offsetBy(dx: origin.x, dy: origin.y)
            var glyphIDs: [CGGlyph]? = nil
            var positions: [CGPoint] = []
            if let glyphs = run.glyphs {
                guard !glyphs.isEmpty, glyphs.allSatisfy({ $0.glyphID > 0 && $0.glyphID < graphicsFont.numberOfGlyphs }) else {
                    throw MediaEngineError.unsupportedCapability
                }
                let ids = glyphs.map { CGGlyph($0.glyphID) }
                positions = glyphs.map { CGPoint(x: origin.x + $0.x, y: origin.y - $0.y) }
                var boxes = [CGRect](repeating: .zero, count: ids.count)
                CTFontGetBoundingRectsForGlyphs(font, .default, ids, &boxes, ids.count)
                inkBounds = zip(boxes, positions).reduce(CGRect.null) { $0.union($1.0.offsetBy(dx: $1.1.x, dy: $1.1.y)) }
                glyphIDs = ids
            }
            var box = inkBounds
            box = box.insetBy(dx: -run.strokeWidth - 2, dy: -run.strokeWidth - 2).applying(rotation)
            bounds = bounds.union(box)
            for blur in run.blurLayers {
                bounds = bounds.union(inkBounds.offsetBy(dx: blur.dx, dy: -blur.dy)
                    .insetBy(dx: -ceil(3 * blur.sigma) - 2, dy: -ceil(3 * blur.sigma) - 2).applying(rotation))
            }
            var outline: CGPath?
            if outlineGlyphs {
                let path = CGMutablePath()
                func append(_ ids: [CGGlyph], _ locations: [CGPoint]) {
                    for (id, point) in zip(ids, locations) {
                        if let glyphPath = CTFontCreatePathForGlyph(font, id, nil) {
                            path.addPath(glyphPath, transform: CGAffineTransform(translationX: point.x, y: point.y))
                        }
                    }
                }
                if let glyphIDs { append(glyphIDs, positions) }
                else {
                    for item in CTLineGetGlyphRuns(line) as! [CTRun] {
                        let count = CTRunGetGlyphCount(item)
                        var ids = [CGGlyph](repeating: 0, count: count)
                        var points = [CGPoint](repeating: .zero, count: count)
                        CTRunGetGlyphs(item, CFRange(location: 0, length: 0), &ids)
                        CTRunGetPositions(item, CFRange(location: 0, length: 0), &points)
                        append(ids, points.map { CGPoint(x: $0.x + origin.x, y: $0.y + origin.y) })
                    }
                }
                outline = path
            }
            runs.append(Run(line: line, stroke: stroke, mask: mask, origin: origin, blurs: run.blurLayers, gradient: run.gradient, font: font, glyphs: glyphIDs, positions: positions,
                            fill: run.fill.cgColor, strokeColor: run.stroke.cgColor, strokeWidth: run.strokeWidth, outline: outline))
        }
        // Moving/scaling text can enter the canvas from an offscreen position.
        // Keep its complete bitmap, still subject to the aggregate memory budget.
        if layer.motion == nil && (layer.effect == .static || layer.effect == .none) && layer.runs.allSatisfy({ $0.blurLayers.isEmpty }) { bounds = bounds.intersection(CGRect(origin: .zero, size: canvas)) }
        self.bounds = bounds.integral
        self.runs = runs
    }

    private func drawScaledShadow(run: Run, blur: TextBlurLayer, context: CGContext,
                                  bounds: CGRect, transform: CGAffineTransform, maxBitmapBytes: Int, opacity: Double) throws {
        // SkBlurMaskFilterImpl::computeXformedSigma caps device-space sigma at 128.
        // This also bounds the off-canvas source mask needed for visible shadow pixels.
        let sigma = min(128, blur.sigma * hypot(transform.a, transform.b))
        // SkDraw::compute_mask_bounds caps the source-mask clip outset at 128.
        let padding = min(128, ceil(3 * sigma) + 2)
        let maskBounds = bounds.insetBy(dx: -padding, dy: -padding).integral
        guard maskBounds.width * maskBounds.height * 4 <= Double(maxBitmapBytes),
              let mask = CGContext(data: nil, width: Int(maskBounds.width), height: Int(maskBounds.height),
                  bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { throw MediaEngineError.unsupportedCapability }
        mask.translateBy(x: -maskBounds.minX, y: -maskBounds.minY)
        mask.concatenate(transform)
        mask.concatenate(rotation)
        mask.translateBy(x: blur.dx, y: -blur.dy)
        run.draw(mask, isMask: true)
        guard let image = mask.makeImage() else { throw MediaEngineError.exportFailed }
        let tint = TextInk(red: blur.color.red, green: blur.color.green, blue: blur.color.blue,
            alpha: floor(blur.color.alpha * pow(opacity, Double(blur.alphaPower)) * 255) / 255)
        var shadow = tint.tint(mask: CIImage(cgImage: image))
        if sigma >= 2 {
            // SkMaskBlurFilter uses two equal boxes and an odd-width third.
            // CIBoxBlur's inputRadius is the full kernel width (measured on a
            // half-plane), despite its name. Half that value gives narrow tails.
            let window = max(1, Int(floor(sigma * 3 * sqrt(2 * Double.pi) / 4 + 0.5)))
            for size in [window, window, window.isMultiple(of: 2) ? window + 1 : window] {
                shadow = shadow.applyingFilter("CIBoxBlur", parameters: ["inputRadius": Double(size)])
            }
        } else if sigma > 0 {
            shadow = shadow.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": sigma])
        }
        let crop = CGRect(x: bounds.minX - maskBounds.minX, y: bounds.minY - maskBounds.minY,
                          width: bounds.width, height: bounds.height)
        guard let output = imageContext.createCGImage(shadow, from: crop) else { throw MediaEngineError.exportFailed }
        context.draw(output, in: bounds)
    }

    func image(bounds requestedBounds: CGRect? = nil, maxBitmapBytes: Int, transform: CGAffineTransform = .identity, clip: CGPath? = nil, opacity: Double = 1) throws -> CIImage {
        let bounds = requestedBounds ?? self.bounds
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0,
              bounds.width * bounds.height * 4 <= Double(maxBitmapBytes),
              let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height), bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.unsupportedCapability
        }
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        if let clip { context.addPath(clip); context.clip() }
        let maskBounds = transform.isIdentity ? bounds : self.bounds
        guard maskBounds.width * maskBounds.height * 4 <= Double(maxBitmapBytes) else { throw MediaEngineError.unsupportedCapability }
        for run in runs {
            if !run.blurs.isEmpty && run.outline != nil {
                for blur in run.blurs {
                    try drawScaledShadow(run: run, blur: blur, context: context, bounds: bounds,
                                         transform: transform, maxBitmapBytes: maxBitmapBytes, opacity: opacity)
                }
            } else if !run.blurs.isEmpty {
                guard let maskContext = CGContext(data: nil, width: Int(maskBounds.width), height: Int(maskBounds.height), bitsPerComponent: 8,
                    bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
                    throw MediaEngineError.exportFailed
                }
                maskContext.translateBy(x: -maskBounds.minX, y: -maskBounds.minY)
                maskContext.concatenate(rotation)
                run.draw(maskContext, isMask: true)
                guard let maskImage = maskContext.makeImage() else { throw MediaEngineError.exportFailed }
                for blur in run.blurs {
                    let angle = -layer.rotationDegrees * .pi / 180
                    let dx = blur.dx * cos(angle) + blur.dy * sin(angle)
                    let dy = blur.dx * sin(angle) - blur.dy * cos(angle)
                    var shadow = blur.color.tint(mask: CIImage(cgImage: maskImage))
                    if blur.sigma > 0 { shadow = shadow.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": blur.sigma]) }
                    shadow = shadow.transformed(by: CGAffineTransform(translationX: dx, y: dy))
                    if !transform.isIdentity {
                        // Blur is continuous under uniform scaling. Keep the soft mask in
                        // source coordinates while rasterizing foreground glyphs at final scale.
                        shadow = shadow.transformed(by: CGAffineTransform(translationX: maskBounds.minX, y: maskBounds.minY))
                            .transformed(by: transform)
                            .transformed(by: CGAffineTransform(translationX: -bounds.minX, y: -bounds.minY))
                    }
                    guard let cgShadow = imageContext.createCGImage(shadow, from: CGRect(origin: .zero, size: bounds.size)) else {
                        throw MediaEngineError.exportFailed
                    }
                    context.draw(cgShadow, in: bounds)
                }
            }
            context.saveGState()
            context.concatenate(transform)
            context.concatenate(rotation)
            if run.strokeWidth > 0 { run.draw(context, mode: .stroke, opacity: opacity) }
            if let gradient = run.gradient {
                run.draw(context, mode: .clip, isMask: true)
                guard let cgGradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                    colors: gradient.stops.map { $0.color.cgColor } as CFArray, locations: gradient.stops.map { CGFloat($0.position) }) else {
                    throw MediaEngineError.unsupportedCapability
                }
                context.setAlpha(floor(opacity * 255) / 255)
                context.drawLinearGradient(cgGradient,
                    start: CGPoint(x: gradient.startX, y: canvas.height - gradient.startY),
                    end: CGPoint(x: gradient.endX, y: canvas.height - gradient.endY),
                    options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
            } else { run.draw(context, opacity: opacity) }
            context.restoreGState()
        }
        guard let image = context.makeImage() else { throw MediaEngineError.exportFailed }
        return CIImage(cgImage: image)
    }
}
#endif
