import Foundation
import Combine
import KriaMediaEngine
import OSLog

struct SlidePostText: Codable, Equatable, Sendable {
    var content: String
    var position: String
}

/// One styled text on a slide. Mirrors the server's `SlideTextElement`; any key this
/// client does not know yet is kept in `extra` and written back untouched, so a newer
/// server field survives an edit made on an older app.
struct SlidePostTextElement: Codable, Equatable, Identifiable, Sendable {
    static let defaultFont = "Inter-Bold"
    static let maxLength = 120
    /// The longest start of `text` the server takes (`maxLength` code points) that never splits a character:
    /// an emoji ZWJ sequence, a flag or a letter with its accent stays whole or is left out whole (KRI-826).
    static func clipped(_ text: String) -> String {
        var scalars = 0
        var kept = ""
        for character in text {
            scalars += character.unicodeScalars.count
            guard scalars <= maxLength else { break }
            kept.append(character)
        }
        return kept
    }
    static let sizeRange = 8...200
    var id: String
    var text: String
    var role = "text"
    var labelSource: String? = nil
    var edited = false
    var fontFamily = SlidePostTextElement.defaultFont
    var color = "#FFFFFF"
    var sizePx = 86
    var alignment = "center"
    var position = "bottom"
    var xFrac: Double? = nil
    var yFrac: Double? = nil
    var maxWidthFrac: Double? = nil
    var strokeWidth = 0
    var shadowEnabled = true
    var background = "none"
    var extra: [String: JSONValue] = [:]

    init(id: String = UUID().uuidString, text: String) { self.id = id; self.text = text }

    private struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ value: String) { stringValue = value }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }
    private static let knownKeys: Set<String> = [
        "id", "text", "role", "label_source", "edited", "font_family", "color", "size_px", "alignment", "position",
        "x_frac", "y_frac", "max_width_frac", "stroke_width", "shadow_enabled", "background",
    ]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        id = try c.decode(String.self, forKey: Key("id"))
        text = try c.decode(String.self, forKey: Key("text"))
        role = try c.decodeIfPresent(String.self, forKey: Key("role")) ?? role
        labelSource = try c.decodeIfPresent(String.self, forKey: Key("label_source"))
        edited = try c.decodeIfPresent(Bool.self, forKey: Key("edited")) ?? edited
        fontFamily = try c.decodeIfPresent(String.self, forKey: Key("font_family")) ?? fontFamily
        color = try c.decodeIfPresent(String.self, forKey: Key("color")) ?? color
        sizePx = try c.decodeIfPresent(Int.self, forKey: Key("size_px")) ?? sizePx
        alignment = try c.decodeIfPresent(String.self, forKey: Key("alignment")) ?? alignment
        position = try c.decodeIfPresent(String.self, forKey: Key("position")) ?? position
        xFrac = try c.decodeIfPresent(Double.self, forKey: Key("x_frac"))
        yFrac = try c.decodeIfPresent(Double.self, forKey: Key("y_frac"))
        maxWidthFrac = try c.decodeIfPresent(Double.self, forKey: Key("max_width_frac"))
        strokeWidth = try c.decodeIfPresent(Int.self, forKey: Key("stroke_width")) ?? strokeWidth
        shadowEnabled = try c.decodeIfPresent(Bool.self, forKey: Key("shadow_enabled")) ?? shadowEnabled
        background = try c.decodeIfPresent(String.self, forKey: Key("background")) ?? background
        for key in c.allKeys where !Self.knownKeys.contains(key.stringValue) {
            extra[key.stringValue] = try c.decode(JSONValue.self, forKey: key)
        }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        for (key, value) in extra where !Self.knownKeys.contains(key) { try c.encode(value, forKey: Key(key)) }
        try c.encode(id, forKey: Key("id"))
        try c.encode(text, forKey: Key("text"))
        try c.encode(role, forKey: Key("role"))
        try c.encodeIfPresent(labelSource, forKey: Key("label_source"))
        try c.encode(edited, forKey: Key("edited"))
        try c.encode(fontFamily, forKey: Key("font_family"))
        try c.encode(color, forKey: Key("color"))
        try c.encode(sizePx, forKey: Key("size_px"))
        try c.encode(alignment, forKey: Key("alignment"))
        try c.encode(position, forKey: Key("position"))
        try c.encodeIfPresent(xFrac, forKey: Key("x_frac"))
        try c.encodeIfPresent(yFrac, forKey: Key("y_frac"))
        try c.encodeIfPresent(maxWidthFrac, forKey: Key("max_width_frac"))
        try c.encode(strokeWidth, forKey: Key("stroke_width"))
        try c.encode(shadowEnabled, forKey: Key("shadow_enabled"))
        try c.encode(background, forKey: Key("background"))
    }

    /// The server's top / center / bottom bucket for this element (its legacy-mirror rule).
    var legacyPositionBucket: String {
        guard position == "custom" else { return position }
        let y = yFrac ?? 0.5
        return y < 0.33 ? "top" : (y > 0.66 ? "bottom" : "center")
    }
    /// Copies everything about how the text looks, including where it sits. Never the words,
    /// identity or label provenance.
    mutating func copyStyle(from other: Self) {
        fontFamily = other.fontFamily; color = other.color; sizePx = other.sizePx; alignment = other.alignment
        position = other.position; xFrac = other.xFrac; yFrac = other.yFrac; maxWidthFrac = other.maxWidthFrac
        strokeWidth = other.strokeWidth; shadowEnabled = other.shadowEnabled; background = other.background
        // Style keys not typed yet (rotation, outline colour, shadow, preset ...) move as a set: a default
        // on the source clears the target's value instead of leaving it behind.
        for key in Self.styleExtraKeys { extra[key] = nil }
        // `wrap_lines` belongs to the words, not the look: each text keeps its own line breaking.
        for (key, value) in other.extra where key != Self.wrapLinesKey { extra[key] = value }
    }
    static let wrapLinesKey = "wrap_lines"
    /// False once the creator has typed the text: only their own line breaks apply, exactly like the video
    /// editor's text (KRI-508). Absent = the legacy auto-wrap at `maxWidthFrac`.
    var wrapsLines: Bool { extra[Self.wrapLinesKey] != .bool(false) }
    var isInvalid: Bool {
        // The server counts code points, not grapheme clusters, so count unicode scalars.
        text.isEmpty || text.unicodeScalars.count > Self.maxLength || fontFamily.isEmpty || !Self.sizeRange.contains(sizePx)
            || (maxWidthFrac.map { !(0.2...1).contains($0) } ?? false)
            || !["left", "center", "right"].contains(alignment) || !["top", "center", "bottom", "custom"].contains(position)
            || !["none", "box"].contains(background) || !Self.isHex(color) || !Self.strokeRange.contains(strokeWidth)
            || [xFrac, yFrac].contains { $0.map { !(0...1).contains($0) } ?? false }
    }
    /// Font chips for the Style tab: the default face first, then every live registry font, with
    /// faces that share a font file collapsed (the registry's "Inter" is the default Inter-Bold).
    /// The server accepts every registry name, so no chip can be rejected on save.
    static func fontChoices(catalog: NativeFontCatalog = .shared) -> [String] {
        var seen = Set<String>()
        func key(_ name: String) -> String { catalog.fontURL(for: name)?.lastPathComponent ?? name }
        return ([defaultFont] + catalog.pickerFonts).filter { seen.insert(key($0)).inserted }
    }
    static func isHex(_ value: String) -> Bool {
        value.count == 7 && value.hasPrefix("#") && value.dropFirst().allSatisfy(\.isHexDigit)
    }
}

