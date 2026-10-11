import Foundation

// KRI-589 freehand drawing. Mirrors `app/agents/_schemas/drawing.py` (stroke vocabulary) and
// `app/kria/portable_drawing.py` (recipe layer). The wire is snake_case, decoded with
// `.convertFromSnakeCase`; the shared fixtures in `tests/fixtures/drawing-parity/` pin both sides.

public enum DrawingTexture: String, Codable, CaseIterable, Sendable {
    case pen, marker, highlighter, neon, chalk, spray
}

public enum DrawingEntrance: String, Codable, Sendable { case draw, fade, none }
public enum DrawingExit: String, Codable, Sendable { case none, fade }

public enum DrawingLimits {
    public static let maxDrawings = 12
    public static let maxStrokesPerDrawing = 60
    public static let maxPointsPerStroke = 1500
    public static let maxPointsPerDrawing = 3000
    public static let maxPointsPerVariant = 8000
    public static let minDuration = 0.1
    public static let maxDrawDuration = 10.0
}

/// One finger stroke. Points are normalized (0...1) to the authoring canvas.
public struct DrawingStroke: Codable, Equatable, Sendable {
    public let color: String
    public let opacity: Double
    public let widthFrac: Double
    public let texture: DrawingTexture
    public let seed: Int
    public let points: [[Double]]

    public init(color: String, opacity: Double = 1, widthFrac: Double, texture: DrawingTexture = .pen,
                seed: Int = 0, points: [[Double]]) {
        self.color = color; self.opacity = opacity; self.widthFrac = widthFrac
        self.texture = texture; self.seed = seed; self.points = points
    }
    private enum CodingKeys: String, CodingKey { case color, opacity, widthFrac, texture, seed, points }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["color", "opacity", "widthFrac", "texture", "seed", "points"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        color = try c.decode(String.self, forKey: .color)
        opacity = try c.decodeIfPresent(Double.self, forKey: .opacity) ?? 1
        widthFrac = try c.decode(Double.self, forKey: .widthFrac)
        texture = try c.decodeIfPresent(DrawingTexture.self, forKey: .texture) ?? .pen
        seed = try c.decodeIfPresent(Int.self, forKey: .seed) ?? 0
        points = try c.decode([[Double]].self, forKey: .points)
        try validate()
    }
    public func validate() throws {
        guard color.count == 7, color.hasPrefix("#"), color.dropFirst().allSatisfy({ $0.isHexDigit }),
              opacity.isFinite, (0.1...1).contains(opacity),
              widthFrac.isFinite, (0.004...0.08).contains(widthFrac),
              (0...Int(Int32.max)).contains(seed),
              (2...DrawingLimits.maxPointsPerStroke).contains(points.count),
              points.allSatisfy({ $0.count == 2 && $0.allSatisfy { $0.isFinite && (0...1).contains($0) } })
        else { throw RecipeError.invalidTimeline }
    }
}

/// A drawing resolved onto the output timeline (seconds). See `compile_v1.json` for the compile rules.
public struct PortableDrawingLayer: Codable, Equatable, Sendable {
    public let id: String
    public let start: Double
    public let end: Double
    public let canvasAspect: Double
    public let centerX: Double
    public let centerY: Double
    public let scale: Double
    public let rotationDeg: Double
    public let entrance: DrawingEntrance
    public let drawDuration: Double
    public let exit: DrawingExit
    public let strokes: [DrawingStroke]

    public init(id: String, start: Double, end: Double, canvasAspect: Double, centerX: Double = 0.5,
                centerY: Double = 0.5, scale: Double = 1, rotationDeg: Double = 0,
                entrance: DrawingEntrance = .draw, drawDuration: Double = 1.2, exit: DrawingExit = .none,
                strokes: [DrawingStroke]) {
        self.id = id; self.start = start; self.end = end; self.canvasAspect = canvasAspect
        self.centerX = centerX; self.centerY = centerY; self.scale = scale; self.rotationDeg = rotationDeg
        self.entrance = entrance; self.drawDuration = drawDuration; self.exit = exit; self.strokes = strokes
    }
    private enum CodingKeys: String, CodingKey {
        case id, start, end, canvasAspect, centerX, centerY, scale, rotationDeg, entrance, drawDuration, exit, strokes
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["id", "start", "end", "canvasAspect", "centerX", "centerY", "scale", "rotationDeg", "entrance", "drawDuration", "exit", "strokes"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        start = try c.decode(Double.self, forKey: .start); end = try c.decode(Double.self, forKey: .end)
        canvasAspect = try c.decode(Double.self, forKey: .canvasAspect)
        centerX = try c.decode(Double.self, forKey: .centerX); centerY = try c.decode(Double.self, forKey: .centerY)
        scale = try c.decode(Double.self, forKey: .scale); rotationDeg = try c.decode(Double.self, forKey: .rotationDeg)
        entrance = try c.decode(DrawingEntrance.self, forKey: .entrance)
        drawDuration = try c.decode(Double.self, forKey: .drawDuration)
        exit = try c.decode(DrawingExit.self, forKey: .exit)
        strokes = try c.decode([DrawingStroke].self, forKey: .strokes)
        try validate()
    }
    public func validate() throws {
        let numbers = [start, end, canvasAspect, centerX, centerY, scale, rotationDeg, drawDuration]
        guard !id.isEmpty, id.count <= 160, numbers.allSatisfy(\.isFinite),
              (0...1800).contains(start), end > 0, end <= 1800,
              end - start >= DrawingLimits.minDuration - 1e-6,
              canvasAspect > 0.2, canvasAspect < 5,
              (0...1).contains(centerX), (0...1).contains(centerY),
              (0.2...3).contains(scale), (-180...180).contains(rotationDeg),
              (DrawingLimits.minDuration...DrawingLimits.maxDrawDuration).contains(drawDuration),
              drawDuration <= end - start + 1e-6,
              (1...DrawingLimits.maxStrokesPerDrawing).contains(strokes.count),
              strokes.reduce(0, { $0 + $1.points.count }) <= DrawingLimits.maxPointsPerDrawing
        else { throw RecipeError.invalidTimeline }
        for stroke in strokes { try stroke.validate() }
    }
}
