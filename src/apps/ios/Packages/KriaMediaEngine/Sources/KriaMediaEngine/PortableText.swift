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

public struct TextBlurLayer: Codable, Equatable, Sendable {
    public let alphaPower: Int
    public let color: TextInk
    public let sigma: Double
    public let dx: Double
    public let dy: Double
    private enum CodingKeys: String, CodingKey { case color, sigma, dx, dy, alphaPower }
    public init(color: TextInk, sigma: Double, dx: Double, dy: Double, alphaPower: Int = 1) {
        self.color = color; self.sigma = sigma; self.dx = dx; self.dy = dy; self.alphaPower = alphaPower
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["color", "sigma", "dx", "dy", "alphaPower"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        color = try c.decode(TextInk.self, forKey: .color); sigma = try c.decode(Double.self, forKey: .sigma)
        dx = try c.decode(Double.self, forKey: .dx); dy = try c.decode(Double.self, forKey: .dy)
        alphaPower = try c.decodeIfPresent(Int.self, forKey: .alphaPower) ?? 1
    }
    func validate() throws {
        try color.validate()
        guard (1...2).contains(alphaPower), sigma.isFinite, (0...100).contains(sigma), dx.isFinite, dy.isFinite,
              abs(dx) <= 1000, abs(dy) <= 1000 else { throw RecipeError.invalidTimeline }
    }
}

public struct TextGradientStop: Codable, Equatable, Sendable {
    public let position: Double
    public let color: TextInk
    public init(position: Double, color: TextInk) { self.position = position; self.color = color }
    private enum CodingKeys: String, CodingKey { case position, color }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["position", "color"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        position = try c.decode(Double.self, forKey: .position); color = try c.decode(TextInk.self, forKey: .color)
    }
}

public struct TextGradient: Codable, Equatable, Sendable {
    public let startX: Double
    public let startY: Double
    public let endX: Double
    public let endY: Double
    public let stops: [TextGradientStop]
    public init(startX: Double, startY: Double, endX: Double, endY: Double, stops: [TextGradientStop]) {
        self.startX = startX; self.startY = startY; self.endX = endX; self.endY = endY; self.stops = stops
    }
    private enum CodingKeys: String, CodingKey { case startX, startY, endX, endY, stops }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["startX", "startY", "endX", "endY", "stops"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        startX = try c.decode(Double.self, forKey: .startX); startY = try c.decode(Double.self, forKey: .startY)
        endX = try c.decode(Double.self, forKey: .endX); endY = try c.decode(Double.self, forKey: .endY)
        stops = try c.decode([TextGradientStop].self, forKey: .stops)
    }
    func validate() throws {
        guard [startX, startY, endX, endY].allSatisfy({ $0.isFinite && abs($0) <= 10000 }),
              startX != endX || startY != endY, (2...16).contains(stops.count),
              stops.map(\.position) == stops.map(\.position).sorted() else { throw RecipeError.invalidTimeline }
        for stop in stops {
            try stop.color.validate()
            guard stop.position.isFinite, (0...1).contains(stop.position) else { throw RecipeError.invalidTimeline }
        }
    }
}

public struct PositionedGlyph: Codable, Equatable, Sendable {
    public let glyphID: Int
    public let x: Double
    public let y: Double
    public init(glyphID: Int, x: Double, y: Double) { self.glyphID = glyphID; self.x = x; self.y = y }
    private enum CodingKeys: String, CodingKey { case glyphID = "glyphId", x, y }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["glyphId", "x", "y"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        glyphID = try c.decode(Int.self, forKey: .glyphID)
        x = try c.decode(Double.self, forKey: .x); y = try c.decode(Double.self, forKey: .y)
    }
    func validate() throws {
        guard (1...65535).contains(glyphID), x.isFinite, y.isFinite,
              abs(x) <= 10000, abs(y) <= 10000 else { throw RecipeError.invalidTimeline }
    }
}

public struct PositionedTextRun: Codable, Equatable, Sendable {
    public let text: String
    public let fontAssetID: String
    public let fontSize: Double
    public let fontVariations: [String: Double]
    public let x: Double
    public let baselineY: Double
    public let letterSpacing: Double
    public let shaped: Bool
    public let glyphs: [PositionedGlyph]?
    public let fill: TextInk
    public let stroke: TextInk
    public let strokeWidth: Double
    public let blurLayers: [TextBlurLayer]
    public let gradient: TextGradient?
    private enum CodingKeys: String, CodingKey {
        case text, fontAssetID = "fontAssetId", fontSize, fontVariations, x, baselineY, letterSpacing, shaped, fill, stroke, strokeWidth, blurLayers, gradient, glyphs
    }
    public init(text: String, fontAssetID: String, fontSize: Double, x: Double, baselineY: Double,
                letterSpacing: Double, shaped: Bool, fill: TextInk, stroke: TextInk, strokeWidth: Double, blurLayers: [TextBlurLayer] = [], gradient: TextGradient? = nil, glyphs: [PositionedGlyph]? = nil, fontVariations: [String: Double] = [:]) {
        self.fontVariations = fontVariations
        self.text = text; self.fontAssetID = fontAssetID; self.fontSize = fontSize; self.x = x; self.baselineY = baselineY
        self.letterSpacing = letterSpacing; self.shaped = shaped; self.fill = fill; self.stroke = stroke; self.strokeWidth = strokeWidth; self.blurLayers = blurLayers; self.gradient = gradient; self.glyphs = glyphs
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "fontAssetId", "fontSize", "fontVariations", "x", "baselineY", "letterSpacing", "shaped", "fill", "stroke", "strokeWidth", "blurLayers", "gradient", "glyphs"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); fontAssetID = try c.decode(String.self, forKey: .fontAssetID)
        fontVariations = try c.decodeIfPresent([String: Double].self, forKey: .fontVariations) ?? [:]
        fontSize = try c.decode(Double.self, forKey: .fontSize); x = try c.decode(Double.self, forKey: .x)
        baselineY = try c.decode(Double.self, forKey: .baselineY); letterSpacing = try c.decode(Double.self, forKey: .letterSpacing)
        shaped = try c.decode(Bool.self, forKey: .shaped); fill = try c.decode(TextInk.self, forKey: .fill)
        glyphs = try c.decodeIfPresent([PositionedGlyph].self, forKey: .glyphs)
        gradient = try c.decodeIfPresent(TextGradient.self, forKey: .gradient)
        blurLayers = try c.decodeIfPresent([TextBlurLayer].self, forKey: .blurLayers) ?? []
        stroke = try c.decode(TextInk.self, forKey: .stroke); strokeWidth = try c.decode(Double.self, forKey: .strokeWidth)
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(text, forKey: .text); try c.encode(fontAssetID, forKey: .fontAssetID)
        try c.encode(fontSize, forKey: .fontSize); try c.encode(x, forKey: .x); try c.encode(baselineY, forKey: .baselineY)
        try c.encode(letterSpacing, forKey: .letterSpacing); try c.encode(shaped, forKey: .shaped)
        try c.encode(fill, forKey: .fill); try c.encode(stroke, forKey: .stroke); try c.encode(strokeWidth, forKey: .strokeWidth)
        try c.encode(blurLayers, forKey: .blurLayers); try c.encodeIfPresent(gradient, forKey: .gradient); try c.encodeIfPresent(glyphs, forKey: .glyphs)
        if !fontVariations.isEmpty { try c.encode(fontVariations, forKey: .fontVariations) }
    }
    func validate() throws {
        guard fontVariations.count <= 16,
              fontVariations.allSatisfy({ $0.key.utf8.count == 4 && $0.key.utf8.allSatisfy { (32...126).contains($0) } && $0.value.isFinite && abs($0.value) <= 65536 }) else { throw RecipeError.invalidTimeline }
        guard (shaped || glyphs != nil), !text.isEmpty, text.unicodeScalars.count <= 2000, !fontAssetID.isEmpty, fontAssetID.count <= 160,
              [fontSize, x, baselineY, letterSpacing, strokeWidth].allSatisfy(\.isFinite),
              fontSize > 0, abs(x) <= 10000, abs(baselineY) <= 10000,
              (-100...1000).contains(letterSpacing), (0...100).contains(strokeWidth) else { throw RecipeError.invalidTimeline }
        try fill.validate(); try stroke.validate()
        guard blurLayers.count <= 8 else { throw RecipeError.invalidTimeline }
        for layer in blurLayers { try layer.validate() }
        try gradient?.validate()
        if let glyphs {
            guard (1...4000).contains(glyphs.count) else { throw RecipeError.invalidTimeline }
            for glyph in glyphs { try glyph.validate() }
        }
    }
}