struct SlidePostEdits: Codable, Equatable, Sendable {
    static let maxTexts = 4
    /// KRI-460: pictures pasted onto one slide (the server's cap).
    static let maxStickers = 4
    var text: SlidePostText? = nil
    var lookPreset: String = "none"
    /// The rich model. nil = an older draft that only has the single legacy `text`.
    var texts: [SlidePostTextElement]? = nil
    /// KRI-44: the slide's shape and crop. nil = portrait, centred cover (and, on save, "clear any stored framing").
    var framing: SlidePostFraming? = nil
    /// KRI-606: show the whole photo with bars instead of cropping it. nil = fill. A sibling of `framing` (see `SlidePostFit`).
    var fit: SlidePostFit? = nil
    /// KRI-589: freehand ink over the slide (static, no animation). nil = none (and, on save, "clear any stored drawing").
    var drawing: SlidePostDrawing? = nil
    /// KRI-460: pictures pasted onto the slide, bottom to top (the last one is on top). nil = none.
    var stickers: [SlidePostSticker]? = nil
    /// KRI-1138: the photo or video turned to any angle over a backdrop. nil = upright (and, on save, "clear any stored tilt").
    var tilt: SlidePostTilt? = nil
    enum CodingKeys: String, CodingKey { case text, texts, framing, fit, drawing, stickers, tilt; case lookPreset = "look_preset" }
    init(text: SlidePostText? = nil, lookPreset: String = "none", texts: [SlidePostTextElement]? = nil, framing: SlidePostFraming? = nil, fit: SlidePostFit? = nil,
         drawing: SlidePostDrawing? = nil, stickers: [SlidePostSticker]? = nil, tilt: SlidePostTilt? = nil) {
        self.text = text; self.lookPreset = lookPreset; self.texts = texts; self.framing = framing; self.fit = fit; self.drawing = drawing; self.stickers = stickers
        self.tilt = tilt
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decodeIfPresent(SlidePostText.self, forKey: .text)
        lookPreset = try c.decodeIfPresent(String.self, forKey: .lookPreset) ?? "none"
        texts = try c.decodeIfPresent([SlidePostTextElement].self, forKey: .texts)
        framing = try c.decodeIfPresent(SlidePostFraming.self, forKey: .framing)
        // Tolerant: a treatment this build does not know shows as fill rather than failing the whole post.
        fit = (try? c.decodeIfPresent(SlidePostFit.self, forKey: .fit)) ?? nil
        // Strict, unlike `fit`: dropping a drawing this build cannot read would make the next save send `null` and
        // erase ink the server still holds. The server validates it, so a failure here means a contract break.
        drawing = try c.decodeIfPresent(SlidePostDrawing.self, forKey: .drawing)
        // Strict for the same reason: a sticker this build cannot read must not silently vanish from the next save.
        stickers = try c.decodeIfPresent([SlidePostSticker].self, forKey: .stickers)
        // Strict as well (KRI-1138): a turn this build cannot read must not silently vanish from the next save.
        tilt = try c.decodeIfPresent(SlidePostTilt.self, forKey: .tilt)
    }
    /// An empty list means the same as none, so a sticker that was added and removed again is not an unsaved change.
    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.text == rhs.text && lhs.lookPreset == rhs.lookPreset && lhs.texts == rhs.texts && lhs.framing == rhs.framing
            && lhs.fit == rhs.fit && lhs.drawing == rhs.drawing && lhs.effectiveStickers == rhs.effectiveStickers && lhs.tilt == rhs.tilt
    }
    /// Writes both shapes: `texts`, and the legacy `text` mirroring `texts[0]` (the server's own rule).
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        if let texts {
            try c.encode(texts, forKey: .texts)
            try c.encodeIfPresent(texts.first.map { SlidePostText(content: $0.text, position: $0.legacyPositionBucket) }, forKey: .text)
        } else {
            try c.encodeIfPresent(text, forKey: .text)
        }
        try c.encode(lookPreset, forKey: .lookPreset)
        // The server reads the key's presence: missing keeps what it stored (an older client), `null` clears it,
        // a value sets it. This build always says which, so removing a crop can never be mistaken for "unchanged".
        if let framing { try c.encode(framing, forKey: .framing) } else { try c.encodeNil(forKey: .framing) }
        // Same three-state contract for Fit (KRI-606): a value sets it, `null` clears it, and a missing key (a
        // build that predates it) keeps what the server stored.
        if let fit { try c.encode(fit, forKey: .fit) } else { try c.encodeNil(forKey: .fit) }
        // And for the drawing (KRI-589, protocol 7): a value sets it, `null` clears it, a missing key (a build below 7)
        // keeps what the server stored.
        if let drawing { try c.encode(drawing, forKey: .drawing) } else { try c.encodeNil(forKey: .drawing) }
        // And for stickers (KRI-460, protocol 8), except that nothing is ever `null`: a list sets them, an EMPTY list
        // clears them (the creator removed the last one), and no key at all keeps what the server stored. A draft that
        // never had a sticker therefore stays byte-identical to before this feature.
        if let stickers { try c.encode(stickers, forKey: .stickers) }
        // And for the tilt (KRI-1138, protocol 28): a value sets it, `null` clears it, a missing key (a build below 28)
        // keeps what the server stored.
        if let tilt { try c.encode(tilt, forKey: .tilt) } else { try c.encodeNil(forKey: .tilt) }
    }
    /// What the editor shows: `texts`, or the legacy text lifted into one boxed element.
    var effectiveTexts: [SlidePostTextElement] {
        if let texts { return texts }
        guard let text else { return [] }
        var element = SlidePostTextElement(id: "legacy", text: text.content)
        element.position = text.position; element.background = "box"; element.shadowEnabled = false
        return [element]
    }
    /// Replaces the texts and re-derives the legacy mirror so the value always equals its own round trip.
    mutating func setTexts(_ new: [SlidePostTextElement]) {
        texts = new
        text = new.first.map { SlidePostText(content: $0.text, position: $0.legacyPositionBucket) }
    }
}
/// KRI-589: a slide's freehand ink. Mirrors the server's `SlideDrawing` (`{canvas_aspect, strokes}`) and reuses its
/// `DrawingStroke`; `tests/fixtures/slide-drawing-parity/v1.json` pins the wire. Strokes only, never a PNG: every
/// renderer paints them (`NativeDrawingPainter` here), and the identity-transform geometry is aspect-fit of
/// `canvasAspect` into the output canvas (stroke width = `width_frac * min(W, H)`).
///
/// The slide API's decoder has no `convertFromSnakeCase`, so the wire is spelled out here and the stroke parse is
/// the strict one the editor's `drawings` lane uses (`DrawingStrokeWire`): an unknown key or an out-of-range value
/// fails to decode, exactly like the fixture's `invalid` entries.
struct SlidePostDrawing: Codable, Equatable, Sendable {
    static let maxStrokes = DrawingLimits.maxStrokesPerDrawing
    static let maxPointsPerSlide = DrawingLimits.maxPointsPerDrawing
    static let maxPointsPerPost = DrawingLimits.maxPointsPerVariant
    private static let keys: Set<String> = ["canvas_aspect", "strokes"]

    /// Width / height of the canvas the strokes were drawn on (4 decimals, like the points).
    var canvasAspect: Double
    var strokes: [DrawingStroke]

    init(canvasAspect: Double, strokes: [DrawingStroke]) { self.canvasAspect = canvasAspect; self.strokes = strokes }

    init?(json: JSONValue) {
        guard case .object(let object) = json, Set(object.keys).isSubset(of: Self.keys),
              let aspect = object["canvas_aspect"]?.numberValue, let rows = object["strokes"]?.arrayValue else { return nil }
        var strokes: [DrawingStroke] = []
        for row in rows {
            guard case .object(let stroke) = row, let parsed = DrawingStrokeWire.parse(stroke) else { return nil }
            strokes.append(parsed)
        }
        self.init(canvasAspect: aspect, strokes: strokes)
        guard isValid else { return nil }
    }
    init(from decoder: Decoder) throws {
        guard let value = SlidePostDrawing(json: try JSONValue(from: decoder)) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Invalid slide drawing"))
        }
        self = value
    }
    func encode(to encoder: Encoder) throws { try json.encode(to: encoder) }

    var json: JSONValue {
        .object(["canvas_aspect": .number(canvasAspect), "strokes": .array(strokes.map(DrawingStrokeWire.json))])
    }
    var pointCount: Int { strokes.reduce(0) { $0 + $1.points.count } }
    /// The server's `SlideDrawing` rules (the post-wide point cap is the draft's: `SlidePostDraft.validationMessage`).
    var isValid: Bool {
        canvasAspect.isFinite && canvasAspect > 0.2 && canvasAspect < 5
            && (1...Self.maxStrokes).contains(strokes.count) && pointCount <= Self.maxPointsPerSlide
            && strokes.allSatisfy { (try? $0.validate()) != nil }
    }
}

struct SlidePostSlide: Codable, Equatable, Identifiable, Sendable {
    var id: String
    var assetID: String
    var kind: String
    var alt: String? = nil
    var edits: SlidePostEdits? = nil
    enum CodingKeys: String, CodingKey { case id, kind, alt, edits; case assetID = "asset_id" }
}
struct SlidePostDraft: Codable, Equatable, Sendable {
    var schemaVersion: Int = 1
    var version: Int
    var platformProfile: String
    var slides: [SlidePostSlide]
    var coverIndex: Int = 0
    var caption: String = ""
    var renderedVersion: Int? = nil
    var userEdited: Bool = false
    /// Opaque server binding for the request that produced this draft.
    var briefBinding: JSONValue? = nil
    /// KRI-606: the post-wide size (`SlidePostSize` raw value). nil = the platform's default size.
    var aspect: String? = nil
    enum CodingKeys: String, CodingKey {
        case version, slides, caption, aspect
        case schemaVersion = "schema_version", platformProfile = "platform_profile", coverIndex = "cover_index"
        case renderedVersion = "rendered_version", userEdited = "user_edited"
        case briefBinding = "brief_binding"
    }
    /// Written by hand so `aspect` is ALWAYS present (`null` when default): the server treats a missing key as
    /// "an older app, keep what is stored", so clearing a size must be said, not omitted.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(schemaVersion, forKey: .schemaVersion)
        try c.encode(version, forKey: .version)
        try c.encode(platformProfile, forKey: .platformProfile)
        try c.encode(slides, forKey: .slides)
        try c.encode(coverIndex, forKey: .coverIndex)
        try c.encode(caption, forKey: .caption)
        try c.encodeIfPresent(renderedVersion, forKey: .renderedVersion)
        try c.encode(userEdited, forKey: .userEdited)
        try c.encodeIfPresent(briefBinding, forKey: .briefBinding)
        if let aspect { try c.encode(aspect, forKey: .aspect) } else { try c.encodeNil(forKey: .aspect) }
    }
    /// The post's size, resolved against its platform.
    var size: SlidePostSize { SlidePostSize.resolved(aspect, profile: platformProfile) }
    /// Rendering stamps and server metadata do not turn a clean editor dirty.
    func hasSameContent(as other: Self) -> Bool {
        platformProfile == other.platformProfile && aspect == other.aspect && slides == other.slides
            && coverIndex == other.coverIndex && caption == other.caption
    }
    /// What the platform cannot represent goes back to something it can, as the server's validator does: an
    /// Instagram carousel has no landscape slide (and loses its landscape-shaped crop), and a size the platform
    /// does not offer falls back to the platform's default. Fit is a photo treatment, so videos never keep it.
    func withProfileCoercions() -> SlidePostDraft {
        var copy = self
        if !SlidePostFraming.allowsLandscape(profile: platformProfile) {
            for index in copy.slides.indices where copy.slides[index].edits?.framing?.orientation == .landscape {
                copy.slides[index].edits?.framing = nil
            }
        }
        copy.aspect = SlidePostSize.storedAspect(aspect, profile: platformProfile)
        for index in copy.slides.indices where copy.slides[index].kind == "video" && copy.slides[index].edits?.fit != nil {
            copy.slides[index].edits?.fit = nil
        }
        return copy
    }
    /// Every stroke point in the post; the server caps it (KRI-589).
    var drawingPointCount: Int { slides.reduce(0) { $0 + ($1.edits?.drawing?.pointCount ?? 0) } }
    var validationMessage: String? {
        guard ["tiktok_photo", "instagram_carousel"].contains(platformProfile) else { return "Choose a supported post format." }
        let minCount = platformProfile == "instagram_carousel" ? 2 : 1
        let maxCount = platformProfile == "instagram_carousel" ? 20 : 35
        guard (minCount...maxCount).contains(slides.count) else { return "Choose \(minCount)–\(maxCount) slides for this format." }
        guard slides.indices.contains(coverIndex), Set(slides.map(\.id)).count == slides.count,
              Set(slides.map(\.assetID)).count == slides.count else { return "Review the slide order and cover before saving." }
        guard caption.count <= 2200 else { return "Keep the caption under 2,200 characters." }
        guard !slides.contains(where: { !["image", "video"].contains($0.kind) }) else { return "This post contains an unsupported media type." }
        if platformProfile == "tiktok_photo" && slides.contains(where: { $0.kind == "video" }) { return "Choose Instagram carousel to include videos." }
        for slide in slides {
            if let texts = slide.edits?.texts {
                if texts.count > SlidePostEdits.maxTexts || Set(texts.map(\.id)).count != texts.count || texts.contains(where: \.isInvalid) {
                    return "Slide text needs 1–120 characters, and a slide holds up to 4 texts."
                }
            } else if let text = slide.edits?.text, text.content.isEmpty || text.content.count > 120 || !["top", "center", "bottom"].contains(text.position) {
                return "Slide text needs 1–120 characters and a top, center, or bottom position."
            }
            if let look = slide.edits?.lookPreset, !Self.lookPresets.contains(look) { return "Choose a supported look." }
            if let drawing = slide.edits?.drawing, !drawing.isValid { return "A drawing on one slide is too detailed. Clear it or draw less." }
            if let stickers = slide.edits?.stickers, stickers.count > SlidePostEdits.maxStickers || Set(stickers.map(\.id)).count != stickers.count
                || stickers.contains(where: { !$0.isValid }) {
                return "A slide holds up to \(SlidePostEdits.maxStickers) pictures, each one on the slide and no smaller than 5% of its width."
            }
        }
        if drawingPointCount > SlidePostDrawing.maxPointsPerPost { return "Your drawings are too detailed to save. Clear some of them." }
        return nil
    }
    static let lookPresets = ["none", "stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"]
}
struct SlidePostAsset: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let kind: String
    let status: String
    var sourceFilename: String? = nil
    var displayURL: URL? = nil
    var previewURL: URL? = nil
    var sourceURL: URL? = nil
    var durationS: Double? = nil
    var mediaStatus: String? = nil
    /// When/where the photo or video was taken (KRI-300); nil when the phone sent none.
    var capture: SlidePostAssetCapture? = nil
    /// KRI-820: why analysis failed (`analysis_timed_out`, …), whether Retry can fix it, and
    /// while in flight the server's declared time by which it resolves (ISO-8601).
    var errorCode: String? = nil
    var retryable: Bool? = nil
    var analysisDeadlineAt: String? = nil
    enum CodingKeys: String, CodingKey {
        case id, kind, status, capture, retryable
        case sourceFilename = "source_filename", displayURL = "display_url", previewURL = "preview_url", sourceURL = "source_url"
        case durationS = "duration_s", mediaStatus = "media_status"
        case errorCode = "error_code", analysisDeadlineAt = "analysis_deadline_at"
    }
}

