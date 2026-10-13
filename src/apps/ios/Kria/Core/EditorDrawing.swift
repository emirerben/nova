import Foundation
import KriaMediaEngine

/// KRI-589: one `drawings` lane row. Mirrors the API's `DrawingElement` (`app/agents/_schemas/drawing.py`);
/// `tests/fixtures/drawing-parity/v1.json` pins the wire and `compile_v1.json` pins `NativeDrawingCompile`.
///
/// The parse is strict: a row that would not validate server-side (unknown field, out-of-range value)
/// is not a typed record, so the document keeps it verbatim as an opaque row instead of rewriting it.
struct EditorDrawing: Equatable, Sendable {
    var id: String
    var startS: Double
    var endS: Double
    /// Width / height of the canvas the strokes were drawn on; renderers aspect-fit it.
    var canvasAspect: Double
    /// Where the strokes' bounding-box centre sits (0...1); scale and rotation pivot there.
    var centerX: Double
    var centerY: Double
    var scale: Double
    var rotationDeg: Double
    var entrance: DrawingEntrance
    var drawDurationS: Double
    var exit: DrawingExit
    var strokes: [DrawingStroke]

    init(id: String = UUID().uuidString, startS: Double, endS: Double, canvasAspect: Double, centerX: Double = 0.5, centerY: Double = 0.5,
         scale: Double = 1, rotationDeg: Double = 0, entrance: DrawingEntrance = .draw, drawDurationS: Double = 1.2,
         exit: DrawingExit = .none, strokes: [DrawingStroke]) {
        self.id = id; self.startS = startS; self.endS = endS; self.canvasAspect = canvasAspect
        self.centerX = centerX; self.centerY = centerY; self.scale = scale; self.rotationDeg = rotationDeg
        self.entrance = entrance; self.drawDurationS = drawDurationS; self.exit = exit; self.strokes = strokes
    }

    // MARK: Wire

    private static let elementKeys: Set<String> = ["id", "start_s", "end_s", "canvas_aspect", "center_x", "center_y", "scale", "rotation_deg", "animation", "strokes"]
    private static let animationKeys: Set<String> = ["entrance", "draw_duration_s", "exit"]

    init?(json: [String: JSONValue]) {
        guard Set(json.keys).isSubset(of: Self.elementKeys),
              let id = json["id"]?.stringValue,
              let start = json["start_s"]?.numberValue, let end = json["end_s"]?.numberValue,
              let aspect = json["canvas_aspect"]?.numberValue,
              let rows = json["strokes"]?.arrayValue else { return nil }
        var entrance = DrawingEntrance.draw, exit = DrawingExit.none, drawDuration = 1.2
        if let animation = json["animation"] {
            guard case .object(let object) = animation, Set(object.keys).isSubset(of: Self.animationKeys) else { return nil }
            if let value = object["entrance"] { guard let parsed = value.stringValue.flatMap(DrawingEntrance.init(rawValue:)) else { return nil }; entrance = parsed }
            if let value = object["exit"] { guard let parsed = value.stringValue.flatMap(DrawingExit.init(rawValue:)) else { return nil }; exit = parsed }
            if let value = object["draw_duration_s"] { guard let parsed = value.numberValue else { return nil }; drawDuration = parsed }
        }
        func optional(_ key: String, _ fallback: Double) -> Double? {
            guard let value = json[key] else { return fallback }
            return value.numberValue
        }
        guard let centerX = optional("center_x", 0.5), let centerY = optional("center_y", 0.5),
              let scale = optional("scale", 1), let rotation = optional("rotation_deg", 0) else { return nil }
        var strokes: [DrawingStroke] = []
        for row in rows {
            guard case .object(let object) = row, let stroke = Self.stroke(object) else { return nil }
            strokes.append(stroke)
        }
        self.init(id: id, startS: start, endS: end, canvasAspect: aspect, centerX: centerX, centerY: centerY, scale: scale, rotationDeg: rotation,
                  entrance: entrance, drawDurationS: drawDuration, exit: exit, strokes: strokes)
        guard isValid else { return nil }
    }

    private static func stroke(_ object: [String: JSONValue]) -> DrawingStroke? { DrawingStrokeWire.parse(object) }

    var json: JSONValue {
        .object([
            "id": .string(id), "start_s": .number(startS), "end_s": .number(endS), "canvas_aspect": .number(canvasAspect),
            "center_x": .number(centerX), "center_y": .number(centerY), "scale": .number(scale), "rotation_deg": .number(rotationDeg),
            "animation": .object(["entrance": .string(entrance.rawValue), "draw_duration_s": .number(drawDurationS), "exit": .string(exit.rawValue)]),
            "strokes": .array(strokes.map(DrawingStrokeWire.json)),
        ])
    }

