import CoreGraphics
import Foundation

/// KRI-589: paints `PortableDrawingLayer`s over a finished frame with Core Graphics.
///
/// - strokes reveal in recorded order by cumulative arc length over `drawDuration` (entrance `.draw`);
/// - `.fade` entrance/exit fades the whole layer over `min(0.25s, window/2)`;
/// - the authoring canvas (`canvasAspect`) is aspect-fit into the output canvas, then translated to
///   `centerX/centerY` (relative to that fitted rectangle, so a fresh drawing is the identity),
///   rotated (positive = clockwise) and scaled about the strokes' bounding-box centre;
/// - a stroke is `widthFrac * min(canvas w, h) * scale` pixels wide;
/// - textures are deterministic functions of (stroke seed, arc length), so preview and export match
///   and a growing reveal never reshuffles grain or spray dots.
public enum NativeDrawingPainter {
    /// Layers visible at `time`, in paint order.
    public static func activeLayers(_ layers: [PortableDrawingLayer], at time: Double) -> [PortableDrawingLayer] {
        layers.filter { time >= $0.start && time < $0.end }
    }

    /// 0...1 share of the ink drawn at `time`: the pen walks the strokes during `.draw`, everything
    /// else is fully drawn from the first frame.
    public static func revealProgress(of layer: PortableDrawingLayer, at time: Double) -> Double {
        guard layer.entrance == .draw else { return 1 }
        return min(1, max(0, (time - layer.start) / max(layer.drawDuration, 1e-6)))
    }

    /// Whole-layer opacity from the `.fade` entrance and exit.
    public static func fadeAlpha(of layer: PortableDrawingLayer, at time: Double) -> Double {
        let fade = max(1e-6, min(0.25, (layer.end - layer.start) / 2))
        var alpha = 1.0
        if layer.entrance == .fade { alpha = min(alpha, (time - layer.start) / fade) }
        if layer.exit == .fade { alpha = min(alpha, (layer.end - time) / fade) }
        return min(1, max(0, alpha))
    }

    /// Paint one layer at `time` into `context` (top-left origin, `canvas` in output pixels).
    public static func paint(_ layer: PortableDrawingLayer, at time: Double, canvas: CGSize, in context: CGContext) {
        guard time >= layer.start, time < layer.end else { return }
        let alpha = fadeAlpha(of: layer, at: time)
        let geometry = DrawingGeometry(layer: layer, canvas: canvas)
        let inks = resolvedInks(layer, geometry: geometry, progress: revealProgress(of: layer, at: time))
        guard alpha > 0, !inks.isEmpty else { return }
        context.saveGState()
        if alpha < 1 { context.setAlpha(alpha); context.beginTransparencyLayer(auxiliaryInfo: nil) }
        for ink in inks { draw(ink, in: context) }
        if alpha < 1 { context.endTransparencyLayer() }
        context.restoreGState()
    }

    // MARK: - Rasterised passes (compositor path)

    /// One bitmap of consecutive strokes sharing a blend: highlighter runs multiply against the frame,
    /// everything else composites normally. `origin` is the bitmap's top-left in canvas pixels.
    struct Pass {
        let image: CGImage
        let origin: CGPoint
        let multiply: Bool
    }

    /// The ink of `layer` once `progress` of it is drawn, un-faded (the caller applies `fadeAlpha`).
    static func passes(for layer: PortableDrawingLayer, progress: Double, canvas: CGSize) -> [Pass] {
        let geometry = DrawingGeometry(layer: layer, canvas: canvas)
        let inks = resolvedInks(layer, geometry: geometry, progress: progress)
        guard !inks.isEmpty, let bounds = inkBounds(layer, geometry: geometry, canvas: canvas) else { return [] }
        var runs: [(multiply: Bool, inks: [Ink])] = []
        for ink in inks {
            let multiply = ink.stroke.texture == .highlighter
            if let last = runs.last, last.multiply == multiply { runs[runs.count - 1].inks.append(ink) }
            else { runs.append((multiply, [ink])) }
        }
        var result: [Pass] = []
        for run in runs {
            guard let context = CGContext(data: nil, width: Int(bounds.width), height: Int(bounds.height), bitsPerComponent: 8,
                                          bytesPerRow: 0, space: DrawingColor.space,
                                          bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) else { continue }
            context.translateBy(x: 0, y: bounds.height)
            context.scaleBy(x: 1, y: -1)
            context.translateBy(x: -bounds.minX, y: -bounds.minY)
            for ink in run.inks { draw(ink, in: context) }
            if let image = context.makeImage() { result.append(Pass(image: image, origin: bounds.origin, multiply: run.multiply)) }
        }
        return result
    }

