#if canImport(AVFoundation)
import Foundation
import CoreGraphics
import CoreImage

/// Paint centerlines after the camera transform, without enlarging a raster tile.
///
/// Stateful and retained by `NativeGiantTitlePainter` across frames (mirroring
/// the non-giant `NativeHandwritingPainter`'s settled-shadow cache), for two
/// independent reasons profiling on a physical iPhone 13 Pro attributed to
/// giant-title handwriting's ~106s/6s export time (`docs/reviews/kri-29/giant-title.md`):
///
///  - A stroke's centerline `CGPath` and its stroked ink bounding box, once
///    the stroke is fully revealed (`endProgress <= progress`), never change
///    again — `visiblePoints(at:)` returns the same full point list for every
///    later `progress`. Recomputing them every frame (and the ink bounds once
///    PER BLUR LAYER — 3x per stroke) was pure waste.
///  - A settled stroke's blurred, tinted shadow tile from `NativeSkiaShadowPainter.tile`
///    is byte-identical across any two frames requesting the same
///    `(transform, opacity)` — the two inputs the tile actually depends on
///    (`bounds`/`rotation` are constant for one painter instance). Caching it
///    is exact by construction: a cache hit re-composites the literal `CGImage`
///    `tile` would otherwise redraw; any change to `(transform, opacity)`
///    simply invalidates the cache and recomputes, exactly as before. This can
///    only skip work that would have produced identical bytes, never diverge.
///    In particular this collapses the entire "writing" phase (camera
///    transform == `.identity`, alpha == 1 the whole time — see
///    `TextTransformTiming`'s `.handwriting` case) to one real render per
///    stroke instead of one per stroke per frame.
final class NativeGiantHandwritingPainter: @unchecked Sendable {
    private let content: HandwritingContent
    private let canvas: CGSize

    /// Settled centerline path/ink-bounds per stroke index. `nil` until that
    /// stroke's `endProgress <= progress` is observed for the first time.
    private var settledPaths: [CGPath?]
    private var settledInkBounds: [CGRect?]

    private struct ShadowTile { let image: CGImage; let crop: CGRect }
    /// `[blurLayerIndex][strokeIndex]`. Valid only under `shadowSignature`;
    /// any signature change invalidates every entry before the next draw.
    private var shadowTiles: [[ShadowTile?]]
    private var shadowSignature: (transform: CGAffineTransform, opacity: Double)?
    /// Soft budget for retained tiles, independent of `maxBitmapBytes` (which
    /// bounds one frame's CGContext, not this cache's cross-frame footprint).
    /// Exceeding it only disables caching for the overflow tile — never a
    /// correctness or crash risk, only a smaller speedup. 32 MiB comfortably
    /// covers the measured fixture (99 strokes x 3 blur layers ~ 15 MB).
    /// Overridable (test-only) so the starved-budget fallback path itself can
    /// be driven and pinned, rather than only ever exercised by coincidence.
    private let maxCacheBytes: Int
    private var cachedTileBytes = 0
    private let lock = NSLock()

    init(content: HandwritingContent, canvas: CGSize, maxCacheBytes: Int = 32 * 1024 * 1024) {
        self.content = content
        self.canvas = canvas
        self.maxCacheBytes = maxCacheBytes
        settledPaths = [CGPath?](repeating: nil, count: content.strokes.count)
        settledInkBounds = [CGRect?](repeating: nil, count: content.strokes.count)
        shadowTiles = content.blurLayers.map { _ in [ShadowTile?](repeating: nil, count: content.strokes.count) }
    }

    private func path(forStroke index: Int, progress: Double) -> CGPath? {
        let stroke = content.strokes[index]
        // A cached path is only valid when THIS call is also settled: the
        // one-time `settledImage()` warm-up (see `NativeGiantTitlePainter`)
        // deliberately calls in at progress 1 before real playback starts,
        // which would otherwise mark every stroke "settled" and poison every
        // later, lower-progress call on this same retained instance.
        if progress >= stroke.endProgress, let cached = settledPaths[index] { return cached }
        let points = stroke.visiblePoints(at: progress)
        guard points.count >= 2 else { return nil }
        let path = CGMutablePath()
        path.move(to: CGPoint(x: points[0].x, y: canvas.height - points[0].y))
        for point in points.dropFirst() { path.addLine(to: CGPoint(x: point.x, y: canvas.height - point.y)) }
        if progress >= stroke.endProgress { settledPaths[index] = path }
        return path
    }