/// KRI-820: which pool assets may hold a slide post back. Only media the draft actually uses (its
/// slides and their stickers) can block export or asking Kria; a photo still preparing, or one that
/// failed or timed out, outside the draft never blocks the ready slides. A photo whose AI analysis
/// failed but whose bytes the server verified (`media_status == "ready"`) stays placeable, matching
/// the server's slide routes (`_owned_ready_slide_assets`).
enum SlidePostAssetGate {
    static let inFlightStatuses: Set<String> = ["pending", "queued", "uploaded", "processing", "analyzing", "uploading"]

    static func isInFlight(_ asset: SlidePostAsset) -> Bool { inFlightStatuses.contains(asset.status) }

    /// Still preparing inside the server's declared bound (`analysis_deadline_at`). Asking Kria
    /// waits for these, draft or not: the server proposes from ready rows only, so asking now
    /// would silently leave out photos the creator just added. Past the bound it no longer waits.
    static func isAwaited(_ asset: SlidePostAsset, now: Date = Date()) -> Bool {
        guard isInFlight(asset) else { return false }
        guard let deadline = asset.analysisDeadlineAt.flatMap(PlanBlock.parseDate) else { return true }
        return deadline > now
    }

    static func isUsable(_ asset: SlidePostAsset) -> Bool {
        guard !["missing", "expired"].contains(asset.mediaStatus ?? "") else { return false }
        // Verified bytes stay placeable while an analysis-only Retry runs (server keeps media ready).
        return asset.status == "ready" || (["failed", "queued", "analyzing"].contains(asset.status) && asset.mediaStatus == "ready")
    }

    /// Needs the creator: Retry or Remove (the row explains which).
    static func isFailed(_ asset: SlidePostAsset) -> Bool {
        !isUsable(asset) && (asset.status == "failed" || ["missing", "expired"].contains(asset.mediaStatus ?? ""))
    }

    static func usedAssetIDs(_ draft: SlidePostDraft?) -> Set<String> {
        guard let draft else { return [] }
        var ids = Set(draft.slides.map(\.assetID))
        for slide in draft.slides {
            for sticker in slide.edits?.stickers ?? [] { ids.insert(sticker.assetID) }
        }
        return ids
    }

    static func used(_ assets: [SlidePostAsset], by draft: SlidePostDraft?) -> [SlidePostAsset] {
        let ids = usedAssetIDs(draft)
        return assets.filter { ids.contains($0.id) }
    }
}
/// Mirrors the server's `ClipCapture`: every part may be absent. `captureTime` stays the ISO-8601 string
/// the server sends; use `date` for ordering.
struct SlidePostAssetCapture: Codable, Equatable, Sendable {
    var captureTime: String? = nil
    var coarseLocation: SlidePostAssetLocation? = nil
    var place: SlidePostAssetPlace? = nil
    enum CodingKeys: String, CodingKey { case place; case captureTime = "capture_time", coarseLocation = "coarse_location" }
    var date: Date? { captureTime.flatMap { ISO8601DateFormatter().date(from: $0) } }
}
struct SlidePostAssetLocation: Codable, Equatable, Sendable { let lat: Double; let lon: Double }
struct SlidePostAssetPlace: Codable, Equatable, Sendable {
    var subLocality: String? = nil
    var locality: String? = nil
    var country: String? = nil
    enum CodingKeys: String, CodingKey { case locality, country; case subLocality = "sub_locality" }
}
struct SlidePostRenderedSlide: Codable, Equatable, Identifiable, Sendable {
    let id: String
    let assetID: String
    let kind: String
    var url: URL? = nil
    var previewURL: URL? = nil
    enum CodingKeys: String, CodingKey { case id, kind, url; case assetID = "asset_id", previewURL = "preview_url" }
}
struct SlidePostValidationError: Codable, Equatable, Sendable {
    let code: String
    let message: String
    var slideID: String? = nil
    enum CodingKeys: String, CodingKey { case code, message; case slideID = "slide_id" }
}
struct SlidePostState: Codable, Equatable, Sendable {
    var schemaVersion: Int = 1
    let itemID: String
    let title: String
    var jobID: UUID? = nil
    var draft: SlidePostDraft? = nil
    var assets: [SlidePostAsset] = []
    var renderStatus: String = "not_rendered"
    var renderedVersion: Int? = nil
    var slides: [SlidePostRenderedSlide] = []
    var validationErrors: [SlidePostValidationError] = []
    var bundleURL: URL? = nil
    enum CodingKeys: String, CodingKey {
        case title, draft, assets, slides
        case schemaVersion = "schema_version", itemID = "item_id", jobID = "job_id", renderStatus = "render_status"
        case renderedVersion = "rendered_version", validationErrors = "validation_errors", bundleURL = "bundle_url"
    }
    var canExport: Bool {
        guard schemaVersion == 1, renderStatus == "ready", validationErrors.isEmpty,
              let draft, draft.validationMessage == nil, draft.version == renderedVersion,
              draft.renderedVersion == draft.version, slides.count == draft.slides.count else { return false }
        // Preserve exact output order and stable identity, not just an asset set.
        return zip(draft.slides, slides).allSatisfy { ref, output in
            ref.id == output.id && ref.assetID == output.assetID && ref.kind == output.kind && output.url?.scheme == "https"
        }
    }
}
struct SlidePostProposal: Codable, Equatable, Sendable {
    var draft: SlidePostDraft
    let baseVersion: Int
    let fallbackUsed: Bool
    let summary: String
    enum CodingKeys: String, CodingKey { case draft, summary; case baseVersion = "base_version", fallbackUsed = "fallback_used" }
}
struct SlidePostProposalRequest: Encodable, Sendable {
    let expectedVersion: Int
    let platformProfile: String
    let assetIDs: [String]?
    let instruction: String
    let clientRequestID: String?
    enum CodingKeys: String, CodingKey {
        case instruction
        case expectedVersion = "expected_version", platformProfile = "platform_profile", assetIDs = "asset_ids", clientRequestID = "client_request_id"
    }
}
struct SlidePostSaveRequest: Encodable, Sendable {
    let expectedVersion: Int
    let platformProfile: String
    let slides: [SlidePostSlide]
    let coverIndex: Int
    let caption: String
    let briefBinding: JSONValue?
    let aspect: String?
    init(draft: SlidePostDraft, expectedVersion: Int) {
        self.expectedVersion = expectedVersion; platformProfile = draft.platformProfile; slides = draft.slides
        coverIndex = draft.coverIndex; caption = draft.caption
        briefBinding = draft.briefBinding
        aspect = draft.aspect
    }
    enum CodingKeys: String, CodingKey {
        case slides, caption, aspect
        case expectedVersion = "expected_version", platformProfile = "platform_profile", coverIndex = "cover_index"
        case briefBinding = "brief_binding"
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(expectedVersion, forKey: .expectedVersion)
        try c.encode(platformProfile, forKey: .platformProfile)
        try c.encode(slides, forKey: .slides)
        try c.encode(coverIndex, forKey: .coverIndex)
        try c.encode(caption, forKey: .caption)
        try c.encodeIfPresent(briefBinding, forKey: .briefBinding)
        // Always said (`null` = the platform's default), never omitted: a missing key means "keep what is stored".
        if let aspect { try c.encode(aspect, forKey: .aspect) } else { try c.encodeNil(forKey: .aspect) }
    }
}

// MARK: Chat edit (KRI-298 Lane E)

/// One prior turn sent to the server (`user` | `assistant`).
/// Mirrors the server's `SlidePostChatTurn` (routes/plan_items.py): content <= 2000 chars,
/// applied/rejected <= 20 strings. Bounds are enforced here so the request never 422s.
struct SlidePostChatTurn: Codable, Equatable, Sendable {
    static let maxContent = 2000
    static let maxListed = 20
    let role: String
    let content: String
    let applied: [String]
    let rejected: [String]
    init(role: String, content: String, applied: [String] = [], rejected: [String] = []) {
        self.role = role
        self.content = String(content.prefix(Self.maxContent))
        self.applied = Array(applied.prefix(Self.maxListed))
        self.rejected = Array(rejected.prefix(Self.maxListed))
    }
}

struct SlidePostChatEditRequest: Encodable, Sendable {
    let message: String
    let expectedVersion: Int
    /// The editor's unsaved draft; nil when the editor is clean (the server edits its stored copy).
    let draft: SlidePostDraft?
    let turns: [SlidePostChatTurn]
    let clientRequestID: String
    init(message: String, expectedVersion: Int, draft: SlidePostDraft?, turns: [SlidePostChatTurn], clientRequestID: String) {
        self.message = message; self.expectedVersion = expectedVersion; self.turns = turns; self.clientRequestID = clientRequestID
        // A brand-new post's locally seeded draft carries version 0 (nothing saved yet), but the server's
        // draft model requires version >= 1 (`SlidePostDraft.version: ge=1`) and 422s the whole request.
        // The server ignores the client's version here (it re-stamps from the stored one), so send >= 1.
        var sent = draft
        if var value = sent, value.version < 1 { value.version = 1; sent = value }
        self.draft = sent
    }
    enum CodingKeys: String, CodingKey {
        case message, draft, turns
        case expectedVersion = "expected_version", clientRequestID = "client_request_id"
    }
}

