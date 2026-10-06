import SwiftUI

/// Begin/end of one undo group around a gesture (a slider drag, typing). Nested `begin`s are no-ops.
@MainActor protocol NativeTransactionControlling: ObservableObject {
    func beginTransaction()
    func endTransaction()
}

/// Exactly what the real Text panel (`NativeEditorTextPanel`) reads and writes. The video editor's
/// `NativeEditorSession` conforms with no behaviour change; slide posts conform through
/// `SlidePostTextEditor`, so both surfaces share one panel instead of two look-alikes.
///
/// Style lives in `EditorTextElement.raw` (font_family, color, alignment, position, x_frac, y_frac,
/// rotation_deg, editor_preset, size_px, background_color, stroke_*, shadow_* ...). A conformer that
/// stores style differently maps it in `textElement(id:)` and back in `updateTextRaw`.
@MainActor protocol NativeTextEditing: NativeTransactionControlling {
    func textElement(id: String) -> EditorTextElement?
    /// Where the surface draws the text (0–1 canvas), presets resolved its own way.
    func textAnchor(id: String) -> CGPoint?
    func canEdit(_ section: EditorSection) -> Bool
    var timelineProjection: NativeEditorTimelineProjection { get }
    func textDeletion(id: String) -> NativeEditorSession.TextDeletion
    @discardableResult func deleteText(id: String) -> Bool

    func updateTextContent(id: String, content: String)
    func updateTextTiming(id: String, startS: Double?, endS: Double?)
    func setTextStyle(id: String, style: String)
    func setTextSize(id: String, sizePX: Double?)
    func setTextAlignment(id: String, alignment: String?)
    func setTextColor(id: String, color: String?)
    func setTextPosition(id: String, x: Double, y: Double)
    func setTextShadow(id: String, enabled: Bool)
    func updateTextRaw(id: String, key: String, value: JSONValue?)
    func applyTextPreset(id: String, preset: String)
    func setTextPhase(id: String, phase: String, effect: String)
    func setTextAnimationSpeed(id: String, speed: Double)
}

extension NativeTextEditing {
    /// Move the text along one axis; the other stays where the text is drawn,
    /// so a row on a named preset keeps that spot rather than stale fracs.
    func moveText(id: String, x: Double? = nil, y: Double? = nil) {
        guard let anchor = textAnchor(id: id) else { return }
        setTextPosition(id: id, x: x ?? anchor.x, y: y ?? anchor.y)
    }
}

extension NativeEditorSession: NativeTextEditing {
    func textElement(id: String) -> EditorTextElement? { document.textElements.first { $0.id == id } }
    func textAnchor(id: String) -> CGPoint? { textElement(id: id)?.anchor }
}
