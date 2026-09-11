#if canImport(AVFoundation)
import Foundation
import CoreGraphics
import CoreImage

/// Paint centerlines after the camera transform, without enlarging a raster tile.
struct NativeGiantHandwritingPainter {
    static func image(content: HandwritingContent, canvas: CGSize, bounds: CGRect,
        rotation: CGAffineTransform, transform: CGAffineTransform, progress: Double,
        opacity: Double, maxBitmapBytes: Int, imageContext: CIContext) throws -> CIImage {
        guard bounds.width * bounds.height * 4 <= Double(maxBitmapBytes),
              let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height),
                bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw MediaEngineError.unsupportedCapability
        }
        context.translateBy(x: -bounds.minX, y: -bounds.minY)
        let paths = content.strokes.compactMap { stroke -> CGPath? in
            let points = stroke.visiblePoints(at: progress)
            guard points.count >= 2 else { return nil }
            let path = CGMutablePath()
            path.move(to: CGPoint(x: points[0].x, y: canvas.height - points[0].y))
            for point in points.dropFirst() { path.addLine(to: CGPoint(x: point.x, y: canvas.height - point.y)) }
            return path
        }
        for blur in content.blurLayers {
            for path in paths {
                let inkBounds = path.copy(strokingWithWidth: content.inkWidth, lineCap: .round,
                    lineJoin: .round, miterLimit: 10).boundingBoxOfPath
                try NativeSkiaShadowPainter.draw(pathBounds: inkBounds, blur: blur, context: context,
                    bounds: bounds, rotation: rotation, transform: transform,
                    maxBitmapBytes: maxBitmapBytes, opacity: opacity, imageContext: imageContext, integerCompositing: true) { mask in
                    mask.setLineCap(.round); mask.setLineJoin(.round)
                    mask.setLineWidth(content.inkWidth); mask.setStrokeColor(CGColor(gray: 1, alpha: 1))
                    mask.addPath(path); mask.strokePath()
                }
            }
        }
        context.concatenate(rotation.concatenating(transform))
        context.setLineCap(.round); context.setLineJoin(.round)
        let paintAlpha = floor(min(255, max(0, opacity * 255))) / 255
        context.setAlpha(paintAlpha)
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
#endif