struct SlidePostChatEditResponse: Decodable, Equatable, Sendable {
    enum Outcome: String, Decodable, Sendable { case edited, clarification, unsupported, noEffect = "no_effect", failed }
    let outcome: Outcome
    let reply: String
    let draft: SlidePostDraft?
    let baseVersion: Int
    var changes: [String] = []
    var suggestions: [String] = []
    enum CodingKeys: String, CodingKey { case outcome, reply, draft, changes, suggestions; case baseVersion = "base_version" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        outcome = try c.decode(Outcome.self, forKey: .outcome); reply = try c.decode(String.self, forKey: .reply)
        draft = try c.decodeIfPresent(SlidePostDraft.self, forKey: .draft); baseVersion = try c.decode(Int.self, forKey: .baseVersion)
        changes = try c.decodeIfPresent([String].self, forKey: .changes) ?? []
        suggestions = try c.decodeIfPresent([String].self, forKey: .suggestions) ?? []
    }
    init(outcome: Outcome, reply: String, draft: SlidePostDraft? = nil, baseVersion: Int, changes: [String] = [], suggestions: [String] = []) {
        self.outcome = outcome; self.reply = reply; self.draft = draft; self.baseVersion = baseVersion; self.changes = changes; self.suggestions = suggestions
    }
}

/// One bubble in the in-workspace Kria thread. `reply` text is server-composed and shown verbatim.
struct SlidePostChatMessage: Codable, Equatable, Identifiable, Sendable {
    var id = UUID()
    let role: String            // "user" | "assistant"
    let text: String
    var changes: [String] = []
    /// Set on a failed send: the message to resend with Retry.
    var retryText: String? = nil
    /// Completed provider-marked summaries are part of the local transcript;
    /// streaming and failed attempts are deliberately never persisted.
    var thoughts: [KriaThoughtSummary] = []
    var isUser: Bool { role == "user" }
    /// Notes about something Kria could NOT do read as a warning, not a success. Matches ONLY the
    /// server's fixed note phrasings in `compile_slide_post_ops`
    /// (src/apps/api/app/services/slide_post_chat_edit.py): "N photo(s) has/have no location." and
    /// "Animation and spacing aren't available on slides...". Keep in sync with that file; a loose
    /// substring match would flag success chips like "Text updated without ..." as warnings.
    static func isNote(_ change: String) -> Bool {
        let text = change.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.range(of: #"^\d+ photos? (has|have) no location\.?$"#, options: .regularExpression) != nil { return true }
        return text.hasPrefix("Animation and spacing aren't available on slides")
    }
    enum CodingKeys: String, CodingKey { case id, role, text, changes, retryText, thoughts }
    init(id: UUID = UUID(), role: String, text: String, changes: [String] = [], retryText: String? = nil, thoughts: [KriaThoughtSummary] = []) {
        self.id = id; self.role = role; self.text = text; self.changes = changes; self.retryText = retryText; self.thoughts = thoughts
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decodeIfPresent(UUID.self, forKey: .id) ?? UUID()
        role = try values.decode(String.self, forKey: .role)
        text = try values.decode(String.self, forKey: .text)
        changes = try values.decodeIfPresent([String].self, forKey: .changes) ?? []
        retryText = try values.decodeIfPresent(String.self, forKey: .retryText)
        thoughts = try values.decodeIfPresent([KriaThoughtSummary].self, forKey: .thoughts) ?? []
    }
}

extension KriaAPI {
    func slidePostChatEdit(itemID: String, body: SlidePostChatEditRequest) async throws -> SlidePostChatEditResponse {
        try await request(path: "plan-items/\(itemID)/slide-post/chat-edit", method: "POST", bodyData: JSONEncoder().encode(body), decode: SlidePostChatEditResponse.self)
    }
    func slidePost(itemID: String) async throws -> SlidePostState {
        try await request(path: "plan-items/\(itemID)/slide-post", method: "GET", bodyData: nil, decode: SlidePostState.self)
    }
    func proposeSlidePost(itemID: String, request body: SlidePostProposalRequest) async throws -> SlidePostProposal {
        try await request(path: "plan-items/\(itemID)/slide-post/propose", method: "POST", bodyData: JSONEncoder().encode(body), decode: SlidePostProposal.self)
    }
    func saveSlidePost(itemID: String, request body: SlidePostSaveRequest) async throws -> SlidePostDraft {
        let result = try await request(path: "plan-items/\(itemID)/slide-post", method: "PUT", bodyData: JSONEncoder().encode(body), decode: SlidePostItemResponse.self)
        guard let draft = result.draft else { throw APIError.invalidResponse }
        return draft
    }
    func generateSlidePost(itemID: String, expectedVersion: Int) async throws {
        let _: SlidePostItemResponse = try await request(path: "plan-items/\(itemID)/slide-post/generate", method: "POST", bodyData: JSONEncoder().encode(["expected_version": expectedVersion]), decode: SlidePostItemResponse.self)
    }
}
private struct SlidePostItemResponse: Decodable {
    let draft: SlidePostDraft?
    enum CodingKeys: String, CodingKey { case draft = "slide_post" }
}

/// A versioned, server-authoritative editor. Unsent text, proposals and local
/// edits survive navigation; local storage never contains signed media URLs.
@MainActor final class SlidePostSession: ObservableObject {
    @Published private(set) var state: SlidePostState?
    @Published var draft: SlidePostDraft? { didSet { persist() } }
    @Published var proposal: SlidePostProposal? { didSet { persist() } }
    @Published var selectedID: String? { didSet { persist(); if oldValue != selectedID { selectedStickerID = nil } } }
    @Published var instruction = "" { didSet { persist() } }
    @Published private(set) var isBusy = false
    /// The Kria thread for this post (chat-edit turns). Persisted with the rest of the local state.
    @Published private(set) var chat: [SlidePostChatMessage] = [] { didSet { persist() } }
    @Published private(set) var isChatting = false
    @Published private(set) var liveThoughtSummaries: [KriaThoughtSummary] = []
    @Published private(set) var proposalThoughtSummaries: [KriaThoughtSummary] = [] { didSet { persist() } }
    @Published private(set) var proposalRequestText: String? { didSet { persist() } }
    @Published var error: String?
    @Published private(set) var operationMessage: String?
    var isProposing: Bool { isBusy && operationMessage == "Kria is arranging your post…" }
    /// Text being edited on the canvas (not persisted; the text panel owns it). Selecting a text deselects a sticker.
    @Published var selectedTextID: String? { didSet { if selectedTextID != nil { selectedStickerID = nil } } }
    /// KRI-460: the pasted picture being moved / resized on the canvas (not persisted). Mutually exclusive with `selectedTextID`.
    @Published var selectedStickerID: String? { didSet { if selectedStickerID != nil { selectedTextID = nil } } }
    /// True while a pasted picture uploads and becomes a sticker.
    @Published private(set) var isPastingSticker = false
    /// File names of the pictures this device uploaded as stickers. A sticker's asset lives in the same pool as the
    /// slides' photos, so without this it would be appended to the post as a NEW SLIDE the moment it is ready (and again
    /// after a relaunch or a refresh). Names come from the picture's bytes and are never removed.
    private(set) var stickerUploadNames: Set<String> = []
    /// Why some freshly imported media did not become a slide (limit / photos-only). Cleared by the user.
    @Published var autoAppendNotice: String?
    /// Pool assets the session has already decided about (in the draft, removed, or skipped). Only an
    /// asset outside this set is appended automatically; nil until the pool was first looked at.
    private(set) var seenAssetIDs: Set<String>?
    /// The local draft always wins (KRI-298): a newer server version is rebased onto silently,
    /// so there is no conflict state. Kept as a constant for older call sites.
    var hasConflict: Bool { false }
    private var baseVersion = 0
    private var undoStack: [SlidePostDraft] = []
    private var redoStack: [SlidePostDraft] = []
    private var lastCoalesceKey: String?
    private static let maxHistory = 100
    private static let maxRebases = 2
    private var baselineDraft: SlidePostDraft?
    private var undoDraft: SlidePostDraft?
    private var itemID: String?
    private var restoring = false
    private var mutationGeneration = 0
    private var refreshSequence = 0
    private var appliedRefreshSequence = 0
    private let defaults: UserDefaults
    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    /// Pool assets that can become slides: everything except the pictures used (or being uploaded) as stickers.
    var slideAssets: [SlidePostAsset] { state?.assets.filter { !isStickerAsset($0) } ?? [] }
    var readyAssets: [SlidePostAsset] { slideAssets.filter(SlidePostAssetGate.isUsable) }
    /// A sticker's picture: referenced by a sticker of any draft we hold, or uploaded by a paste on this device.
    func isStickerAsset(_ asset: SlidePostAsset) -> Bool {
        if let name = asset.sourceFilename, stickerUploadNames.contains(BackgroundUploadCoordinator.displayFilename(name)) { return true }
        return [draft, proposal?.draft, state?.draft, baselineDraft].contains { candidate in
            candidate?.slides.contains { $0.edits?.stickers?.contains { $0.assetID == asset.id } == true } == true
        }
    }
    var selectedSlide: SlidePostSlide? { (draft ?? proposal?.draft)?.slides.first { $0.id == selectedID } ?? (draft ?? proposal?.draft)?.slides.first }
    var selectedAsset: SlidePostAsset? { state?.assets.first { $0.id == selectedSlide?.assetID } }
    var hasUnsavedChanges: Bool {
        guard let draft else { return false }
        guard let saved = state?.draft else { return true }
        return !draft.hasSameContent(as: saved)
    }
    var canExport: Bool { state?.canExport == true && !hasUnsavedChanges && !isBusy }
    /// Undo of the last SAVED change (the server's previous version).
    var canUndo: Bool { undoDraft != nil && !isBusy }
    /// Undo/redo of unsaved edits in this editor.
    var canUndoEdit: Bool { !undoStack.isEmpty && !isBusy }
    var canRedoEdit: Bool { !redoStack.isEmpty && !isBusy }
    var isRendering: Bool { ["pending", "queued", "generating", "rendering", "processing"].contains(state?.renderStatus ?? "") }

    func refresh(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy else { return }
        let generation = mutationGeneration
        refreshSequence += 1
        let sequence = refreshSequence
        do {
            let result = try await api.slidePost(itemID: itemID)
            // An upload refresh must not roll a completed save back, or let an
            // older poll replace a more recent render/status projection.
            guard generation == mutationGeneration, !isBusy, sequence >= appliedRefreshSequence else { return }
            guard result.schemaVersion == 1, result.itemID == itemID else { throw APIError.invalidResponse }
            if self.itemID != itemID {
                restoring = true
                self.itemID = itemID
                restore()
                restoring = false
            }
            appliedRefreshSequence = sequence
            adopt(result)
        } catch is CancellationError { } catch { self.error = error.localizedDescription }
    }

