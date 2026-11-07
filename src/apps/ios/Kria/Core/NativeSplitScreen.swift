import Foundation

struct EditorSplitScreenSection: Equatable, Sendable, Identifiable {
    var id: String
    var durationS: Double
    var panelCount: Int
    var panelWeights: [Double]
    var gutter: Double
    var backgroundColor: String

    init(id: String, durationS: Double, panelCount: Int, panelWeights: [Double]? = nil, gutter: Double = 0, backgroundColor: String = "#000000") {
        self.id = id; self.durationS = durationS; self.panelCount = panelCount
        self.panelWeights = panelWeights ?? Array(repeating: 1, count: panelCount)
        self.gutter = gutter; self.backgroundColor = backgroundColor.uppercased()
    }

    init?(json: JSONValue) {
        guard let o = json.objectValue, Set(o.keys).isSubset(of: ["id", "duration_s", "panel_count", "panel_weights", "gutter", "background_color"]), let id = o["id"]?.stringValue,
              let duration = o["duration_s"]?.numberValue, let count = o["panel_count"]?.numberValue,
              count == 2 || count == 3 else { return nil }
        let weights: [Double]?
        if case .array(let values)? = o["panel_weights"] {
            let numbers = values.compactMap(\.numberValue)
            guard numbers.count == values.count else { return nil }
            weights = numbers
        } else { guard o["panel_weights"] == nil else { return nil }; weights = nil }
        guard o["gutter"] == nil || o["gutter"]?.numberValue != nil,
              o["background_color"] == nil || o["background_color"]?.stringValue != nil else { return nil }
        self.init(id: id, durationS: duration, panelCount: Int(count), panelWeights: weights,
                  gutter: o["gutter"]?.numberValue ?? 0, backgroundColor: o["background_color"]?.stringValue ?? "#000000")
        guard isValid else { return nil }
    }

    var isValid: Bool {
        !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && id.count <= 160 && durationS.isFinite && durationS >= 0.1 && durationS <= 1800
        && (2...3).contains(panelCount) && panelWeights.count == panelCount
        && panelWeights.allSatisfy { $0.isFinite && $0 > 0 } && panelWeights.reduce(0, +).isFinite
        && gutter.isFinite && (0...0.1).contains(gutter)
        && EditorSpeakerBackground.isStrictHexColor(backgroundColor)
    }

    var json: JSONValue {
        .object(["id": .string(id), "duration_s": .number(durationS), "panel_count": .number(Double(panelCount)),
                 "panel_weights": .array(panelWeights.map(JSONValue.number)), "gutter": .number(gutter), "background_color": .string(backgroundColor)])
    }
}

extension EditorTimelineSlot {
    var splitSectionID: String? { raw["split_section_id"]?.stringValue }
    var splitPanelIndex: Int? {
        guard let n = raw["split_panel_index"]?.numberValue, n.isFinite, n.rounded() == n, (0...2).contains(n) else { return nil }
        return Int(n)
    }
    var splitOffsetS: Double { raw["split_offset_s"]?.numberValue ?? 0 }
    mutating func setSplitMembership(sectionID: String, panelIndex: Int, offset: Double) {
        raw["split_section_id"] = .string(sectionID)
        raw["split_panel_index"] = .number(Double(panelIndex))
        raw["split_offset_s"] = .number(offset)
    }
    mutating func clearSplitMembership() {
        for key in ["split_section_id", "split_panel_index", "split_offset_s"] { raw.removeValue(forKey: key) }
    }
}

extension EditorDocument {
    var splitScreenIsValid: Bool {
        guard !unreadableSplitScreen, splitScreenSections.count <= 100, splitScreenSections.allSatisfy(\.isValid),
              Set(splitScreenSections.map(\.id)).count == splitScreenSections.count else { return false }
        var seen = Set<String>()
        var previous: String?
        for slot in clips where !slot.removed {
            guard let id = slot.splitSectionID else {
                guard ["split_section_id", "split_panel_index", "split_offset_s"].allSatisfy({ slot.raw[$0] == nil || slot.raw[$0] == .null }) else { return false }
                previous = nil; continue
            }
            guard let section = splitScreenSections.first(where: { $0.id == id }),
                  let panel = slot.splitPanelIndex, panel < section.panelCount,
                  slot.raw["split_offset_s"]?.numberValue != nil,
                  slot.splitOffsetS.isFinite, slot.splitOffsetS >= 0,
                  let duration = slot.durationS, duration.isFinite, duration >= 0.1,
                  slot.splitOffsetS + duration <= section.durationS + 0.000001 else { return false }
            if previous != id, !seen.insert(id).inserted { return false }
            previous = id
        }
        for section in splitScreenSections {
            for panel in 0..<section.panelCount {
                let members = clips.filter { !$0.removed && $0.splitSectionID == section.id && $0.splitPanelIndex == panel }.sorted { $0.splitOffsetS < $1.splitOffsetS }
                for i in members.indices {
                    for j in 0..<i {
                        let left = members[j], right = members[i]
                        let overlap = left.splitOffsetS + (left.durationS ?? 0) - right.splitOffsetS
                        guard overlap > 0.000001 else { continue }
                        let allowed = min(0.3, left.transitionDurationS ?? 0.3, (left.durationS ?? 0) * 0.3, (right.durationS ?? 0) * 0.3)
                        guard j == i - 1, right.splitOffsetS > left.splitOffsetS,
                              left.transitionAfter != "cut", allowed >= 0.1, overlap <= allowed + 0.000001 else { return false }
                    }
                }
            }
        }
        guard seen == Set(splitScreenSections.map(\.id)) else { return false }
        return splitScreenSections.isEmpty || NativeEditorInteraction.timelineProjection(slots: clips, carousel: nil, splitSections: splitScreenSections).totalDuration <= 1800
    }
}