    /// Pixel rectangle that can hold any state of the layer's ink (stroke widths, neon glow, spray).
    private static func inkBounds(_ layer: PortableDrawingLayer, geometry: DrawingGeometry, canvas: CGSize) -> CGRect? {
        var box = CGRect.null
        var pad = 0.0
        for stroke in layer.strokes {
            for point in stroke.points {
                let p = geometry.map(point)
                box = box.union(CGRect(x: p.x, y: p.y, width: 0, height: 0))
            }
            pad = max(pad, geometry.width(of: stroke) * (stroke.texture == .neon ? 2.6 : 1.3) + 3)
        }
        let rect = box.insetBy(dx: -pad, dy: -pad).intersection(CGRect(origin: .zero, size: canvas)).integral
        return rect.isNull || rect.width < 1 || rect.height < 1 ? nil : rect
    }

    // MARK: - Geometry

    struct DrawingPoint: StrokeRevealPoint, Equatable {
        var x: Double
        var y: Double
    }

    struct Ink {
        let stroke: DrawingStroke
        let visible: [DrawingPoint]
        let full: [DrawingPoint]
        let width: Double
        let fullLength: Double
        let visibleLength: Double
    }

    struct DrawingGeometry {
        let fitOrigin: CGPoint, fitSize: CGSize
        let anchor: CGPoint, target: CGPoint
        let cosine: Double, sine: Double, scale: Double, unit: Double

        init(layer: PortableDrawingLayer, canvas: CGSize) {
            let outputAspect = canvas.width / max(canvas.height, 1)
            let size = layer.canvasAspect > outputAspect
                ? CGSize(width: canvas.width, height: canvas.width / layer.canvasAspect)
                : CGSize(width: canvas.height * layer.canvasAspect, height: canvas.height)
            fitSize = size
            fitOrigin = CGPoint(x: (canvas.width - size.width) / 2, y: (canvas.height - size.height) / 2)
            var minX = Double.infinity, maxX = -Double.infinity, minY = Double.infinity, maxY = -Double.infinity
            for point in layer.strokes.flatMap(\.points) where point.count == 2 {
                minX = min(minX, point[0]); maxX = max(maxX, point[0])
                minY = min(minY, point[1]); maxY = max(maxY, point[1])
            }
            if !minX.isFinite { minX = 0.5; maxX = 0.5; minY = 0.5; maxY = 0.5 }
            anchor = CGPoint(x: fitOrigin.x + (minX + maxX) / 2 * size.width, y: fitOrigin.y + (minY + maxY) / 2 * size.height)
            target = CGPoint(x: fitOrigin.x + layer.centerX * size.width, y: fitOrigin.y + layer.centerY * size.height)
            let radians = layer.rotationDeg * .pi / 180
            cosine = cos(radians); sine = sin(radians)
            scale = layer.scale
            unit = min(canvas.width, canvas.height)
        }

        func map(_ point: [Double]) -> CGPoint {
            let dx = fitOrigin.x + point[0] * fitSize.width - anchor.x
            let dy = fitOrigin.y + point[1] * fitSize.height - anchor.y
            return CGPoint(x: target.x + (dx * cosine - dy * sine) * scale, y: target.y + (dx * sine + dy * cosine) * scale)
        }

        func width(of stroke: DrawingStroke) -> Double { stroke.widthFrac * unit * scale }
    }

    private static func length(_ points: [DrawingPoint]) -> Double { StrokeReveal.segmentLengths(points).reduce(0, +) }

    private static func resolvedInks(_ layer: PortableDrawingLayer, geometry: DrawingGeometry, progress: Double) -> [Ink] {
        let fulls = layer.strokes.map { stroke in stroke.points.map { point -> DrawingPoint in
            let mapped = geometry.map(point); return DrawingPoint(x: mapped.x, y: mapped.y)
        } }
        let lengths = fulls.map(length)
        let revealed = lengths.reduce(0, +) * progress
        var walked = 0.0
        var result: [Ink] = []
        for (index, stroke) in layer.strokes.enumerated() {
            let full = fulls[index], span = lengths[index]
            var visible = full, visibleLength = span
            if progress < 1 {
                // A zero-length stroke is a dot: it appears as soon as the pen starts.
                let fraction = span <= 1e-9 ? (progress > 0 ? 1 : 0) : (revealed - walked) / span
                if fraction <= 0 { break }
                if fraction < 1 { visible = StrokeReveal.prefix(full, fraction: fraction); visibleLength = length(visible) }
            }
            walked += span
            result.append(Ink(stroke: stroke, visible: visible, full: full, width: geometry.width(of: stroke),
                              fullLength: span, visibleLength: visibleLength))
        }
        return result
    }

    // MARK: - Textures