    /// The server's `DrawingElement` rules.
    var isValid: Bool {
        let numbers = [startS, endS, canvasAspect, centerX, centerY, scale, rotationDeg, drawDurationS]
        return !id.isEmpty && id.count <= 160 && numbers.allSatisfy(\.isFinite)
            && (0...1800).contains(startS) && endS > 0 && endS <= 1800
            && endS - startS >= DrawingLimits.minDuration - 1e-6
            && canvasAspect > 0.2 && canvasAspect < 5
            && (0...1).contains(centerX) && (0...1).contains(centerY)
            && (0.2...3).contains(scale) && (-180...180).contains(rotationDeg)
            && (0.2...DrawingLimits.maxDrawDuration).contains(drawDurationS)
            && (1...DrawingLimits.maxStrokesPerDrawing).contains(strokes.count)
            && pointCount <= DrawingLimits.maxPointsPerDrawing
            && strokes.allSatisfy { (try? $0.validate()) != nil }
    }

    var pointCount: Int { strokes.reduce(0) { $0 + $1.points.count } }

    /// Rotation kept inside the wire's -180...180.
    static func wrappedRotation(_ degrees: Double) -> Double {
        var value = degrees.truncatingRemainder(dividingBy: 360)
        if value > 180 { value -= 360 } else if value < -180 { value += 360 }
        return value
    }

    /// Where an authoring-canvas point lands on the preview (both 0...1), through this drawing's own move,
    /// scale and rotation about its stroke centre. Lengths are measured in canvas-height units so the
    /// rotation never shears on a non-square canvas.
    func previewPoint(of point: [Double]) -> [Double] {
        let pivot = strokeCenter
        return project(point, pivot: pivot, target: (centerX, centerY))
    }

    private func project(_ point: [Double], pivot: (x: Double, y: Double), target: (x: Double, y: Double)) -> [Double] {
        let radians = rotationDeg * .pi / 180
        let dx = (point[0] - pivot.x) * canvasAspect * scale, dy = (point[1] - pivot.y) * scale
        return [target.x + (dx * cos(radians) - dy * sin(radians)) / canvasAspect, target.y + dx * sin(radians) + dy * cos(radians)]
    }

    /// The inverse of `previewPoint`: a point drawn on the preview, in authoring-canvas terms.
    func authoringPoint(of preview: [Double]) -> [Double] {
        let center = strokeCenter, radians = -rotationDeg * .pi / 180
        let dx = (preview[0] - centerX) * canvasAspect, dy = preview[1] - centerY
        let rx = (dx * cos(radians) - dy * sin(radians)) / scale, ry = (dx * sin(radians) + dy * cos(radians)) / scale
        return [center.x + rx / canvasAspect, center.y + ry]
    }

    /// Adds strokes drawn on the preview ("Draw more"). The bounding box grows, and with it the pivot, so
    /// the centre is re-pinned: everything already drawn stays exactly where it was on screen.
    mutating func append(previewStrokes: [DrawingStroke]) {
        let oldPivot = strokeCenter, oldTarget = (x: centerX, y: centerY)
        let carried = previewStrokes.map { stroke in
            DrawingStroke(color: stroke.color, opacity: stroke.opacity,
                          widthFrac: min(0.08, max(0.004, stroke.widthFrac / scale)), texture: stroke.texture, seed: stroke.seed,
                          points: stroke.points.map { DrawingStrokeTools.normalized(authoringPoint(of: $0)) })
        }
        let grown = { () -> (x: Double, y: Double) in
            var copy = self; copy.strokes += carried; return copy.strokeCenter
        }()
        let landing = project([grown.x, grown.y], pivot: oldPivot, target: oldTarget)
        strokes += carried
        centerX = min(1, max(0, landing[0])); centerY = min(1, max(0, landing[1]))
    }

    /// Centre of the strokes' bounding box in authoring-canvas fractions.
    var strokeCenter: (x: Double, y: Double) {
        let points = strokes.flatMap(\.points)
        guard !points.isEmpty else { return (0.5, 0.5) }
        let xs = points.map { $0[0] }, ys = points.map { $0[1] }
        return (((xs.min() ?? 0.5) + (xs.max() ?? 0.5)) / 2, ((ys.min() ?? 0.5) + (ys.max() ?? 0.5)) / 2)
    }
}

// MARK: - Compile

/// Editor `drawings` -> the recipe's portable layers. The rules are frozen in
/// `tests/fixtures/drawing-parity/compile_v1.json` and mirrored by `compile_drawing_layers` on the server:
/// a drawing starting at or after the timeline end is dropped; `end` is clamped to the timeline and the
/// drawing is dropped when under 0.1 s remains; `draw_duration` is clamped to the window; order is kept;
/// everything else is copied. Tolerant by design: it never throws.
enum NativeDrawingCompile {
    static func layers(_ drawings: [EditorDrawing], timelineDuration: Double) -> [PortableDrawingLayer] {
        var layers: [PortableDrawingLayer] = []
        for drawing in drawings.prefix(DrawingLimits.maxDrawings) where drawing.isValid {
            let end = min(drawing.endS, timelineDuration)
            guard drawing.startS < timelineDuration, end - drawing.startS >= DrawingLimits.minDuration - 1e-6 else { continue }
            let layer = PortableDrawingLayer(
                id: drawing.id, start: drawing.startS, end: end, canvasAspect: drawing.canvasAspect,
                centerX: drawing.centerX, centerY: drawing.centerY, scale: drawing.scale, rotationDeg: drawing.rotationDeg,
                entrance: drawing.entrance, drawDuration: min(drawing.drawDurationS, end - drawing.startS), exit: drawing.exit,
                strokes: drawing.strokes)
            if (try? layer.validate()) != nil { layers.append(layer) }
        }
        return layers
    }
}

