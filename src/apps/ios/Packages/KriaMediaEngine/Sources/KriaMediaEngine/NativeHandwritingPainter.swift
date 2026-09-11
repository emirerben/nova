#if canImport(AVFoundation)
import Foundation
import CoreGraphics
import CoreImage

/// Paints the authored centerlines locally. Fonts and pre-rendered effect media
/// are not substituted for a partially drawn path.
struct NativeHandwritingPainter: @unchecked Sendable {
    let content: HandwritingContent
    let canvas: CGSize
    let bounds: CGRect
    let rotation: CGAffineTransform
    private struct Shadow { let image: CGImage; let extent: CGRect }
    private var shadowCache: [[Shadow?]] = []
    private(set) var bitmapBytes = 0
    // Retain the Metal context across partial frames; recreating it per frame
    // repeatedly compiles the same shadow filters on physical devices.
    private let imageContext = CIContext(options: [.cacheIntermediates: false])

    init(layer: PortableTextLayer, canvas: CGSize, maxBitmapBytes: Int, cacheShadows: Bool = true) throws {
        guard let content = layer.handwriting else { throw RecipeError.invalidTimeline }
        try content.validate()
        self.content = content; self.canvas = canvas
        let anchor = CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY)
        rotation = CGAffineTransform(translationX: -anchor.x, y: -anchor.y)
            .concatenating(CGAffineTransform(rotationAngle: -layer.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: anchor.x, y: anchor.y))
        var box = CGRect.null
        for stroke in content.strokes {
            let path = Self.path(stroke.points, canvas: canvas)
            let ink = path.boundingBoxOfPath
            let halfWidth = (content.inkWidth + content.outlineWidth) / 2 + 2
            box = box.union(ink.insetBy(dx: -halfWidth, dy: -halfWidth).applying(rotation))
            for blur in content.blurLayers {
                let bleed = content.inkWidth / 2 + ceil(3 * blur.sigma) + 2
                box = box.union(ink.insetBy(dx: -bleed, dy: -bleed)
                    .offsetBy(dx: blur.dx, dy: -blur.dy).applying(rotation))
            }
        }
        bounds = box.integral
        // Keep room for both the settled bitmap and a live partial frame.
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0,
              bounds.width * bounds.height * 8 <= Double(maxBitmapBytes) else {
            throw MediaEngineError.unsupportedCapability
        }
        bitmapBytes = Int(bounds.width * bounds.height * 8)
        if cacheShadows {
            for blur in content.blurLayers {
                var row: [Shadow?] = []
                for stroke in content.strokes {
                    let path = Self.path(stroke.points, canvas: canvas)
                    let extent = shadowExtent(path: path, blur: blur)
                    let bytes = Int(extent.width * extent.height * 4)
                    if bytes <= maxBitmapBytes - bitmapBytes {
                        row.append(try shadow(path: path, blur: blur))
                        bitmapBytes += bytes
                    } else { row.append(nil) }
                }
                shadowCache.append(row)
            }
        }
    }

    private static func path(_ points: [TextStrokePoint], canvas: CGSize) -> CGPath {
        let path = CGMutablePath()
        if let first = points.first {
            path.move(to: CGPoint(x: first.x, y: canvas.height - first.y))
            for point in points.dropFirst() { path.addLine(to: CGPoint(x: point.x, y: canvas.height - point.y)) }
        }
        return path
    }

    private func context(size: CGSize) throws -> CGContext {
        guard size.width > 0, size.height > 0,
              let context = CGContext(data: nil, width: Int(size.width), height: Int(size.height), bitsPerComponent: 8,
                  bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.exportFailed
        }
        context.setAllowsAntialiasing(true); context.setShouldAntialias(true)
        context.setLineCap(.round); context.setLineJoin(.round)
        return context
    }

    private func maskBounds(path: CGPath, blur: TextBlurLayer) -> CGRect {
        let bleed = content.inkWidth / 2 + ceil(3 * blur.sigma) + 2
        return path.boundingBoxOfPath.insetBy(dx: -bleed, dy: -bleed).integral
    }

    private func shadowExtent(path: CGPath, blur: TextBlurLayer) -> CGRect {
        maskBounds(path: path, blur: blur)
            .offsetBy(dx: blur.dx, dy: -blur.dy).applying(rotation).integral
    }

    private func shadow(path: CGPath, blur: TextBlurLayer) throws -> Shadow {
        let maskBounds = maskBounds(path: path, blur: blur)
        let mask = try context(size: maskBounds.size)
        mask.translateBy(x: -maskBounds.minX, y: -maskBounds.minY)
        mask.setStrokeColor(CGColor(gray: 1, alpha: 1)); mask.setLineWidth(content.inkWidth)
        mask.addPath(path); mask.strokePath()
        guard let maskImage = mask.makeImage() else { throw MediaEngineError.exportFailed }
        var shadow = CIImage(cgImage: maskImage).applyingFilter("CIColorMatrix", parameters: [
            "inputRVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.red * blur.color.alpha),
            "inputGVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.green * blur.color.alpha),
            "inputBVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.blue * blur.color.alpha),
            "inputAVector": CIVector(x: 0, y: 0, z: 0, w: blur.color.alpha)
        ])
        if blur.sigma > 0 { shadow = shadow.applyingFilter("CIGaussianBlur", parameters: ["inputRadius": blur.sigma]) }
        shadow = shadow.cropped(to: CGRect(origin: .zero, size: maskBounds.size))
            .transformed(by: CGAffineTransform(translationX: maskBounds.minX + blur.dx, y: maskBounds.minY - blur.dy))
            .transformed(by: rotation)
        let extent = shadow.extent.integral
        guard let image = imageContext.createCGImage(shadow, from: extent) else { throw MediaEngineError.exportFailed }
        return Shadow(image: image, extent: extent)
    }

    func image(progress: Double) throws -> CIImage {
        let context = try context(size: bounds.size)
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        let visiblePaths = content.strokes.enumerated().compactMap { index, stroke -> (Int, CGPath)? in
            let visible = stroke.visiblePoints(at: progress)
            return visible.count >= 2 ? (index, Self.path(visible, canvas: canvas)) : nil
        }
        let paths = visiblePaths.map { $0.1 }
        // Completed strokes reuse their exact shadow tiles. Only the currently
        // growing stroke needs a new blur; draw order and backward seeks stay unchanged.
        for (blurIndex, blur) in content.blurLayers.enumerated() {
            for (index, path) in visiblePaths {
                let cached = progress >= content.strokes[index].endProgress && blurIndex < shadowCache.count
                    ? shadowCache[blurIndex][index] : nil
                let tile = try cached ?? shadow(path: path, blur: blur)
                context.draw(tile.image, in: tile.extent)
            }
        }
        context.concatenate(rotation)
        if content.outlineWidth > 0 {
            context.setStrokeColor(content.outline.cgColor)
            context.setLineWidth(content.inkWidth + content.outlineWidth)
            for path in paths { context.addPath(path); context.strokePath() }
        }
        context.setLineWidth(content.inkWidth)
        for path in paths {
            context.saveGState()
            context.addPath(path)
            if let gradient = content.gradient {
                context.replacePathWithStrokedPath(); context.clip()
                guard let colors = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                    colors: gradient.stops.map { $0.color.cgColor } as CFArray,
                    locations: gradient.stops.map { CGFloat($0.position) }) else { throw MediaEngineError.exportFailed }
                context.drawLinearGradient(colors,
                    start: CGPoint(x: gradient.startX, y: canvas.height - gradient.startY),
                    end: CGPoint(x: gradient.endX, y: canvas.height - gradient.endY),
                    options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
            } else {
                context.setStrokeColor(content.fill.cgColor); context.strokePath()
            }
            context.restoreGState()
        }
        guard let image = context.makeImage() else { throw MediaEngineError.exportFailed }
        return CIImage(cgImage: image)
    }
}

extension RecipeTextLayer {
    static func makeHandwriting(_ layer: PortableTextLayer, canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        let painter = try NativeHandwritingPainter(layer: layer, canvas: canvas, maxBitmapBytes: maxBitmapBytes)
        return Self(image: try painter.image(progress: 1), frame: painter.bounds, start: layer.start, end: layer.end,
                    animation: .none, portable: layer, portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY),
                    handwriting: painter)
    }
}
#endif