    private static func draw(_ ink: Ink, in context: CGContext) {
        let stroke = ink.stroke
        let width = max(ink.width, 0.5)
        let path = smoothPath(ink.visible)
        let dot = ink.visibleLength <= 1e-9
        let head = ink.visible[0]
        let opacity = stroke.opacity
        context.saveGState()
        defer { context.restoreGState() }
        switch stroke.texture {
        case .pen:
            fillOrStroke(context, path: path, head: head, dot: dot, width: width,
                         color: DrawingColor.cgColor(stroke.color, alpha: opacity), cap: .round, join: .round)
        case .marker:
            // Flat tip: butt ends read as a chisel marker against the pen's round ones.
            fillOrStroke(context, path: path, head: head, dot: dot, width: width,
                         color: DrawingColor.cgColor(stroke.color, alpha: opacity * 0.85), cap: .butt, join: .round, squareDot: true)
        case .highlighter:
            // One path per stroke (a self-overlap never double-darkens), and multiplied so the picture
            // always shows through; the compositor applies the same blend against the frame.
            context.setBlendMode(.multiply)
            fillOrStroke(context, path: path, head: head, dot: dot, width: width,
                         color: DrawingColor.cgColor(stroke.color, alpha: opacity), cap: .square, join: .miter, squareDot: true)
        case .neon:
            context.setAlpha(opacity)
            context.beginTransparencyLayer(auxiliaryInfo: nil)
            let color = DrawingColor.cgColor(stroke.color, alpha: 1)
            context.saveGState()
            context.setShadow(offset: .zero, blur: width * 1.7, color: DrawingColor.cgColor(stroke.color, alpha: 0.95))
            fillOrStroke(context, path: path, head: head, dot: dot, width: width * 0.95, color: color, cap: .round, join: .round)
            context.restoreGState()
            fillOrStroke(context, path: path, head: head, dot: dot, width: width * 0.95, color: color, cap: .round, join: .round)
            fillOrStroke(context, path: path, head: head, dot: dot, width: width * 0.4,
                         color: CGColor(colorSpace: DrawingColor.space, components: [1, 1, 1, 1])!, cap: .round, join: .round)
            context.endTransparencyLayer()
        case .chalk:
            context.setAlpha(opacity)
            context.beginTransparencyLayer(auxiliaryInfo: nil)
            fillOrStroke(context, path: path, head: head, dot: dot, width: width * 1.15,
                         color: DrawingColor.cgColor(stroke.color, alpha: 0.95), cap: .round, join: .round)
            // Grain: keep the ink only where the in-code noise tile has alpha. The tile is fixed in canvas
            // space (offset by the seed), so the grain never swims as the stroke grows.
            context.setBlendMode(.destinationIn)
            let size = Double(ChalkGrain.size)
            let offset = ChalkGrain.offset(seed: stroke.seed)
            context.draw(ChalkGrain.tile, in: CGRect(x: offset.x, y: offset.y, width: size, height: size), byTiling: true)
            context.endTransparencyLayer()
        case .spray:
            drawSpray(ink, in: context)
        }
    }

    private static func fillOrStroke(_ context: CGContext, path: CGPath, head: DrawingPoint, dot: Bool, width: Double,
                                     color: CGColor, cap: CGLineCap, join: CGLineJoin, squareDot: Bool = false) {
        if dot {
            context.setFillColor(color)
            let rect = CGRect(x: head.x - width / 2, y: head.y - width / 2, width: width, height: width)
            if squareDot { context.fill(rect) } else { context.fillEllipse(in: rect) }
            return
        }
        context.setStrokeColor(color)
        context.setLineWidth(width)
        context.setLineCap(cap); context.setLineJoin(join)
        context.addPath(path)
        context.strokePath()
    }

    /// Midpoint-smoothed quadratic path through the (RDP-simplified) points.
    private static func smoothPath(_ points: [DrawingPoint]) -> CGPath {
        let path = CGMutablePath()
        guard let first = points.first else { return path }
        path.move(to: CGPoint(x: first.x, y: first.y))
        if points.count == 2 { path.addLine(to: CGPoint(x: points[1].x, y: points[1].y)); return path }
        for index in 1..<(points.count - 1) {
            let control = CGPoint(x: points[index].x, y: points[index].y)
            let mid = CGPoint(x: (points[index].x + points[index + 1].x) / 2, y: (points[index].y + points[index + 1].y) / 2)
            path.addQuadCurve(to: mid, control: control)
        }
        path.addLine(to: CGPoint(x: points[points.count - 1].x, y: points[points.count - 1].y))
        return path
    }