// MARK: - Stroke tools

extension DrawingStroke {
    func withColor(_ hex: String) -> DrawingStroke {
        DrawingStroke(color: hex, opacity: opacity, widthFrac: widthFrac, texture: texture, seed: seed, points: points)
    }
}

enum DrawingStrokeTools {
    /// Ramer-Douglas-Peucker in normalized units; keeps the first and last point.
    static func simplify(_ points: [[Double]], epsilon: Double = 0.0015) -> [[Double]] {
        guard points.count > 2 else { return points }
        var keep = [Bool](repeating: false, count: points.count)
        keep[0] = true; keep[points.count - 1] = true
        var stack = [(0, points.count - 1)]
        while let (first, last) = stack.popLast() {
            var worst = 0.0, index = -1
            for candidate in (first + 1)..<last where last > first + 1 {
                let distance = perpendicularDistance(points[candidate], points[first], points[last])
                if distance > worst { worst = distance; index = candidate }
            }
            if index >= 0, worst > epsilon {
                keep[index] = true
                stack.append((first, index)); stack.append((index, last))
            }
        }
        return points.enumerated().filter { keep[$0.offset] }.map(\.element)
    }

    private static func perpendicularDistance(_ p: [Double], _ a: [Double], _ b: [Double]) -> Double {
        let dx = b[0] - a[0], dy = b[1] - a[1]
        let length = hypot(dx, dy)
        if length < 1e-12 { return hypot(p[0] - a[0], p[1] - a[1]) }
        return abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0]) / length
    }

    /// Rounds to the 4 decimals the API stores and keeps every value in 0...1.
    static func normalized(_ point: [Double]) -> [Double] {
        point.map { min(1, max(0, ($0 * 10_000).rounded() / 10_000)) }
    }

    static func polylineLength(_ points: [[Double]]) -> Double {
        zip(points, points.dropFirst()).reduce(0) { $0 + hypot($1.0[0] - $1.1[0], $1.0[1] - $1.1[1]) }
    }
}

// MARK: - Stroke wire

/// The snake_case JSON of one `DrawingStroke`, shared by the editor's `drawings` lane and a slide's `drawing`
/// (`SlidePostDrawing`). Both pin it with the fixtures in `tests/fixtures/{drawing,slide-drawing}-parity/`.
/// The parse is strict (unknown key, bad value => nil) because the server's `DrawingStroke` is `extra="forbid"`.
enum DrawingStrokeWire {
    private static let strokeKeys: Set<String> = ["color", "opacity", "width_frac", "texture", "seed", "points"]

    static func parse(_ object: [String: JSONValue]) -> DrawingStroke? {
        guard Set(object.keys).isSubset(of: strokeKeys),
              let color = object["color"]?.stringValue, let width = object["width_frac"]?.numberValue,
              let rows = object["points"]?.arrayValue else { return nil }
        var opacity = 1.0, texture = DrawingTexture.pen, seed = 0
        if let value = object["opacity"] { guard let parsed = value.numberValue else { return nil }; opacity = parsed }
        if let value = object["texture"] { guard let parsed = value.stringValue.flatMap(DrawingTexture.init(rawValue:)) else { return nil }; texture = parsed }
        if let value = object["seed"] { guard let parsed = value.numberValue, parsed.rounded() == parsed, abs(parsed) <= Double(Int32.max) else { return nil }; seed = Int(parsed) }
        var points: [[Double]] = []
        for row in rows {
            guard let pair = row.arrayValue, pair.count == 2, let x = pair[0].numberValue, let y = pair[1].numberValue else { return nil }
            points.append([rounded(x), rounded(y)])
        }
        let stroke = DrawingStroke(color: color.uppercased(), opacity: opacity, widthFrac: width, texture: texture, seed: seed, points: points)
        return (try? stroke.validate()) == nil ? nil : stroke
    }

    /// The API stores 4 decimals.
    private static func rounded(_ value: Double) -> Double { (value * 10_000).rounded() / 10_000 }


    static func json(_ stroke: DrawingStroke) -> JSONValue {
        .object(["color": .string(stroke.color), "opacity": .number(stroke.opacity), "width_frac": .number(stroke.widthFrac),
                 "texture": .string(stroke.texture.rawValue), "seed": .number(Double(stroke.seed)),
                 "points": .array(stroke.points.map { .array($0.map { .number($0) }) })])
    }
}
