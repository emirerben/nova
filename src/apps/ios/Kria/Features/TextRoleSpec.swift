import Foundation

/// A text spot as fractions of the video frame. `x` is the left edge, centre or right edge by `alignment`,
/// `y` is the centre of the text block (the server's `LABEL_ANCHORS` convention).
struct TextAnchor: Equatable, Sendable {
    let x: Double
    let y: Double
    let alignment: String
}

/// One row per text type: everything the preferences screen and its preview need to know about it.
struct TextRoleSpec: Identifiable, Sendable {
    let role: String
    let label: String
    let blurb: String
    let sample: String
    /// Size on the 1080x1920 canvas, the unit the renderers work in.
    let sizePx: Int
    /// Where the preview draws this type when the creator has not placed it. Label and corner text sit at
    /// their real usual spot. Title, caption and lyric normally move to whichever corner is clear, so these
    /// are spots picked to keep all five readable on one frame.
    let defaultAnchor: TextAnchor
    /// Width of the text block as a fraction of the frame; nil keeps the renderer's default.
    let maxWidthFrac: Double?
    /// Types that keep their usual spot unless the creator moves them; the rest let Kria pick the clearest spot.
    let keepsUsualSpot: Bool
    /// That usual spot when it is one fixed place. Corner text has none: the creator picks the corner per pin.
    let usualPosition: String?
    var id: String { role }

    static let kriaPicksPlacement = "Kria picks the clearest spot"

    static let all: [TextRoleSpec] = [
        TextRoleSpec(role: "all_text", label: "All text", blurb: "Every kind of text below, unless you set one differently.",
                     sample: "", sizePx: 0, defaultAnchor: TextAnchor(x: 0.5, y: 0.5, alignment: "center"),
                     maxWidthFrac: nil, keepsUsualSpot: false, usualPosition: nil),
        TextRoleSpec(role: "title", label: "Title", blurb: "The big opening line in the first seconds.",
                     sample: "Weekend in Istanbul", sizePx: 90, defaultAnchor: TextAnchor(x: 0.08, y: 0.27, alignment: "left"),
                     maxWidthFrac: 0.62, keepsUsualSpot: false, usualPosition: nil),
        TextRoleSpec(role: "label", label: "Label", blurb: "A short tag like a name, place or score.",
                     sample: "Day 1", sizePx: 56, defaultAnchor: TextAnchor(x: 0.5, y: 0.78, alignment: "center"),
                     maxWidthFrac: nil, keepsUsualSpot: true, usualPosition: "bottom"),
        TextRoleSpec(role: "pinned", label: "Corner text", blurb: "A small note that stays in a corner for the whole video.",
                     sample: "Part 2", sizePx: 52, defaultAnchor: TextAnchor(x: 0.92, y: 0.12, alignment: "right"),
                     maxWidthFrac: nil, keepsUsualSpot: true, usualPosition: nil),
        TextRoleSpec(role: "caption", label: "Caption", blurb: "Your spoken words, shown as you say them.",
                     sample: "and then we found this tiny café", sizePx: 78, defaultAnchor: TextAnchor(x: 0.5, y: 0.90, alignment: "center"),
                     maxWidthFrac: 0.84, keepsUsualSpot: false, usualPosition: nil),
        TextRoleSpec(role: "lyric", label: "Lyric", blurb: "Song words, shown in time with the music.",
                     sample: "I'll be waiting right here", sizePx: 58, defaultAnchor: TextAnchor(x: 0.5, y: 0.62, alignment: "center"),
                     maxWidthFrac: nil, keepsUsualSpot: false, usualPosition: nil),
    ]
    static let roles = all.map(\.role)
    /// The five types drawn on the frame, back to front.
    static var drawn: [TextRoleSpec] { all.filter { $0.role != "all_text" } }

    static func spec(_ role: String) -> TextRoleSpec { all.first { $0.role == role } ?? all[0] }

