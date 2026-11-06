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

public struct SplitScreenSection: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var start: Double
    public var end: Double
    public var panelWeights: [Double]
    public var gutter: Double
    public var backgroundColor: String

    public init(id: String, start: Double, end: Double, panelWeights: [Double], gutter: Double = 0, backgroundColor: String = "#000000") {
        self.id = id; self.start = start; self.end = end; self.panelWeights = panelWeights
        self.gutter = gutter; self.backgroundColor = backgroundColor
    }

    public func validate() throws {
        guard !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              start.isFinite, end.isFinite, start >= 0, end > start, end <= 1800,
              (2...3).contains(panelWeights.count), panelWeights.allSatisfy({ $0.isFinite && $0 > 0 }),
              panelWeights.reduce(0, +).isFinite, gutter.isFinite, (0...0.1).contains(gutter),
              backgroundColor.range(of: "^#[0-9A-Fa-f]{6}$", options: .regularExpression) != nil else {
            throw RecipeError.invalidTimeline
        }
    }

    public func panelRect(index: Int, canvas: CGRect) -> CGRect {
        let gap = canvas.height * gutter
        let available = canvas.height - gap * Double(panelWeights.count - 1)
        let total = panelWeights.reduce(0, +)
        let height = available * (panelWeights[index] / total)
        let top = available * (panelWeights.prefix(index).reduce(0, +) / total) + gap * Double(index)
        return CGRect(x: canvas.minX, y: canvas.maxY - top - height, width: canvas.width, height: height)
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
