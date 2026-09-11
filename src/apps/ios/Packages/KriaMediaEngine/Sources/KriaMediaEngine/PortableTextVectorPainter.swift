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
    private static let glyphTintKernel: Result<CIKernel, Error> = Result {
        let kernels = try CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        [[stitchable]] float4 kriaGlyphMaskTint(sampler mask, float4 colorBytes, destination dest) {
            float coverage = round(clamp(mask.sample(mask.transform(dest.coord())).a, 0.0, 1.0) * 255.0);
            return floor(colorBytes * (coverage + 1.0) / 256.0) / 255.0;
        }
        }}
        """)
        guard let kernel = kernels.first else { throw MediaEngineError.unsupportedCapability }
        return kernel
    }
    let anchor: CGPoint
    let rotation: CGAffineTransform
    let bounds: CGRect
    struct Run {
        struct Glyph { let id: CGGlyph; let position: CGPoint; let path: CGPath }
        let outlinedGlyphs: [Glyph]
        let line: CTLine; let stroke: CTLine?; let mask: CTLine; let origin: CGPoint
        let blurs: [TextBlurLayer]; let gradient: TextGradient?
        let font: CTFont; let glyphs: [CGGlyph]?; let positions: [CGPoint]
        let fill: CGColor; let strokeColor: CGColor; let strokeWidth: Double
        let outline: CGPath?
        func draw(_ context: CGContext, mode: CGTextDrawingMode = .fill, isMask: Bool = false, opacity: Double = 1) {
            // Hint ordinary-size glyphs at fractional positions. Above 256px,
            // use outlines to bound the platform glyph cache during a 60× zoom.
            let deviceFontSize = CTFontGetSize(font) * hypot(context.ctm.a, context.ctm.b)
            if let outline, isMask && mode != .clip || deviceFontSize > 256 {
                context.setFillColor(isMask ? CGColor(gray: 1, alpha: 1) : fill.copy(alpha: floor(fill.alpha * opacity * 255) / 255)!)
                context.setStrokeColor(strokeColor.copy(alpha: floor(strokeColor.alpha * opacity * 255) / 255)!)
                context.setLineWidth(strokeWidth); context.setLineJoin(.round)
                context.addPath(outline)
                if mode == .clip { context.clip() }
                else if mode == .stroke { context.strokePath() }
                else { context.fillPath() }
                return
            }
            if mode != .clip { context.saveGState() }
            defer { if mode != .clip { context.restoreGState() } }
            if !isMask, opacity < 1 {
                let paintAlpha = mode == .stroke ? strokeColor.alpha : fill.alpha
                context.setAlpha(paintAlpha > 0 ? floor(paintAlpha * opacity * 255) / 255 / paintAlpha : 0)
            }
            if outline != nil {
                context.setShouldSubpixelPositionFonts(true)
                context.setShouldSubpixelQuantizeFonts(false)
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
            var outlines: [Run.Glyph] = []
            if outlineGlyphs {
                let path = CGMutablePath()
                func append(_ ids: [CGGlyph], _ locations: [CGPoint]) {
                    for (id, point) in zip(ids, locations) {
                        if let glyphPath = CTFontCreatePathForGlyph(font, id, nil) {
                            let placed = CGMutablePath()
                            placed.addPath(glyphPath, transform: CGAffineTransform(translationX: point.x, y: point.y))
                            path.addPath(placed)
                            outlines.append(Run.Glyph(id: id, position: point, path: placed))
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
            runs.append(Run(outlinedGlyphs: outlines, line: line, stroke: stroke, mask: mask, origin: origin, blurs: run.blurLayers, gradient: run.gradient, font: font, glyphs: glyphIDs, positions: positions,
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
        let window = max(1, Int(floor(sigma * 3 * sqrt(2 * Double.pi) / 4 + 0.5)))
        let border = window.isMultiple(of: 2) ? 3 * (window / 2) - 1 : 3 * ((window - 1) / 2)
        let padding = min(128, sigma >= 2 ? Double(border) : ceil(3 * sigma))
        let sourceClip = bounds.insetBy(dx: -padding, dy: -padding).integral
        let placement = CGAffineTransform(translationX: blur.dx, y: -blur.dy)
            .concatenating(rotation).concatenating(transform)
        let tint = TextInk(red: blur.color.red, green: blur.color.green, blue: blur.color.blue,
            alpha: floor(blur.color.alpha * pow(opacity, Double(blur.alphaPower)) * 255) / 255)
        // Skia blurs and blends each glyph mask separately. Combining the line
        // changes overlapping tails and accumulates different byte rounding.
        for glyph in run.outlinedGlyphs {
            let maskBounds = glyph.path.boundingBoxOfPath.applying(placement)
                .insetBy(dx: -0.5, dy: -0.5).integral.intersection(sourceClip)
            if maskBounds.isNull || maskBounds.isEmpty { continue }
            guard maskBounds.width * maskBounds.height * 4 <= Double(maxBitmapBytes),
                  let mask = CGContext(data: nil, width: Int(maskBounds.width), height: Int(maskBounds.height),
                      bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { throw MediaEngineError.unsupportedCapability }
            mask.translateBy(x: -maskBounds.minX, y: -maskBounds.minY)
            mask.concatenate(placement)
            mask.setFillColor(CGColor(gray: 1, alpha: 1))
            if CTFontGetSize(run.font) * hypot(mask.ctm.a, mask.ctm.b) > 256 {
                mask.addPath(glyph.path); mask.fillPath()
            } else {
                mask.setShouldSubpixelPositionFonts(true); mask.setShouldSubpixelQuantizeFonts(false)
                CTFontDrawGlyphs(run.font, [glyph.id], [glyph.position], 1, mask)
            }
            guard let image = mask.makeImage() else { throw MediaEngineError.exportFailed }
            var shadow = CIImage(cgImage: image)
            if sigma >= 2 {
                // CIBoxBlur uses a full kernel width, despite inputRadius's name.
                for size in [window, window, window.isMultiple(of: 2) ? window + 1 : window] {
                    shadow = shadow.applyingFilter("CIBoxBlur", parameters: ["inputRadius": Double(size)])
                }
            } else if sigma > 0 {
                shadow = shadow.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": sigma])
            }
            let alphaByte = Int((tint.alpha * 255).rounded())
            func premultiplied(_ channel: Double) -> CGFloat {
                CGFloat((Int((channel * 255).rounded()) * alphaByte + 127) / 255)
            }
            let color = CIVector(x: premultiplied(tint.red), y: premultiplied(tint.green),
                z: premultiplied(tint.blue), w: CGFloat(alphaByte))
            guard let tinted = try Self.glyphTintKernel.get().apply(extent: shadow.extent,
                roiCallback: { _, rect in rect }, arguments: [shadow, color]) else { throw MediaEngineError.unsupportedCapability }
            shadow = tinted.transformed(by: CGAffineTransform(translationX: maskBounds.minX, y: maskBounds.minY))
            let crop = shadow.extent.intersection(bounds).integral
            if crop.isNull || crop.isEmpty { continue }
            guard let output = imageContext.createCGImage(shadow, from: crop) else { throw MediaEngineError.exportFailed }
            context.draw(output, in: crop)
        }
    }

    func image(bounds requestedBounds: CGRect? = nil, maxBitmapBytes: Int, transform: CGAffineTransform = .identity, clip: CGPath? = nil, runClips: [CGPath?]? = nil, opacity: Double = 1) throws -> CIImage {
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
        guard runClips == nil || runClips?.count == runs.count else { throw RecipeError.invalidTimeline }
        for (index, run) in runs.enumerated() {
            context.saveGState(); defer { context.restoreGState() }
            if let clip = runClips?[index] { context.addPath(clip); context.clip() }
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