    /// Export requires an authoritative check; a cached ready projection is
    /// insufficient when another device can save a newer version.
    func revalidateForExport(api: any KriaAPIClient, itemID: String) async throws {
        guard !isBusy, !hasUnsavedChanges else { throw APIError.conflict }
        let generation = mutationGeneration
        let result = try await api.slidePost(itemID: itemID)
        guard result.schemaVersion == 1, result.itemID == itemID else { throw APIError.invalidResponse }
        guard generation == mutationGeneration, !isBusy, !hasUnsavedChanges,
              (result.draft?.version ?? 0) >= baseVersion else { throw APIError.conflict }
        // Invalidate background GETs that began before this authoritative read.
        mutationGeneration += 1
        adopt(result)
        guard canExport else { throw APIError.conflict }
    }

    private func adopt(_ result: SlidePostState) {
        let remoteVersion = result.draft?.version ?? 0
        guard remoteVersion >= baseVersion else { return }
        // Without a remembered baseline (a fresh session, or a restored draft saved before baselines were
        // kept) the server's draft IS the baseline: a local copy equal to it is clean, so a post just
        // opened from the gallery never reads "Unsaved changes". With no server draft either, the local
        // draft is a never-saved seed and stays unsaved.
        let wasDirty = draft.map { local in
            (baselineDraft ?? result.draft).map { !local.hasSameContent(as: $0) } ?? true
        } ?? false
        state = result
        baselineDraft = result.draft
        // A clean copy follows the server; unsaved edits stay and simply rebase onto the new version.
        if !wasDirty {
            // Undo must never restore a snapshot taken before a different remote draft was adopted.
            let changed: Bool
            if let local = draft, let remote = result.draft { changed = !local.hasSameContent(as: remote) } else { changed = (draft == nil) != (result.draft == nil) }
            if changed { undoStack = []; redoStack = []; lastCoalesceKey = nil }
            draft = result.draft
        }
        baseVersion = remoteVersion
        if let selectedID, (draft ?? proposal?.draft)?.slides.contains(where: { $0.id == selectedID }) != true {
            self.selectedID = (draft ?? proposal?.draft)?.slides.first?.id
        } else if selectedID == nil { selectedID = (draft ?? proposal?.draft)?.slides.first?.id }
        persist()
    }

    func discardLocalChanges() {
        guard let state else { return }
        error = nil; proposal = nil; proposalThoughtSummaries = []; proposalRequestText = nil; undoDraft = nil; undoStack = []; redoStack = []; lastCoalesceKey = nil
        baseVersion = state.draft?.version ?? 0; draft = state.draft
        selectedID = draft?.slides.first?.id
    }

    func propose(api: any SlidePostProposalClient, itemID: String, instruction: String, platformProfile: String? = nil) async {
        guard !isBusy else { return }
        // A never-saved draft is only the editor's starting point, so it does not block a proposal.
        guard !hasUnsavedChanges || state?.draft == nil else { error = "Save your slide edits before asking Kria for another direction."; return }
        let prompt = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty, prompt.count <= 2000 else { error = "Tell Kria your direction in 2,000 characters or fewer."; return }
        guard !readyAssets.isEmpty else { error = "Add photos or videos and wait for them to finish preparing."; return }
        mutationGeneration += 1
        let previousProposalRequestText = proposalRequestText
        self.instruction = instruction; proposalRequestText = prompt; isBusy = true; error = nil; liveThoughtSummaries = []; operationMessage = "Kria is arranging your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            let profile = platformProfile ?? draft?.platformProfile ?? proposal?.draft.platformProfile ?? (readyAssets.contains(where: { $0.kind == "video" }) ? "instagram_carousel" : "tiktok_photo")
            let ids = draft?.slides.map(\.assetID) ?? proposal?.draft.slides.map(\.assetID) ?? readyAssets.map(\.id)
            // Media that arrives while Kria arranges is not in this request, so it must stay unseen.
            seenAssetIDs = (seenAssetIDs ?? []).union(ids)
            let requestID = UUID().uuidString
            let polling = Task { [api, itemID] in
                while !Task.isCancelled {
                    if let response = try? await api.slidePostThoughtSummaries(itemID: itemID, clientRequestID: requestID), response.clientRequestID == requestID {
                        guard !Task.isCancelled else { return }
                        self.liveThoughtSummaries = response.summaries.filter { $0.hasText }
                    }
                    try? await Task.sleep(for: .seconds(1))
                }
            }
            defer { polling.cancel() }
            let result = try await api.proposeSlidePost(itemID: itemID, request: .init(expectedVersion: baseVersion, platformProfile: profile, assetIDs: ids, instruction: prompt, clientRequestID: requestID))
            polling.cancel()
            if let response = try? await api.slidePostThoughtSummaries(itemID: itemID, clientRequestID: requestID), response.clientRequestID == requestID {
                liveThoughtSummaries = response.summaries.filter { $0.hasText }
            }
            guard result.baseVersion == baseVersion else { throw APIError.conflict }
            proposal = result
            proposalThoughtSummaries = completedThoughts()
            if selectedID == nil { selectedID = result.draft.slides.first?.id }
        } catch { liveThoughtSummaries = []; proposalRequestText = previousProposalRequestText; handle(error) }
    }

    func applyProposal(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, let proposal else { return }
        guard let message = proposal.draft.validationMessage else {
            await persistDraft(proposal.draft, api: api, itemID: itemID)
            return
        }
        error = message
    }

    func save(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, !isChatting, let draft else { return }
        if let message = draft.validationMessage { error = message; return }
        await persistDraft(draft, api: api, itemID: itemID)
    }

