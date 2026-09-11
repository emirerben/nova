import Foundation

public struct TextStrokePoint: Codable, Equatable, Sendable {
    public let x: Double
    public let y: Double
    public init(x: Double, y: Double) { self.x = x; self.y = y }
    private enum CodingKeys: String, CodingKey { case x, y }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["x", "y"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        x = try c.decode(Double.self, forKey: .x); y = try c.decode(Double.self, forKey: .y)
        try validate()
    }
    public func validate() throws {
        guard [x, y].allSatisfy({ $0.isFinite && abs($0) <= 10000 }) else { throw RecipeError.invalidTimeline }
    }
}

public struct TextPenStroke: Codable, Equatable, Sendable {
    public let points: [TextStrokePoint]
    public let startProgress: Double
    public let endProgress: Double
    public init(points: [TextStrokePoint], startProgress: Double, endProgress: Double) {
        self.points = points; self.startProgress = startProgress; self.endProgress = endProgress
    }
    private enum CodingKeys: String, CodingKey { case points, startProgress, endProgress }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["points", "startProgress", "endProgress"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        points = try c.decode([TextStrokePoint].self, forKey: .points)
        startProgress = try c.decode(Double.self, forKey: .startProgress); endProgress = try c.decode(Double.self, forKey: .endProgress)
        try validate()
    }
    public func validate() throws {
        guard (2...2000).contains(points.count), startProgress.isFinite, endProgress.isFinite,
              startProgress >= 0, endProgress > startProgress, endProgress <= 1 else { throw RecipeError.invalidTimeline }
        for point in points { try point.validate() }
    }
    /// Distance along the authored polyline, not point count, owns pen motion.
    public func visiblePoints(at revealProgress: Double) -> [TextStrokePoint] {
        if revealProgress <= startProgress { return [] }
        if revealProgress >= endProgress { return points }
        let progress = (revealProgress - startProgress) / max(1e-9, endProgress - startProgress)
        let lengths = zip(points, points.dropFirst()).map { hypot($1.x - $0.x, $1.y - $0.y) }
        let total = lengths.reduce(0, +)
        if total <= 1e-9 { return Array(points.prefix(2)) }
        let target = total * progress
        var walked = 0.0, result = [points[0]]
        for (index, length) in lengths.enumerated() {
            let next = walked + length
            if target >= next { result.append(points[index + 1]); walked = next; continue }
            let ratio = (target - walked) / max(length, 1e-9)
            result.append(TextStrokePoint(x: points[index].x + (points[index + 1].x - points[index].x) * ratio,
                                          y: points[index].y + (points[index + 1].y - points[index].y) * ratio))
            break
        }
        return result
    }
}

public struct HandwritingContent: Codable, Equatable, Sendable {
    public let text: String
    public let strokes: [TextPenStroke]
    public let inkWidth: Double
    public let fill: TextInk
    public let outline: TextInk
    public let outlineWidth: Double
    public let blurLayers: [TextBlurLayer]
    public let gradient: TextGradient?
    public init(text: String, strokes: [TextPenStroke], inkWidth: Double, fill: TextInk, outline: TextInk,
                outlineWidth: Double, blurLayers: [TextBlurLayer] = [], gradient: TextGradient? = nil) {
        self.text = text; self.strokes = strokes; self.inkWidth = inkWidth; self.fill = fill
        self.outline = outline; self.outlineWidth = outlineWidth; self.blurLayers = blurLayers; self.gradient = gradient
    }
    private enum CodingKeys: String, CodingKey { case text, strokes, inkWidth, fill, outline, outlineWidth, blurLayers, gradient }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "strokes", "inkWidth", "fill", "outline", "outlineWidth", "blurLayers", "gradient"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); strokes = try c.decode([TextPenStroke].self, forKey: .strokes)
        inkWidth = try c.decode(Double.self, forKey: .inkWidth); fill = try c.decode(TextInk.self, forKey: .fill)
        outline = try c.decode(TextInk.self, forKey: .outline); outlineWidth = try c.decode(Double.self, forKey: .outlineWidth)
        blurLayers = try c.decodeIfPresent([TextBlurLayer].self, forKey: .blurLayers) ?? []
        gradient = try c.decodeIfPresent(TextGradient.self, forKey: .gradient)
        try validate()
    }
    public func validate() throws {
        guard (1...5000).contains(text.unicodeScalars.count), (1...4000).contains(strokes.count),
              strokes.reduce(0, { $0 + $1.points.count }) <= 40000,
              inkWidth.isFinite, inkWidth > 0, inkWidth <= 1000,
              outlineWidth.isFinite, (0...200).contains(outlineWidth), blurLayers.count <= 8 else { throw RecipeError.invalidTimeline }
        for stroke in strokes { try stroke.validate() }
        try fill.validate(); try outline.validate(); try gradient?.validate()
        for blur in blurLayers { try blur.validate() }
    }
}
