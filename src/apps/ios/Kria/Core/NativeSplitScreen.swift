import CoreGraphics
import Foundation
import KriaMediaEngine

enum NativeSplitScope: Equatable { case selectedClip, wholeVideo }

/// Which side of the playhead leaves the split and plays full screen.
enum NativeSplitFullScreenSide: Equatable { case fromPlayhead, untilPlayhead }

/// Panel geometry in top-origin unit space: x 0, width 1, y and height as fractions of the canvas height.
/// Heights follow the compiler and engine: the gutters come off first, the rest is shared by weight.
struct NativeSplitPanelLayout {
    static let minimumPairShare = 0.15
    static let equalSnapShare = 0.02

    static func panelRects(weights: [Double], gutter: Double) -> [CGRect] {
        let total = weights.reduce(0, +)
        guard !weights.isEmpty, total.isFinite, total > 0 else { return [] }
        let usable = 1 - gutter * Double(weights.count - 1)
        var top = 0.0
        return weights.map { weight in
            let height = usable * weight / total
            defer { top += height + gutter }
            return CGRect(x: 0, y: top, width: 1, height: height)
        }
    }

    static func dividerYs(weights: [Double], gutter: Double) -> [Double] {
        panelRects(weights: weights, gutter: gutter).dropLast().map { Double($0.maxY) + gutter / 2 }
    }

    /// The panel indexes of `active` that exist, ascending and without repeats.
    static func normalizedActive(_ active: [Int], count: Int) -> [Int] {
        Array(Set(active.filter { (0..<count).contains($0) })).sorted()
    }

    /// Collapse layout: only the active panels are drawn. The gutters between them come off first and the rest is
    /// shared by the active panels' weights, so one active panel fills the canvas and none gives an empty list.
    /// Element `i` is the rect of `normalizedActive(active, count:)[i]`.
    static func panelRects(weights: [Double], gutter: Double, active: [Int]) -> [CGRect] {
        panelRects(weights: normalizedActive(active, count: weights.count).map { weights[$0] }, gutter: gutter)
    }

    static func dividerYs(weights: [Double], gutter: Double, active: [Int]) -> [Double] {
        dividerYs(weights: normalizedActive(active, count: weights.count).map { weights[$0] }, gutter: gutter)
    }

    /// The panels shown at `localTime` (seconds from the section start). A collapse section shows a panel while one
    /// of its members' half-open windows `[offset, offset + duration)` holds the time; a panel with no members is
    /// never shown and overlapping members of one panel count once. Any other section always shows every panel.
    static func activePanels(in section: EditorSplitScreenSection, members: [EditorTimelineSlot], at localTime: Double) -> [Int] {
        guard section.collapseEmptyPanels else { return Array(0..<section.panelCount) }
        var active = Set<Int>()
        for slot in members where !slot.removed && slot.splitSectionID == section.id {
            guard let panel = slot.splitPanelIndex, panel < section.panelCount, let duration = slot.durationS else { continue }
            if slot.splitOffsetS <= localTime, localTime < slot.splitOffsetS + duration { active.insert(panel) }
        }
        return active.sorted()
    }

    /// Moves the divider between active panels `a` and `b`, which must be neighbours among `active`, by
    /// `deltaFraction` of the canvas height. Only those two weights change and their sum is kept.
    static func draggedWeights(_ weights: [Double], between a: Int, and b: Int, active: [Int], gutter: Double, deltaFraction: Double) -> [Double] {
        let shown = normalizedActive(active, count: weights.count)
        guard let position = shown.firstIndex(of: a), shown.indices.contains(position + 1), shown[position + 1] == b else { return weights }
        let moved = draggedWeights(shown.map { weights[$0] }, boundary: position, gutter: gutter, deltaFraction: deltaFraction)
        var result = weights
        result[a] = moved[position]; result[b] = moved[position + 1]
        return result
    }

    /// Moves the divider below panel `boundary` by `deltaFraction` of the canvas height. Only that panel and the
    /// next change, and their sum is kept, so every other panel keeps its height.
    static func draggedWeights(_ weights: [Double], boundary: Int, gutter: Double, deltaFraction: Double) -> [Double] {
        guard boundary >= 0, weights.indices.contains(boundary + 1), deltaFraction.isFinite else { return weights }
        let total = weights.reduce(0, +)
        let usable = 1 - gutter * Double(weights.count - 1)
        let pair = weights[boundary] + weights[boundary + 1]
        guard total.isFinite, total > 0, usable > 0, pair > 0 else { return weights }
        let share = min(1 - minimumPairShare, max(minimumPairShare, (weights[boundary] + deltaFraction * total / usable) / pair))
        var result = weights
        if abs(share - 0.5) <= equalSnapShare {
            result[boundary] = pair / 2; result[boundary + 1] = pair / 2
        } else {
            result[boundary] = pair * share; result[boundary + 1] = pair - pair * share
        }
        return result
    }
}

