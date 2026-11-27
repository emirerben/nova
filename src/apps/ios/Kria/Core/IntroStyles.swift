import Foundation
import KriaMediaEngine

// KRI-1633 intro styles on tap: Kria offers several fitted intro looks for a video's title and the creator picks one.
// Contract: docs/pipelines/intro-scene.md ("Intro styles on tap"). The route computes and stores nothing: the editor
// applies the picked rows as an unsaved change and the ordinary editor-commit Save keeps them (the KRI-1501 caption
// translation precedent).
//
// Wire shape (`POST plan-items/{item_id}/variants/{variant_id}/intro-styles`):
//   request  `{version: 1, base_generation, title_rows: [TextElement JSON]}`
//   response `{version: 1, available, reason, offer_id, route, title_family, language, replace_ids,
//              options: [{key, recommended, current, rows: [TextElement JSON], preview, cards?}]}`
// `rows` are kept as `[JSONValue]` and decoded into the document by the same decoder a loaded edit uses, so
// `source_params` (`title_look.intro_style`), `text_case`, `line_spacing` and `text_language` reach Save untouched.
// KRI-1615 (protocol 62): a card look (`<font_pair>+<arrangement>`, after the plain looks) also carries `cards`, her own
// photos laid out with the title, and its `preview.cards` boxes. Contract: intro-scene.md ("Photo cards in the offer").

/// The three title families a Save can carry: a Talking edit's `opening-title`, a Voiceover edit's `narrated-title`
/// and a montage's `guided-title`, each optionally followed by `-line-N` for the later lines of an intro stack.
enum IntroStyleFamily {
    static let ids = ["opening-title", "narrated-title", "guided-title"]
    /// A stack is at most this many rows, and at most this many characters in total (the route's own limits).
    static let maxRows = 5
    static let maxCharacters = 160

    /// The family a row id belongs to, or nil. Strict on purpose: `guided-title::sequence-3` (a word bar) is not a
    /// title line.
    static func family(of id: String) -> String? {
        for base in ids {
            if id == base { return base }
            let prefix = base + "-line-"
            guard id.hasPrefix(prefix) else { continue }
            let rest = id.dropFirst(prefix.count)
            if !rest.isEmpty, rest.allSatisfy(\.isASCII), rest.allSatisfy(\.isNumber) { return base }
        }
        return nil
    }

    /// 1 for the base row, N for `<family>-line-N`.
    static func line(of id: String) -> Int {
        guard let range = id.range(of: "-line-", options: .backwards), let number = Int(id[range.upperBound...]) else { return 1 }
        return number
    }
}

/// `POST intro-styles` request body.
struct IntroStylesRequest: Encodable, Equatable, Sendable {
    let version = 1
    let baseGeneration: String
    /// The editor's CURRENT title-family rows, unsaved edits included, exactly as Save would send them.
    let titleRows: [JSONValue]
    enum CodingKeys: String, CodingKey { case version; case baseGeneration = "base_generation"; case titleRows = "title_rows" }
}

/// The raw answer. Parsing is separate (`IntroStylesOffer.parse`) so an unknown shape degrades to "unavailable"
/// instead of failing the decode.
struct IntroStylesResponse: Decodable, Equatable, Sendable {
    let json: JSONValue
    init(from decoder: Decoder) throws { json = try JSONValue(from: decoder) }
    init(json: JSONValue) { self.json = json }
}

/// KRI-1615: one of her own Visuals photos laid out with the title, as a card look's `cards` entry carries it
/// (`IntroCard` in `app/pipeline/intro_styles.py`). Canvas fractions of 1080x1920: `x`/`y` the centre, `scale` the width;
/// the photo keeps its own shape. The id is the slot's (`intro-card-N` for `prop_N`), so applying a look twice converges.
struct IntroStyleCard: Equatable, Sendable {
    static let idPrefix = "intro-card-"
    static let maxPerLook = 4

    let id: String
    let gcsPath: String
    let x: Double
    let y: Double
    let scale: Double
    /// The server's mark that this card sits clear of her captions (`intro_card_pool.caption_clear_decisions`).
    let captionClear: Bool
    let z: Int
    let startS: Double
    let endS: Double

