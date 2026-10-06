import Combine
import SwiftUI

/// The wire/UI vocabulary shared by the slide Text panel, its tests and (later) on-preview editing.
/// A slide text keeps its typed fields (`SlidePostTextElement`) and everything newer than this client
/// in `extra`; the real Text panel speaks the video editor's raw-key vocabulary. This maps between them.
extension SlidePostTextElement {
    static let strokeRange = 0...20
    static let rotationRange = -360.0...360.0
    static let widthRange = 0.2...1.0
    /// What the server draws when a shadow is on but no opacity is stored (alpha 160/255).
    static let defaultShadowOpacity = 0.63
    static let presets: Set<String> = ["Simple", "Bold", "Highlight"]
    /// Style keys that live in `extra` until the server model types them. "Apply style to all" replaces these
    /// as a set (a reset to default on the source clears the target too).
    static let styleExtraKeys: [String] = [
        "rotation_deg", "stroke_color", "shadow_color", "shadow_opacity", "background_color", "editor_preset",
        "text_case", "letter_spacing", "line_spacing",
    ]
    private static let hexColorKeys: Set<String> = ["stroke_color", "shadow_color", "background_color"]
    /// Raw keys the panel may write that a slide does not store at all (video-only concepts).
    private static let ignoredKeys: Set<String> = ["wrap_lines", "animation_phases", "effect", "highlight_color", "behind_subject"]

    /// The text as the real Text panel sees it.
    var editorElement: EditorTextElement {
        var raw: [String: JSONValue] = [:]
        for (key, value) in extra { raw[key] = value }
        // "Inter" is the registry name of the default Inter-Bold face (they share a font file).
        raw["font_family"] = .string(fontFamily == Self.defaultFont ? "Inter" : fontFamily)
        raw["color"] = .string(color)
        raw["size_px"] = .number(Double(sizePx))
        raw["alignment"] = .string(alignment)
        raw["position"] = .string(position)
        if let xFrac { raw["x_frac"] = .number(xFrac) }
        if let yFrac { raw["y_frac"] = .number(yFrac) }
        if let maxWidthFrac { raw["max_width_frac"] = .number(maxWidthFrac) }
        raw["stroke_width"] = .number(Double(strokeWidth))
        raw["shadow_enabled"] = .bool(shadowEnabled)
        if raw["shadow_opacity"] == nil { raw["shadow_opacity"] = .number(shadowEnabled ? Self.defaultShadowOpacity : 0) }
        else if !shadowEnabled { raw["shadow_opacity"] = .number(0) }
        return EditorTextElement(id: id, text: text, role: role, raw: raw)
    }

    /// Applies one raw-key write from the panel. Out-of-range values clamp (never reject), invalid
    /// colours/enums are ignored, `nil`/`.null` resets the field to its default.
    mutating func setEditorValue(_ value: JSONValue?, forKey key: String) {
        let value: JSONValue? = { if case .some(.null) = value { return nil } else { return value } }()
        if Self.ignoredKeys.contains(key) { return }
        switch key {
        case "font_family":
            guard let name = value?.stringValue, !name.isEmpty else { fontFamily = Self.defaultFont; return }
            fontFamily = name == "Inter" ? Self.defaultFont : name
        case "color":
            if let hex = value?.stringValue, Self.isHex(hex) { color = hex.uppercased() }
            else if value == nil { color = "#FFFFFF" }
        case "size_px":
            setSize(value?.numberValue)
        case "alignment":
            if let next = value?.stringValue, ["left", "center", "right"].contains(next) { alignment = next }
            else if value == nil { alignment = "center" }
        case "position":
            if let next = value?.stringValue, ["top", "center", "bottom", "custom"].contains(next) { position = next }
            else if value == nil { position = "bottom"; xFrac = nil; yFrac = nil }
        case "x_frac": xFrac = Self.clamped(value?.numberValue, 0...1)
        case "y_frac": yFrac = Self.clamped(value?.numberValue, 0...1)
        case "max_width_frac": maxWidthFrac = Self.clamped(value?.numberValue, Self.widthRange)
        case "stroke_width":
            strokeWidth = Self.clamped(value?.numberValue, Double(Self.strokeRange.lowerBound)...Double(Self.strokeRange.upperBound)).map { Int($0.rounded()) } ?? 0
        case "shadow_enabled":
            if case .bool(let on)? = value { shadowEnabled = on } else if value == nil { shadowEnabled = true }
        case "background":
            if let next = value?.stringValue, ["none", "box"].contains(next) { background = next }
            else if value == nil { background = "none" }
        case "rotation_deg":
            guard let number = Self.clamped(value?.numberValue, Self.rotationRange), number != 0 else { extra["rotation_deg"] = nil; return }
            extra["rotation_deg"] = .number(number)
        case "shadow_opacity":
            guard let number = Self.clamped(value?.numberValue, 0...1) else { extra[key] = nil; return }
            extra[key] = .number(number)
        case "letter_spacing":
            guard let number = Self.clamped(value?.numberValue, -0.05...0.5) else { extra[key] = nil; return }
            extra[key] = .number(number)
        case "line_spacing":
            guard let number = Self.clamped(value?.numberValue, 0.5...3.0) else { extra[key] = nil; return }
            extra[key] = .number(number)
        case "text_case":
            if let next = value?.stringValue, ["none", "upper", "lower", "title"].contains(next) { extra[key] = .string(next) } else if value == nil { extra[key] = nil }
        case "editor_preset":
            if let preset = value?.stringValue, Self.presets.contains(preset) { extra[key] = .string(preset) } else if value == nil { extra[key] = nil }
        default:
            if Self.hexColorKeys.contains(key) {
                if let hex = value?.stringValue, Self.isHex(hex) { extra[key] = .string(hex.uppercased()) } else if value == nil { extra[key] = nil }
            } else if let value { extra[key] = value } else { extra[key] = nil }
        }
    }

