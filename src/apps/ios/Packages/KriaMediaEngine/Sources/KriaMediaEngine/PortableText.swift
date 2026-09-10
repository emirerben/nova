import Foundation

public struct TextInk: Codable, Equatable, Sendable {
    public let red: Double
    public let green: Double
    public let blue: Double
    public let alpha: Double
    public init(red: Double, green: Double, blue: Double, alpha: Double) {
        self.red = red; self.green = green; self.blue = blue; self.alpha = alpha
    }
    private enum CodingKeys: String, CodingKey { case red, green, blue, alpha }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["red", "green", "blue", "alpha"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        red = try c.decode(Double.self, forKey: .red); green = try c.decode(Double.self, forKey: .green)
        blue = try c.decode(Double.self, forKey: .blue); alpha = try c.decode(Double.self, forKey: .alpha)
    }
    func validate() throws {
        guard [red, green, blue, alpha].allSatisfy({ $0.isFinite && (0...1).contains($0) }) else { throw RecipeError.invalidTimeline }
    }
}

public struct PositionedTextRun: Codable, Equatable, Sendable {
    public let text: String
    public let fontAssetID: String
    public let fontSize: Double
    public let x: Double
    public let baselineY: Double
    public let letterSpacing: Double
    public let shaped: Bool
    public let fill: TextInk
    public let stroke: TextInk
    public let strokeWidth: Double
    private enum CodingKeys: String, CodingKey {
        case text, fontAssetID = "fontAssetId", fontSize, x, baselineY, letterSpacing, shaped, fill, stroke, strokeWidth
    }
    public init(text: String, fontAssetID: String, fontSize: Double, x: Double, baselineY: Double,
                letterSpacing: Double, shaped: Bool, fill: TextInk, stroke: TextInk, strokeWidth: Double) {
        self.text = text; self.fontAssetID = fontAssetID; self.fontSize = fontSize; self.x = x; self.baselineY = baselineY
        self.letterSpacing = letterSpacing; self.shaped = shaped; self.fill = fill; self.stroke = stroke; self.strokeWidth = strokeWidth
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "fontAssetId", "fontSize", "x", "baselineY", "letterSpacing", "shaped", "fill", "stroke", "strokeWidth"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); fontAssetID = try c.decode(String.self, forKey: .fontAssetID)
        fontSize = try c.decode(Double.self, forKey: .fontSize); x = try c.decode(Double.self, forKey: .x)
        baselineY = try c.decode(Double.self, forKey: .baselineY); letterSpacing = try c.decode(Double.self, forKey: .letterSpacing)
        shaped = try c.decode(Bool.self, forKey: .shaped); fill = try c.decode(TextInk.self, forKey: .fill)
        stroke = try c.decode(TextInk.self, forKey: .stroke); strokeWidth = try c.decode(Double.self, forKey: .strokeWidth)
    }
    func validate() throws {
        guard shaped, !text.isEmpty, text.unicodeScalars.count <= 2000, !fontAssetID.isEmpty, fontAssetID.count <= 160,
              [fontSize, x, baselineY, letterSpacing, strokeWidth].allSatisfy(\.isFinite),
              fontSize > 0, fontSize <= 1000, abs(x) <= 10000, abs(baselineY) <= 10000,
              (-100...1000).contains(letterSpacing), (0...100).contains(strokeWidth) else { throw RecipeError.invalidTimeline }
        try fill.validate(); try stroke.validate()
    }
}

public struct PortableTextLayer: Codable, Equatable, Sendable {
    public enum Effect: String, Codable, Sendable { case `static`, none, fadeIn = "fade-in", scaleUp = "scale-up", slideUp = "slide-up", slideDown = "slide-down", popIn = "pop-in", bounce }
    public let id: String
    public let start: Double
    public let end: Double
    public let anchorX: Double
    public let anchorY: Double
    public let rotationDegrees: Double
    public let runs: [PositionedTextRun]
    public let motion: TextMotionParameters?
    public let effect: Effect
    private enum CodingKeys: String, CodingKey { case id, start, end, anchorX, anchorY, rotationDegrees, runs, effect, motion }
    public init(id: String, start: Double, end: Double, anchorX: Double, anchorY: Double,
                rotationDegrees: Double, runs: [PositionedTextRun], effect: Effect = .static, motion: TextMotionParameters? = nil) {
        self.id = id; self.start = start; self.end = end; self.anchorX = anchorX; self.anchorY = anchorY
        self.rotationDegrees = rotationDegrees; self.runs = runs; self.effect = effect; self.motion = motion
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["id", "start", "end", "anchorX", "anchorY", "rotationDegrees", "runs", "effect", "motion"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id); start = try c.decode(Double.self, forKey: .start)
        end = try c.decode(Double.self, forKey: .end); anchorX = try c.decode(Double.self, forKey: .anchorX)
        anchorY = try c.decode(Double.self, forKey: .anchorY); rotationDegrees = try c.decode(Double.self, forKey: .rotationDegrees)
        motion = try c.decodeIfPresent(TextMotionParameters.self, forKey: .motion)
        runs = try c.decode([PositionedTextRun].self, forKey: .runs); effect = try c.decode(Effect.self, forKey: .effect)
    }
    public func validate(duration: Double, manifest: RenderAssetManifest?) throws {
        guard !id.isEmpty, id.count <= 160, [start, end, anchorX, anchorY, rotationDegrees].allSatisfy(\.isFinite),
              start >= 0, end > start, end <= min(1800, duration), abs(anchorX) <= 10000, abs(anchorY) <= 10000,
              abs(rotationDegrees) <= 3600, (1...100).contains(runs.count),
              runs.reduce(0, { $0 + $1.text.unicodeScalars.count }) <= 5000 else { throw RecipeError.invalidTimeline }
        if effect != .static && effect != .none && motion == nil { throw RecipeError.invalidTimeline }
        try motion?.validate()
        for run in runs {
            try run.validate()
            guard let font = manifest?.assets.first(where: { $0.id == run.fontAssetID }),
                  case .library(catalog: .font, catalogID: _, generation: _) = font.source else { throw RecipeError.missingAssetReference }
        }
    }
}