    /// Dots keyed by (seed, arc-length bucket): a bucket's dots depend only on the stroke, never on how
    /// much of it is revealed, and only the ones whose arc position the pen has reached are painted.
    private static func drawSpray(_ ink: Ink, in context: CGContext) {
        let stroke = ink.stroke
        let width = max(ink.width, 1)
        let bucket = max(width * 0.22, 0.75, ink.fullLength / 4000)
        let count = min(40, max(5, Int(width * 0.55)))
        let radius = max(0.8, width * 0.05)
        let sampler = ArcSampler(ink.full)
        let reach = ink.visibleLength
        var index = 0
        while Double(index) * bucket <= reach {
            var random = DrawingRandom(seed: stroke.seed, salt: index)
            for _ in 0..<count {
                let position = (Double(index) + random.unit()) * bucket
                var dx = 0.0, dy = 0.0
                for _ in 0..<4 {
                    let x = random.unit() * 2 - 1, y = random.unit() * 2 - 1
                    if x * x + y * y <= 1 { dx = x; dy = y; break }
                }
                let shade = 0.35 + 0.65 * random.unit()
                guard position <= reach else { continue }
                let center = sampler.point(at: position)
                context.setFillColor(DrawingColor.cgColor(stroke.color, alpha: stroke.opacity * shade))
                context.fillEllipse(in: CGRect(x: center.x + dx * width / 2 - radius, y: center.y + dy * width / 2 - radius,
                                               width: radius * 2, height: radius * 2))
            }
            index += 1
        }
    }
}

// MARK: - Deterministic helpers

/// SplitMix64: integer-only, so the same (seed, salt) yields the same stream on every device.
struct DrawingRandom {
    private var state: UInt64
    init(seed: Int, salt: Int) {
        state = (UInt64(truncatingIfNeeded: seed) &* 0x9E37_79B9_7F4A_7C15) ^ ((UInt64(truncatingIfNeeded: salt) &+ 1) &* 0xBF58_476D_1CE4_E5B9)
    }
    mutating func next() -> UInt64 {
        state = state &+ 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
    mutating func unit() -> Double { Double(next() >> 11) / 9_007_199_254_740_992 }
}

/// Position along a polyline by arc length.
struct ArcSampler {
    private let points: [NativeDrawingPainter.DrawingPoint]
    private let cumulative: [Double]
    init(_ points: [NativeDrawingPainter.DrawingPoint]) {
        self.points = points
        var total = 0.0
        cumulative = [0] + StrokeReveal.segmentLengths(points).map { total += $0; return total }
    }
    func point(at distance: Double) -> CGPoint {
        guard let first = points.first else { return .zero }
        guard points.count > 1, distance > 0 else { return CGPoint(x: first.x, y: first.y) }
        var low = 0, high = cumulative.count - 1
        while low + 1 < high {
            let middle = (low + high) / 2
            if cumulative[middle] <= distance { low = middle } else { high = middle }
        }
        let span = cumulative[high] - cumulative[low]
        let ratio = span <= 1e-9 ? 0 : min(1, (distance - cumulative[low]) / span)
        return CGPoint(x: points[low].x + (points[high].x - points[low].x) * ratio,
                       y: points[low].y + (points[high].y - points[low].y) * ratio)
    }
}

enum DrawingColor {
    static let space = CGColorSpace(name: CGColorSpace.sRGB)!
    static func cgColor(_ hex: String, alpha: Double) -> CGColor {
        let value = UInt32(hex.dropFirst(), radix: 16) ?? 0
        let red = Double((value >> 16) & 0xFF) / 255, green = Double((value >> 8) & 0xFF) / 255, blue = Double(value & 0xFF) / 255
        return CGColor(colorSpace: space, components: [red, green, blue, alpha]) ?? CGColor(gray: 0, alpha: alpha)
    }
}

/// A 256x256 noise tile generated in code (no bundled bitmap to drift): integer SplitMix noise,
/// one 3x3 box pass to clump it into grain, then thresholded so roughly a third of the ink is speckled out.
enum ChalkGrain {
    static let size = 256
    static func offset(seed: Int) -> (x: Double, y: Double) {
        (Double(abs(seed &* 37) % size), Double(abs(seed &* 91 &+ 53) % size))
    }
    static let tile: CGImage = {
        var random = DrawingRandom(seed: 589, salt: 1)
        var noise = [Int](repeating: 0, count: size * size)
        for index in noise.indices { noise[index] = Int(random.next() >> 56) }
        var bytes = [UInt8](repeating: 0, count: size * size * 4)
        for y in 0..<size {
            for x in 0..<size {
                var sum = 0
                for dy in -1...1 { for dx in -1...1 { sum += noise[((y + dy + size) % size) * size + (x + dx + size) % size] } }
                let value = UInt8(min(255, max(0, (sum / 9 - 118) * 255 / 30)))
                let base = (y * size + x) * 4
                bytes[base] = value; bytes[base + 1] = value; bytes[base + 2] = value; bytes[base + 3] = value
            }
        }
        return CGImage(width: size, height: size, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: size * 4,
                       space: DrawingColor.space, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                       provider: CGDataProvider(data: Data(bytes) as CFData)!, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
    }()
}
