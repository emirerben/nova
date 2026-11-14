import Foundation

public struct SplitScreenMembership: Codable, Equatable, Sendable {
    public var sectionID: String
    public var panelIndex: Int
    private enum CodingKeys: String, CodingKey { case sectionID = "sectionId", panelIndex }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["sectionId", "panelIndex"])
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sectionID = try container.decode(String.self, forKey: .sectionID)
        panelIndex = try container.decode(Int.self, forKey: .panelIndex)
        guard !sectionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, (0...2).contains(panelIndex) else {
            throw RecipeError.invalidTimeline
        }
    }
    public init(sectionID: String, panelIndex: Int) {
        self.sectionID = sectionID; self.panelIndex = panelIndex
    }
}

/// KRI-1421 (protocol 51, `splitBlankPanel`): a flat colour drawn inside one panel for `[start, end)`. It has no source,
/// so it adds no clip, track or audio; it only counts as a member for `collapseEmptyPanels`.
public struct SplitScreenBlank: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var panelIndex: Int
    public var start: Double
    public var end: Double
    /// `#RRGGBB`, any case.
    public var color: String

    public init(id: String, panelIndex: Int, start: Double, end: Double, color: String) {
        self.id = id; self.panelIndex = panelIndex; self.start = start; self.end = end; self.color = color
    }

    private enum CodingKeys: String, CodingKey { case id, panelIndex, start, end, color }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["id", "panelIndex", "start", "end", "color"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        panelIndex = try c.decode(Int.self, forKey: .panelIndex)
        start = try c.decode(Double.self, forKey: .start)
        end = try c.decode(Double.self, forKey: .end)
        color = try c.decode(String.self, forKey: .color)
        guard ClipBackdrop.components(color) != nil else { throw RecipeError.invalidTimeline }
    }
}

public struct SplitScreenSection: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var start: Double
    public var end: Double
    public var panelWeights: [Double]
    public var gutter: Double
    public var backgroundColor: String
    /// Contract "Collapse mode": only panels with a member playing are drawn, sharing the height by weight.
    public var collapseEmptyPanels: Bool
    /// Protocol 51. Flat-colour blocks drawn in a panel; empty for every section without one.
    public var blanks: [SplitScreenBlank]

    public init(id: String, start: Double, end: Double, panelWeights: [Double], gutter: Double = 0, backgroundColor: String = "#000000",
                collapseEmptyPanels: Bool = false, blanks: [SplitScreenBlank] = []) {
        self.id = id; self.start = start; self.end = end; self.panelWeights = panelWeights
        self.gutter = gutter; self.backgroundColor = backgroundColor; self.collapseEmptyPanels = collapseEmptyPanels; self.blanks = blanks
    }

    private enum CodingKeys: String, CodingKey { case id, start, end, panelWeights, gutter, backgroundColor, collapseEmptyPanels, blanks }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["id", "start", "end", "panelWeights", "gutter", "backgroundColor", "collapseEmptyPanels", "blanks"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        start = try c.decode(Double.self, forKey: .start)
        end = try c.decode(Double.self, forKey: .end)
        panelWeights = try c.decode([Double].self, forKey: .panelWeights)
        gutter = try c.decode(Double.self, forKey: .gutter)
        backgroundColor = try c.decode(String.self, forKey: .backgroundColor)
        collapseEmptyPanels = try c.decodeIfPresent(Bool.self, forKey: .collapseEmptyPanels) ?? false
        blanks = try c.decodeIfPresent([SplitScreenBlank].self, forKey: .blanks) ?? []
    }
    /// A fixed-layout section keeps the bytes it had before collapse existed.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id); try c.encode(start, forKey: .start); try c.encode(end, forKey: .end)
        try c.encode(panelWeights, forKey: .panelWeights); try c.encode(gutter, forKey: .gutter)
        try c.encode(backgroundColor, forKey: .backgroundColor)
        if collapseEmptyPanels { try c.encode(true, forKey: .collapseEmptyPanels) }
        if !blanks.isEmpty { try c.encode(blanks, forKey: .blanks) }
    }

    public func validate() throws {
        guard !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              start.isFinite, end.isFinite, start >= 0, end > start, end <= 1800,
              (2...3).contains(panelWeights.count), panelWeights.allSatisfy({ $0.isFinite && $0 > 0 }),
              panelWeights.reduce(0, +).isFinite, gutter.isFinite, (0...0.1).contains(gutter),
              backgroundColor.range(of: "^#[0-9A-Fa-f]{6}$", options: .regularExpression) != nil else {
            throw RecipeError.invalidTimeline
        }
        guard blanks.count <= 300, Set(blanks.map(\.id)).count == blanks.count else { throw RecipeError.invalidTimeline }
        for blank in blanks {
            guard !blank.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, blank.id.count <= 160,
                  panelWeights.indices.contains(blank.panelIndex), blank.start.isFinite, blank.end.isFinite,
                  blank.end > blank.start, blank.start >= start - 0.000_001, blank.end <= end + 0.000_001,
                  ClipBackdrop.components(blank.color) != nil else { throw RecipeError.invalidTimeline }
        }
        for (index, blank) in blanks.enumerated() {
            for other in blanks[..<index] where other.panelIndex == blank.panelIndex
                && other.start < blank.end - 0.000_001 && blank.start < other.end - 0.000_001 { throw RecipeError.invalidTimeline }
        }
    }

    /// `activePanels` nil lays out every panel. Otherwise only the listed panels share the canvas, with gutters
    /// only between them, so one active panel fills it. `index` always counts as active.
    public func panelRect(index: Int, canvas: CGRect, activePanels: [Int]? = nil) -> CGRect {
        let shown = activePanels.map { Set($0).union([index]).filter(panelWeights.indices.contains).sorted() } ?? Array(panelWeights.indices)
        let above = shown.filter { $0 < index }
        let gap = canvas.height * gutter
        let available = canvas.height - gap * Double(shown.count - 1)
        let total = shown.reduce(0) { $0 + panelWeights[$1] }
        let height = available * (panelWeights[index] / total)
        let top = available * (above.reduce(0) { $0 + panelWeights[$1] } / total) + gap * Double(above.count)
        return CGRect(x: canvas.minX, y: canvas.maxY - top - height, width: canvas.width, height: height)
    }
}

