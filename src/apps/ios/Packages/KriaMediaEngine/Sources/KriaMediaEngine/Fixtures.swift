import Foundation

/// Deterministic fixtures used by unit and device tests. Media files are supplied by the host test
/// harness; the fixture recipe is intentionally metadata-only and therefore runs on macOS too.
public enum MediaEngineFixtures {
    public static func recipe(assetIDs: [String] = ["fixture-a", "fixture-b"], clipDuration: TimeInterval = 2) -> EditRecipe {
        let assets = assetIDs.map { MediaAsset(id: $0, relativePath: "originals/\($0).mov", duration: clipDuration) }
        let clips = assetIDs.enumerated().map { index, id in
            TimelineClip(id: "clip-\(index)", sourceAssetID: id, sourceDuration: clipDuration,
                         timelineStart: Double(index) * clipDuration,
                         transition: index == 0 ? nil : Transition(duration: min(0.25, clipDuration / 2)),
                         text: index == 0 ? TextTreatment(text: "Kria", animation: .fadeScale) : nil)
        }
        return EditRecipe(assets: assets,
                          tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)],
                          requiredCapabilities: [.basicComposition, .animatedText, .crossfade, .local1080Export])
    }

    public static func deterministicBytes(count: Int = 1024) -> Data {
        Data((0..<max(0, count)).map { UInt8(($0 * 31 + 17) % 251) })
    }
}
