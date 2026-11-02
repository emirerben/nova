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
    /// KRI-1162: the timed-background contract, enforced identically by `portable_speaker_background.py`.
    public static let maxWindows = 40
    public static let maxTimeline = 1800.0
    public static let minWindowSeconds = 0.5
    public static let minFrameWidthFraction = 0.2
    public static let maxIDLength = 160
    public static let defaultSubjectTrackID = "subtitled"
}

/// KRI-1162: one timed background. Its photo (held) or muted video (looping inside the window from
/// `sourceStart`) shows from `start` to `end` seconds of the edit; where no window covers the time the
/// layer's own (fill) background shows.
public struct SpeakerBackgroundWindow: Codable, Equatable, Sendable {
    public let assetID: String
    public let mediaKind: VisualMediaKind
    public let sourceStart: Double
    public let start: Double
    public let end: Double

    public init(assetID: String, mediaKind: VisualMediaKind, sourceStart: Double = 0, start: Double, end: Double) {
        self.assetID = assetID; self.mediaKind = mediaKind; self.sourceStart = sourceStart; self.start = start; self.end = end
    }

    private enum CodingKeys: String, CodingKey { case assetID = "assetId", mediaKind, sourceStart, start, end }

    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["assetId", "mediaKind", "sourceStart", "start", "end"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assetID = try c.decode(String.self, forKey: .assetID)
        mediaKind = try c.decode(VisualMediaKind.self, forKey: .mediaKind)
        sourceStart = try c.decodeIfPresent(Double.self, forKey: .sourceStart) ?? 0
        start = try c.decode(Double.self, forKey: .start)
        end = try c.decode(Double.self, forKey: .end)
        try validate()
    }

    public func validate() throws {
        guard !assetID.isEmpty, assetID.count <= SpeakerBackgroundLimits.maxIDLength, !assetID.contains(where: \.isWhitespace),
              sourceStart.isFinite, (0...SpeakerBackgroundLimits.maxSourceStart).contains(sourceStart),
              mediaKind == .video || sourceStart == 0,
              start.isFinite, end.isFinite, (0...SpeakerBackgroundLimits.maxTimeline).contains(start),
              end > 0, end <= SpeakerBackgroundLimits.maxTimeline,
              end - start >= SpeakerBackgroundLimits.minWindowSeconds
        else { throw RecipeError.invalidTimeline }
    }
}

/// KRI-1162: where the matted speaker is drawn. The speaker layer (after its own crop and zoom) is scaled
/// uniformly so its width is `widthFraction` of the canvas width and centred at (`xFraction`, `yFraction`),
/// canvas fractions with the origin at the top-left, the convention of `VisualMediaPlacement`.
public struct SpeakerFrame: Codable, Equatable, Sendable {
    public let widthFraction: Double
    public let xFraction: Double
    public let yFraction: Double

    public init(widthFraction: Double, xFraction: Double, yFraction: Double) {
        self.widthFraction = widthFraction; self.xFraction = xFraction; self.yFraction = yFraction
    }

    private enum CodingKeys: String, CodingKey { case widthFraction, xFraction, yFraction }

    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["widthFraction", "xFraction", "yFraction"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        widthFraction = try c.decode(Double.self, forKey: .widthFraction)
        xFraction = try c.decode(Double.self, forKey: .xFraction)
        yFraction = try c.decode(Double.self, forKey: .yFraction)
        try validate()
    }

    public func validate() throws {
        guard widthFraction.isFinite, xFraction.isFinite, yFraction.isFinite,
              (SpeakerBackgroundLimits.minFrameWidthFraction...1).contains(widthFraction),
              (0...1).contains(xFraction), (0...1).contains(yFraction)
        else { throw RecipeError.invalidTimeline }
    }
}

/// One picture or looping video composited behind the speaker for the whole edit. KRI-1162 adds timed
/// `windows` that override it in their span and a `speakerFrame` that draws the speaker small; both are
/// absent from the wire while unused, so a first-cut layer encodes exactly as before.
public struct SpeakerBackgroundLayer: Codable, Equatable, Sendable {
    /// Names a `VisualRenderAsset` in the recipe's manifest.
    public let assetID: String
    public let mediaKind: VisualMediaKind
    /// Seconds into a video background where playback starts. Always 0 for a photo.
    public let sourceStart: Double
    public let loop: Bool
    public let subjectTrackID: String
    /// Sorted by `start`, non-overlapping, 1...40 entries; nil while unused (never empty).
    public let windows: [SpeakerBackgroundWindow]?
    public let speakerFrame: SpeakerFrame?

