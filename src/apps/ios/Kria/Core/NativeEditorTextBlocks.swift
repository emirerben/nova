import Foundation

/// One authored text block as the editor's Text tab lists it (KRI-185).
///
/// The document does not type its text elements: a title, a per-clip label and a
/// creator's own text are all `EditorTextElement`s that differ only by id and role
/// (the server names them in `guided_story.py`: `guided-title`, `clip-label-<cut>`).
/// This is the one place that reads those names, so the list, its tests and any
/// later consumer agree on what a block is called.
struct EditorTextBlock: Equatable, Identifiable, Sendable {
    enum Kind: Equatable, Sendable {
        case title
        /// 1-based position among the clip labels, in time order.
        case clipLabel(Int)
        case closing
        case other
    }

    let id: String
    let text: String
    let startS: Double
    let endS: Double
    let kind: Kind

    /// A short name for the row's second line.
    var kindLabel: String {
        switch kind {
        case .title: return "Title"
        case .clipLabel(let position): return "Clip \(position)"
        case .closing: return "Closing"
        case .other: return "Text"
        }
    }

    var timeRange: String { "\(Self.clock(startS)) – \(Self.clock(endS))" }

    /// `m:ss.s`, the same shape as the timeline's timecodes.
    static func clock(_ seconds: Double) -> String {
        let clamped = max(0, seconds)
        let tenths = Int((clamped * 10).rounded())
        return String(format: "%d:%02d.%d", tenths / 600, (tenths / 10) % 60, tenths % 10)
    }
}

extension EditorTextBlock {
    /// A row opens its block by abandoning the new-text draft, so a draft with words
    /// in it keeps the list inert instead of silently discarding what was typed.
    static func listIsInteractive(draft: String?, canEdit: Bool) -> Bool {
        canEdit && (draft ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

extension EditorDocument {
    fileprivate static func trailingNumber(of id: String) -> Int? {
        let digits = id.reversed().prefix { $0.isNumber }
        return digits.isEmpty ? nil : Int(String(digits.reversed()))
    }

    /// The on-screen text a creator can edit, in the order it appears.
    ///
    /// Left out on purpose: captions (they have their own tab and list), blocks the
    /// renderer skips (`removed`, `enabled == false`), and zero-length blocks that
    /// cannot be selected on the timeline either.
    var textBlocks: [EditorTextBlock] {
        var seen = Set<String>()
        let authored = textElements.filter { element in
            !element.isCaption
                && element.raw["removed"] != .bool(true)
                && element.raw["enabled"] != .bool(false)
                && element.endS > element.startS
                // A duplicate id would crash `ForEach` and mislabel a row; keep the first.
                && seen.insert(element.id).inserted
        }
        // "Clip N" is the clip's own number, so a clip without a label, or a removed
        // label, never renumbers its neighbours. The server names a label after its cut
        // ("clip-label-unified-cut-3"); an id without a trailing number falls back to the
        // label's place in time order.
        var labelPosition: [String: Int] = [:]
        let labels = authored
            .filter { $0.id.hasPrefix("clip-label-") }
            .sorted { ($0.startS, $0.id) < ($1.startS, $1.id) }
        for (index, element) in labels.enumerated() {
            labelPosition[element.id] = Self.trailingNumber(of: element.id) ?? index + 1
        }
        func kind(of element: EditorTextElement) -> EditorTextBlock.Kind {
            if element.id == "guided-title" { return .title }
            if let position = labelPosition[element.id] { return .clipLabel(position) }
            if element.id == "guided-closing-title" { return .closing }
            return .other
        }
        // The title and its first clip label start together; the title reads first.
        func rank(_ kind: EditorTextBlock.Kind) -> Int {
            switch kind {
            case .title: return 0
            case .clipLabel: return 1
            case .other: return 2
            case .closing: return 3
            }
        }
        return authored
            .map { element in
                EditorTextBlock(
                    id: element.id, text: element.text,
                    startS: element.startS, endS: element.endS, kind: kind(of: element)
                )
            }
            .sorted { lhs, rhs in
                (lhs.startS, rank(lhs.kind), lhs.id) < (rhs.startS, rank(rhs.kind), rhs.id)
            }
    }
}
