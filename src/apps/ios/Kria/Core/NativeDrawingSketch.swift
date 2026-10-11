import CoreGraphics
import Foundation
import KriaMediaEngine

/// KRI-589: the strokes being drawn in Draw mode, before Done turns them into a drawing.
///
/// Points are fractions (0...1) of the preview. A finished stroke is simplified at once (RDP, epsilon 0.0015),
/// so the caps count the points that will actually be saved, never the raw touch samples. The client-side caps
/// are the server's (`DrawingLimits`); reaching one blocks new strokes and says so.
@MainActor final class NativeDrawingSketch: ObservableObject {
    /// Eight swatches; the colour picker covers everything else.
    static let swatches = ["#FFFFFF", "#30352C", "#FF3B30", "#FF9F0A", "#FFD60A", "#34C759", "#32ADE6", "#FF7AD9"]
    static let widthRange = 0.004...0.08

    @Published private(set) var strokes: [DrawingStroke] = []
    @Published private(set) var liveStroke: DrawingStroke?
    @Published private(set) var note: String?
    @Published var colorHex = NativeDrawingSketch.swatches[2]
    @Published var widthFrac = NativeDrawingSketch.defaultWidth(.pen)
    @Published var texture = DrawingTexture.pen

    private(set) var canvasAspect = 9.0 / 16.0
    /// Why one more stroke (this many extra points / strokes) cannot be added, or nil when it can.
    var budget: (_ extraPoints: Int, _ extraStrokes: Int) -> String? = { _, _ in nil }
    var seedSource: () -> Int = { Int.random(in: 0...Int(Int32.max)) }
    private var livePoints: [[Double]] = []
    private var liveSeed = 0

    static func defaultWidth(_ texture: DrawingTexture) -> Double {
        switch texture {
        case .highlighter: 0.05
        case .marker: 0.024
        case .spray: 0.04
        case .chalk: 0.02
        case .pen, .neon: 0.012
        }
    }

    /// Highlighter ink is translucent by design; the others are solid (the painter shapes marker and neon).
    static func opacity(_ texture: DrawingTexture) -> Double { texture == .highlighter ? 0.35 : 1 }

    var hasInk: Bool { !strokes.isEmpty || liveStroke != nil }
    var committedPointCount: Int { strokes.reduce(0) { $0 + $1.points.count } }

    func reset(canvasAspect: Double, budget: @escaping (Int, Int) -> String?) {
        self.canvasAspect = canvasAspect
        self.budget = budget
        strokes = []; liveStroke = nil; livePoints = []; note = nil
    }

    func select(texture next: DrawingTexture) {
        texture = next
        widthFrac = Self.defaultWidth(next)
    }

    // MARK: Drawing

    @discardableResult
    func begin(at point: CGPoint) -> Bool {
        guard livePoints.isEmpty else { return false }
        if let message = budget(committedPointCount + 2, strokes.count + 1) { note = message; return false }
        note = nil
        liveSeed = seedSource()
        livePoints = [DrawingStrokeTools.normalized([point.x, point.y])]
        refreshLive()
        return true
    }

    func append(_ point: CGPoint) {
        guard !livePoints.isEmpty else { return }
        let next = DrawingStrokeTools.normalized([point.x, point.y])
        if let last = livePoints.last, hypot(last[0] - next[0], last[1] - next[1]) < 0.0005 { return }
        if livePoints.count >= DrawingLimits.maxPointsPerStroke {
            note = "That stroke is as long as it can be."
            return
        }
        if let message = budget(committedPointCount + livePoints.count + 1, strokes.count + 1) {
            note = message
            return
        }
        livePoints.append(next)
        refreshLive()
    }

    func end() {
        guard !livePoints.isEmpty else { return }
        var points = DrawingStrokeTools.simplify(livePoints)
        // A tap is a dot: two coincident points, which the painter draws as the pen's tip.
        if points.count == 1 { points.append(points[0]) }
        let stroke = makeStroke(points)
        livePoints = []; liveStroke = nil
        if let message = budget(committedPointCount + stroke.points.count, strokes.count + 1) { note = message; return }
        strokes.append(stroke)
    }

    func cancelLiveStroke() { livePoints = []; liveStroke = nil }

    func undoStroke() {
        guard !strokes.isEmpty else { return }
        strokes.removeLast()
        note = nil
    }

    func clear() { strokes = []; liveStroke = nil; livePoints = []; note = nil }

    /// What Done saves: every finished stroke, in the order drawn.
    func finishedStrokes() -> [DrawingStroke] {
        end()
        return strokes
    }

    /// The whole sketch as one always-visible layer, painted by the same code as the compositor.
    var previewLayer: PortableDrawingLayer {
        PortableDrawingLayer(id: "sketch", start: 0, end: 1, canvasAspect: canvasAspect, entrance: .none, strokes: strokes + [liveStroke].compactMap { $0 })
    }

    private func refreshLive() { liveStroke = makeStroke(livePoints.count == 1 ? livePoints + livePoints : livePoints) }

    private func makeStroke(_ points: [[Double]]) -> DrawingStroke {
        DrawingStroke(color: colorHex.uppercased(), opacity: Self.opacity(texture),
                      widthFrac: min(Self.widthRange.upperBound, max(Self.widthRange.lowerBound, widthFrac)),
                      texture: texture, seed: liveSeed, points: points)
    }
}
