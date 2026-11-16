import Foundation

// KRI-1501 two-language captions: the creator's spoken caption plus a smaller translated line for the same cue.
// Contract: docs/pipelines/dual-captions.md. The translated line is its own editor track ("separate track"): it is
// moved, sized, coloured and turned off apart from the spoken captions, but always shown for its cue's window.
//
// Wire shape:
//   - `caption_cues[i].translation`: the cue's translated line (`EditorCaptionCue.translation`, kept in `raw`).
//   - `caption_meta.translation`: the track (`EditorCaptionTranslation`). `outcome` / `dropped_cue_ids` are written by
//     the server only and are sent back exactly as received.
// An untouched document never re-encodes either key, so a Save without a translation edit is byte-identical.

/// The translated-line track, a typed view over `caption_meta.translation`. Every key the editor does not own (and any
/// newer server key) stays in `raw` and is written back unchanged.
struct EditorCaptionTranslation: Equatable, Sendable {
    var enabled: Bool
    var language: String
    var sourceLanguage: String?
    var sizePx: Double?
    var color: String?
    var font: String?
    var strokeWidth: Double?
    var positionXFrac: Double?
    var positionYFrac: Double?
    /// Per-cue overrides (`position_x_frac`, `position_y_frac`, `size_px`), same shape as the spoken captions'
    /// `appearance.line_overrides`.
    var lineOverrides: [String: [String: JSONValue]]
    /// Server-owned (`translated` | `partial` | `unmet`).
    var outcome: String?
    /// Server-owned: cues the translator could not translate (or that came back in the wrong language).
    var droppedCueIDs: [String]
    /// The object as loaded, so unknown keys round-trip.
    var raw: [String: JSONValue]

    init(enabled: Bool = true, language: String, sourceLanguage: String? = nil, sizePx: Double? = nil,
         color: String? = nil, font: String? = nil, strokeWidth: Double? = nil, positionXFrac: Double? = nil,
         positionYFrac: Double? = nil, lineOverrides: [String: [String: JSONValue]] = [:], outcome: String? = nil,
         droppedCueIDs: [String] = [], raw: [String: JSONValue] = [:]) {
        self.enabled = enabled; self.language = language; self.sourceLanguage = sourceLanguage; self.sizePx = sizePx
        self.color = color; self.font = font; self.strokeWidth = strokeWidth; self.positionXFrac = positionXFrac
        self.positionYFrac = positionYFrac; self.lineOverrides = lineOverrides; self.outcome = outcome
        self.droppedCueIDs = droppedCueIDs; self.raw = raw
    }

    init?(json: JSONValue?) {
        guard let object = json?.objectValue, let language = object["language"]?.stringValue, !language.isEmpty else { return nil }
        func finite(_ key: String) -> Double? { object[key]?.numberValue.flatMap { $0.isFinite ? $0 : nil } }
        var overrides: [String: [String: JSONValue]] = [:]
        for (id, value) in object["line_overrides"]?.objectValue ?? [:] {
            if let own = value.objectValue { overrides[id] = own }
        }
        self.init(enabled: object["enabled"]?.boolValue ?? true, language: language,
                  sourceLanguage: object["source_language"]?.stringValue, sizePx: finite("size_px"),
                  color: object["color"]?.stringValue, font: object["font"]?.stringValue, strokeWidth: finite("stroke_width"),
                  positionXFrac: finite("position_x_frac"), positionYFrac: finite("position_y_frac"),
                  lineOverrides: overrides, outcome: object["outcome"]?.stringValue,
                  droppedCueIDs: object["dropped_cue_ids"]?.arrayValue?.compactMap(\.stringValue) ?? [], raw: object)
    }

    /// The contract shape. Editor-owned keys are always written (null when unset); `outcome` and `dropped_cue_ids`
    /// keep whatever the server sent.
    var json: JSONValue {
        var object = raw
        object["enabled"] = .bool(enabled)
        object["language"] = .string(language)
        object["source_language"] = sourceLanguage.map(JSONValue.string) ?? .null
        object["size_px"] = sizePx.map(JSONValue.number) ?? .null
        object["color"] = color.map(JSONValue.string) ?? .null
        object["font"] = font.map(JSONValue.string) ?? .null
        object["stroke_width"] = strokeWidth.map(JSONValue.number) ?? .null
        object["position_x_frac"] = positionXFrac.map(JSONValue.number) ?? .null
        object["position_y_frac"] = positionYFrac.map(JSONValue.number) ?? .null
        object["line_overrides"] = .object(lineOverrides.mapValues(JSONValue.object))
        if raw["outcome"] != nil || outcome != nil { object["outcome"] = outcome.map(JSONValue.string) ?? .null }
        if raw["dropped_cue_ids"] != nil || !droppedCueIDs.isEmpty { object["dropped_cue_ids"] = .array(droppedCueIDs.map(JSONValue.string)) }
        return .object(object)
    }
}