    private func inkBounds(forStroke index: Int, path: CGPath, settled: Bool) -> CGRect {
        // Same rule as `path(forStroke:progress:)`: only trust the cache when
        // the CURRENT call is settled for this stroke, not merely because a
        // (possibly out-of-order) earlier call cached something.
        if settled, let cached = settledInkBounds[index] { return cached }
        let bounds = path.copy(strokingWithWidth: content.inkWidth, lineCap: .round,
            lineJoin: .round, miterLimit: 10).boundingBoxOfPath
        if settled { settledInkBounds[index] = bounds }
        return bounds
    }

    func image(bounds: CGRect, rotation: CGAffineTransform, transform: CGAffineTransform, progress: Double,
        opacity: Double, maxBitmapBytes: Int, imageContext: CIContext) throws -> CIImage {
        try RenderProfiler.measure("giant.handwriting.frame") { () throws -> CIImage in
        lock.lock(); defer { lock.unlock() }
        let bitmapBytes: Double = Double(bounds.width) * Double(bounds.height) * 4
        guard bitmapBytes <= Double(maxBitmapBytes),
              let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height),
                bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw NativePreviewFeatureError("NativeGiantHandwritingPainter-15")
        }
        context.translateBy(x: -bounds.minX, y: -bounds.minY)

        var pathsByIndex: [Int: CGPath] = [:]
        RenderProfiler.measure("giant.handwriting.paths") {
            for index in content.strokes.indices {
                if let path = path(forStroke: index, progress: progress) { pathsByIndex[index] = path }
            }
        }
        let paths = content.strokes.indices.compactMap { pathsByIndex[$0] }

        // A cached tile is only valid for the exact (transform, opacity) it was
        // built under; anything else invalidates all of them and falls back to
        // today's per-call render, so this can never serve a stale byte.
        if shadowSignature?.transform != transform || shadowSignature?.opacity != opacity {
            for layerIndex in shadowTiles.indices {
                shadowTiles[layerIndex] = [ShadowTile?](repeating: nil, count: content.strokes.count)
            }
            shadowSignature = (transform, opacity)
            cachedTileBytes = 0
        }

        for (blurIndex, blur) in content.blurLayers.enumerated() {
            for index in content.strokes.indices {
                guard let path = pathsByIndex[index] else { continue }
                let settled = progress >= content.strokes[index].endProgress
                if settled, let cached = shadowTiles[blurIndex][index] {
                    RenderProfiler.count("shadow.cache.hit")
                    try NativeSkiaShadowPainter.composite(cached.image, in: cached.crop, onto: context, bounds: bounds)
                    continue
                }
                let ink = RenderProfiler.measure("giant.handwriting.inkBounds") {
                    inkBounds(forStroke: index, path: path, settled: settled)
                }
                guard let tile = try NativeSkiaShadowPainter.tile(pathBounds: ink, blur: blur, bounds: bounds,
                    rotation: rotation, transform: transform, maxBitmapBytes: maxBitmapBytes, opacity: opacity,
                    imageContext: imageContext, drawMask: { mask in
                        mask.setLineCap(.round); mask.setLineJoin(.round)
                        mask.setLineWidth(content.inkWidth); mask.setStrokeColor(CGColor(gray: 1, alpha: 1))
                        mask.addPath(path); mask.strokePath()
                    }) else { continue }
                if settled {
                    let bytes = Int(tile.crop.width * tile.crop.height) * 4
                    if cachedTileBytes + bytes <= maxCacheBytes {
                        shadowTiles[blurIndex][index] = ShadowTile(image: tile.image, crop: tile.crop)
                        cachedTileBytes += bytes
                    }
                }
                try NativeSkiaShadowPainter.composite(tile.image, in: tile.crop, onto: context, bounds: bounds)
            }
        }

        try RenderProfiler.measure("giant.handwriting.ink") {
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
        }
        guard let image = context.makeImage() else { throw MediaEngineError.exportFailed }
        return CIImage(cgImage: image)
        }
    }
}
#endif