    static func isCardID(_ id: String) -> Bool { id.hasPrefix(idPrefix) }

    /// A look's cards, or nil when any card is one this build cannot place exactly.
    static func look(_ value: JSONValue) -> [IntroStyleCard]? {
        guard let entries = value.arrayValue, (1...maxPerLook).contains(entries.count) else { return nil }
        let cards = entries.compactMap(card)
        guard cards.count == entries.count, Set(cards.map(\.id)).count == cards.count else { return nil }
        return cards
    }

    private static func card(_ value: JSONValue) -> IntroStyleCard? {
        guard let fields = value.objectValue, let id = fields["id"]?.stringValue, id.hasPrefix(idPrefix) else { return nil }
        let slot = id.dropFirst(idPrefix.count)
        func number(_ key: String, in range: ClosedRange<Double>) -> Double? {
            guard let value = fields[key]?.numberValue, value.isFinite, range.contains(value) else { return nil }
            return value
        }
        guard !slot.isEmpty, slot.allSatisfy({ $0.isASCII && $0.isNumber }), let n = Int(slot), n > 0,
              fields["slot"]?.stringValue == "prop_\(n)",
              fields["media_id"]?.stringValue?.isEmpty == false, fields["generation"]?.stringValue?.isEmpty == false,
              let path = fields["gcs_path"]?.stringValue, !path.isEmpty,
              let x = number("x_frac", in: 0...1), let y = number("y_frac", in: 0...1),
              let scale = number("scale", in: 0...1), scale > 0,
              let captionClear = fields["caption_clear"]?.boolValue,
              let z = number("z", in: 0...Double(Int32.max)), z == z.rounded(),
              let start = number("start_s", in: 0...Double.greatestFiniteMagnitude),
              let end = number("end_s", in: 0...Double.greatestFiniteMagnitude), end > start else { return nil }
        return IntroStyleCard(id: id, gcsPath: path, x: x, y: y, scale: scale, captionClear: captionClear, z: Int(z),
                              startS: start, endS: end)
    }

    /// True when `overlay` is this card where the look put it: one she moved, resized or retimed is hers.
    func isPlaced(as overlay: EditorTimedEffect) -> Bool {
        func same(_ key: String, _ value: Double) -> Bool { overlay.raw[key]?.numberValue.map { abs($0 - value) < 1e-6 } ?? false }
        return overlay.id == id && overlay.raw["src_gcs_path"]?.stringValue == gcsPath && same("x_frac", x) && same("y_frac", y)
            && same("scale", scale) && same("z", Double(z)) && abs(overlay.startS - startS) < 1e-6 && abs(overlay.endS - endS) < 1e-6
    }

    /// A media-overlay row as the Save sends it. An intro card goes without `caption_clear`: the server decides it again
    /// from its own evidence on every Save (`intro_card_pool.caption_clear_decisions`).
    static func committed(_ row: JSONValue) -> JSONValue {
        guard var fields = row.objectValue, let id = fields["id"]?.stringValue, isCardID(id) else { return row }
        fields.removeValue(forKey: "caption_clear")
        return .object(fields)
    }

    /// Her card on the editor's media-overlay lane, in the shape the stage C contract names. `caption_clear` is kept on
    /// the row (as the server's own projection of a marked card does) so the live preview draws the card where the
    /// export will; the Save leaves it out (`committed`), because the server decides it again.
    var overlay: EditorTimedEffect {
        var raw: [String: JSONValue] = [
            "id": .string(id), "kind": .string("image"), "src_gcs_path": .string(gcsPath), "display_mode": .string("pip"),
            "position": .string("custom"), "x_frac": .number(x), "y_frac": .number(y), "scale": .number(scale),
            "start_s": .number(startS), "end_s": .number(endS), "z": .number(Double(z)),
        ]
        if captionClear { raw["caption_clear"] = .bool(true) }
        return EditorTimedEffect(id: id, startS: startS, endS: endS, kind: "image", raw: raw)
    }
}