/// The translated line's look and layout numbers. The server twin is `src/apps/api/app/pipeline/phone_captions.py`
/// (`TRANSLATION_*` / `_TRANSLATION_*` constants and `_translation_layer`, KRI-1501). Change both together.
enum CaptionTranslationLook {
    /// The reference look's yellow.
    static let defaultColor = "#F5D33C"
    /// Default size: `round(0.6 × spoken size)`, clamped 28–120.
    static let sizeFactor = 0.6
    static let defaultSizeRange: ClosedRange<Double> = 28...120
    /// A size the creator sets: 24–120 (also the pinch range).
    static let sizeRange: ClosedRange<Double> = 24...120
    /// Default outline: `max(2, round(0.6 × spoken stroke))`.
    static let strokeFactor = 0.6
    static let minimumStroke = 2.0
    /// One row, at most this share of the canvas width; wider text shrinks to fit, never below `minimumFitSize`.
    /// Still too wide at the floor ⇒ the line is dropped (never wrapped, never cut).
    static let widthBudgetFraction = 0.9
    static let minimumFitSize = 24.0
    /// Attached placement: the gap between the spoken block and the translated line, as a share of the SMALL size.
    static let gapFactor = 0.25
    /// Attached placement flips above the spoken line when the translated line would end below this share of the
    /// canvas height (`_TRANSLATION_FRAME_BUDGET_FRAC`; the server uses an ASKED caption position's own budget, 0.88,
    /// when the variant has one, which the editor document does not carry).
    static let frameBudgetFraction = 0.90
    /// A line flipped above the spoken one never starts higher than this share of the height (`_TRANSLATION_TOP_FRAC`).
    static let topFraction = 0.05
    /// The longest translated line the editor keeps (the Save route refuses more).
    static let maximumCharacters = 300
    /// Detached centre clamps, the same as the spoken captions' gesture clamps.
    static let xRange: ClosedRange<Double> = 0.05...0.95
    static let yRange: ClosedRange<Double> = 0.10...0.90
    /// Swatches offered in the editor; the first is the default.
    static let swatches = ["#F5D33C", "#FFFFFF", "#9BCAFF", "#FFF19E", "#E5DAF5"]

    static func defaultSize(spoken: Double) -> Double {
        min(defaultSizeRange.upperBound, max(defaultSizeRange.lowerBound, (sizeFactor * spoken).rounded()))
    }
    static func defaultStroke(spoken: Double) -> Double { max(minimumStroke, (strokeFactor * spoken).rounded()) }
    static func clampedSize(_ size: Double) -> Double { min(sizeRange.upperBound, max(sizeRange.lowerBound, size.rounded())) }

    /// The one-row size for text `width` px wide at `size`: `size` while it fits the budget, else scaled down to fit
    /// (rounded down to a whole pixel); nil when that is below the floor (the line is dropped).
    static func fittedSize(size: Double, width: Double, canvasWidth: Double) -> Double? {
        let budget = widthBudgetFraction * canvasWidth
        guard width > budget, width > 0 else { return size }
        let fitted = (size * budget / width).rounded(.down)
        return fitted >= minimumFitSize ? fitted : nil
    }
}

/// `POST plan-items/{item_id}/variants/{variant_id}/caption-translation`. The route persists nothing: the editor applies
/// the answer to its document and the normal Save keeps it.
struct CaptionTranslationRequest: Encodable, Equatable, Sendable {
    struct Cue: Encodable, Equatable, Sendable {
        let id: String
        let text: String
        let startS: Double
        let endS: Double
        enum CodingKeys: String, CodingKey { case id, text; case startS = "start_s"; case endS = "end_s" }
    }
    let language: String
    let cues: [Cue]
}

struct CaptionTranslationResponse: Decodable, Equatable, Sendable {
    struct Line: Decodable, Equatable, Sendable {
        let id: String
        let text: String?
    }
    let language: String
    let sourceLanguage: String?
    let outcome: String
    let translations: [Line]
    let droppedCueIDs: [String]
    enum CodingKeys: String, CodingKey {
        case language, outcome, translations
        case sourceLanguage = "source_language", droppedCueIDs = "dropped_cue_ids"
    }
    init(language: String, sourceLanguage: String?, outcome: String, translations: [Line], droppedCueIDs: [String]) {
        self.language = language; self.sourceLanguage = sourceLanguage; self.outcome = outcome
        self.translations = translations; self.droppedCueIDs = droppedCueIDs
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        language = try c.decode(String.self, forKey: .language)
        sourceLanguage = try c.decodeIfPresent(String.self, forKey: .sourceLanguage)
        outcome = try c.decodeIfPresent(String.self, forKey: .outcome) ?? "unmet"
        translations = try c.decodeIfPresent([Line].self, forKey: .translations) ?? []
        droppedCueIDs = try c.decodeIfPresent([String].self, forKey: .droppedCueIDs) ?? []
    }
}

extension EditorCaptionCue {
    /// The cue's translated line (`raw["translation"]`); nil when absent or blank.
    var translation: String? {
        get { raw["translation"]?.stringValue.flatMap { $0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : $0 } }
        set {
            if let newValue, !newValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                raw["translation"] = .string(newValue)
            } else {
                raw.removeValue(forKey: "translation")
            }
        }
    }
}

extension EditorDocument {
    /// `caption_meta.translation`. Setting nil removes the key.
    var captionTranslation: EditorCaptionTranslation? {
        get { EditorCaptionTranslation(json: captionMeta["translation"]) }
        set {
            if let newValue { captionMeta["translation"] = newValue.json } else { captionMeta.removeValue(forKey: "translation") }
        }
    }
}