/// One stretch of a clip's window drawn at one place: inside `viewport`, or across the canvas when nil.
struct PanelPlacement: Equatable {
    let start: Double
    let end: Double
    let viewport: CGRect?
}

extension EditRecipe {
    /// Where `clip` is drawn across `[start, end)`. A clip outside any section, or in a fixed section, has one
    /// placement. In a collapsing section a placement ends wherever the set of active panels changes; a panel is
    /// active while one of its members' half-open windows holds the time (contract "Collapse mode").
    func panelPlacements(of clip: TimelineClip, start: Double, end: Double, canvas: CGRect) -> [PanelPlacement] {
        guard let membership = clip.splitScreen else { return [.init(start: start, end: end, viewport: nil)] }
        return panelPlacements(membership: membership, start: start, end: end, canvas: canvas)
    }

    /// The same for anything that sits in a panel, a clip or a blank block. A blank's window counts as a member's.
    func panelPlacements(membership: SplitScreenMembership, start: Double, end: Double, canvas: CGRect) -> [PanelPlacement] {
        guard let section = splitScreenSections.first(where: { $0.id == membership.sectionID }) else {
            return [.init(start: start, end: end, viewport: nil)]
        }
        guard section.collapseEmptyPanels else {
            return [.init(start: start, end: end, viewport: section.panelRect(index: membership.panelIndex, canvas: canvas))]
        }
        // Bounds go on the composition's 1/60000 s clock, where frames are sampled: 0.1 + 0.2 ends an ulp after the
        // 0.3 s frame another member starts on, and would otherwise draw that frame at its old rect beside the new layout.
        func onClock(_ seconds: Double) -> Double { (seconds * 60_000).rounded() / 60_000 }
        let windows = tracks.flatMap(\.clips).compactMap { member -> (panel: Int, start: Double, end: Double)? in
            guard let peer = member.splitScreen, peer.sectionID == section.id else { return nil }
            return (peer.panelIndex, onClock(member.timelineStart), onClock(member.timelineStart + member.duration))
        } + section.blanks.map { ($0.panelIndex, onClock($0.start), onClock($0.end)) }
        let (first, last) = (onClock(start), onClock(end))
        var cuts = [first]
        for cut in windows.flatMap({ [$0.start, $0.end] }).sorted() where cut > cuts[cuts.count - 1] && cut < last { cuts.append(cut) }
        cuts.append(last)
        var placements: [PanelPlacement] = []
        for (from, to) in zip(cuts, cuts.dropFirst()) {
            let middle = (from + to) / 2
            let active = windows.filter { $0.start <= middle && middle < $0.end }.map(\.panel)
            let viewport = section.panelRect(index: membership.panelIndex, canvas: canvas, activePanels: active)
            if let last = placements.last, last.viewport == viewport {
                placements[placements.count - 1] = .init(start: last.start, end: to, viewport: viewport)
            } else {
                placements.append(.init(start: from, end: to, viewport: viewport))
            }
        }
        return placements
    }
}