struct NativeSplitContext: Equatable {
    let sectionID: String
    /// Timeline seconds.
    let sectionStart: Double
    let section: EditorSplitScreenSection
    /// Panels with no clips at all.
    let emptyPanels: [Int]
    /// The panels drawn at the playhead: every panel, unless the section collapses its empty ones.
    let activePanels: [Int]
}

struct EditorSplitScreenSection: Equatable, Sendable, Identifiable {
    var id: String
    var durationS: Double
    var panelCount: Int
    var panelWeights: [Double]
    var gutter: Double
    var backgroundColor: String
    /// Phone protocol 47. When a panel's clips end, the panels still playing grow to fill the screen. A section
    /// lasts until its longest member ends. False keeps every panel's place for the whole section.
    var collapseEmptyPanels: Bool

    init(id: String, durationS: Double, panelCount: Int, panelWeights: [Double]? = nil, gutter: Double = 0, backgroundColor: String = "#000000",
         collapseEmptyPanels: Bool = false) {
        self.id = id; self.durationS = durationS; self.panelCount = panelCount
        self.panelWeights = panelWeights ?? Array(repeating: 1, count: panelCount)
        self.gutter = gutter; self.backgroundColor = backgroundColor.uppercased()
        self.collapseEmptyPanels = collapseEmptyPanels
    }

    init?(json: JSONValue) {
        guard let o = json.objectValue, Set(o.keys).isSubset(of: ["id", "duration_s", "panel_count", "panel_weights", "gutter", "background_color", "collapse_empty_panels"]), let id = o["id"]?.stringValue,
              let duration = o["duration_s"]?.numberValue, let count = o["panel_count"]?.numberValue,
              count == 2 || count == 3 else { return nil }
        let weights: [Double]?
        if case .array(let values)? = o["panel_weights"] {
            let numbers = values.compactMap(\.numberValue)
            guard numbers.count == values.count else { return nil }
            weights = numbers
        } else { guard o["panel_weights"] == nil else { return nil }; weights = nil }
        guard o["gutter"] == nil || o["gutter"]?.numberValue != nil,
              o["background_color"] == nil || o["background_color"]?.stringValue != nil,
              o["collapse_empty_panels"] == nil || o["collapse_empty_panels"]?.boolValue != nil else { return nil }
        self.init(id: id, durationS: duration, panelCount: Int(count), panelWeights: weights,
                  gutter: o["gutter"]?.numberValue ?? 0, backgroundColor: o["background_color"]?.stringValue ?? "#000000",
                  collapseEmptyPanels: o["collapse_empty_panels"]?.boolValue ?? false)
        guard isValid else { return nil }
    }

    var isValid: Bool {
        !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && id.count <= 160 && durationS.isFinite && durationS >= EditorLimits.minClipSeconds && durationS <= 1800
        && (2...3).contains(panelCount) && panelWeights.count == panelCount
        && panelWeights.allSatisfy { $0.isFinite && $0 > 0 } && panelWeights.reduce(0, +).isFinite
        && gutter.isFinite && (0...0.1).contains(gutter)
        && EditorSpeakerBackground.isStrictHexColor(backgroundColor)
    }

    var json: JSONValue {
        var object: [String: JSONValue] = ["id": .string(id), "duration_s": .number(durationS), "panel_count": .number(Double(panelCount)),
                                           "panel_weights": .array(panelWeights.map(JSONValue.number)), "gutter": .number(gutter), "background_color": .string(backgroundColor)]
        if collapseEmptyPanels { object["collapse_empty_panels"] = .bool(true) }
        return .object(object)
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

    /// KRI-1421: a flat-colour block in a split panel. It has no source, so no clip index (the in-memory `clipIndex` is -1
    /// and the wire row omits the key), and it lives only inside a section.
    static let blankMediaKind = "blank"
    var isBlank: Bool { raw["media_kind"]?.stringValue == Self.blankMediaKind }
    /// `#RRGGBB`, uppercase; nil when this is not a blank or its colour is unreadable.
    var blankColor: String? {
        guard isBlank, let value = raw["color"]?.stringValue else { return nil }
        return EditorSpeakerBackground.normalizedColor(value)
    }
    static func blank(id: String = UUID().uuidString, color: String, durationS: Double, sectionID: String, panelIndex: Int, offset: Double) -> EditorTimelineSlot {
        var slot = EditorTimelineSlot(id: id, clipIndex: -1, inS: 0, durationS: durationS,
                                      raw: ["media_kind": .string(blankMediaKind), "color": .string(EditorSpeakerBackground.normalizedColor(color) ?? color)])
        slot.setSplitMembership(sectionID: sectionID, panelIndex: panelIndex, offset: offset)
        return slot
    }
}

extension EditorDocument {
    /// A collapse section lasts until its longest member ends. Called after every timeline edit, so a trim,
    /// extension, add, remove, cut, move or swap that changes the members also changes the section, in the same
    /// undo step. Other sections keep the length they were given.
    mutating func fitCollapseSectionDurations() {
        for index in splitScreenSections.indices where splitScreenSections[index].collapseEmptyPanels {
            let id = splitScreenSections[index].id
            let end = clips.filter { !$0.removed && $0.splitSectionID == id }
                .compactMap { slot in slot.durationS.map { slot.splitOffsetS + $0 } }.max()
            if let end, end.isFinite, abs(end - splitScreenSections[index].durationS) > 0.000001 { splitScreenSections[index].durationS = end }
        }
    }

