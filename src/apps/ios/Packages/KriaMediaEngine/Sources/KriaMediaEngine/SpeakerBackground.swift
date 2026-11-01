import Foundation
#if canImport(AVFoundation)
import AVFoundation
import CoreImage
import ImageIO
#endif

// Talk-to-camera green screen (KRI-1115). Mirrors `app/kria/portable_speaker_background.py`; the wire is
// snake_case, decoded with `.convertFromSnakeCase`, and `tests/fixtures/speaker-background-parity/v1.json`
// pins both sides. Rendered by `SpeakerMatte` and `RecipeVideoCompositor.composeBelowText` (KRI-1116).

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

#if canImport(AVFoundation)
extension AVPlayerPreviewComposer {
    static let speakerBackgroundClipID = "kria-speaker-background"

    /// The clips the matte cuts out: the creator's own footage on the background's subject track. A
    /// Visuals-pool clip on that track (the ending clip) is not a speaker and renders as without a background.
    static func speakerClipIDs(in recipe: EditRecipe) -> Set<String> {
        guard let background = recipe.speakerBackground else { return [] }
        let visuals = Set((recipe.assetManifest?.assets ?? []).compactMap { asset -> String? in
            if case .visual = asset.source { return asset.id }
            return nil
        })
        return Set(recipe.tracks.filter { $0.id == background.subjectTrackID }.flatMap(\.clips)
            .filter { !visuals.contains($0.sourceAssetID) }.map(\.id))
    }

    /// The background as one layer spanning the edit, cover-fit the way a fullscreen cutaway is
    /// (`VisualMediaPlacement` without a width). A photo is fitted and downscaled to the canvas once, so no
    /// frame resamples the full-resolution original. A video gets its own composition track, looped from
    /// `sourceStart`, or held on its last frame when `loop` is false; its audio is never inserted (S11).
    static func speakerBackgroundLayer(_ background: SpeakerBackgroundLayer, url: URL, composition: AVMutableComposition,
                                       canvas: CGSize, duration: Double) async throws -> RecipeVideoLayer {
        let frame = CGRect(origin: .zero, size: canvas)
        let placement = VisualMediaPlacement(order: 0, windowStart: 0, windowEnd: duration)
        var layer: RecipeVideoLayer
        switch background.mediaKind {
        case .image:
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil), CGImageSourceGetCount(source) > 0,
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { throw MediaEngineError.missingAsset(background.assetID) }
            let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
            let orientation = (properties?[kCGImagePropertyOrientation] as? NSNumber)?.int32Value ?? 1
            guard (1...8).contains(orientation) else { throw MediaEngineError.missingAsset(background.assetID) }
            let oriented = CIImage(cgImage: try StillFrame.flattened(image)).oriented(forExifOrientation: orientation)
            let upright = oriented.transformed(by: CGAffineTransform(translationX: -oriented.extent.minX, y: -oriented.extent.minY))
            let fitted = placement.position(upright, preferred: .identity, canvas: frame, time: 0, clipStart: 0, clipEnd: duration).cropped(to: frame)
            let context = CIContext(options: [.cacheIntermediates: false, .workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])
            guard let rendered = context.createCGImage(fitted, from: frame, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!) else {
                throw MediaEngineError.exportUnavailable
            }
            layer = RecipeVideoLayer(trackID: nil, image: CIImage(cgImage: rendered), transform: .identity, start: 0, end: duration,
                                     fadeIn: 0, isPrimary: false, clipID: speakerBackgroundClipID, naturalSize: canvas)
        case .video:
            let asset = AVURLAsset(url: url)
            guard let source = try await asset.loadTracks(withMediaType: .video).first,
                  let track = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid) else {
                throw MediaEngineError.missingAsset(background.assetID)
            }
            let window = try await videoSampleWindow(of: source)
            let fps = Double(try await source.load(.nominalFrameRate))
            let sourceFrame = 1 / max(1, fps.isFinite && fps > 0 ? fps : 30)
            let start = max(background.sourceStart, window.start)
            guard window.end - start >= sourceFrame else { throw RecipeError.invalidTimeline }
            func time(_ seconds: Double) -> CMTime { CMTime(value: Int64((seconds * 60_000).rounded()), timescale: 60_000) }
            let range = CMTimeRange(start: time(start), end: time(window.end))
            let end = time(duration)
            if background.loop {
                var at = CMTime.zero
                while at < end {
                    try track.insertTimeRange(CMTimeRange(start: range.start, duration: min(range.duration, end - at)), of: source, at: at)
                    // Anchor each repeat on what the track actually holds, so no sliver between repeats is left empty.
                    guard track.timeRange.end > at else { throw MediaEngineError.missingAsset(background.assetID) }
                    at = track.timeRange.end
                }
            } else {
                try track.insertTimeRange(CMTimeRange(start: range.start, duration: min(range.duration, end)), of: source, at: .zero)
                let tailStart = track.timeRange.end
                if tailStart < end {
                    let tail = CMTimeRange(start: range.end - time(sourceFrame), duration: time(sourceFrame))
                    try track.insertTimeRange(tail, of: source, at: tailStart)
                    track.scaleTimeRange(CMTimeRange(start: tailStart, duration: tail.duration), toDuration: end - tailStart)
                }
            }
            layer = RecipeVideoLayer(trackID: track.trackID, image: nil, transform: .identity, start: 0, end: duration, fadeIn: 0,
                                     isPrimary: false, clipID: speakerBackgroundClipID, naturalSize: try await source.load(.naturalSize),
                                     preferredTransform: coreImagePreferredTransform(try await source.load(.preferredTransform)),
                                     visualPlacement: placement)
        }
        layer.speakerRole = .background
        return layer
    }
}
#endif