struct IntroStylesOffer: Equatable, Sendable {
    struct Option: Equatable, Sendable, Identifiable {
        let key: String
        let recommended: Bool
        /// The server says these rows are what the title shows now.
        let current: Bool
        /// Complete TextElement rows to put in the document, raw.
        let rows: [JSONValue]
        /// What the option looks like over her own clip, drawn on this iPhone. All options have one or none do.
        let preview: ChoiceQuestion.Preview?
        /// KRI-1615: her photos this look lays out with its rows; empty for a plain look and "none".
        var cards: [IntroStyleCard] = []
        /// Where the preview draws each card's photo, slot order (present exactly when `preview` and `cards` are).
        var previewCards: [PreviewCard] = []
        var id: String { key }
        var isNone: Bool { key == IntroStylesOffer.noneKey }
        var isCardLook: Bool { !cards.isEmpty }
    }

    /// A card's photo in a card look's preview: `box` in canvas fractions, drawn in `z` order under the title.
    struct PreviewCard: Equatable, Sendable {
        let mediaID: String
        let box: CGRect
        let z: Int
    }

    static let noneKey = "none"
    static let supportedVersion = 1

    let offerID: String
    let route: String
    let titleFamily: String
    let language: String?
    let replaceIDs: [String]
    let options: [Option]

    var hasPreviews: Bool { options.allSatisfy { $0.preview != nil } }

    /// What the unavailable answer says, or what is wrong with an available one.
    enum Parsed: Equatable, Sendable {
        case offer(IntroStylesOffer)
        case unavailable(reason: String?)
    }

    /// An answer this build cannot apply is `unavailable` rather than half shown: a newer version, a missing key,
    /// a row that is not an object with an id and text, an id outside the title family, a duplicate key, or no
    /// "none" option last.
    static func parse(_ value: JSONValue) -> Parsed {
        guard let fields = value.objectValue,
              let version = fields["version"]?.numberValue.map(Int.init), version == supportedVersion else {
            return .unavailable(reason: nil)
        }
        guard fields["available"]?.boolValue == true else {
            return .unavailable(reason: fields["reason"]?.stringValue)
        }
        guard let offerID = fields["offer_id"]?.stringValue, !offerID.isEmpty,
              let family = fields["title_family"]?.stringValue, IntroStyleFamily.ids.contains(family),
              let route = fields["route"]?.stringValue,
              let replace = fields["replace_ids"]?.arrayValue?.compactMap(\.stringValue),
              replace.allSatisfy({ IntroStyleFamily.family(of: $0) == family }) else {
            return .unavailable(reason: nil)
        }
        var seen = Set<String>()
        var options: [Option] = []
        for entry in fields["options"]?.arrayValue ?? [] {
            guard let object = entry.objectValue, let key = object["key"]?.stringValue, !key.isEmpty, seen.insert(key).inserted,
                  let rows = object["rows"]?.arrayValue, !rows.isEmpty, rows.count <= IntroStyleFamily.maxRows else {
                return .unavailable(reason: nil)
            }
            for row in rows {
                guard let id = row.objectValue?["id"]?.stringValue, IntroStyleFamily.family(of: id) == family,
                      row.objectValue?["text"]?.stringValue != nil else { return .unavailable(reason: nil) }
            }
            // A card look's rows place the title between its cards: without every card it is not offered at all.
            var cards: [IntroStyleCard] = []
            if let wire = object["cards"], wire != .null {
                guard let look = IntroStyleCard.look(wire) else { continue }
                cards = look
            }
            var preview = ChoiceQuestion.Preview.parse(object["preview"])
            var previewCards: [PreviewCard] = []
            if !cards.isEmpty, preview != nil {
                if let drawn = PreviewCard.look(object["preview"]?.objectValue?["cards"]), drawn.count == cards.count {
                    previewCards = drawn
                } else {
                    preview = nil
                }
            }
            options.append(Option(key: key, recommended: object["recommended"]?.boolValue ?? false,
                                  current: object["current"]?.boolValue ?? false, rows: rows,
                                  preview: preview, cards: cards, previewCards: previewCards))
        }
        // At least one look and "none", which is last: a menu without a way back is not offered.
        guard options.count >= 2, options.last?.key == noneKey, options.filter(\.isNone).count == 1 else {
            return .unavailable(reason: nil)
        }
        // Previews are all or nothing, as on the wire: one that did not parse turns every tile into a label tile.
        let previewed = options.allSatisfy { $0.preview != nil }
            ? options
            : options.map { Option(key: $0.key, recommended: $0.recommended, current: $0.current, rows: $0.rows, preview: nil,
                                   cards: $0.cards) }
        return .offer(IntroStylesOffer(offerID: offerID, route: route, titleFamily: family,
                                       language: fields["language"]?.stringValue, replaceIDs: replace, options: previewed))
    }
}