public struct TextRevealBounds: Codable, Equatable, Sendable {
    public let left: Double
    public let top: Double
    public let right: Double
    public let bottom: Double
    public init(left: Double, top: Double, right: Double, bottom: Double) {
        self.left = left; self.top = top; self.right = right; self.bottom = bottom
    }
    private enum CodingKeys: String, CodingKey { case left, top, right, bottom }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["left", "top", "right", "bottom"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        left = try c.decode(Double.self, forKey: .left); top = try c.decode(Double.self, forKey: .top)
        right = try c.decode(Double.self, forKey: .right); bottom = try c.decode(Double.self, forKey: .bottom)
        try validate()
    }
    public func validate() throws {
        guard [left, top, right, bottom].allSatisfy({ $0.isFinite && abs($0) <= 20000 }), right > left, bottom > top else { throw RecipeError.invalidTimeline }
    }
}

public struct KaraokeContent: Codable, Equatable, Sendable {
    public let starts: [Double]
    public let highlight: TextInk
    public let activeOnly: Bool?
    public init(starts: [Double], highlight: TextInk, activeOnly: Bool? = nil) {
        self.starts = starts; self.highlight = highlight; self.activeOnly = activeOnly
    }
    private enum CodingKeys: String, CodingKey { case starts, highlight, activeOnly }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["starts", "highlight", "activeOnly"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        starts = try c.decode([Double].self, forKey: .starts)
        highlight = try c.decode(TextInk.self, forKey: .highlight)
        activeOnly = try c.decodeIfPresent(Bool.self, forKey: .activeOnly)
    }
    func validate(runs: [PositionedTextRun]) throws {
        try highlight.validate()
        if activeOnly == true && starts != starts.sorted() { throw RecipeError.invalidTimeline }
        guard (1...100).contains(starts.count), starts.count == runs.count,
              starts.allSatisfy({ $0.isFinite && (0...1800).contains($0) }),
              runs.allSatisfy({ (!$0.shaped || activeOnly == true) && $0.gradient == nil }) else { throw RecipeError.invalidTimeline }
    }
}