    private func persistDraft(_ value: SlidePostDraft, api: any KriaAPIClient, itemID: String) async {
        mutationGeneration += 1
        isBusy = true; error = nil; operationMessage = "Saving your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            let previous = state?.draft
            var rebases = 0
            var saved: SlidePostDraft
            while true {
                do {
                    saved = try await api.saveSlidePost(itemID: itemID, request: .init(draft: value, expectedVersion: baseVersion))
                    break
                } catch let failure as APIError where failure == .conflict && rebases < Self.maxRebases {
                    // The newest thing the user saw in the editor wins: learn the server's current
                    // version and write the local draft over it. No banner, no choice.
                    rebases += 1
                    let latest = try await api.slidePost(itemID: itemID)
                    guard latest.schemaVersion == 1, latest.itemID == itemID else { throw APIError.invalidResponse }
                    baseVersion = latest.draft?.version ?? baseVersion
                }
            }
            baseVersion = saved.version; baselineDraft = saved; proposal = nil; undoDraft = previous
            // Saved content is what the user was looking at; keep any later undo history but
            // swap in the server's stamps (version, rendered_version).
            draft = saved
            // Keep the old render in place (no flash); `canExport` compares versions, so it reads
            // stale until the follow-up render lands.
            state?.draft = saved
            state?.renderedVersion = saved.renderedVersion
            if state?.jobID != nil { state?.renderStatus = "rendering" }
            let result = try await api.slidePost(itemID: itemID)
            adopt(result)
        } catch { handle(error) }
    }

    // MARK: Chat edit

    static let maxChatTurns = 12
    /// Cap on the persisted transcript so UserDefaults never grows without bound.
    static let maxPersistedChat = 50

    /// Sends one chat message. An `edited` reply is STAGED (undoable, unsaved) -- never saved. Every
    /// other outcome only adds an assistant bubble. Returns true when a draft was staged.
    @discardableResult
    func chatEdit(api: any KriaAPIClient, itemID: String, message: String) async -> Bool {
        guard canChat(message: message), let current = draft else { return false }
        let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let turns = chatTurns()
        chat.append(.init(role: "user", text: text))
        isChatting = true; error = nil
        defer { isChatting = false }
        let requestID = UUID().uuidString
        let body = SlidePostChatEditRequest(message: text, expectedVersion: baseVersion, draft: hasUnsavedChanges ? current : nil,
                                            turns: Array(turns), clientRequestID: requestID)
        let thoughtPoll = Task { [api, itemID] in
            while !Task.isCancelled {
                if let response = try? await api.slidePostThoughtSummaries(itemID: itemID, clientRequestID: requestID), response.clientRequestID == requestID {
                    guard !Task.isCancelled else { return }
                    self.liveThoughtSummaries = response.summaries.filter { $0.hasText }
                }
                try? await Task.sleep(for: .seconds(1))
            }
        }
        defer { thoughtPoll.cancel() }
        let result: SlidePostChatEditResponse
        do { result = try await api.slidePostChatEdit(itemID: itemID, body: body) } catch is CancellationError { return false } catch {
            Self.logChatFailure(error)
            liveThoughtSummaries = []
            chat.append(.init(role: "assistant", text: Self.chatFailureMessage(error), retryText: text))
            return false
        }
        thoughtPoll.cancel()
        // The final response can beat the poll by a fraction of a second. Read
        // once more before writing history so a completed disclosure survives
        // closing and reopening the editor.
        if let response = try? await api.slidePostThoughtSummaries(itemID: itemID, clientRequestID: requestID), response.clientRequestID == requestID {
            liveThoughtSummaries = response.summaries.filter { $0.hasText }
        }
        guard result.outcome == .edited, let proposed = result.draft else {
            chat.append(.init(role: "assistant", text: result.reply, changes: result.changes, thoughts: completedThoughts())); return false
        }
        // The user changed the slides while Kria worked: never overwrite newer local edits.
        guard let latest = draft, latest.hasSameContent(as: current) else {
            chat.append(.init(role: "assistant", text: "Your slides changed while I was working, so I left them as they are. Ask me again.", thoughts: completedThoughts()))
            return false
        }
        var staged = proposed
        // The returned version is meaningless; keep the editor's stamps and quote the server's
        // current version on the next save.
        staged.version = latest.version; staged.renderedVersion = latest.renderedVersion; staged.userEdited = latest.userEdited
        baseVersion = result.baseVersion
        stageDraft(staged)
        if let selectedID, !staged.slides.contains(where: { $0.id == selectedID }) { self.selectedID = staged.slides.first?.id }
        selectedTextID = nil
        chat.append(.init(role: "assistant", text: result.reply, changes: result.changes, thoughts: completedThoughts()))
        return true
    }
    private func completedThoughts() -> [KriaThoughtSummary] {
        defer { liveThoughtSummaries = [] }
        return liveThoughtSummaries.filter { $0.status == .completed && $0.hasText }
    }
    /// Plain-language reason a chat send failed. Connection drops blame the connection; a server
    /// refusal never does (it was reached) and never leaks raw validation text.
    nonisolated static func chatFailureMessage(_ error: Error) -> String {
        if let api = error as? APIError {
            if let reason = api.conflictDetail { return "Kria couldn't apply that: \(reason)" }
            if case let .requestFailed(status, _) = api {
                if (500...599).contains(status) { return "Kria hit a problem on its side. Your slides are safe. Try again in a moment." }
                return "Kria couldn't use that edit request. Your slides are safe. Try again, or rephrase."
            }
            if case .offline = api { return "I couldn't reach Kria. Check your connection and try again." }
        }
        return "Something went wrong sending that. Your message is kept; try again."
    }
    private static func logChatFailure(_ error: Error) {
        if case let APIError.requestFailed(status, detail) = error {
            Logger(subsystem: "com.kria.app", category: "slidepost").error("chat-edit failed status=\(status, privacy: .public) detail=\(detail.message ?? "none", privacy: .public)")
        } else {
            Logger(subsystem: "com.kria.app", category: "slidepost").error("chat-edit failed: \(String(describing: error), privacy: .public)")
        }
    }
    /// Whether `message` would be sent; sets the user-facing error when it is too long. Callers that
    /// clear a composer must check this first so rejected text is never lost.
    func canChat(message: String) -> Bool {
        guard !isBusy, !isChatting, draft != nil else { return false }
        let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= SlidePostChatTurn.maxContent else {
            if !text.isEmpty { error = "Tell Kria what to change in 2,000 characters or fewer." }
            return false
        }
        return true
    }

    /// Prior turns for the request: only user messages Kria actually answered (a failed send leaves a
    /// dangling user bubble followed by a retry bubble, which are both skipped).
    func chatTurns() -> [SlidePostChatTurn] {
        var turns: [SlidePostChatTurn] = []
        var index = 0
        while index < chat.count {
            let message = chat[index]
            if message.isUser {
                if index + 1 < chat.count, !chat[index + 1].isUser, chat[index + 1].retryText == nil {
                    turns.append(.init(role: "user", content: message.text))
                    turns.append(.init(role: "assistant", content: chat[index + 1].text, applied: chat[index + 1].changes))
                    index += 2; continue
                }
            } else if message.retryText == nil, turns.last?.role != "assistant" {
                // A standalone assistant message (e.g. restored state) with no user turn before it.
                turns.append(.init(role: "assistant", content: message.text, applied: message.changes))
            }
            index += 1
        }
        return Array(turns.suffix(Self.maxChatTurns))
    }

    /// Retry a failed send: drops the failure bubble and the user bubble it answered, then resends.
    func retryChat(api: any KriaAPIClient, itemID: String, bubble: SlidePostChatMessage) async {
        guard let text = bubble.retryText, let at = chat.firstIndex(where: { $0.id == bubble.id }), canChat(message: text) else { return }
        chat.remove(at: at)
        if at > 0, chat[at - 1].isUser, chat[at - 1].text == text { chat.remove(at: at - 1) }
        await chatEdit(api: api, itemID: itemID, message: text)
    }
    func clearChat() { chat = [] }

    func create(api: any KriaAPIClient, itemID: String) async {
        guard !isBusy, !isChatting else { return }
        error = nil
        if proposal != nil { await applyProposal(api: api, itemID: itemID) }
        else if hasUnsavedChanges { await save(api: api, itemID: itemID) }
        guard error == nil, let draft, draft.validationMessage == nil else { return }
        if isRendering { return }
        mutationGeneration += 1
        isBusy = true; error = nil; operationMessage = "Creating your post…"
        defer { isBusy = false; operationMessage = nil }
        do {
            try await api.generateSlidePost(itemID: itemID, expectedVersion: draft.version)
            state?.renderStatus = "rendering"; state?.slides = []; state?.bundleURL = nil
            let result = try await api.slidePost(itemID: itemID)
            adopt(result)
        } catch { handle(error) }
    }

    func undo(api: any KriaAPIClient, itemID: String) async {
        guard !isChatting, canUndo, let previous = undoDraft else { return }
        await persistDraft(previous, api: api, itemID: itemID)
    }

    // MARK: Unsaved-edit history

    /// The single way an edit reaches `draft`: pushes the previous draft onto the undo stack and
    /// clears redo. `coalescing` merges a run of the same gesture (typing, a slider drag) into one
    /// undo step. Also the entry point for a staged AI result (Lane E): stage it, show it, and the
    /// user can undo it before saving.
    func stageDraft(_ staged: SlidePostDraft, coalescing key: String? = nil) {
        // Switching to Instagram (a staged AI edit can) turns landscape slides back to portrait and drops a size the
        // platform lacks, as the server would.
        let new = staged.withProfileCoercions()
        // Dropped while a save/propose is in flight: the draft is being rebased onto the server's
        // answer, and the controls that call this are disabled/veiled in that window anyway.
        guard !isBusy, let current = draft, !new.hasSameContent(as: current) else { return }
        if key == nil || key != lastCoalesceKey {
            undoStack.append(current)
            if undoStack.count > Self.maxHistory { undoStack.removeFirst() }
        }
        lastCoalesceKey = key
        redoStack = []
        draft = new
    }
    func undoEdit() {
        guard !isBusy, let current = draft, let previous = undoStack.popLast() else { return }
        redoStack.append(current)
        restoreHistory(previous, over: current)
    }
    func redoEdit() {
        guard !isBusy, let current = draft, let next = redoStack.popLast() else { return }
        undoStack.append(current)
        restoreHistory(next, over: current)
    }
    private func restoreHistory(_ entry: SlidePostDraft, over current: SlidePostDraft) {
        var value = entry
        value.version = current.version; value.renderedVersion = current.renderedVersion; value.userEdited = current.userEdited
        lastCoalesceKey = nil
        draft = value
        if let selectedID, !value.slides.contains(where: { $0.id == selectedID }) { self.selectedID = value.slides.first?.id }
        if let selectedTextID, selectedSlide?.edits?.effectiveTexts.contains(where: { $0.id == selectedTextID }) != true { self.selectedTextID = nil }
        if let selectedStickerID, selectedSlide?.edits?.effectiveStickers.contains(where: { $0.id == selectedStickerID }) != true { self.selectedStickerID = nil }
    }

    // MARK: Slide edits

    func updateSlide(_ slide: SlidePostSlide) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slide.id }) else { return }
        value.slides[index] = slide
        stageDraft(value)
    }
    func setCover(id: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == id }) else { return }
        value.coverIndex = index
        stageDraft(value)
    }
    func setCaption(_ caption: String) {
        guard var value = draft else { return }
        value.caption = String(caption.prefix(2200))
        stageDraft(value, coalescing: "caption")
    }
    func moveSlide(id: String, offset: Int) {
        guard let from = draft?.slides.firstIndex(where: { $0.id == id }) else { return }
        moveSlide(id: id, toIndex: from + offset)
    }
    /// Reorders keeping the cover on the same slide (identity, not position).
    func moveSlide(id: String, toIndex to: Int) {
        guard var value = draft, let from = value.slides.firstIndex(where: { $0.id == id }),
              value.slides.indices.contains(to), to != from else { return }
        let coverID = value.slides[value.coverIndex].id
        let slide = value.slides.remove(at: from); value.slides.insert(slide, at: to)
        value.coverIndex = value.slides.firstIndex { $0.id == coverID } ?? 0
        stageDraft(value)
    }
    func removeSlide(id: String) {
        guard var value = draft, value.slides.count > 1 else { return }
        let coverID = value.slides[value.coverIndex].id
        value.slides.removeAll { $0.id == id }; value.coverIndex = value.slides.firstIndex { $0.id == coverID } ?? 0
        stageDraft(value)
        if selectedID == id { selectedID = value.slides.first?.id }
    }
    /// A post with no saved draft yet starts as an unsaved local draft of every ready asset, in pool
    /// order, so the editor is always the one rich layout (strip, text, look) instead of a separate
    /// "start your post" screen. Nothing reaches the server until the user saves. Returns true when seeded.
    @discardableResult
    func seedDraftIfNeeded() -> Bool {
        // A post that already has a server draft adopts it (see `adopt`); only a post with none is seeded.
        guard !isBusy, !isChatting, draft == nil, proposal == nil, let state, state.draft == nil, !readyAssets.isEmpty else { return false }
        let ready = readyAssets
        let profile = (ready.count >= 2 || ready.contains { $0.kind == "video" }) ? "instagram_carousel" : "tiktok_photo"
        let limit = SlidePostAutoAppend.maxSlides(profile: profile)
        let usable = ready.filter { profile != "tiktok_photo" || $0.kind != "video" }.prefix(limit)
        guard !usable.isEmpty else { return false }
        seenAssetIDs = Set(ready.map(\.id))
        let value = SlidePostDraft(
            version: baseVersion, platformProfile: profile,
            slides: usable.map { SlidePostSlide(id: UUID().uuidString, assetID: $0.id, kind: $0.kind) }
        )
        draft = value
        selectedID = value.slides.first?.id
        return true
    }
    /// Appends every ready pool asset the session has not decided about yet, as ONE undoable step.
    /// Safe to call on any poll or refresh: it defers (touching nothing) while a save/propose or an AI
    /// edit is in flight, so newer local edits and a staged AI result are never clobbered, and it never
    /// re-adds an asset the user removed. Returns the number of slides added.
    @discardableResult
    func appendNewlyReadyAssets() -> Int {
        guard !isBusy, !isChatting, proposal == nil, state != nil, let current = draft else { return 0 }
        let plan = SlidePostAutoAppend.plan(draft: current, ready: readyAssets, seen: seenAssetIDs)
        guard plan.seen != seenAssetIDs || !plan.toAppend.isEmpty else { return 0 }
        seenAssetIDs = plan.seen
        persist()
        if let notice = plan.notice { autoAppendNotice = notice }
        guard !plan.toAppend.isEmpty else { return 0 }
        var value = current
        if plan.switchedToInstagram { value.platformProfile = "instagram_carousel" }
        value.slides.append(contentsOf: plan.toAppend.map { SlidePostSlide(id: UUID().uuidString, assetID: $0.id, kind: $0.kind) })
        stageDraft(value)
        return plan.toAppend.count
    }
    func addAsset(id: String) {
        guard var value = draft,
              let asset = readyAssets.first(where: { $0.id == id }), !value.slides.contains(where: { $0.assetID == id }) else { return }
        value.slides.append(.init(id: UUID().uuidString, assetID: id, kind: asset.kind))
        stageDraft(value)
    }

    // MARK: Slide text

    /// Adds a text to the slide (up to four) and selects it. The first rich edit of a legacy slide
    /// carries its old text over as `texts[0]` so nothing is lost.
    @discardableResult
    func addText(slideID: String, text: String = "Your text") -> String? {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return nil }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        guard texts.count < SlidePostEdits.maxTexts else { return nil }
        var element = SlidePostTextElement(text: text)
        element.extra[SlidePostTextElement.wrapLinesKey] = .bool(false)
        texts.append(element)
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value)
        selectedTextID = element.id
        return element.id
    }
    /// KRI-647: the system Paste control on a slide. Text becomes a new text element, cut to the slide text limit; a
    /// picture becomes a sticker on this slide (KRI-460) once the server advertises it. Every refusal says why.
    func paste(providers: [NSItemProvider], slideID: String, stickers context: SlidePostStickerPaste? = nil) async {
        pasteNotice = nil
        switch await PasteboardIntake.load(from: providers) {
        case .text(let text):
            let fitted = SlidePostTextElement.clipped(text)
            if addText(slideID: slideID, text: fitted) == nil {
                pasteNotice = "A slide can have up to \(SlidePostEdits.maxTexts) texts. Remove one to paste another."
            } else if fitted != text {
                pasteNotice = "Pasted text was cut to \(SlidePostTextElement.maxLength) characters."
            }
        case .image(let url):
            await pasteImage(fileURL: url, slideID: slideID, context: context)
        case nil:
            pasteNotice = "Kria can’t use what’s on your clipboard. Copy text or a picture and try again."
        }
    }

    /// Why a picture can't go onto `slideID` right now, or nil. Checked BEFORE anything is uploaded.
    func stickerPasteRefusal(slideID: String, context: SlidePostStickerPaste?) -> String? {
        guard let context, context.isEnabled else { return "Pasting a picture onto a slide isn’t available yet." }
        guard let slide = draft?.slides.first(where: { $0.id == slideID }) else { return "Open a slide first, then paste." }
        guard slide.kind == "image" || slide.kind == "video" else { return "Pictures can’t go on this slide." }
        if slide.edits?.effectiveStickers.count ?? 0 >= SlidePostEdits.maxStickers {
            return "A slide can have up to \(SlidePostEdits.maxStickers) pictures. Remove one to paste another."
        }
        if isPastingSticker { return "Still adding your last picture. Try again in a moment." }
        if isBusy || isChatting { return "Wait for the current change to finish, then paste again." }
        if state == nil { return "Your post is still loading. Try again in a moment." }
        return nil
    }

    /// Uploads the picture into the post's pool, waits until it is ready, then places it as a sticker. The upload goes
    /// through the same background uploader the video editor's Paste uses; the picture's own file name finds its asset
    /// again (a second upload or a stale read can never hand back another one).
    func pasteImage(fileURL: URL, slideID: String, context: SlidePostStickerPaste?) async {
        if let refusal = stickerPasteRefusal(slideID: slideID, context: context) {
            try? FileManager.default.removeItem(at: fileURL)
            pasteNotice = refusal
            return
        }
        guard let context else { return }
        isPastingSticker = true
        defer { isPastingSticker = false }
        let filename = BackgroundUploadCoordinator.displayFilename(fileURL.lastPathComponent)
        // Before the upload starts: from this moment the picture's asset must never be offered as a slide.
        stickerUploadNames.insert(filename)
        persist()
        // Not deleted on refusal: a busy server leaves a Retry in the uploader's failures that re-reads this file.
        guard await context.uploads.uploadSticker(fileURL: fileURL, projectID: context.projectID, itemID: context.itemID) else {
            pasteNotice = "That picture couldn’t be added. Your slide is unchanged."
            return
        }
        for _ in 0..<Self.stickerPollAttempts {
            guard !Task.isCancelled, itemID == context.itemID else { return }
            await refresh(api: context.api, itemID: context.itemID)
            // Any asset with this name, not only a new one: the file is named from its bytes and the pool dedupes
            // identical uploads, so pasting the same picture again finds the copy it already holds.
            if let asset = state?.assets.first(where: { $0.sourceFilename.map(BackgroundUploadCoordinator.displayFilename) == filename }) {
                // Decided now, so even a relaunch before it is ready cannot turn it into a slide.
                seenAssetIDs = (seenAssetIDs ?? []).union([asset.id])
                persist()
                if asset.status == "ready", asset.mediaStatus != "missing" {
                    if addSticker(slideID: slideID, assetID: asset.id) == nil {
                        pasteNotice = draft?.slides.contains(where: { $0.id == slideID }) == true
                            ? "A slide can have up to \(SlidePostEdits.maxStickers) pictures. Remove one to paste another."
                            : "That slide is gone, so the picture wasn’t added."
                    }
                    return
                }
                if asset.status == "failed" || asset.mediaStatus == "missing" {
                    pasteNotice = "That picture couldn’t be prepared. Your slide is unchanged."
                    return
                }
            }
            try? await Task.sleep(for: Self.stickerPollInterval)
        }
        pasteNotice = "Adding the picture is taking longer than expected. Paste it again in a moment."
    }
    static var stickerPollAttempts = 120
    static var stickerPollInterval = Duration.seconds(1)

    /// Shown above every other banner so a paste is never silent, even while an error is up. Tap to dismiss.
    @Published var pasteNotice: String?

    /// Edits one text in place. `coalescing` collapses a continuous gesture into one undo step.
    func updateText(slideID: String, textID: String, coalescing key: String? = nil, _ mutate: (inout SlidePostTextElement) -> Void) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        guard let at = texts.firstIndex(where: { $0.id == textID }) else { return }
        let before = texts[at]
        mutate(&texts[at])
        if texts[at].role == "label", texts[at] != before { texts[at].edited = true }
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value, coalescing: key.map { "\($0)-\(textID)" })
    }
    func removeText(slideID: String, textID: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        texts.removeAll { $0.id == textID }
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value)
        if selectedTextID == textID { selectedTextID = texts.first?.id }
    }
    /// Copies a text onto the same slide (the slide itself cannot be duplicated: one asset, one slide).
    @discardableResult
    func duplicateText(slideID: String, textID: String) -> String? {
        guard let source = draft?.slides.first(where: { $0.id == slideID })?.edits?.effectiveTexts.first(where: { $0.id == textID }),
              var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return nil }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var texts = edits.effectiveTexts
        guard texts.count < SlidePostEdits.maxTexts else { return nil }
        var copy = source
        copy.id = UUID().uuidString
        if copy.position == "custom", let y = copy.yFrac { copy.yFrac = min(1, y + 0.08) }
        texts.append(copy)
        edits.setTexts(texts)
        value.slides[index].edits = edits
        stageDraft(value)
        selectedTextID = copy.id
        return copy.id
    }
    /// One undo step: every other slide's texts take this text's whole look, position included.
    /// Words, ids and label provenance are never copied.
    func applyStyleToAllSlides(slideID: String, textID: String) {
        guard var value = draft,
              let source = value.slides.first(where: { $0.id == slideID })?.edits?.effectiveTexts.first(where: { $0.id == textID }) else { return }
        for index in value.slides.indices where value.slides[index].id != slideID {
            guard var edits = value.slides[index].edits, !edits.effectiveTexts.isEmpty else { continue }
            var texts = edits.effectiveTexts
            // Only the matching text takes the look (and position); siblings keep their own place.
            let sourceIndex = value.slides.first(where: { $0.id == slideID })?.edits?.effectiveTexts.firstIndex(where: { $0.id == textID }) ?? 0
            texts[sourceIndex < texts.count ? sourceIndex : 0].copyStyle(from: source)
            edits.setTexts(texts)
            value.slides[index].edits = edits
        }
        stageDraft(value)
    }
    /// Sets (or, with nil/default, clears) a slide's shape and crop. Landscape is a TikTok-only shape.
    func setFraming(slideID: String, _ framing: SlidePostFraming?) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var framing = framing
        if framing?.orientation == .landscape, !SlidePostFraming.allowsLandscape(profile: value.platformProfile) { framing?.orientation = .portrait }
        if framing?.isDefault == true { framing = nil }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        guard edits.framing != framing else { return }
        edits.framing = framing
        value.slides[index].edits = edits
        stageDraft(value)
    }
    /// KRI-606: sets (nil = the platform default) the post-wide size. A rectangle chosen for the old shape would be
    /// mis-shaped for the new canvas, so every portrait slide drops its crop (landscape slides keep theirs).
    /// One undo step. Returns how many crops were reset, for the sheet's notice.
    @discardableResult
    func setAspect(_ aspect: String?) -> Int {
        // `stageDraft` drops edits while a save/propose is in flight; report nothing changed rather than a reset that never happened.
        guard !isBusy, var value = draft else { return 0 }
        let stored = SlidePostSize.storedAspect(aspect, profile: value.platformProfile)
        guard stored != value.aspect else { return 0 }
        value.aspect = stored
        var reset = 0
        for index in value.slides.indices {
            guard var edits = value.slides[index].edits, let framing = edits.framing,
                  framing.orientation == .portrait, framing.sourceCrop != nil else { continue }
            edits.framing = nil
            value.slides[index].edits = edits
            reset += 1
        }
        stageDraft(value)
        return reset
    }
    /// KRI-606: switches platform. Crops are kept except where the platform can't hold them (see `withProfileCoercions`),
    /// and a size the new platform lacks falls back to its default, so the shape the creator sees is the shape they get.
    @discardableResult
    func setPlatform(_ profile: String) -> Int {
        guard !isBusy, var value = draft, value.platformProfile != profile else { return 0 }
        let before = value.size
        value.platformProfile = profile
        var reset = 0
        if value.size != before {
            for index in value.slides.indices {
                guard var edits = value.slides[index].edits, let framing = edits.framing,
                      framing.orientation == .portrait, framing.sourceCrop != nil else { continue }
                edits.framing = nil
                value.slides[index].edits = edits
                reset += 1
            }
        }
        stageDraft(value)
        return reset
    }
    /// KRI-1138: turns the slide's photo or video to `tilt.rotationDeg` over its backdrop; nil (or an upright turn) puts
    /// it back. Photos and videos both turn. One undo step (a dial drag passes one `coalescing` key so its many
    /// changes are one); a no-op change stages nothing.
    func setTilt(slideID: String, _ tilt: SlidePostTilt?, coalescing key: String? = nil) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        let next = tilt.flatMap { $0.isUpright ? nil : $0 }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        guard edits.tilt != next else { return }
        edits.tilt = next
        value.slides[index].edits = edits
        stageDraft(value, coalescing: key)
    }
    /// KRI-606: shows the photo whole with bars (or fills the frame again with nil). Photos only.
    func setFit(slideID: String, _ fit: SlidePostFit?) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }),
              value.slides[index].kind == "image" else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        guard edits.fit != fit else { return }
        edits.fit = fit
        value.slides[index].edits = edits
        stageDraft(value)
    }
    /// KRI-606: one undo step for "Fit all" / "Fill all". Videos always fill the frame, so they are skipped.
    func setFitForAllPhotos(_ fit: SlidePostFit?) {
        guard var value = draft else { return }
        var changed = false
        for index in value.slides.indices where value.slides[index].kind == "image" {
            var edits = value.slides[index].edits ?? SlidePostEdits()
            guard edits.fit != fit else { continue }
            edits.fit = fit
            value.slides[index].edits = edits
            changed = true
        }
        if changed { stageDraft(value) }
    }
    /// What the Size sheet's "Photos" control shows: one treatment when every photo agrees, else mixed.
    enum FitSummary: Equatable { case none, uniform(SlidePostFit?), mixed }
    var fitSummary: FitSummary {
        let photos = (draft?.slides ?? []).filter { $0.kind == "image" }
        guard let first = photos.first else { return .none }
        let value = first.edits?.fit
        return photos.allSatisfy { $0.edits?.fit == value } ? .uniform(value) : .mixed
    }
    // MARK: Stickers (KRI-460)

    /// Puts a pasted picture on the slide (centre, 40% of its width, upright), on top, and selects it. Nil when the
    /// slide is gone, it already holds the maximum, or a save is in flight.
    @discardableResult
    func addSticker(slideID: String, assetID: String) -> String? {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return nil }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var stickers = edits.effectiveStickers
        guard stickers.count < SlidePostEdits.maxStickers else { return nil }
        let sticker = SlidePostSticker(assetID: assetID)
        stickers.append(sticker)
        edits.stickers = stickers
        value.slides[index].edits = edits
        stageDraft(value)
        guard draft?.slides.first(where: { $0.id == slideID })?.edits?.effectiveStickers.contains(where: { $0.id == sticker.id }) == true else { return nil }
        selectedStickerID = sticker.id
        return sticker.id
    }
    /// Edits one sticker in place; the result is kept inside the contract. `coalescing` collapses a continuous
    /// gesture (a drag, a pinch) into one undo step.
    func updateSticker(slideID: String, stickerID: String, coalescing key: String? = nil, _ mutate: (inout SlidePostSticker) -> Void) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var stickers = edits.effectiveStickers
        guard let at = stickers.firstIndex(where: { $0.id == stickerID }) else { return }
        mutate(&stickers[at])
        stickers[at].clamp()
        edits.stickers = stickers
        value.slides[index].edits = edits
        stageDraft(value, coalescing: key.map { "\($0)-\(stickerID)" })
    }
    func removeSticker(slideID: String, stickerID: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        var stickers = edits.effectiveStickers
        guard stickers.contains(where: { $0.id == stickerID }) else { return }
        stickers.removeAll { $0.id == stickerID }
        // An empty list (not nil) is how a save tells the server to drop the last one.
        edits.stickers = stickers
        value.slides[index].edits = edits
        stageDraft(value)
        if selectedStickerID == stickerID { selectedStickerID = nil }
    }

    // MARK: Drawing (KRI-589)

    /// Why `extraStrokes` / `extraPoints` more ink cannot go on `slideID`, or nil when it can. The server's caps
    /// (60 strokes and 3,000 points per slide, 8,000 points per post), checked before Done so a scribble is
    /// never lost to a 422 on Save. `extra*` count what the sketch holds, on top of what the slide already has.
    func drawingCapMessage(slideID: String, extraPoints: Int = 0, extraStrokes: Int = 0) -> String? {
        guard let value = draft else { return nil }
        let current = value.slides.first { $0.id == slideID }?.edits?.drawing
        if (current?.strokes.count ?? 0) + extraStrokes > SlidePostDrawing.maxStrokes { return "That’s as many strokes as one slide holds." }
        if (current?.pointCount ?? 0) + extraPoints > SlidePostDrawing.maxPointsPerSlide { return "This slide is as detailed as it can get." }
        if value.drawingPointCount + extraPoints > SlidePostDrawing.maxPointsPerPost { return "This post has as many drawings as it can hold." }
        return nil
    }
    /// Sets the slide's drawing outright (no strokes = clears it). One undo step. False when it was refused: no such
    /// slide, ink the caps or the server's rules would reject, or a save in flight.
    @discardableResult
    func setDrawing(slideID: String, strokes: [DrawingStroke], canvasAspect: Double) -> Bool {
        guard !isBusy, var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return false }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        if strokes.isEmpty {
            guard edits.drawing != nil else { return true }
            edits.drawing = nil
        } else {
            let next = SlidePostDrawing(canvasAspect: canvasAspect, strokes: strokes)
            guard next.isValid else { return false }
            let others = value.drawingPointCount - (edits.drawing?.pointCount ?? 0)
            guard others + next.pointCount <= SlidePostDrawing.maxPointsPerPost else { return false }
            guard edits.drawing != next else { return true }
            edits.drawing = next
        }
        value.slides[index].edits = edits
        stageDraft(value)
        return true
    }
    /// "Draw more": appends strokes drawn on the current canvas to what the slide already has. Done in the drawing's
    /// OWN authoring space: when the slide's size or shape changed after it was drawn, its ink stays letterboxed
    /// in the old aspect (by contract: no re-projection), and Draw mode lays the sketch over that same region
    /// (`SlidePostDrawing.authoringAspect`), so the new strokes' points are already in the drawing's space and are
    /// appended unchanged. A first drawing takes the current canvas's aspect.
    @discardableResult
    func addDrawingStrokes(slideID: String, strokes new: [DrawingStroke], canvas: CGSize) -> Bool {
        guard !new.isEmpty, let slide = draft?.slides.first(where: { $0.id == slideID }) else { return false }
        let existing = slide.edits?.drawing
        guard drawingCapMessage(slideID: slideID, extraPoints: new.reduce(0) { $0 + $1.points.count }, extraStrokes: new.count) == nil else { return false }
        return setDrawing(slideID: slideID, strokes: (existing?.strokes ?? []) + new,
                          canvasAspect: SlidePostDrawing.authoringAspect(existing: existing, canvas: canvas))
    }
    func clearDrawing(slideID: String) { setDrawing(slideID: slideID, strokes: [], canvasAspect: 1) }

    func setLook(slideID: String, preset: String) {
        guard var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        edits.lookPreset = preset
        value.slides[index].edits = edits
        stageDraft(value)
    }
    /// Drops texts the user left empty (the server rejects them) when text editing ends.
    func removeEmptyTexts(slideID: String) {
        guard let texts = draft?.slides.first(where: { $0.id == slideID })?.edits?.texts, texts.contains(where: { $0.text.isEmpty }),
              var value = draft, let index = value.slides.firstIndex(where: { $0.id == slideID }) else { return }
        var edits = value.slides[index].edits ?? SlidePostEdits()
        edits.setTexts(texts.filter { !$0.text.isEmpty })
        value.slides[index].edits = edits
        stageDraft(value)
    }

    private func handle(_ error: Error) {
        if let apiError = error as? APIError, case .conflict = apiError {
            self.error = "This post kept changing while we saved. Your edits are still here. Try again."
        } else { self.error = error.localizedDescription }
    }
    private struct LocalState: Codable {
        let draft: SlidePostDraft?
        let proposal: SlidePostProposal?
        let selectedID: String?
        let instruction: String
        let baseVersion: Int
        let baselineDraft: SlidePostDraft?
        var chat: [SlidePostChatMessage]? = nil
        var proposalThoughtSummaries: [KriaThoughtSummary]? = nil
        var proposalRequestText: String? = nil
        var seenAssets: [String]? = nil
        var stickerUploadNames: [String]? = nil
    }
    private func persist() {
        guard !restoring, let itemID else { return }
        let local = LocalState(draft: draft, proposal: proposal, selectedID: selectedID, instruction: instruction, baseVersion: baseVersion, baselineDraft: baselineDraft, chat: Array(chat.suffix(Self.maxPersistedChat)), proposalThoughtSummaries: proposalThoughtSummaries.filter { $0.status == .completed && $0.hasText }, proposalRequestText: proposalRequestText, seenAssets: seenAssetIDs.map { Array($0).sorted() }, stickerUploadNames: Array(stickerUploadNames).sorted())
        guard let data = try? JSONEncoder().encode(local) else { return }
        defaults.set(data, forKey: "kria.slide-post.\(itemID)")
    }
    private func restore() {
        guard let itemID, let data = defaults.data(forKey: "kria.slide-post.\(itemID)"), let local = try? JSONDecoder().decode(LocalState.self, from: data) else {
            stickerUploadNames = []
            draft = nil; proposal = nil; proposalThoughtSummaries = []; proposalRequestText = nil; selectedID = nil; instruction = ""; baseVersion = 0; baselineDraft = nil; chat = []; seenAssetIDs = nil; return
        }
        draft = local.draft; proposal = local.proposal; selectedID = local.selectedID; instruction = local.instruction; baseVersion = local.baseVersion; baselineDraft = local.baselineDraft
        chat = Array((local.chat ?? []).suffix(Self.maxPersistedChat))
        proposalThoughtSummaries = (local.proposalThoughtSummaries ?? []).filter { $0.status == .completed && $0.hasText }
        proposalRequestText = local.proposalRequestText
        seenAssetIDs = local.seenAssets.map(Set.init)
        stickerUploadNames = Set(local.stickerUploadNames ?? [])
    }
}