extension EditRecipe {
    func validateSplitScreen() throws {
        guard splitScreenSections.count <= 100, Set(splitScreenSections.map(\.id)).count == splitScreenSections.count,
              schemaVersion == 2 || splitScreenSections.isEmpty else { throw RecipeError.invalidTimeline }
        let sections = splitScreenSections.sorted { $0.start < $1.start }
        for section in sections { try section.validate() }
        for (a, b) in zip(sections, sections.dropFirst()) where a.end > b.start { throw RecipeError.invalidTimeline }
        for track in tracks {
            for clip in track.clips {
                if let membership = clip.splitScreen {
                    guard schemaVersion == 2, track.kind == .video, clip.visualPlacement == nil,
                          let section = sections.first(where: { $0.id == membership.sectionID }),
                          section.panelWeights.indices.contains(membership.panelIndex),
                          clip.timelineStart >= section.start, clip.timelineStart + clip.duration <= section.end + 0.000_001 else {
                        throw RecipeError.invalidTimeline
                    }
                } else if track.kind == .video {
                    guard !sections.contains(where: { clip.timelineStart < $0.end && clip.timelineStart + clip.duration > $0.start }) else {
                        throw RecipeError.invalidTimeline
                    }
                }
            }
        }
        for section in sections {
            let members = tracks.flatMap(\.clips).filter { $0.splitScreen?.sectionID == section.id }
            guard !members.isEmpty else { throw RecipeError.invalidTimeline }
            for panel in section.panelWeights.indices {
                let clips = members.filter { $0.splitScreen?.panelIndex == panel }.sorted { $0.timelineStart < $1.timelineStart }
                for (a, b) in zip(clips, clips.dropFirst()) where a.timelineStart + a.duration > b.timelineStart + 0.000_001 {
                    guard b.transition != nil else { throw RecipeError.invalidTimeline }
                }
                // A blank has no transition: it never shares time with a clip in its panel.
                for blank in section.blanks where blank.panelIndex == panel {
                    for clip in clips where clip.timelineStart < blank.end - 0.000_001 && blank.start < clip.timelineStart + clip.duration - 0.000_001 {
                        throw RecipeError.invalidTimeline
                    }
                }
            }
        }
    }
}

extension EditRecipe {
    func validateSpeakerOwnership() throws {
        let overriddenTracks = tracks.filter { $0.clips.contains { $0.speakerFrameOverride != nil } }
        guard overriddenTracks.isEmpty || (schemaVersion == 2 && overriddenTracks.allSatisfy { $0.kind == .video }) else {
            throw RecipeError.invalidTimeline
        }
        // An explicit inherited policy is the durable reset value; it remains inert after
        // removing a background or replacing a subject with a different source.
        let overrides = overriddenTracks.flatMap(\.clips).filter {
            $0.speakerFrameOverride != nil && $0.speakerFrameOverride?.mode != .inherit
        }
        guard speakerBackground != nil || overrides.isEmpty else { throw RecipeError.invalidTimeline }
        let ids = speakerBackground?.subjectClipIDs
        let originalAssets = Set((assetManifest?.assets ?? []).compactMap { asset -> String? in
            if case .original = asset.source { return asset.id }
            return nil
        })
        let eligible = Set(tracks.filter { $0.kind == .video }.flatMap(\.clips)
            .filter { originalAssets.contains($0.sourceAssetID) }.map(\.id))
        if let ids { guard Set(ids).isSubset(of: eligible) else { throw RecipeError.invalidTimeline } }
        let subjects = Set(ids ?? tracks.filter { $0.id == speakerBackground?.subjectTrackID }.flatMap(\.clips).map(\.id))
        guard overrides.allSatisfy({ eligible.contains($0.id) && subjects.contains($0.id) }) else { throw RecipeError.invalidTimeline }
    }
}