    var splitScreenIsValid: Bool {
        guard !unreadableSplitScreen, splitScreenSections.count <= 100, splitScreenSections.allSatisfy(\.isValid),
              Set(splitScreenSections.map(\.id)).count == splitScreenSections.count else { return false }
        var seen = Set<String>()
        var previous: String?
        for slot in clips where !slot.removed {
            guard let id = slot.splitSectionID else {
                guard !slot.isBlank, ["split_section_id", "split_panel_index", "split_offset_s"].allSatisfy({ slot.raw[$0] == nil || slot.raw[$0] == .null }) else { return false }
                previous = nil; continue
            }
            // A blank is a colour and a window: no source, and a colour the engine can draw.
            guard !slot.isBlank || (slot.clipIndex == -1 && slot.blankColor != nil) else { return false }
            guard let section = splitScreenSections.first(where: { $0.id == id }),
                  let panel = slot.splitPanelIndex, panel < section.panelCount,
                  slot.raw["split_offset_s"]?.numberValue != nil,
                  slot.splitOffsetS.isFinite, slot.splitOffsetS >= 0,
                  let duration = slot.durationS, duration.isFinite, duration >= EditorLimits.minClipSeconds,
                  slot.splitOffsetS + duration <= section.durationS + 0.000001 else { return false }
            if previous != id, !seen.insert(id).inserted { return false }
            previous = id
        }
        for section in splitScreenSections {
            // A section of only blanks has nothing to play.
            guard clips.contains(where: { !$0.removed && !$0.isBlank && $0.splitSectionID == section.id }) else { return false }
            for panel in 0..<section.panelCount {
                let members = clips.filter { !$0.removed && $0.splitSectionID == section.id && $0.splitPanelIndex == panel }.sorted { $0.splitOffsetS < $1.splitOffsetS }
                for i in members.indices {
                    for j in 0..<i {
                        let left = members[j], right = members[i]
                        let overlap = left.splitOffsetS + (left.durationS ?? 0) - right.splitOffsetS
                        guard overlap > 0.000001 else { continue }
                        // A blank has no transition: it never shares time with another member of its panel.
                        guard !left.isBlank, !right.isBlank else { return false }
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

    /// A panel's live clips in the order they play.
    func splitPanelMembers(sectionID: String, panel: Int) -> [EditorTimelineSlot] {
        clips.filter { !$0.removed && $0.splitSectionID == sectionID && $0.splitPanelIndex == panel }.sorted { $0.splitOffsetS < $1.splitOffsetS }
    }

    /// The free part of a panel a new clip goes into, in section seconds: the gap at `local` when `local` falls
    /// in one, else everything after the panel's last clip.
    func splitPanelOpening(sectionID: String, panel: Int, near local: Double?) -> (start: Double, end: Double)? {
        guard let section = splitScreenSections.first(where: { $0.id == sectionID }), (0..<section.panelCount).contains(panel) else { return nil }
        let spans = splitPanelMembers(sectionID: sectionID, panel: panel)
            .map { (start: $0.splitOffsetS, end: $0.splitOffsetS + ($0.durationS ?? 0)) }
        if let local, local >= 0, local < section.durationS,
           !spans.contains(where: { $0.start <= local + 0.000001 && local < $0.end - 0.000001 }) {
            return (spans.map(\.end).filter { $0 <= local + 0.000001 }.max() ?? 0,
                    spans.map(\.start).filter { $0 > local }.min() ?? section.durationS)
        }
        return (spans.map(\.end).max() ?? 0, section.durationS)
    }
}