/// What a picture paste needs besides the clipboard: whether the server takes stickers (`slide_post_stickers`), the
/// uploader, and where the post lives.
struct SlidePostStickerPaste {
    let isEnabled: Bool
    let api: any KriaAPIClient
    let uploads: any SlidePostStickerUploading
    let projectID: UUID
    let itemID: String
}

/// The one thing a sticker paste needs from the background uploader (a seam so a test, or the UI-test fixture, can stand in).
@MainActor protocol SlidePostStickerUploading: AnyObject {
    /// Starts the upload of a picture into the post's pool. False when it was refused (the file is kept for a Retry).
    func uploadSticker(fileURL: URL, projectID: UUID, itemID: String) async -> Bool
}

extension BackgroundUploadCoordinator: SlidePostStickerUploading {
    func uploadSticker(fileURL: URL, projectID: UUID, itemID: String) async -> Bool {
        await enqueue(fileURL: fileURL, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource, role: .visual, itemID: itemID)
    }
}

/// Pure preview geometry, mirroring the server's `render_text_element_png`: `x_frac` is the line's
/// left edge for left alignment, its right edge for right alignment and its centre for centre;
/// `y_frac` (or a vertical preset) is the block's vertical centre.
enum SlidePostTextLayout {
    static let xDefaults: [String: Double] = ["left": 0.08, "center": 0.5, "right": 0.92]
    static let yPresets: [String: Double] = ["top": 0.12, "center": 0.5, "bottom": 0.82]

    static func anchor(for element: SlidePostTextElement) -> (x: Double, y: Double) {
        let x = element.xFrac ?? xDefaults[element.alignment] ?? 0.5
        let y = element.position == "custom" ? (element.yFrac ?? 0.5) : (yPresets[element.position] ?? 0.82)
        return (x, y)
    }
    /// The anchor after a drag of `translation` points, as canvas fractions (clamped to 0...1).
    /// Starting from the edge anchor means dragging a left/right text writes an edge x, not a centre.
    static func dragged(from element: SlidePostTextElement, translation: CGSize, canvas: CGSize) -> (x: Double, y: Double) {
        guard canvas.width > 0, canvas.height > 0 else { return anchor(for: element) }
        let start = anchor(for: element)
        return (min(max(start.x + Double(translation.width / canvas.width), 0), 1),
                min(max(start.y + Double(translation.height / canvas.height), 0), 1))
    }
}