    public init(assetID: String, mediaKind: VisualMediaKind, sourceStart: Double = 0, loop: Bool = true,
                subjectTrackID: String = SpeakerBackgroundLimits.defaultSubjectTrackID,
                windows: [SpeakerBackgroundWindow]? = nil, speakerFrame: SpeakerFrame? = nil) {
        self.assetID = assetID; self.mediaKind = mediaKind; self.sourceStart = sourceStart
        self.loop = loop; self.subjectTrackID = subjectTrackID
        self.windows = windows; self.speakerFrame = speakerFrame
    }

    private enum CodingKeys: String, CodingKey {
        case assetID = "assetId", mediaKind, sourceStart, loop, subjectTrackID = "subjectTrackId", windows, speakerFrame
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["assetId", "mediaKind", "sourceStart", "loop", "subjectTrackId", "windows", "speakerFrame"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assetID = try c.decode(String.self, forKey: .assetID)
        mediaKind = try c.decode(VisualMediaKind.self, forKey: .mediaKind)
        sourceStart = try c.decodeIfPresent(Double.self, forKey: .sourceStart) ?? 0
        loop = try c.decodeIfPresent(Bool.self, forKey: .loop) ?? true
        subjectTrackID = try c.decodeIfPresent(String.self, forKey: .subjectTrackID) ?? SpeakerBackgroundLimits.defaultSubjectTrackID
        // `null` reads as absent, as on the server; an empty list is rejected by `validate()`.
        windows = try c.decodeIfPresent([SpeakerBackgroundWindow].self, forKey: .windows)
        speakerFrame = try c.decodeIfPresent(SpeakerFrame.self, forKey: .speakerFrame)
        try validate()
    }