    static let positions = ["top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right"]
    /// The server's `LABEL_ANCHORS`, used when the API does not send its own.
    static let fallbackAnchors: [String: TextAnchor] = [
        "top_left": TextAnchor(x: 0.08, y: 0.12, alignment: "left"), "top": TextAnchor(x: 0.5, y: 0.12, alignment: "center"),
        "top_right": TextAnchor(x: 0.92, y: 0.12, alignment: "right"), "middle": TextAnchor(x: 0.5, y: 0.5, alignment: "center"),
        "bottom_left": TextAnchor(x: 0.08, y: 0.78, alignment: "left"), "bottom": TextAnchor(x: 0.5, y: 0.78, alignment: "center"),
        "bottom_right": TextAnchor(x: 0.92, y: 0.78, alignment: "right"),
    ]
    static let fallbackColorHint = "White, black or gray, whichever reads best on each shot."
    static let fallbackCaptionFont = "TikTok Sans Bold"

    static func positionName(_ key: String) -> String { key.replacingOccurrences(of: "_", with: " ").capitalized }
}

/// What Kria does for a type when nothing is saved, worded for the screen. Built from the server's
/// `text_style_defaults` when present and from the `TextRoleSpec` constants otherwise.
struct TextRoleDefaults: Equatable, Sendable {
    let outline: String
    let shadow: String
    let color: String
    let font: String
    let fontHint: String?
    let placement: String
    let usualPosition: String?
    let colorHint: String
    /// The placement default in a few words, for the one-line summary beside the preview.
    var shortPlacement: String { usualPosition != nil ? "Usual spot" : placement == TextRoleSpec.kriaPicksPlacement ? "Kria picks" : "Your corner" }
}

struct TextStyleDefaults: Decodable, Sendable {
    struct Role: Decodable, Sendable {
        let outline: String?
        let shadow: String?
        let color: String?
        let font: String?
        let fontHint: String?
        let placement: String?
        let usualPosition: String?
        enum CodingKeys: String, CodingKey {
            case outline, shadow, color, font, placement
            case fontHint = "font_hint", usualPosition = "usual_position"
        }
    }
    struct Anchor: Decodable, Sendable {
        let x: Double?
        let y: Double?
        let alignment: String?
    }
    let roles: [String: Role]?
    let positionAnchors: [String: Anchor]?
    let colorHint: String?
    enum CodingKeys: String, CodingKey {
        case roles, positionAnchors = "position_anchors", colorHint = "color_hint"
    }
}

/// Decodes `T` or quietly yields nil, so a newer or older server shape never breaks the screen.
struct LenientDecodable<T: Decodable & Sendable>: Decodable, Sendable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

extension TextRoleSpec {
    static func defaults(for role: String, server: TextStyleDefaults?) -> TextRoleDefaults {
        let spec = spec(role)
        let remote = server?.roles?[role]
        let keepsUsual = remote.map { $0.placement == "usual" } ?? spec.keepsUsualSpot
        let usual = keepsUsual ? (remote == nil ? spec.usualPosition : remote?.usualPosition) : nil
        func word(_ value: String?, _ fallback: String) -> String {
            guard let value, !value.isEmpty else { return fallback }
            return value.prefix(1).uppercased() + value.dropFirst()
        }
        return TextRoleDefaults(
            outline: word(remote?.outline, "Off"), shadow: word(remote?.shadow, "Off"), color: word(remote?.color, "Auto"),
            font: remote?.font == nil || remote?.font == "auto" ? "Kria picks" : (remote?.font ?? "Kria picks"),
            fontHint: remote?.fontHint ?? (role == "caption" ? fallbackCaptionFont : nil),
            placement: !keepsUsual ? kriaPicksPlacement : (usual != nil ? "Usual spot" : "The corner you pick"),
            usualPosition: usual,
            colorHint: server?.colorHint ?? fallbackColorHint)
    }

    static func anchor(position: String, server: TextStyleDefaults?) -> TextAnchor? {
        if let remote = server?.positionAnchors?[position], let x = remote.x, let y = remote.y {
            return TextAnchor(x: x, y: y, alignment: remote.alignment ?? "center")
        }
        return fallbackAnchors[position]
    }

    /// Where the preview draws a type: the creator's chosen spot, else the usual spot, else the preview default.
    /// The server nudges text slightly off these nominal anchors, so the spot is approximate.
    static func anchor(role: String, position: String?, server: TextStyleDefaults?) -> TextAnchor {
        let spec = spec(role)
        if let position, let chosen = anchor(position: position, server: server) { return chosen }
        if let usual = defaults(for: role, server: server).usualPosition, let usualAnchor = anchor(position: usual, server: server) {
            return usualAnchor
        }
        return spec.defaultAnchor
    }
}
