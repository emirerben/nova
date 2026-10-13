#if canImport(AVFoundation)
import Foundation
import CoreText
import CoreImage

/// Retains resolved font/glyph geometry so vector text can be painted at output scale.
struct PortableTextVectorPainter: @unchecked Sendable {
    private struct FontKey: Hashable { let assetID: String; let size: Double; let variations: String }
    private let layer: PortableTextLayer
    private let canvas: CGSize
    private let runs: [Run]
    private let imageContext = CIContext(options: [.cacheIntermediates: false, .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
    let anchor: CGPoint
    let rotation: CGAffineTransform
    let bounds: CGRect
    let selectionBounds: ResolvedTextSelectionBounds
    /// KRI-646: one glyph of a bent run, in the same y-up canvas space as `Run.positions`. `font` is the
    /// glyph's own CTRun font (so a color emoji keeps the OS face). `transform` moves it onto the arc.
    struct BentGlyph { let font: CTFont; let id: CGGlyph; let position: CGPoint; let transform: CGAffineTransform }
    private struct RawGlyph { let font: CTFont; let id: CGGlyph; let position: CGPoint; let advance: Double; let box: CGRect }
    private struct RawRun { let origin: CGPoint; let fontSize: Double; let glyphs: [RawGlyph] }
    struct Run {
        struct Glyph { let id: CGGlyph; let position: CGPoint; let path: CGPath; var transform: CGAffineTransform = .identity }
        let outlinedGlyphs: [Glyph]
        let line: CTLine; let stroke: CTLine?; let mask: CTLine; let origin: CGPoint
        let blurs: [TextBlurLayer]; let gradient: TextGradient?
        // Variable CTFont instances can retain references into the source CGFont
        // tables. Keep that source alive for every deferred draw.
        let sourceFont: CGFont
        let font: CTFont; let glyphs: [CGGlyph]?; let positions: [CGPoint]
        let fill: CGColor; let strokeColor: CGColor; let strokeWidth: Double
        let outline: CGPath?
        /// Non-nil when the layer is bent: every glyph is drawn on its own with its arc transform.
        var bent: [BentGlyph]? = nil
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
            if let bent, mode == .clip {
                // A text clip cannot span several saved/restored draws; clip to the glyph outlines instead.
                let clipPath = CGMutablePath()
                for glyph in bent {
                    guard let path = CTFontCreatePathForGlyph(glyph.font, glyph.id, nil) else { continue }
                    clipPath.addPath(path, transform: CGAffineTransform(translationX: glyph.position.x, y: glyph.position.y)
                        .concatenating(glyph.transform))
                }
                context.addPath(clipPath); context.clip()
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
            if let bent {
                context.textPosition = .zero
                context.setFillColor(isMask ? CGColor(gray: 1, alpha: 1) : fill)
                context.setStrokeColor(strokeColor)
                context.setLineWidth(strokeWidth)
                context.setLineJoin(.round)
                for glyph in bent {
                    context.saveGState()
                    context.concatenate(glyph.transform)
                    CTFontDrawGlyphs(glyph.font, [glyph.id], [glyph.position], 1, context)
                    context.restoreGState()
                }
            } else if let glyphs {
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

    init(layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, outlineGlyphs: Bool = false, preserveOffscreenInk: Bool = false) throws {
        self.layer = layer
        self.canvas = canvas
        anchor = CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY)
        rotation = CGAffineTransform(translationX: -anchor.x, y: -anchor.y)
            .concatenating(CGAffineTransform(rotationAngle: -layer.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: anchor.x, y: anchor.y))
        var runs: [Run] = []
        var fonts: [FontKey: (CGFont, CTFont)] = [:]
        var bounds = CGRect.null
        var inkSelectionBounds = CGRect.null
        let bend: Double? = TextBend.isStraight(layer.bend) ? nil : layer.bend
        var rawRuns: [RawRun] = []
        for run in layer.runs {
            guard run.shaped || run.glyphs != nil else { throw NativePreviewFeatureError("PortableTextVectorPainter-76") }
            let key = FontKey(assetID: run.fontAssetID, size: run.fontSize, variations: run.fontVariations.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: ";"))
            let graphicsFont: CGFont, font: CTFont
            if let cached = fonts[key] { (graphicsFont, font) = cached }
            else {
                guard let url = assetURLs[run.fontAssetID],
                      let provider = CGDataProvider(url: url as CFURL), let graphics = CGFont(provider) else {
                    throw MediaEngineError.missingAsset(run.fontAssetID)
                }
                graphicsFont = graphics
                font = NativeFontIdentity.font(graphics, size: run.fontSize, variations: run.fontVariations)
                fonts[key] = (graphicsFont, font)
            }
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
            // Ordinary text retains the manifest face; color emoji use the OS face.
            var containsEmoji = false
            for item in (run.glyphs == nil ? CTLineGetGlyphRuns(line) as! [CTRun] : []) {
                let resolved = (CTRunGetAttributes(item) as NSDictionary)[kCTFontAttributeName] as! CTFont
                containsEmoji = containsEmoji || NativeFontIdentity.isColorEmoji(resolved)
                guard NativeFontIdentity.matches(resolved, graphics: graphicsFont) || NativeFontIdentity.isColorEmoji(resolved) else {
                    throw NativePreviewFeatureError("PortableTextVectorPainter-109")
                }
                var glyphs = [CGGlyph](repeating: 0, count: CTRunGetGlyphCount(item))
                CTRunGetGlyphs(item, CFRange(location: 0, length: 0), &glyphs)
                guard !glyphs.contains(0) else { throw NativePreviewFeatureError("PortableTextVectorPainter-113") }
            }
            let origin = CGPoint(x: run.x, y: canvas.height - run.baselineY)
            var inkBounds = CTLineGetImageBounds(line, nil).offsetBy(dx: origin.x, dy: origin.y)
            var glyphIDs: [CGGlyph]? = nil
            var positions: [CGPoint] = []
            if let glyphs = run.glyphs {
                guard !glyphs.isEmpty, glyphs.allSatisfy({ $0.glyphID > 0 && $0.glyphID < graphicsFont.numberOfGlyphs }) else {
                    throw NativePreviewFeatureError("PortableTextVectorPainter-121")
                }
                let ids = glyphs.map { CGGlyph($0.glyphID) }
                positions = glyphs.map { CGPoint(x: origin.x + $0.x, y: origin.y - $0.y) }
                var boxes = [CGRect](repeating: .zero, count: ids.count)
                CTFontGetBoundingRectsForGlyphs(font, .default, ids, &boxes, ids.count)
                inkBounds = zip(boxes, positions).reduce(CGRect.null) { $0.union($1.0.offsetBy(dx: $1.1.x, dy: $1.1.y)) }
                glyphIDs = ids
            }
            if bend != nil {
                var raws: [RawGlyph] = []
                if let ids = glyphIDs {
                    var advances = [CGSize](repeating: .zero, count: ids.count)
                    var boxes = [CGRect](repeating: .zero, count: ids.count)
                    CTFontGetAdvancesForGlyphs(font, .horizontal, ids, &advances, ids.count)
                    CTFontGetBoundingRectsForGlyphs(font, .default, ids, &boxes, ids.count)
                    for index in ids.indices {
                        raws.append(RawGlyph(font: font, id: ids[index], position: positions[index], advance: Double(advances[index].width), box: boxes[index]))
                    }
                } else {
                    for item in CTLineGetGlyphRuns(line) as! [CTRun] {
                        let count = CTRunGetGlyphCount(item)
                        guard count > 0 else { continue }
                        let itemFont = (CTRunGetAttributes(item) as NSDictionary)[kCTFontAttributeName] as! CTFont
                        var ids = [CGGlyph](repeating: 0, count: count)
                        var points = [CGPoint](repeating: .zero, count: count)
                        var advances = [CGSize](repeating: .zero, count: count)
                        var boxes = [CGRect](repeating: .zero, count: count)
                        CTRunGetGlyphs(item, CFRange(location: 0, length: 0), &ids)
                        CTRunGetPositions(item, CFRange(location: 0, length: 0), &points)
                        CTRunGetAdvances(item, CFRange(location: 0, length: 0), &advances)
                        CTFontGetBoundingRectsForGlyphs(itemFont, .default, ids, &boxes, count)
                        for index in 0..<count {
                            raws.append(RawGlyph(font: itemFont, id: ids[index],
                                position: CGPoint(x: points[index].x + origin.x, y: points[index].y + origin.y),
                                advance: Double(advances[index].width), box: boxes[index]))
                        }
                    }
                }
                rawRuns.append(RawRun(origin: origin, fontSize: run.fontSize, glyphs: raws))
            }
            inkSelectionBounds = inkSelectionBounds.union(inkBounds.insetBy(dx: -run.strokeWidth / 2, dy: -run.strokeWidth / 2))
            var box = inkBounds
            box = box.insetBy(dx: -run.strokeWidth - 2, dy: -run.strokeWidth - 2).applying(rotation)
            bounds = bounds.union(box)
            for blur in run.blurLayers {
                bounds = bounds.union(inkBounds.offsetBy(dx: blur.dx, dy: -blur.dy)
                    .insetBy(dx: -ceil(3 * blur.sigma) - 2, dy: -ceil(3 * blur.sigma) - 2).applying(rotation))
            }
            var outline: CGPath?
            var outlines: [Run.Glyph] = []
            if outlineGlyphs && !containsEmoji {
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
            runs.append(Run(outlinedGlyphs: outlines, line: line, stroke: stroke, mask: mask, origin: origin, blurs: run.blurLayers, gradient: run.gradient, sourceFont: graphicsFont, font: font, glyphs: glyphIDs, positions: positions,
                            fill: run.fill.cgColor, strokeColor: run.stroke.cgColor, strokeWidth: run.strokeWidth, outline: outline))
        }
        if let bend, rawRuns.count == runs.count, !runs.isEmpty {
            // The arc is block-wide (the widest line sets the radius), so it can only be resolved once
            // every line has been shaped. Redraw each run glyph by glyph and measure the bent ink: the
            // straight-line bounds above would clip the ends of the arc.
            let transforms = TextBend.transforms(lines: rawRuns.map { raw in
                TextBend.Line(baselineY: Double(raw.origin.y), fontSize: raw.fontSize,
                              glyphs: raw.glyphs.map { TextBend.Glyph(x: Double($0.position.x), advance: $0.advance) })
            }, bend: bend)
            bounds = .null
            inkSelectionBounds = .null
            let layerRotation = rotation
            runs = runs.enumerated().map { index, run in
                let source = layer.runs[index]
                var ink = CGRect.null
                var bentGlyphs: [BentGlyph] = []
                var outlines: [Run.Glyph] = []
                let outlinePath = CGMutablePath()
                for (raw, transform) in zip(rawRuns[index].glyphs, transforms[index]) {
                    bentGlyphs.append(BentGlyph(font: raw.font, id: raw.id, position: raw.position, transform: transform))
                    ink = ink.union(raw.box.offsetBy(dx: raw.position.x, dy: raw.position.y).applying(transform))
                    if run.outline != nil, let glyphPath = CTFontCreatePathForGlyph(raw.font, raw.id, nil) {
                        let placed = CGMutablePath()
                        placed.addPath(glyphPath, transform: CGAffineTransform(translationX: raw.position.x, y: raw.position.y).concatenating(transform))
                        outlinePath.addPath(placed)
                        outlines.append(Run.Glyph(id: raw.id, position: raw.position, path: placed, transform: transform))
                    }
                }
                inkSelectionBounds = inkSelectionBounds.union(ink.insetBy(dx: -source.strokeWidth / 2, dy: -source.strokeWidth / 2))
                bounds = bounds.union(ink.insetBy(dx: -source.strokeWidth - 2, dy: -source.strokeWidth - 2).applying(layerRotation))
                for blur in source.blurLayers {
                    bounds = bounds.union(ink.offsetBy(dx: blur.dx, dy: -blur.dy)
                        .insetBy(dx: -ceil(3 * blur.sigma) - 2, dy: -ceil(3 * blur.sigma) - 2).applying(layerRotation))
                }
                return Run(outlinedGlyphs: outlines, line: run.line, stroke: run.stroke, mask: run.mask, origin: run.origin,
                           blurs: run.blurs, gradient: run.gradient, sourceFont: run.sourceFont, font: run.font,
                           glyphs: run.glyphs, positions: run.positions, fill: run.fill, strokeColor: run.strokeColor,
                           strokeWidth: run.strokeWidth, outline: run.outline == nil ? nil : outlinePath, bent: bentGlyphs)
            }
        }
        if let background = layer.background {
            let rectangle = CGRect(x: background.left, y: canvas.height - background.top - background.height,
                                   width: background.width, height: background.height)
            inkSelectionBounds = inkSelectionBounds.union(rectangle)
            bounds = bounds.union(rectangle.applying(rotation).insetBy(dx: -1, dy: -1))
        }
        selectionBounds = ResolvedTextSelectionBounds(rectangle: CGRect(x: inkSelectionBounds.minX,
            y: canvas.height - inkSelectionBounds.maxY, width: inkSelectionBounds.width, height: inkSelectionBounds.height), canvas: canvas)
        // Moving/scaling text can enter the canvas from an offscreen position.
        // Keep its complete bitmap, still subject to the aggregate memory budget.
        if !preserveOffscreenInk && layer.motion == nil && layer.animationPhases == nil && (layer.effect == .static || layer.effect == .none) && layer.runs.allSatisfy({ $0.blurLayers.isEmpty }) { bounds = bounds.intersection(CGRect(origin: .zero, size: canvas)) }
        self.bounds = bounds.integral
        self.runs = runs
    }

    private func drawScaledShadow(run: Run, blur: TextBlurLayer, context: CGContext,
                                  bounds: CGRect, transform: CGAffineTransform, maxBitmapBytes: Int, opacity: Double) throws {
        for glyph in run.outlinedGlyphs {
            try NativeSkiaShadowPainter.draw(pathBounds: glyph.path.boundingBoxOfPath,
                blur: blur, context: context, bounds: bounds, rotation: rotation,
                transform: transform, maxBitmapBytes: maxBitmapBytes, opacity: opacity,
                imageContext: imageContext) { mask in
                mask.setFillColor(CGColor(gray: 1, alpha: 1))
                if CTFontGetSize(run.font) * hypot(mask.ctm.a, mask.ctm.b) > 256 {
                    mask.addPath(glyph.path); mask.fillPath()
                } else {
                    mask.setShouldSubpixelPositionFonts(true); mask.setShouldSubpixelQuantizeFonts(false)
                    mask.saveGState(); mask.concatenate(glyph.transform)
                    CTFontDrawGlyphs(run.font, [glyph.id], [glyph.position], 1, mask)
                    mask.restoreGState()
                }
            }
        }
    }

    func image(bounds requestedBounds: CGRect? = nil, maxBitmapBytes: Int, transform: CGAffineTransform = .identity, clip: CGPath? = nil, runClips: [CGPath?]? = nil, runTransforms: [CGAffineTransform]? = nil, runGroupAlphas: [Double]? = nil, opacity: Double = 1) throws -> CIImage {
        let bounds = requestedBounds ?? self.bounds
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0,
              bounds.width * bounds.height * 4 <= Double(maxBitmapBytes),
              let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height), bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw NativePreviewFeatureError("text-bitmap:\(bounds.width)x\(bounds.height):limit=\(maxBitmapBytes)")
        }
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        if let clip { context.addPath(clip); context.clip() }
        let maskBounds = transform.isIdentity ? bounds : self.bounds
        guard maskBounds.width * maskBounds.height * 4 <= Double(maxBitmapBytes) else { throw NativePreviewFeatureError("PortableTextVectorPainter-212") }
        guard runClips == nil || runClips?.count == runs.count else { throw RecipeError.invalidTimeline }
        guard runTransforms == nil || runTransforms?.count == runs.count,
              runGroupAlphas == nil || (runGroupAlphas?.count == runs.count && runGroupAlphas!.allSatisfy { $0.isFinite && (0...1).contains($0) }) else { throw RecipeError.invalidTimeline }
        if let background = layer.background {
            context.saveGState()
            context.concatenate(transform)
            context.concatenate(rotation)
            context.setFillColor(background.color.cgColor)
            context.setAlpha(opacity)
            let rectangle = CGRect(x: background.left, y: canvas.height - background.top - background.height,
                                   width: background.width, height: background.height)
            context.addPath(CGPath(roundedRect: rectangle, cornerWidth: background.radius,
                                   cornerHeight: background.radius, transform: nil))
            context.fillPath()
            context.restoreGState()
        }
        for (index, run) in runs.enumerated() {
            let groupAlpha = runGroupAlphas?[index] ?? 1
            if groupAlpha <= 0.001 { continue }
            let drawingTransform = runTransforms?[index].concatenating(transform) ?? transform
            context.saveGState(); defer { context.restoreGState() }
            if groupAlpha < 1 {
                context.setAlpha(groupAlpha)
                context.beginTransparencyLayer(in: bounds, auxiliaryInfo: nil)
            }
            defer { if groupAlpha < 1 { context.endTransparencyLayer() } }
            if let clip = runClips?[index] { context.addPath(clip); context.clip() }
            if !run.blurs.isEmpty && run.outline != nil {
                for blur in run.blurs {
                    try drawScaledShadow(run: run, blur: blur, context: context, bounds: bounds,
                                         transform: drawingTransform, maxBitmapBytes: maxBitmapBytes, opacity: opacity)
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
            context.concatenate(drawingTransform)
            context.concatenate(rotation)
            if run.strokeWidth > 0 { run.draw(context, mode: .stroke, opacity: opacity) }
            if let gradient = run.gradient {
                run.draw(context, mode: .clip, isMask: true)
                guard let cgGradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                    colors: gradient.stops.map { $0.color.cgColor } as CFArray, locations: gradient.stops.map { CGFloat($0.position) }) else {
                    throw NativePreviewFeatureError("PortableTextVectorPainter-282")
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
