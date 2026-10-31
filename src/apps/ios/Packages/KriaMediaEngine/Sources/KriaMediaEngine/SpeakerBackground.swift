import Foundation

// Talk-to-camera green screen (KRI-1115). Mirrors `app/kria/portable_speaker_background.py`; the wire is
// snake_case, decoded with `.convertFromSnakeCase`, and `tests/fixtures/speaker-background-parity/v1.json`
// pins both sides. Decoding only: no build renders this lane yet.

public enum SpeakerBackgroundLimits {
    public static let maxSourceStart = 1800.0
    public static let maxIDLength = 160
    public static let defaultSubjectTrackID = "subtitled"
}

/// One picture or looping video composited behind the speaker for the whole edit.
public struct SpeakerBackgroundLayer: Codable, Equatable, Sendable {
    /// Names a `VisualRenderAsset` in the recipe's manifest.
    public let assetID: String
    public let mediaKind: VisualMediaKind
    /// Seconds into a video background where playback starts. Always 0 for a photo.
    public let sourceStart: Double
    public let loop: Bool
    public let subjectTrackID: String

    public init(assetID: String, mediaKind: VisualMediaKind, sourceStart: Double = 0, loop: Bool = true,
                subjectTrackID: String = SpeakerBackgroundLimits.defaultSubjectTrackID) {
        self.assetID = assetID; self.mediaKind = mediaKind; self.sourceStart = sourceStart
        self.loop = loop; self.subjectTrackID = subjectTrackID
    }

    private enum CodingKeys: String, CodingKey {
        case assetID = "assetId", mediaKind, sourceStart, loop, subjectTrackID = "subjectTrackId"
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["assetId", "mediaKind", "sourceStart", "loop", "subjectTrackId"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assetID = try c.decode(String.self, forKey: .assetID)
        mediaKind = try c.decode(VisualMediaKind.self, forKey: .mediaKind)
        sourceStart = try c.decodeIfPresent(Double.self, forKey: .sourceStart) ?? 0
        loop = try c.decodeIfPresent(Bool.self, forKey: .loop) ?? true
        subjectTrackID = try c.decodeIfPresent(String.self, forKey: .subjectTrackID) ?? SpeakerBackgroundLimits.defaultSubjectTrackID
        try validate()
    }

    public func validate() throws {
        guard !assetID.isEmpty, assetID.count <= SpeakerBackgroundLimits.maxIDLength,
              !assetID.contains(where: \.isWhitespace),
              !subjectTrackID.isEmpty, subjectTrackID.count <= SpeakerBackgroundLimits.maxIDLength,
              sourceStart.isFinite, (0...SpeakerBackgroundLimits.maxSourceStart).contains(sourceStart),
              mediaKind == .video || sourceStart == 0
        else { throw RecipeError.invalidTimeline }
    }
}
