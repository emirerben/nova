import Foundation
#if canImport(CoreImage)
import CoreImage
#endif
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
#endif

/// What shows behind a main-track clip where the clip does not cover the canvas: the corners a tilt
/// uncovers, or the bars around a letterboxed "fit" clip. Mirrors `app.kria.recipes.ClipBackdrop`
/// (`{"kind": "blank" | "color" | "blur" | "media", "color": "#RRGGBB", "asset_id": "<pool visual id>"}`,
/// `color` only for `color`, `asset_id` only for `media`).
public enum ClipBackdrop: Codable, Equatable, Sendable {
    /// Black, like the bars around a letterboxed clip. Drawn as nothing.
    case blank
    case color(String)
    /// A blurred cover-fit copy of the same clip frame.
    case blur
    /// A Visuals-pool photo (held) or video (looping from the start, muted) cover-fit to the canvas. The payload
    /// is the pool visual id, which the recipe's manifest declares as a `.visual` asset (`mediaAssetID(in:)`).
    case media(String)

    private enum CodingKeys: String, CodingKey { case kind, color, assetID = "assetId" }
    private enum Kind: String, Codable { case blank, color, blur, media }

    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["kind", "color", "assetId"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let hasColor = c.contains(.color)
        let hasAsset = c.contains(.assetID)
        switch try c.decode(Kind.self, forKey: .kind) {
        case .blank where !hasColor && !hasAsset: self = .blank
        case .blur where !hasColor && !hasAsset: self = .blur
        case .color where !hasAsset: self = .color(try c.decode(String.self, forKey: .color))
        case .media where !hasColor: self = .media(try c.decode(String.self, forKey: .assetID))
        default: throw RecipeError.invalidTimeline
        }
        try validate()
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .blank: try c.encode(Kind.blank, forKey: .kind)
        case .blur: try c.encode(Kind.blur, forKey: .kind)
        case .color(let hex): try c.encode(Kind.color, forKey: .kind); try c.encode(hex, forKey: .color)
        case .media(let id): try c.encode(Kind.media, forKey: .kind); try c.encode(id, forKey: .assetID)
        }
    }

    public func validate() throws {
        switch self {
        case .color(let hex): if Self.components(hex) == nil { throw RecipeError.invalidTimeline }
        case .media(let id):
            guard !id.isEmpty, id.count <= SpeakerBackgroundLimits.maxIDLength,
                  !id.contains(where: \.isWhitespace) else { throw RecipeError.invalidTimeline }
        case .blank, .blur: break
        }
    }

    /// The manifest asset (`visual-<pool id>`) a `.media` backdrop draws: the `.visual` entry whose pool id it names.
    func mediaAsset(in manifest: RenderAssetManifest?) -> (id: String, kind: VisualMediaKind)? {
        guard case .media(let poolID) = self else { return nil }
        for asset in manifest?.assets ?? [] {
            if case .visual(let visualID, _, let kind) = asset.source, visualID == poolID { return (asset.id, kind) }
        }
        return nil
    }

    /// Straight sRGB channels in 0...1 for `#RRGGBB`, or nil for any other string.
    static func components(_ hex: String) -> (red: Double, green: Double, blue: Double)? {
        let digits = Array(hex.utf8)
        guard digits.count == 7, digits[0] == UInt8(ascii: "#"),
              digits[1...].allSatisfy({ ($0 >= 48 && $0 <= 57) || ($0 >= 65 && $0 <= 70) || ($0 >= 97 && $0 <= 102) }),
              let value = UInt32(String(decoding: digits[1...], as: UTF8.self), radix: 16) else { return nil }
        return (Double((value >> 16) & 0xFF) / 255, Double((value >> 8) & 0xFF) / 255, Double(value & 0xFF) / 255)
    }
}

#if canImport(AVFoundation)
extension AVPlayerPreviewComposer {
    static func backdropClipID(forPoolID id: String) -> String { "kria-clip-backdrop-" + id }

    /// Every distinct pool visual a clip's `.media` backdrop names, with the manifest asset that carries its bytes,
    /// ordered by pool id so the composition's tracks are built the same way each time.
    static func mediaBackdrops(in recipe: EditRecipe) -> [(poolID: String, assetID: String, kind: VisualMediaKind)] {
        var found: [String: (assetID: String, kind: VisualMediaKind)] = [:]
        for clip in recipe.tracks.flatMap(\.clips) {
            guard case .media(let poolID)? = clip.backdrop, found[poolID] == nil,
                  let asset = clip.backdrop?.mediaAsset(in: recipe.assetManifest) else { continue }
            found[poolID] = (asset.id, asset.kind)
        }
        return found.sorted { $0.key < $1.key }.map { (poolID: $0.key, assetID: $0.value.assetID, kind: $0.value.kind) }
    }

    /// One layer spanning the edit per distinct media backdrop. It reuses the speaker background's layer builder, so a
    /// photo is fitted once and a video gets its own composition track looped from its first frame; the video's
    /// audio is never inserted. The compositor reads it under each clip that names it and never composes it alone.
    static func mediaBackdropLayer(poolID: String, assetID: String, kind: VisualMediaKind, url: URL,
                                   composition: AVMutableComposition, canvas: CGSize, duration: Double) async throws -> RecipeVideoLayer {
        var layer = try await speakerBackgroundLayer(SpeakerBackgroundLayer(assetID: assetID, mediaKind: kind), url: url,
                                                     composition: composition, canvas: canvas, duration: duration)
        layer.speakerRole = nil
        layer.clipID = backdropClipID(forPoolID: poolID)
        layer.backdropAssetID = poolID
        return layer
    }
}

extension RecipeVideoCompositor {
    /// A blur is drawn from a quarter-size copy so a 1080x1920 frame costs a 270x480 blur.
    private static let backdropBlurScale: CGFloat = 0.25
    /// Matches `StillFrame.supportingCard`'s blur (FFmpeg `boxblur=30:2`, sigma 24.9 at 1080 wide).
    private static let backdropBlurSigma1080: CGFloat = 24.9

    /// The canvas-sized image drawn under the clip, or nil when nothing is drawn. A `.media` backdrop is drawn from
    /// its own source layer (`mediaBackdropImage`), never here.
    static func backdropImage(_ backdrop: ClipBackdrop, source: CIImage, layer: RecipeVideoLayer,
                              canvas: CGRect, time: Double) -> CIImage? {
        switch backdrop {
        case .blank, .media:
            return nil
        case .color(let hex):
            guard let rgb = ClipBackdrop.components(hex),
                  let space = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
            return CIImage(color: CIColor(red: rgb.red, green: rgb.green, blue: rgb.blue, alpha: 1, colorSpace: space) ?? .black)
                .cropped(to: canvas)
        case .blur:
            guard let size = layer.naturalSize else { return nil }
            var cover = layer
            cover.transform = AVPlayerPreviewComposer.transform(naturalSize: size, preferred: layer.preferredTransform,
                                                                canvas: canvas.size, media: .identity)
            cover.visualPlacement = nil
            let fitted = positionedSource(source, layer: cover, canvas: canvas, time: time).cropped(to: canvas)
            let small = fitted.transformed(by: CGAffineTransform(scaleX: backdropBlurScale, y: backdropBlurScale))
            let sigma = backdropBlurSigma1080 * canvas.width / 1080 * backdropBlurScale
            let blurred = small.clampedToExtent()
                .applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: sigma])
                .cropped(to: small.extent)
            return blurred.transformed(by: CGAffineTransform(scaleX: canvas.width / small.extent.width,
                                                             y: canvas.height / small.extent.height))
                .cropped(to: canvas)
        }
    }
}
#endif