    public func validate() throws {
        guard !assetID.isEmpty, assetID.count <= SpeakerBackgroundLimits.maxIDLength,
              !assetID.contains(where: \.isWhitespace),
              !subjectTrackID.isEmpty, subjectTrackID.count <= SpeakerBackgroundLimits.maxIDLength,
              sourceStart.isFinite, (0...SpeakerBackgroundLimits.maxSourceStart).contains(sourceStart),
              mediaKind == .video || sourceStart == 0
        else { throw RecipeError.invalidTimeline }
        if let windows {
            guard (1...SpeakerBackgroundLimits.maxWindows).contains(windows.count) else { throw RecipeError.invalidTimeline }
            for window in windows { try window.validate() }
            // S28: the compositor picks THE window containing the time, so they are sorted and disjoint
            // (touching ends are fine).
            for (earlier, later) in zip(windows, windows.dropFirst()) where later.start < earlier.end { throw RecipeError.invalidTimeline }
        }
        try speakerFrame?.validate()
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

    /// The fill background plus one layer per timed window (KRI-1162), fill first. The fill spans the whole
    /// edit; each window layer exists only inside its span, clipped to the edit, and a window that starts at
    /// or past the end builds nothing.
    static func speakerBackgroundLayers(_ background: SpeakerBackgroundLayer, assetURLs: [String: URL], composition: AVMutableComposition,
                                        canvas: CGSize, duration: Double) async throws -> [RecipeVideoLayer] {
        guard let url = assetURLs[background.assetID] else { throw MediaEngineError.missingAsset(background.assetID) }
        var layers = [try await speakerBackgroundLayer(background, url: url, composition: composition, canvas: canvas, duration: duration)]
        for (index, window) in (background.windows ?? []).enumerated() where window.start < duration {
            guard let windowURL = assetURLs[window.assetID] else { throw MediaEngineError.missingAsset(window.assetID) }
            var layer = try await backgroundMediaLayer(assetID: window.assetID, mediaKind: window.mediaKind, sourceStart: window.sourceStart,
                                                       loop: true, url: windowURL, composition: composition, canvas: canvas,
                                                       start: window.start, end: min(window.end, duration),
                                                       clipID: "\(speakerBackgroundClipID)-window-\(index)")
            layer.speakerBackgroundWindow = true
            layers.append(layer)
        }
        return layers
    }

    /// The fill as one layer spanning the edit, cover-fit the way a fullscreen cutaway is
    /// (`VisualMediaPlacement` without a width). A photo is fitted and downscaled to the canvas once, so no
    /// frame resamples the full-resolution original. A video gets its own composition track, looped from
    /// `sourceStart`, or held on its last frame when `loop` is false; its audio is never inserted (S11).
    static func speakerBackgroundLayer(_ background: SpeakerBackgroundLayer, url: URL, composition: AVMutableComposition,
                                       canvas: CGSize, duration: Double) async throws -> RecipeVideoLayer {
        try await backgroundMediaLayer(assetID: background.assetID, mediaKind: background.mediaKind, sourceStart: background.sourceStart,
                                       loop: background.loop, url: url, composition: composition, canvas: canvas,
                                       start: 0, end: duration, clipID: speakerBackgroundClipID)
    }

    /// One background medium shown from `start` to `end` seconds. A video window is its own composition
    /// track holding samples ONLY inside its span, so at most one background track decodes at a time (S31).
    private static func backgroundMediaLayer(assetID: String, mediaKind: VisualMediaKind, sourceStart: Double, loop: Bool, url: URL,
                                             composition: AVMutableComposition, canvas: CGSize, start spanStart: Double, end spanEnd: Double,
                                             clipID: String) async throws -> RecipeVideoLayer {
        let frame = CGRect(origin: .zero, size: canvas)
        let placement = VisualMediaPlacement(order: 0, windowStart: spanStart, windowEnd: spanEnd)
        var layer: RecipeVideoLayer
        switch mediaKind {
        case .image:
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil), CGImageSourceGetCount(source) > 0,
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { throw MediaEngineError.missingAsset(assetID) }
            let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
            let orientation = (properties?[kCGImagePropertyOrientation] as? NSNumber)?.int32Value ?? 1
            guard (1...8).contains(orientation) else { throw MediaEngineError.missingAsset(assetID) }
            let oriented = CIImage(cgImage: try StillFrame.flattened(image)).oriented(forExifOrientation: orientation)
            let upright = oriented.transformed(by: CGAffineTransform(translationX: -oriented.extent.minX, y: -oriented.extent.minY))
            let fitted = placement.position(upright, preferred: .identity, canvas: frame, time: spanStart, clipStart: spanStart, clipEnd: spanEnd).cropped(to: frame)
            let context = CIContext(options: [.cacheIntermediates: false, .workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])
            guard let rendered = context.createCGImage(fitted, from: frame, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!) else {
                throw MediaEngineError.exportUnavailable
            }
            layer = RecipeVideoLayer(trackID: nil, image: CIImage(cgImage: rendered), transform: .identity, start: spanStart, end: spanEnd,
                                     fadeIn: 0, isPrimary: false, clipID: clipID, naturalSize: canvas)
        case .video:
            let asset = AVURLAsset(url: url)
            guard let source = try await asset.loadTracks(withMediaType: .video).first,
                  let track = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid) else {
                throw MediaEngineError.missingAsset(assetID)
            }
            let window = try await videoSampleWindow(of: source)
            let fps = Double(try await source.load(.nominalFrameRate))
            let sourceFrame = 1 / max(1, fps.isFinite && fps > 0 ? fps : 30)
            let start = max(sourceStart, window.start)
            guard window.end - start >= sourceFrame else { throw RecipeError.invalidTimeline }
            func time(_ seconds: Double) -> CMTime { CMTime(value: Int64((seconds * 60_000).rounded()), timescale: 60_000) }
            let range = CMTimeRange(start: time(start), end: time(window.end))
            let from = time(spanStart), end = time(spanEnd)
            if loop {
                var at = from
                while at < end {
                    try track.insertTimeRange(CMTimeRange(start: range.start, duration: min(range.duration, end - at)), of: source, at: at)
                    // Anchor each repeat on what the track actually holds, so no sliver between repeats is left empty.
                    guard track.timeRange.end > at else { throw MediaEngineError.missingAsset(assetID) }
                    at = track.timeRange.end
                }
            } else {
                try track.insertTimeRange(CMTimeRange(start: range.start, duration: min(range.duration, end - from)), of: source, at: from)
                let tailStart = track.timeRange.end
                if tailStart < end {
                    let tail = CMTimeRange(start: range.end - time(sourceFrame), duration: time(sourceFrame))
                    try track.insertTimeRange(tail, of: source, at: tailStart)
                    track.scaleTimeRange(CMTimeRange(start: tailStart, duration: tail.duration), toDuration: end - tailStart)
                }
            }
            layer = RecipeVideoLayer(trackID: track.trackID, image: nil, transform: .identity, start: spanStart, end: spanEnd, fadeIn: 0,
                                     isPrimary: false, clipID: clipID, naturalSize: try await source.load(.naturalSize),
                                     preferredTransform: coreImagePreferredTransform(try await source.load(.preferredTransform)),
                                     visualPlacement: placement)
        }
        layer.speakerRole = .background
        return layer
    }
}
#endif