    /// Sets the size in px (clamped to the server's range). A stored wrap width scales with it so the
    /// line breaks stay put, exactly like the video editor's `transformText`.
    mutating func setSize(_ requested: Double?) {
        guard let requested, requested.isFinite else { sizePx = 86; return }
        let next = min(max(Int(requested.rounded()), Self.sizeRange.lowerBound), Self.sizeRange.upperBound)
        if let maxWidthFrac, sizePx > 0 { self.maxWidthFrac = min(max(maxWidthFrac * Double(next) / Double(sizePx), Self.widthRange.lowerBound), 1) }
        sizePx = next
    }

    /// Simple / Bold / Highlight, with the video editor's exact values (`NativeEditorSession.applyTextPreset`).
    mutating func applyEditorPreset(_ preset: String) {
        guard Self.presets.contains(preset) else { return }
        setEditorValue(.string(preset), forKey: "editor_preset")
        setEditorValue(.string(preset == "Simple" ? "Inter Regular" : "Inter"), forKey: "font_family")
        setEditorValue(.string(preset == "Highlight" ? "#30352C" : "#FFFFFF"), forKey: "color")
        setEditorValue(preset == "Highlight" ? .string("#FFF0A6") : nil, forKey: "background_color")
    }

    private static func clamped(_ value: Double?, _ range: ClosedRange<Double>) -> Double? {
        guard let value, value.isFinite else { return nil }
        return min(max(value, range.lowerBound), range.upperBound)
    }
}

/// Backs the real Text panel with one slide's texts. Every write is one `SlidePostSession.updateText`,
/// so it stages the draft (undoable, unsaved-state aware); a panel gesture (`beginTransaction` ...
/// `endTransaction`) collapses into a single undo step.
@MainActor final class SlidePostTextEditor: NativeTextEditing {
    let session: SlidePostSession
    var slideID: String
    private var group: String?
    private var forwarding: AnyCancellable?

    init(session: SlidePostSession, slideID: String) {
        self.session = session; self.slideID = slideID
        forwarding = session.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }

    private var texts: [SlidePostTextElement] { session.draft?.slides.first { $0.id == slideID }?.edits?.effectiveTexts ?? [] }
    private func mutate(_ id: String, _ body: (inout SlidePostTextElement) -> Void) {
        session.updateText(slideID: slideID, textID: id, coalescing: group, body)
    }

    // MARK: NativeTransactionControlling
    func beginTransaction() { if group == nil { group = "panel-" + UUID().uuidString } }
    func endTransaction() { group = nil }

    // MARK: NativeTextEditing
    func textElement(id: String) -> EditorTextElement? { texts.first { $0.id == id }?.editorElement }
    func textAnchor(id: String) -> CGPoint? {
        texts.first { $0.id == id }.map { SlidePostTextLayout.anchor(for: $0) }.map { CGPoint(x: $0.x, y: $0.y) }
    }
    func canEdit(_ section: EditorSection) -> Bool { !session.isBusy && session.draft != nil }
    /// Slides have no timeline; the panel hides its timing fields for slides.
    var timelineProjection: NativeEditorTimelineProjection {
        NativeEditorTimelineProjection(baseClipWindows: [], clipWindows: [], carouselItem: nil, baseInsertionTime: nil, downstreamShift: 0, totalDuration: 0)
    }
    func textDeletion(id: String) -> NativeEditorSession.TextDeletion {
        texts.contains { $0.id == id } ? .allowed : .blocked("This text no longer exists.")
    }
    @discardableResult func deleteText(id: String) -> Bool {
        guard texts.contains(where: { $0.id == id }), canEdit(.text) else { return false }
        session.removeText(slideID: slideID, textID: id)
        return true
    }
    func updateTextContent(id: String, content: String) {
        guard canEdit(.text) else { return }
        // The server counts code points; refuse growth past 120 but never reject a shorter edit.
        let capped = String(String.UnicodeScalarView(content.unicodeScalars.prefix(SlidePostTextElement.maxLength)))
        mutate(id) { $0.text = capped }
    }
    func updateTextTiming(id: String, startS: Double?, endS: Double?) {}
    func setTextStyle(id: String, style: String) { write(id, "font_family", .string(style)) }
    func setTextSize(id: String, sizePX: Double?) { guard canEdit(.text) else { return }; mutate(id) { $0.setSize(sizePX) } }
    func setTextAlignment(id: String, alignment: String?) { write(id, "alignment", alignment.map(JSONValue.string)) }
    func setTextColor(id: String, color: String?) { write(id, "color", color.map(JSONValue.string)) }
    func setTextPosition(id: String, x: Double, y: Double) {
        guard canEdit(.text) else { return }
        mutate(id) {
            $0.setEditorValue(.string("custom"), forKey: "position")
            $0.setEditorValue(.number(x), forKey: "x_frac"); $0.setEditorValue(.number(y), forKey: "y_frac")
        }
    }
    func setTextShadow(id: String, enabled: Bool) { write(id, "shadow_enabled", .bool(enabled)) }
    func updateTextRaw(id: String, key: String, value: JSONValue?) { write(id, key, value) }
    func applyTextPreset(id: String, preset: String) { guard canEdit(.text) else { return }; mutate(id) { $0.applyEditorPreset(preset) } }
    func setTextPhase(id: String, phase: String, effect: String) {}
    func setTextAnimationSpeed(id: String, speed: Double) {}

    private func write(_ id: String, _ key: String, _ value: JSONValue?) {
        guard canEdit(.text) else { return }
        mutate(id) { $0.setEditorValue(value, forKey: key) }
    }
}
