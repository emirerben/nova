import Foundation

/// Live plan & review (KRI-440/444/445/451): the pure state of the "Review your video" sheet. No view, no
/// network: the flagged sections, the manual edits and the prompt the creator has built up, and what they turn
/// into on the wire. Wire contract: `docs/pipelines/live-plan-blocks.md` (sections 4 and 7).
struct ReviewPlanDraft: Equatable, Sendable {
    /// Sections flagged with "Change". Never contains `post_caption` (display-only in v2).
    private(set) var flagged: Set<PlanSectionID> = []
    /// The title text as the creator typed it; nil = untouched.
    private(set) var titleText: String?
    /// Caption line id -> the text typed for it. A line typed back to its original is removed.
    private(set) var captionEdits: [String: String] = [:]
    private(set) var musicLevel: Double?
    private(set) var originalLevel: Double?
    var prompt = ""

    /// Sections that carry a manual edit. An edit flags its section for the turn.
    var editedSections: Set<PlanSectionID> {
        var sections: Set<PlanSectionID> = []
        if titleText != nil { sections.insert(.title) }
        if !captionEdits.isEmpty { sections.insert(.captions) }
        if musicLevel != nil || originalLevel != nil { sections.insert(.music) }
        return sections
    }

    /// What the turn is scoped to: flagged plus edited sections, in display order.
    var scope: [PlanSectionID] {
        flagged.union(editedSections).filter(\.isScopable).sorted { $0.order < $1.order }
    }

    var isEmpty: Bool { scope.isEmpty }
    var canUpdate: Bool { !scope.isEmpty }
    var trimmedPrompt: String { prompt.trimmingCharacters(in: .whitespacesAndNewlines) }

    func isFlagged(_ section: PlanSectionID) -> Bool { section.isScopable && (flagged.contains(section) || editedSections.contains(section)) }

    /// Tapping "Change" / "Changing". Un-flagging also discards that section's manual edits.
    mutating func toggle(_ section: PlanSectionID) {
        guard section.isScopable else { return }
        if isFlagged(section) { remove(section) } else { flagged.insert(section) }
    }

    /// The x on a chip.
    mutating func remove(_ section: PlanSectionID) {
        flagged.remove(section)
        switch section {
        case .title: titleText = nil
        case .captions: captionEdits = [:]
        case .music: musicLevel = nil; originalLevel = nil
        default: break
        }
    }

    mutating func setFlagged(_ sections: Set<PlanSectionID>) { flagged = sections.filter(\.isScopable) }

    /// Keeps only flags the server still lets the creator change (a refreshed snapshot may say a section is no
    /// longer editable, or was skipped).
    mutating func prune(keeping allowed: Set<PlanSectionID>) {
        flagged = flagged.intersection(allowed)
        if !allowed.contains(.title) { titleText = nil }
        if !allowed.contains(.captions) { captionEdits = [:] }
        if !allowed.contains(.music) { musicLevel = nil; originalLevel = nil }
    }

    mutating func editTitle(_ text: String, original: String) {
        titleText = text == original ? nil : text
    }

    mutating func editCaption(id: String, text: String, original: String) {
        if text == original { captionEdits.removeValue(forKey: id) } else { captionEdits[id] = text }
    }

    mutating func editMix(musicLevel music: Double? = nil, originalLevel original: Double? = nil, current: PlanMixLevels?) {
        if let music { musicLevel = Self.differs(music, current?.musicLevel) ? music : nil }
        if let original { originalLevel = Self.differs(original, current?.originalLevel) ? original : nil }
    }

    private static func differs(_ value: Double, _ current: Double?) -> Bool {
        guard let current else { return true }
        return abs(value - current) > 0.005
    }

    /// The deterministic edits (no model call): title text, caption line text, sound mix. The title edit needs the
    /// title payload's `bar_id`; without one it cannot be targeted and is skipped.
    func manualEdits(titleBarID: String?, captionOrder: [String] = []) -> [ManualPlanEdit] {
        var edits: [ManualPlanEdit] = []
        if let titleText, let titleBarID {
            edits.append(ManualPlanEdit(kind: "rewrite_text", targetID: titleBarID, text: titleText,
                                        musicLevel: nil, originalLevel: nil, musicGainDB: nil))
        }
        let ids = captionOrder.filter { captionEdits[$0] != nil } + captionEdits.keys.filter { !captionOrder.contains($0) }.sorted()
        for id in ids {
            guard let text = captionEdits[id] else { continue }
            edits.append(ManualPlanEdit(kind: "rewrite_text", targetID: id, text: text,
                                        musicLevel: nil, originalLevel: nil, musicGainDB: nil))
        }
        if musicLevel != nil || originalLevel != nil {
            edits.append(ManualPlanEdit(kind: "set_mix", targetID: nil, text: nil,
                                        musicLevel: musicLevel, originalLevel: originalLevel, musicGainDB: nil))
        }
        return edits
    }

    /// The turn message. What the creator typed is sent verbatim; otherwise "Update: captions, music".
    var message: String {
        if !trimmedPrompt.isEmpty { return trimmedPrompt }
        return "Update: " + scope.map { $0.label.lowercased() }.joined(separator: ", ")
    }
}

/// What the Review sheet needs from the chat workspace. Closures, so the workspace keeps owning the thread
/// revision, the delta feed and the approval (the sheet never touches them).
struct ReviewPlanActions {
    var loadSnapshot: @MainActor () async throws -> PlanSnapshot
    /// Submits the scoped turn and auto-approves the resulting draft (tapping Update video is the consent).
    var update: @MainActor (_ scope: [PlanSectionID], _ edits: [ManualPlanEdit], _ message: String) async throws -> Void
    var undoSection: @MainActor (_ section: PlanSectionID, _ blockRevision: Int, _ draftRevision: Int) async throws -> Void
    var undoAll: @MainActor (_ draftRevision: Int) async throws -> Void
}

/// Why an update could not start, in the creator's words.
enum ReviewPlanError: Error, Equatable {
    /// Kria answered with text instead of a draft (nothing to change, or it would touch unflagged sections).
    case declined(String)
    case timedOut

    var message: String {
        switch self {
        case .declined(let text): text.isEmpty ? "Kria couldn’t make that change." : text
        case .timedOut: "Kria is taking longer than usual to start the update. Check the chat and try again."
        }
    }
}