public struct PortableTextLayer: Codable, Equatable, Sendable {
    public enum Effect: String, Codable, Sendable { case captionPop = "caption-pop", `static`, none, fadeIn = "fade-in", scaleUp = "scale-up", slideUp = "slide-up", slideDown = "slide-down", slideIn = "slide-in", popIn = "pop-in", bounce, inkReveal = "ink-reveal", handwriting, typewriter, streamIn = "stream-in", smoothType = "smooth-type", staggeredSlice = "staggered-slice", dissolveOut = "dissolve-out", karaokeLine = "karaoke-line", lyricLine = "lyric-line" }
    public let id: String
    public let start: Double
    public let end: Double
    public let anchorX: Double
    public let anchorY: Double
    public let rotationDegrees: Double
    public let runs: [PositionedTextRun]
    public let motion: TextMotionParameters?
    public let revealBounds: TextRevealBounds?
    public let handwriting: HandwritingContent?
    public let discreteReveal: DiscreteRevealContent?
    public let smoothReveal: SmoothRevealContent?
    public let staggered: StaggeredContent?
    public let fade: TextFadeEnvelope?
    public let giantTitle: GiantTitleTransition?
    public let karaoke: KaraokeContent?
    public let dissolveSeed: UInt32?
    public let effect: Effect
    public let animationPhases: TextAnimationPhases?
    public let background: TextBackground?
    private enum CodingKeys: String, CodingKey { case id, start, end, anchorX, anchorY, rotationDegrees, runs, effect, motion, revealBounds, handwriting, discreteReveal, smoothReveal, staggered, dissolveSeed, karaoke, fade, giantTitle, animationPhases, background }
    public init(id: String, start: Double, end: Double, anchorX: Double, anchorY: Double,
                rotationDegrees: Double, runs: [PositionedTextRun], effect: Effect = .static, motion: TextMotionParameters? = nil, revealBounds: TextRevealBounds? = nil, handwriting: HandwritingContent? = nil, discreteReveal: DiscreteRevealContent? = nil, smoothReveal: SmoothRevealContent? = nil, staggered: StaggeredContent? = nil, dissolveSeed: UInt32? = nil, karaoke: KaraokeContent? = nil, fade: TextFadeEnvelope? = nil, giantTitle: GiantTitleTransition? = nil, animationPhases: TextAnimationPhases? = nil, background: TextBackground? = nil) {
        self.id = id; self.start = start; self.end = end; self.anchorX = anchorX; self.anchorY = anchorY
        self.rotationDegrees = rotationDegrees; self.runs = runs; self.effect = effect; self.motion = motion; self.revealBounds = revealBounds; self.handwriting = handwriting; self.discreteReveal = discreteReveal; self.smoothReveal = smoothReveal; self.staggered = staggered; self.dissolveSeed = dissolveSeed; self.karaoke = karaoke; self.fade = fade; self.giantTitle = giantTitle; self.animationPhases = animationPhases; self.background = background
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["id", "start", "end", "anchorX", "anchorY", "rotationDegrees", "runs", "effect", "motion", "revealBounds", "handwriting", "discreteReveal", "smoothReveal", "staggered", "dissolveSeed", "karaoke", "fade", "giantTitle", "animationPhases", "background"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id); start = try c.decode(Double.self, forKey: .start)
        end = try c.decode(Double.self, forKey: .end); anchorX = try c.decode(Double.self, forKey: .anchorX)
        anchorY = try c.decode(Double.self, forKey: .anchorY); rotationDegrees = try c.decode(Double.self, forKey: .rotationDegrees)
        animationPhases = try c.decodeIfPresent(TextAnimationPhases.self, forKey: .animationPhases)
        background = try c.decodeIfPresent(TextBackground.self, forKey: .background)
        motion = try c.decodeIfPresent(TextMotionParameters.self, forKey: .motion)
        revealBounds = try c.decodeIfPresent(TextRevealBounds.self, forKey: .revealBounds)
        handwriting = try c.decodeIfPresent(HandwritingContent.self, forKey: .handwriting)
        discreteReveal = try c.decodeIfPresent(DiscreteRevealContent.self, forKey: .discreteReveal)
        smoothReveal = try c.decodeIfPresent(SmoothRevealContent.self, forKey: .smoothReveal)
        staggered = try c.decodeIfPresent(StaggeredContent.self, forKey: .staggered)
        fade = try c.decodeIfPresent(TextFadeEnvelope.self, forKey: .fade)
        giantTitle = try c.decodeIfPresent(GiantTitleTransition.self, forKey: .giantTitle)
        karaoke = try c.decodeIfPresent(KaraokeContent.self, forKey: .karaoke)
        dissolveSeed = try c.decodeIfPresent(UInt32.self, forKey: .dissolveSeed)
        runs = try c.decode([PositionedTextRun].self, forKey: .runs); effect = try c.decode(Effect.self, forKey: .effect)
    }
    public func validate(duration: Double, manifest: RenderAssetManifest?) throws {
        guard !id.isEmpty, id.count <= 160, [start, end, anchorX, anchorY, rotationDegrees].allSatisfy(\.isFinite),
              start >= 0, end > start, end <= min(1800, duration), abs(anchorX) <= 10000, abs(anchorY) <= 10000,
              abs(rotationDegrees) <= 3600, runs.count <= 100,
              runs.reduce(0, { $0 + $1.text.unicodeScalars.count }) <= 5000,
              runs.reduce(0, { $0 + ($1.glyphs?.count ?? 0) }) <= 10000 else { throw RecipeError.invalidTimeline }
        guard effect == .handwriting ? (handwriting != nil && runs.isEmpty) : (handwriting == nil && !runs.isEmpty) else { throw RecipeError.invalidTimeline }
        guard (effect == .karaokeLine) == (karaoke != nil) else { throw RecipeError.invalidTimeline }
        try karaoke?.validate(runs: runs)
        guard (effect == .lyricLine) == (fade?.kind == .lyric) else { throw RecipeError.invalidTimeline }
        if fade?.kind == .sequence {
            guard [.static, .none, .fadeIn, .handwriting, .inkReveal].contains(effect) else { throw RecipeError.invalidTimeline }
        }
        try fade?.validate()
        try giantTitle?.validate()
        if giantTitle != nil {
            guard [.static, .none, .fadeIn, .scaleUp, .slideUp, .slideDown, .slideIn, .popIn, .bounce, .inkReveal, .lyricLine, .typewriter, .streamIn, .karaokeLine, .smoothType, .staggeredSlice, .handwriting].contains(effect) else { throw RecipeError.invalidTimeline }
        }
        try handwriting?.validate()
        guard (effect == .typewriter || effect == .streamIn) == (discreteReveal != nil) else { throw RecipeError.invalidTimeline }
        try discreteReveal?.validate(runs: runs)
        guard (effect == .smoothType) == (smoothReveal != nil) else { throw RecipeError.invalidTimeline }
        try smoothReveal?.validate(runs: runs)
        guard (effect == .staggeredSlice) == (staggered != nil) else { throw RecipeError.invalidTimeline }
        try staggered?.validate()
        guard (effect == .dissolveOut) == (dissolveSeed != nil) else { throw RecipeError.invalidTimeline }
        guard (effect == .inkReveal) == (revealBounds != nil) else { throw RecipeError.invalidTimeline }
        try revealBounds?.validate()
        try motion?.validate()
        try animationPhases?.validate()
        try background?.validate()
        for run in runs + (discreteReveal?.lines.map(\.cursorRun) ?? []) + (staggered?.glyphs.map(\.run) ?? []) {
            try run.validate()
            guard let font = manifest?.assets.first(where: { $0.id == run.fontAssetID }),
                  case .library(catalog: .font, catalogID: _, generation: _) = font.source else { throw RecipeError.missingAssetReference }
        }
    }
}