extension IntroStylesOffer.PreviewCard {
    /// `preview.cards`, or nil when one box is not a box inside the canvas.
    static func look(_ value: JSONValue?) -> [Self]? {
        guard let entries = value?.arrayValue, !entries.isEmpty else { return nil }
        let cards = entries.compactMap { entry -> Self? in
            guard let fields = entry.objectValue, let mediaID = fields["media_id"]?.stringValue, !mediaID.isEmpty,
                  let box = fields["box"]?.objectValue, let z = fields["z"]?.numberValue, z.isFinite, z >= 0 else { return nil }
            let edges = ["left", "top", "right", "bottom"].compactMap { box[$0]?.numberValue }.filter { $0.isFinite && (0...1).contains($0) }
            guard edges.count == 4, edges[0] < edges[2], edges[1] < edges[3] else { return nil }
            return Self(mediaID: mediaID, box: CGRect(x: edges[0], y: edges[1], width: edges[2] - edges[0], height: edges[3] - edges[1]),
                        z: Int(z))
        }
        return cards.count == entries.count ? cards : nil
    }
}

/// Why a request produced no offer, each with its own line in the panel.
enum IntroStylesFailure: Equatable, Sendable {
    /// 404 `intro_styles_disabled`, or the item is not hers: the entry goes away.
    case hidden
    /// 426 `client_update_required`, or a 422 for rows the route refuses: nothing to retry.
    case unavailable
    /// 409 `baseline_conflict`: a newer version exists; the editor reloads it.
    case newerVersion
    /// 429, offline, or anything else: one line and a tap to try again.
    case retry

    static func from(_ error: Error) -> IntroStylesFailure {
        guard let error = error as? APIError else { return .retry }
        if error.isBaselineConflict { return .newerVersion }
        switch error {
        case .nativeUpdateRequired: return .unavailable
        case .requestFailed(let status, let detail):
            switch status {
            case 404: return .hidden
            case 426: return .unavailable
            case 422:
                let code = detail.object?["code"]?.stringValue ?? detail.code
                return code == "intro_styles_title_rows_invalid" ? .unavailable : .retry
            default: return .retry
            }
        default: return .retry
        }
    }
}

/// The words on screen. Look names are a closed set of keys; anything else reads "Style N".
enum IntroStyleLabels {
    static func name(forKey key: String, position: Int) -> String {
        switch key {
        case "none": String(localized: "No intro style", comment: "Intro styles: the option that restores the plain title")
        case "grotesk_hand": String(localized: "Bold + handwritten", comment: "Intro style name: a bold sans with a handwritten accent")
        case "serif_italic": String(localized: "Serif + italic", comment: "Intro style name: a serif with an italic accent")
        case "grotesk_loop": String(localized: "Bold + loop script", comment: "Intro style name: a bold sans with a looping script accent")
        case "script_serif": String(localized: "Serif + script", comment: "Intro style name: a serif with an elegant script accent")
        case "young_serif": String(localized: "Soft serif", comment: "Intro style name: one soft serif")
        case "grotesk_pen": String(localized: "Bold + marker", comment: "Intro style name: a bold sans with a marker accent")
        default: String(localized: "Style \(position)", comment: "Intro style name for a look this build has no name for; the number is its place in the list")
        }
    }

    static func unavailableLine(reason: String?) -> String {
        switch reason {
        case "nothing_fits": String(localized: "No intro style fits this video yet.", comment: "Intro styles: no look fits this video")
        case "title_too_short": String(localized: "Intro styles need a title of two or more words.", comment: "Intro styles: the title is one word")
        default: String(localized: "Intro styles aren't available for this video.", comment: "Intro styles: not offered for this video")
        }
    }

    static var failureLine: String {
        String(localized: "Couldn't load intro styles. Tap to try again.", comment: "Intro styles: the request failed; tapping retries")
    }
}
